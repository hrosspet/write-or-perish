"""A prompt entry is private unless its owner sets its level themselves.

A prompt entry is the node a Text-mode session starts with: it shows
its owner's prompt text and carries pinned versions of their profile
and other documents.

- Every creation path stores it private, whatever the message's level.
- "This node and all my replies" leaves prompt entries as they are when
  it raises a level above private; a change to private applies to them.
- The owner's own edit of the prompt entry's level works.
- The public surfaces list a public first message under a private prompt
  root as the top of its thread: once, starting at the message, with a
  permalink of its own; the prompt text reaches nobody but its owner.
- The page cache drops the thread's pages when the message changes.

The real app (create_app) on sqlite; the generation task is mocked.
"""
import itertools
import os
import sys
import tempfile
import uuid
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")
os.environ["AUDIO_STORAGE_PATH"] = tempfile.mkdtemp(prefix="wop-audio-test-")
for _m in ("celery", "celery.utils", "celery.utils.log", "celery.result",
           "ffmpeg"):
    sys.modules.setdefault(_m, MagicMock())
_mock_llm = MagicMock()
_res = MagicMock()
_res.id = "fake-task-id"
_mock_llm.generate_llm_response.delay.return_value = _res

import pytest  # noqa: E402
from flask import g  # noqa: E402
from sqlalchemy import event  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]
import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    User, Node, Draft, UserPrompt, UserProfile,
    UserArtifact, UserTodo, UserRecentContext)
import backend.models as _real_models  # noqa: E402

PROMPT = ("ANNA-PROMPT-TEXT ANNA-TYPED-INTO-PROMPT profile {user_profile} "
          "intentions {user_intentions} memory {user_memory} todo "
          "{user_todo} recent {user_recent} prefs {user_ai_preferences} "
          "index {user_artifacts_index} pad {user_scratchpad}")
PERSONAL = ["ANNA-PROFILE", "ANNA-INTENTIONS", "ANNA-MEMORY", "ANNA-TODO",
            "ANNA-RECENT", "ANNA-SCRATCHPAD", "ANNA-AI_PREFERENCES"]
PROMPT_MARKS = ["ANNA-PROMPT-TEXT", "ANNA-TYPED-INTO-PROMPT",
                "ANNA-EDITED-IN-THREAD"]
ENTRY = "# Anna on rivers\n\nANNA-PUBLIC-BODY about rivers."


def _affected(k):
    return (k == "flask_login" or k.startswith("backend.routes")
            or k == "backend.models" or k == "backend.tasks.llm_completion")


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)


def build_app(monkeypatch, uri="sqlite:///:memory:"):
    """The real app on the database at *uri* with its two people; a
    generator, for a fixture to yield from (the PostgreSQL run of these
    tests gives its own uri)."""
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_models
    sys.modules["backend.tasks.llm_completion"] = _mock_llm
    for k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[k]
    import backend.config as _config
    monkeypatch.setattr(_config.Config, "SQLALCHEMY_DATABASE_URI", uri)
    from backend import create_app
    app = create_app()
    assert app.config["SQLALCHEMY_DATABASE_URI"] == uri
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    app.config["PUBLIC_BASE_URL"] = "https://loore.org"
    app.config["SUPPORTED_MODELS"].setdefault(
        "gpt-5", {"provider": "openai", "api_model": "gpt-5"})
    # No page cache unless a test asks for one (fake_redis).
    from backend.utils import public_cache
    monkeypatch.setattr(public_cache, "_redis", lambda: None)
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        for name in ("ben", "anna"):
            _db.session.add(User(username=name, default_ai_usage="chat",
                                 approved=True, plan="alpha",
                                 public_sharing_enabled=True))
        _db.session.commit()
        anna = _user("anna")
        p = UserPrompt(user_id=anna.id, prompt_key="textmode",
                       title="Text", generated_by="user")
        p.set_content(PROMPT)
        prof = UserProfile(user_id=anna.id, generated_by="x")
        prof.set_content("ANNA-PROFILE")
        todo = UserTodo(user_id=anna.id, generated_by="x")
        todo.set_content("ANNA-TODO")
        _db.session.add_all([p, prof, todo])
        _db.session.flush()
        rc = UserRecentContext(user_id=anna.id, generated_by="x",
                               profile_id=prof.id)
        rc.set_content("ANNA-RECENT")
        _db.session.add(rc)
        for kind in ("intentions", "memory", "scratchpad", "ai_preferences"):
            a = UserArtifact(user_id=anna.id, kind=kind, title=kind,
                             generated_by="x", ai_usage="chat")
            a.set_content("ANNA-" + kind.upper())
            _db.session.add(a)
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()
    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    sys.modules.update(saved)


@pytest.fixture
def app(monkeypatch):
    yield from build_app(monkeypatch)


@pytest.fixture
def fake_redis(app, monkeypatch):
    from backend.utils import public_cache
    fake = FakeRedis()
    monkeypatch.setattr(public_cache, "_redis", lambda: fake)
    return fake


def _user(name):
    return User.query.filter_by(username=name).first()


def _client(app, name=None):
    g.pop("_login_user", None)
    c = app.test_client()
    if name:
        with c.session_transaction() as s:
            s["_user_id"] = str(_user(name).id)
            s["_fresh"] = True
    return c


def _get(node_id):
    _db.session.expire_all()
    return _db.session.get(Node, node_id)


def _llm_account():
    llm = User.query.filter_by(username="gpt-5").first()
    if llm is None:
        llm = User(twitter_id="llm-gpt-5", username="gpt-5")
        _db.session.add(llm)
        _db.session.flush()
    return llm


def _node(owner, text, parent=None, privacy="private", slug=None):
    u = _user(owner)
    n = Node(user_id=u.id, human_owner_id=u.id,
             parent_id=parent.id if parent is not None else None,
             node_type="user", privacy_level=privacy, ai_usage="chat",
             public_slug=slug)
    n.set_content(text)
    _db.session.add(n)
    _db.session.commit()
    return n


def _ai_reply(owner, parent, privacy):
    u = _user(owner)
    n = Node(user_id=_llm_account().id, human_owner_id=u.id,
             parent_id=parent.id, node_type="llm", llm_model="gpt-5",
             llm_task_status="completed", privacy_level=privacy,
             ai_usage="chat")
    n.set_content("Picks.")
    _db.session.add(n)
    _db.session.commit()
    return n


def _gleaning(owner, privacy):
    """*owner*'s entry with an AI reply under it, both at *privacy*."""
    entry = _node(owner, f"{owner} entry", privacy=privacy,
                  slug=(f"{owner}-entry-{uuid.uuid4().hex[:6]}"
                        if privacy == "public" else None))
    return _ai_reply(owner, entry, privacy)


def _stored_prompt_entry(owner, parent, privacy, stamped=True,
                         message_privacy=None, text="anna earlier message"):
    """A prompt entry as an earlier version stored it: at *privacy*,
    with a message under it (at *message_privacy*, default the same)."""
    from backend.utils.context_artifacts import attach_context_artifacts
    from backend.utils.prompts import get_user_prompt_record
    u = _user(owner)
    pn = Node(user_id=u.id, human_owner_id=u.id,
              parent_id=parent.id if parent is not None else None,
              node_type="user", privacy_level=privacy, ai_usage="chat")
    _db.session.add(pn)
    _db.session.flush()
    attach_context_artifacts(
        pn.id, u.id, prompt_record=get_user_prompt_record(u.id, "textmode"))
    if not stamped:
        pn.prompt_key = None
    _db.session.commit()
    msg = _node(owner, text, parent=pn, privacy=message_privacy or privacy)
    return pn, msg


def _start(app, who="anna", level="public", content=ENTRY):
    r = _client(app, who).post("/api/textmode/start", json={
        "content": content, "privacy_level": level, "ai_usage": "chat",
        "auto_generate": False})
    assert r.status_code == 202, r.get_json()
    data = r.get_json()
    return _get(data["conversation_id"]), _get(data["user_node_id"])


def _record(app, who, parent=None, **body):
    d = Draft(user_id=_user(who).id, node_id=None,
              parent_id=parent.id if parent is not None else None,
              session_id=str(uuid.uuid4()), streaming_status="completed",
              streaming_total_chunks=1, streaming_completed_chunks=1,
              privacy_level="private", ai_usage="chat")
    d.set_content("# Recorded words\n\nsaid aloud")
    _db.session.add(d)
    _db.session.commit()
    return _client(app, who).post(
        f"/api/drafts/streaming/{d.session_id}/save-as-node", json=body)


def _put(app, who, node_id, **body):
    return _client(app, who).put(f"/api/nodes/{node_id}", json=body)


def _body(resp):
    return resp.get_data().decode("utf-8", "replace")


def _no_prompt_text(text):
    for mark in PROMPT_MARKS + PERSONAL:
        assert mark not in text, mark


# ── Creation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("level", ["public", "private"])
def test_a_typed_new_thread_has_a_private_prompt_entry(app, level):
    prompt, message = _start(app, level=level)

    assert prompt.parent_id is None and prompt.prompt_key == "textmode"
    assert prompt.privacy_level == "private"
    assert message.parent_id == prompt.id
    assert message.privacy_level == level
    assert bool(message.public_slug) == (level == "public")
    assert prompt.public_slug is None


@pytest.mark.parametrize("level", ["public", "private"])
def test_a_recorded_new_thread_has_a_private_prompt_entry(app, level):
    resp = _record(app, "anna", agentic=True, privacy_level=level,
                   ai_usage="chat")

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()
    prompt = _get(data["conversation_id"])
    message = _get(data["user_node_id"])
    assert prompt.parent_id is None and prompt.prompt_key == "textmode"
    assert prompt.privacy_level == "private"
    assert message.parent_id == prompt.id
    assert message.privacy_level == level
    assert bool(message.public_slug) == (level == "public")


@pytest.mark.parametrize("level", ["public", "private"])
def test_an_uploaded_recording_that_starts_a_thread_has_a_private_prompt_entry(
        app, level):
    resp = _client(app, "anna").post("/api/nodes/upload/init", json={
        "filename": "note.webm", "filesize": 1000, "total_chunks": 1,
        "upload_id": uuid.uuid4().hex, "privacy_level": level,
        "ai_usage": "chat", "agentic": True})

    assert resp.status_code in (200, 201), resp.get_json()
    message = _get(resp.get_json()["node_id"])
    prompt = _get(message.parent_id)
    assert prompt.prompt_key == "textmode" and prompt.parent_id is None
    assert (message.privacy_level, prompt.privacy_level) == (level, "private")
    assert bool(message.public_slug) == (level == "public")


@pytest.mark.parametrize("level", ["public", "private"])
def test_the_helpers_store_private(app, level):
    from backend.utils.session_helpers import (
        attach_agentic_prompt_under, create_agentic_root)
    anna = _user("anna")
    root = create_agentic_root(anna.id, "textmode", "chat")
    own = _node("anna", "an entry of anna's", privacy=level)
    under = attach_agentic_prompt_under(own, anna.id, "textmode", "chat")
    _db.session.commit()

    assert root.privacy_level == "private"
    assert under.privacy_level == "private"


@pytest.mark.parametrize("level", ["public", "private"])
def test_a_recorded_reply_under_ones_own_gleaning_has_a_private_prompt_entry(
        app, level):
    gleaning = _gleaning("anna", level)

    resp = _record(app, "anna", gleaning, agentic=True, privacy_level=level,
                   ai_usage="chat")

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()
    prompt = _get(data["conversation_id"])
    message = _get(data["user_node_id"])
    assert prompt.parent_id == gleaning.id
    assert (message.privacy_level, prompt.privacy_level) == (level, "private")


@pytest.mark.parametrize("level", ["public", "private"])
def test_a_typed_reply_under_ones_own_gleaning_has_a_private_prompt_entry(
        app, level):
    gleaning = _gleaning("anna", level)

    resp = _client(app, "anna").post(
        f"/api/textmode/from-node/{gleaning.id}",
        json={"content": "a typed reply", "privacy_level": level,
              "ai_usage": "chat", "auto_generate": False})

    assert resp.status_code == 202, resp.get_json()
    data = resp.get_json()
    prompt = _get(data["prompt_node_id"])
    message = _get(data["user_node_id"])
    assert prompt.parent_id == gleaning.id
    assert (message.privacy_level, prompt.privacy_level) == (level, "private")


def test_under_ones_own_ai_reply_in_another_persons_public_thread(app):
    """Anna's public reply and the AI reply she asked for sit in Ben's
    public thread. A Text-mode session recorded under her AI reply gets
    a private prompt entry: Ben's thread page shows none of her prompt."""
    ben_gleaning = _gleaning("ben", "public")
    annas = _node("anna", "anna replies to ben", parent=ben_gleaning,
                  privacy="public")
    her_ai_reply = _ai_reply("anna", annas, "public")

    resp = _record(app, "anna", her_ai_reply, agentic=True,
                   privacy_level="public", ai_usage="chat")

    assert resp.status_code == 201, resp.get_json()
    prompt = _get(resp.get_json()["conversation_id"])
    assert prompt.parent_id == her_ai_reply.id
    assert prompt.privacy_level == "private"
    for url in (f"/api/commons/node/{ben_gleaning.id}",
                f"/node/{ben_gleaning.id}", f"/node/{prompt.id}",
                f"/api/commons/node/{prompt.id}"):
        _no_prompt_text(_body(_client(app).get(url)))
    assert _client(app, "ben").get(
        f"/api/nodes/{prompt.id}").status_code == 404


# ── "This node and all my replies" ────────────────────────────────────


def test_raising_a_threads_level_leaves_its_prompt_entries_private(app):
    entry = _node("anna", "anna entry")
    gleaning = _ai_reply("anna", entry, "private")
    prompt, message = _stored_prompt_entry("anna", gleaning, "private")
    earlier, earlier_msg = _stored_prompt_entry(
        "anna", gleaning, "private", stamped=False)

    resp = _put(app, "anna", entry.id, content="anna entry",
                privacy_level="public", apply_to_descendants=True)

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["descendants_updated"] == 3
    assert _get(gleaning.id).privacy_level == "public"
    assert _get(message.id).privacy_level == "public"
    assert _get(earlier_msg.id).privacy_level == "public"
    assert _get(prompt.id).privacy_level == "private"
    assert _get(earlier.id).privacy_level == "private"


def test_lowering_a_threads_level_applies_to_its_prompt_entries(app):
    entry = _node("anna", "anna entry", privacy="public")
    gleaning = _ai_reply("anna", entry, "public")
    prompt, message = _stored_prompt_entry("anna", gleaning, "public")

    resp = _put(app, "anna", entry.id, content="anna entry",
                privacy_level="private", apply_to_descendants=True)

    assert resp.status_code == 200, resp.get_json()
    for node in (entry, gleaning, prompt, message):
        assert _get(node.id).privacy_level == "private"


@pytest.mark.parametrize("level", ["private", "circles"])
def test_lowering_a_threads_level_lowers_a_public_prompt_entry_inside_it(
        app, level):
    """A prompt entry its owner had set public, in a public thread set
    to a less open level with all replies: the entry takes that level."""
    entry = _node("anna", "anna entry", privacy="public")
    gleaning = _ai_reply("anna", entry, "public")
    prompt, message = _stored_prompt_entry("anna", gleaning, "public")
    earlier, _ = _stored_prompt_entry(
        "anna", gleaning, "public", stamped=False)

    resp = _put(app, "anna", entry.id, content="anna entry",
                privacy_level=level, apply_to_descendants=True)

    assert resp.status_code == 200, resp.get_json()
    for node in (entry, gleaning, prompt, message, earlier):
        assert _get(node.id).privacy_level == level, node.id


def test_raising_to_the_middle_level_keeps_private_prompt_entries_and_lowers_public_ones(
        app):
    entry = _node("anna", "anna entry")
    private_prompt, m1 = _stored_prompt_entry("anna", entry, "private")
    public_prompt, m2 = _stored_prompt_entry("anna", entry, "public")

    resp = _put(app, "anna", entry.id, content="anna entry",
                privacy_level="circles", apply_to_descendants=True)

    assert resp.status_code == 200, resp.get_json()
    assert _get(private_prompt.id).privacy_level == "private"
    assert _get(public_prompt.id).privacy_level == "circles"
    assert _get(m1.id).privacy_level == "circles"
    assert _get(m2.id).privacy_level == "circles"


def test_a_change_of_ai_usage_still_applies_to_prompt_entries(app):
    entry = _node("anna", "anna entry")
    prompt, message = _stored_prompt_entry("anna", entry, "private")

    resp = _put(app, "anna", entry.id, content="anna entry",
                privacy_level="public", ai_usage="train",
                apply_to_descendants=True)

    assert resp.status_code == 200, resp.get_json()
    prompt = _get(prompt.id)
    assert (prompt.privacy_level, prompt.ai_usage) == ("private", "train")


# ── The owner's own edit of the prompt entry ──────────────────────────


def test_the_owner_sets_the_prompt_entry_public_and_private_again(app):
    prompt, message = _start(app)

    resp = _put(app, "anna", prompt.id, content=prompt.get_content(),
                privacy_level="public")

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["node"]["privacy_level"] == "public"
    assert _get(prompt.id).privacy_level == "public"
    # Still linked to her prompt (the text did not change).
    assert _get(prompt.id).has_artifact("prompt")
    page = _body(_client(app).get(f"/node/{message.id}"))
    assert "ANNA-PROMPT-TEXT" in page
    for mark in PERSONAL:
        assert mark not in page

    resp = _put(app, "anna", prompt.id, content=prompt.get_content(),
                privacy_level="private")
    assert resp.status_code == 200, resp.get_json()
    assert _get(prompt.id).privacy_level == "private"
    _no_prompt_text(_body(_client(app).get(f"/node/{message.id}")))


def test_the_owner_sets_the_prompt_entry_public_with_all_replies(app):
    prompt, message = _start(app, level="private")

    resp = _put(app, "anna", prompt.id, content=prompt.get_content(),
                privacy_level="public", apply_to_descendants=True)

    assert resp.status_code == 200, resp.get_json()
    assert _get(prompt.id).privacy_level == "public"
    assert _get(message.id).privacy_level == "public"


def test_another_member_cannot_change_the_prompt_entry(app):
    prompt, message = _start(app)

    resp = _put(app, "ben", prompt.id, content="x", privacy_level="public")

    assert resp.status_code in (403, 404)
    assert _get(prompt.id).privacy_level == "private"


# ── Public surfaces ───────────────────────────────────────────────────


def _listed_ids(app):
    """The thread tops each listing gives, by surface."""
    feed = _client(app, "ben").get("/api/commons/feed")
    assert feed.status_code == 200
    share = _client(app).get("/api/share/public/anna")
    assert share.status_code == 200
    return {
        "feed": [i["id"] for i in feed.get_json()["items"]],
        "share": [i["id"] for i in share.get_json()["shares"]],
    }


def test_a_public_thread_with_a_private_prompt_root_is_listed_once_from_its_message(
        app):
    prompt, message = _start(app)
    slug = message.public_slug
    assert slug == "anna-on-rivers"
    reply = _node("ben", "BEN-REPLY to anna", parent=message,
                  privacy="public")

    listed = _listed_ids(app)
    assert listed == {"feed": [message.id], "share": [message.id]}
    item = _client(app).get("/api/share/public/anna").get_json()["shares"][0]
    assert item["permalink"] == f"/@anna/{slug}"
    feed_item = _client(app, "ben").get(
        "/api/commons/feed").get_json()["items"][0]
    assert feed_item["permalink"] == f"/@anna/{slug}"
    assert feed_item["reply_count"] == 1
    assert "ANNA-PUBLIC-BODY" in feed_item["content"]

    anon = _client(app)
    profile = _body(anon.get("/@anna"))
    assert profile.count("<article>") == 1
    assert f"/@anna/{slug}" in profile and "Anna on rivers" in profile
    feed_xml = _body(anon.get("/@anna/feed.xml"))
    assert feed_xml.count("<entry>") == 1
    assert f"https://loore.org/@anna/{slug}" in feed_xml
    sitemap = _body(anon.get("/sitemap.xml"))
    assert sitemap.count("/@anna/") == 1
    assert f"https://loore.org/@anna/{slug}</loc>" in sitemap
    assert "https://loore.org/@anna</loc>" in sitemap

    article = anon.get(f"/@anna/{slug}")
    assert article.status_code == 200
    html = _body(article)
    assert "<h1>Anna on rivers</h1>" in html
    assert "ANNA-PUBLIC-BODY" in html and "BEN-REPLY" in html
    md = anon.get(f"/@anna/{slug}.md")
    assert md.status_code == 200 and "ANNA-PUBLIC-BODY" in _body(md)
    assert anon.get(f"/@anna/{slug}/og.png").status_code == 200
    assert anon.get("/@anna/og.png").status_code == 200

    by_id = anon.get(f"/node/{message.id}")
    assert by_id.status_code == 200
    assert "<h1>Anna on rivers</h1>" in _body(by_id)
    assert f'href="https://loore.org/@anna/{slug}"' in _body(by_id)
    from_reply = anon.get(f"/node/{reply.id}")
    assert from_reply.status_code == 200
    assert "<h1>Anna on rivers</h1>" in _body(from_reply)

    for node_id in (message.id, reply.id):
        thread = anon.get(f"/api/commons/node/{node_id}")
        assert thread.status_code == 200
        assert thread.get_json()["thread"]["id"] == message.id
    resolved = anon.get(f"/api/commons/permalink/anna/{slug}")
    assert resolved.get_json() == {"node_id": message.id}

    # The prompt entry itself: not found, for a visitor and a member.
    assert anon.get(f"/node/{prompt.id}").status_code == 404
    assert anon.get(f"/api/commons/node/{prompt.id}").status_code == 404
    assert _client(app, "ben").get(
        f"/api/nodes/{prompt.id}").status_code == 404
    for resp in (anon.get("/@anna"), anon.get("/@anna/feed.xml"), article,
                 md, by_id, from_reply, anon.get("/sitemap.xml"),
                 anon.get(f"/api/commons/node/{message.id}"),
                 anon.get("/api/share/public/anna"),
                 _client(app, "ben").get("/api/commons/feed")):
        _no_prompt_text(_body(resp))


def test_another_member_reads_the_thread_from_its_message(app):
    prompt, message = _start(app)

    resp = _client(app, "ben").get(f"/api/nodes/{message.id}")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ancestors"] == []
    assert "ANNA-PUBLIC-BODY" in data["content"]
    _no_prompt_text(_body(resp))


def test_a_private_thread_is_listed_nowhere(app):
    prompt, message = _start(app, level="private")

    assert _client(app).get("/api/share/public/anna").get_json()[
        "shares"] == []
    assert _client(app, "ben").get(
        "/api/commons/feed").get_json()["items"] == []
    assert _client(app).get("/@anna").status_code == 404
    assert _client(app).get("/@anna/feed.xml").status_code == 404
    assert "/@anna" not in _body(_client(app).get("/sitemap.xml"))
    assert _client(app).get(f"/node/{message.id}").status_code == 404


def test_what_counts_as_the_top_of_a_thread(app):
    """Listed: a public root; a public node WITH A SLUG under a living
    private prompt root of the same person. Not listed: such a node
    without a slug, or under a deleted prompt root, a private root that
    is no prompt entry, another person's private prompt root, a public
    prompt root (the root is listed), a private prompt entry further
    down a thread, or a prompt root without the key."""
    root = _node("anna", "a public root", privacy="public")

    def under(parent_owner, level, text, slug, **kw):
        prompt, message = _stored_prompt_entry(
            parent_owner, kw.pop("parent", None), level,
            message_privacy="public", text=text, **kw)
        message.public_slug = slug
        _db.session.commit()
        return prompt, message

    prompt, first = under("anna", "private", "slugged", "first")
    _, no_slug = under("anna", "private", "no slug", None)
    gone, under_gone = under("anna", "private", "deleted root", "gone")
    from datetime import datetime
    gone.deleted_at = datetime.utcnow()
    plain = _node("anna", "a private root")
    _node("anna", "under a private plain root", parent=plain,
          privacy="public", slug="plain")
    bens_prompt, _ = _stored_prompt_entry("ben", None, "private")
    _node("anna", "under ben's private prompt root", parent=bens_prompt,
          privacy="public", slug="bens")
    shared_prompt, _ = under("anna", "public", "public prompt root", "shared-root")
    under("anna", "private", "inside a thread", "inside", parent=root)
    under("anna", "private", "root without the key", "unstamped",
          stamped=False)
    _db.session.commit()

    tops = {root.id, first.id, shared_prompt.id}
    listed = _listed_ids(app)
    assert set(listed["feed"]) == tops and len(listed["feed"]) == 3
    assert set(listed["share"]) == tops and len(listed["share"]) == 3
    sitemap = _body(_client(app).get("/sitemap.xml"))
    assert "/@anna/first</loc>" in sitemap
    for slug in ("gone", "plain", "bens", "inside", "unstamped"):
        assert f"/@anna/{slug}</loc>" not in sitemap, slug


def _stored_private_session(key, text):
    """A session stored with a private prompt root (every Voice and
    Glean session; a Text one started private) and a private entry."""
    prompt, message = _stored_prompt_entry("anna", None, "private", text=text)
    prompt.prompt_key = key
    _db.session.commit()
    return prompt, message


def test_setting_a_stored_entry_public_lists_nothing(app):
    """A Voice entry, a gleaning and a Text message whose owner sets
    them public through the edit route: read by their link, in no
    listing, as before the rule. Public stored ones are not listed by
    the deploy either."""
    _, voice = _stored_private_session("voice", "# Voice\n\nVOICE-ENTRY")
    read_prompt, _ = _stored_private_session("read", "read entry")
    gleaning = _ai_reply("anna", read_prompt, "private")
    _, text = _stored_private_session("textmode", "# Text\n\nTEXT-ENTRY")
    _, stored_public = _stored_prompt_entry(
        "anna", None, "private", message_privacy="public",
        text="# Stored\n\nSTORED-PUBLIC")

    for node in (voice, gleaning, text):
        resp = _put(app, "anna", node.id, content=node.get_content(),
                    privacy_level="public")
        assert resp.status_code == 200, resp.get_json()
        assert _get(node.id).privacy_level == "public"
        assert _get(node.id).public_slug is None

    for node in (voice, gleaning, text, stored_public):
        assert _client(app).get(f"/node/{node.id}").status_code == 200
    assert _listed_ids(app) == {"feed": [], "share": []}
    assert _client(app).get("/@anna").status_code == 404
    assert _client(app).get("/@anna/feed.xml").status_code == 404
    assert "/@anna" not in _body(_client(app).get("/sitemap.xml"))


def test_an_older_client_that_records_a_public_thread_gets_the_permalink(
        app):
    """No settings in the request: the recording's own level applies,
    and a public first message under the new prompt root gets its slug."""
    d = Draft(user_id=_user("anna").id, node_id=None, parent_id=None,
              session_id=str(uuid.uuid4()), streaming_status="completed",
              streaming_total_chunks=1, streaming_completed_chunks=1,
              privacy_level="public", ai_usage="chat")
    d.set_content("# Spoken in public\n\nsaid aloud")
    _db.session.add(d)
    _db.session.commit()

    resp = _client(app, "anna").post(
        f"/api/drafts/streaming/{d.session_id}/save-as-node",
        json={"agentic": True})

    assert resp.status_code == 201, resp.get_json()
    message = _get(resp.get_json()["user_node_id"])
    assert _get(message.parent_id).privacy_level == "private"
    assert message.public_slug == "spoken-in-public"
    assert _listed_ids(app)["share"] == [message.id]


def test_an_uploaded_public_thread_is_listed_and_named_by_its_transcript(app):
    resp = _client(app, "anna").post("/api/nodes/upload/init", json={
        "filename": "note.webm", "filesize": 1000, "total_chunks": 1,
        "upload_id": uuid.uuid4().hex, "privacy_level": "public",
        "ai_usage": "chat", "agentic": True})
    assert resp.status_code in (200, 201), resp.get_json()
    message = _get(resp.get_json()["node_id"])
    assert message.public_slug == "recording"
    assert _listed_ids(app)["share"] == [message.id]

    # What the transcription task does once the text exists.
    from backend.utils.slugs import settle_pending_slug
    message.set_content("A walk by the river\n\nand more")
    settle_pending_slug(message, message.get_content())
    _db.session.commit()

    assert _get(message.id).public_slug == "a-walk-by-the-river"
    assert _client(app).get("/@anna/a-walk-by-the-river").status_code == 200
    assert _client(app).get("/@anna/recording").status_code == 404
    # An entry that is no longer public, or has a slug of its own, keeps
    # what it has.
    kept = _node("anna", "Other words", privacy="public", slug="my-own")
    settle_pending_slug(kept, "Other words")
    assert kept.public_slug == "my-own"
    private = _node("anna", "Words", slug="recording-2")
    settle_pending_slug(private, "Words")
    assert private.public_slug == "recording-2"


def test_a_deleted_message_and_a_paused_account_leave_the_listings(app):
    prompt, message = _start(app)
    assert _listed_ids(app)["share"] == [message.id]

    anna = _user("anna")
    anna.public_sharing_enabled = False
    _db.session.commit()
    assert _client(app).get("/api/share/public/anna").status_code == 404
    assert _client(app, "ben").get(
        "/api/commons/feed").get_json()["items"] == []
    anna.public_sharing_enabled = True
    _db.session.commit()

    resp = _client(app, "anna").delete(f"/api/nodes/{message.id}")
    assert resp.status_code in (200, 204), resp.get_json()
    assert _client(app, "ben").get(
        "/api/commons/feed").get_json()["items"] == []
    assert _client(app).get("/@anna").status_code == 404


def test_a_rename_keeps_the_old_handle_for_such_a_thread(app):
    """The account has public writing (the message), so its former
    handle keeps leading to it."""
    from backend.utils.username_history import _published_under
    assert not _published_under(_user("anna"))
    _start(app)
    assert _published_under(_user("anna"))


def test_two_threads_with_the_same_title_get_their_own_permalinks(app):
    _, first = _start(app)
    _, second = _start(app)

    assert first.public_slug == "anna-on-rivers"
    assert second.public_slug == "anna-on-rivers-2"
    for message in (first, second):
        resolved = _client(app).get(
            f"/api/commons/permalink/anna/{message.public_slug}")
        assert resolved.get_json() == {"node_id": message.id}
    assert sorted(_listed_ids(app)["share"]) == [first.id, second.id]


# ── A private prompt entry inside a public thread ─────────────────────


def _typed_under(app, who, node, level="public", text="ANNA-TYPED reply"):
    resp = _client(app, who).post(
        f"/api/textmode/from-node/{node.id}",
        json={"content": text, "privacy_level": level, "ai_usage": "chat",
              "auto_generate": False})
    assert resp.status_code == 202, resp.get_json()
    data = resp.get_json()
    return _get(data["prompt_node_id"]), _get(data["user_node_id"])


def _tree_ids(thread):
    """{node id: its children's ids} of a serialized thread."""
    out = {}
    stack = [thread]
    while stack:
        item = stack.pop()
        out[item["id"]] = [c["id"] for c in item["children"]]
        stack.extend(item["children"])
    return out


def _own_public_thread(app):
    """Anna's public entry and gleaning, a typed public reply under the
    gleaning (with its private prompt entry), Ben's public reply to it."""
    entry = _node("anna", "# Anna gleans\n\nANNA-ENTRY-BODY",
                  privacy="public", slug="anna-gleans")
    gleaning = _ai_reply("anna", entry, "public")
    prompt, message = _typed_under(app, "anna", gleaning)
    assert prompt.parent_id == gleaning.id
    assert (prompt.privacy_level, message.privacy_level) == (
        "private", "public")
    reply = _node("ben", "BEN-REPLY to the typed one", parent=message,
                  privacy="public")
    return entry, gleaning, prompt, message, reply


def test_a_visitor_reads_the_message_inside_its_thread(app):
    entry, gleaning, prompt, message, reply = _own_public_thread(app)
    anon = _client(app)

    for node_id in (entry.id, gleaning.id, message.id, reply.id):
        resp = anon.get(f"/api/commons/node/{node_id}")
        assert resp.status_code == 200
        tree = _tree_ids(resp.get_json()["thread"])
        assert resp.get_json()["thread"]["id"] == entry.id
        assert tree == {entry.id: [gleaning.id], gleaning.id: [message.id],
                        message.id: [reply.id], reply.id: []}
        _no_prompt_text(_body(resp))
    for url in (f"/node/{entry.id}", f"/node/{message.id}",
                f"/node/{reply.id}", "/@anna/anna-gleans"):
        page = anon.get(url)
        assert page.status_code == 200
        html = _body(page)
        assert "<h1>Anna gleans</h1>" in html, url
        assert "ANNA-TYPED reply" in html and "BEN-REPLY" in html, url
        _no_prompt_text(html)
    assert anon.get(f"/node/{prompt.id}").status_code == 404
    assert anon.get(f"/api/commons/node/{prompt.id}").status_code == 404
    # The thread is one entry in the listings; the message is not a top.
    assert _listed_ids(app) == {"feed": [entry.id], "share": [entry.id]}


def test_another_member_reads_the_message_inside_its_thread(app):
    entry, gleaning, prompt, message, reply = _own_public_thread(app)
    ben = _client(app, "ben")

    resp = ben.get(f"/api/nodes/{gleaning.id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [c["id"] for c in data["children"]] == [message.id]
    assert data["child_count"] == 1
    assert [c["id"] for c in data["children"][0]["children"]] == [reply.id]
    _no_prompt_text(_body(resp))

    resp = ben.get(f"/api/nodes/{entry.id}")
    nested = resp.get_json()["children"][0]
    assert nested["id"] == gleaning.id
    assert [c["id"] for c in nested["children"]] == [message.id]
    _no_prompt_text(_body(resp))

    resp = ben.get(f"/api/nodes/{reply.id}")
    data = resp.get_json()
    assert [a["id"] for a in data["ancestors"]] == [
        entry.id, gleaning.id, message.id]
    assert data["ancestors"][1]["child_count"] == 1
    _no_prompt_text(_body(resp))
    assert ben.get(f"/api/nodes/{prompt.id}").status_code == 404


def test_the_owner_still_sees_her_prompt_entry_where_it_is(app):
    entry, gleaning, prompt, message, reply = _own_public_thread(app)

    data = _client(app, "anna").get(f"/api/nodes/{gleaning.id}").get_json()

    assert [c["id"] for c in data["children"]] == [prompt.id]
    assert [c["id"] for c in data["children"][0]["children"]] == [message.id]
    data = _client(app, "anna").get(f"/api/nodes/{message.id}").get_json()
    assert [a["id"] for a in data["ancestors"]] == [
        entry.id, gleaning.id, prompt.id]


def test_the_message_shows_inside_another_persons_public_thread(app):
    ben_entry = _node("ben", "# Ben gleans\n\nBEN-BODY", privacy="public",
                      slug="ben-gleans")
    ben_gleaning = _ai_reply("ben", ben_entry, "public")
    annas = _node("anna", "anna replies to ben", parent=ben_gleaning,
                  privacy="public")
    her_ai_reply = _ai_reply("anna", annas, "public")
    resp = _record(app, "anna", her_ai_reply, agentic=True,
                   privacy_level="public", ai_usage="chat")
    assert resp.status_code == 201, resp.get_json()
    prompt = _get(resp.get_json()["conversation_id"])
    message = _get(resp.get_json()["user_node_id"])
    assert prompt.parent_id == her_ai_reply.id
    assert prompt.privacy_level == "private"

    for viewer in (None, "ben"):
        resp = _client(app, viewer).get(f"/api/commons/node/{message.id}")
        thread = resp.get_json()["thread"]
        assert thread["id"] == ben_entry.id
        tree = _tree_ids(thread)
        assert tree[her_ai_reply.id] == [message.id]
        assert prompt.id not in tree
        _no_prompt_text(_body(resp))
    page = _body(_client(app).get("/@ben/ben-gleans"))
    assert "said aloud" in page
    _no_prompt_text(page)
    data = _client(app, "ben").get(
        f"/api/nodes/{her_ai_reply.id}").get_json()
    assert [c["id"] for c in data["children"]] == [message.id]


def test_only_a_prompt_entry_is_passed_through_and_only_to_its_owners_nodes(
        app):
    entry = _node("anna", "# Anna entry\n\nbody", privacy="public")
    # A private node of Anna's that is no prompt entry, a public one
    # under it.
    plain = _node("anna", "a private note", parent=entry)
    under_plain = _node("anna", "UNDER-A-PRIVATE-NOTE", parent=plain,
                        privacy="public")
    # A private prompt entry of Anna's: her public message under it,
    # Ben's public node directly under it, her private message under it.
    prompt, hers = _stored_prompt_entry(
        "anna", entry, "private", message_privacy="public",
        text="HERS-UNDER-THE-PROMPT")
    bens = _node("ben", "BENS-UNDER-THE-PROMPT", parent=prompt,
                 privacy="public")
    private_msg = _node("anna", "PRIVATE-UNDER-THE-PROMPT", parent=prompt)
    # A deleted prompt entry is not passed through.
    gone, under_gone = _stored_prompt_entry(
        "anna", entry, "private", message_privacy="public",
        text="UNDER-A-DELETED-PROMPT")
    from datetime import datetime
    gone.deleted_at = datetime.utcnow()
    _db.session.commit()

    resp = _client(app).get(f"/api/commons/node/{entry.id}")
    assert _tree_ids(resp.get_json()["thread"]) == {
        entry.id: [hers.id], hers.id: []}
    data = _client(app, "ben").get(f"/api/nodes/{entry.id}").get_json()
    assert [c["id"] for c in data["children"]] == [hers.id]
    for node in (under_plain, bens, under_gone):
        thread = _client(app).get(
            f"/api/commons/node/{node.id}").get_json()["thread"]
        assert thread["id"] == node.id, node.id
    assert _client(app).get(
        f"/api/commons/node/{private_msg.id}").status_code == 404


def test_the_thread_page_makes_as_many_queries_with_a_prompt_entry_in_it(app):
    """A public thread whose reply hangs directly under the gleaning,
    and one whose reply hangs under a private prompt entry there: the
    public thread's JSON and its page make the same number of queries
    for both. (The member's thread view reads the prefetched subtree.)"""
    plain_entry = _node("anna", "# Plain\n\nbody", privacy="public")
    plain_gleaning = _ai_reply("anna", plain_entry, "public")
    _node("anna", "a plain public reply", parent=plain_gleaning,
          privacy="public")
    entry = _node("anna", "# With a prompt\n\nbody", privacy="public")
    gleaning = _ai_reply("anna", entry, "public")
    prompt, message = _typed_under(app, "anna", gleaning)
    assert prompt.privacy_level == "private"

    for url, who in (("/api/commons/node/{}", None), ("/node/{}", None)):
        counts = [_statements(app, lambda i=i: _client(app, who).get(
            url.format(i))) for i in (plain_entry.id, entry.id)]
        assert counts[0] == counts[1], url


# ── Queries of a listing page ─────────────────────────────────────────


def _statements(app, request):
    seen = []
    # A session of its own, as a request has: nothing the test loaded
    # is reused.
    _db.session.remove()

    def _count(conn, cursor, statement, parameters, context, executemany):
        # Not the session's own reads of the signed-in account (and its
        # last-seen stamp, written once in a while).
        if "node" in statement or "share_draft" in statement:
            seen.append(statement)
    event.listen(_db.engine, "before_cursor_execute", _count)
    try:
        resp = request()
    finally:
        event.remove(_db.engine, "before_cursor_execute", _count)
    assert resp.status_code == 200
    return len(seen)


def test_a_listing_page_makes_as_many_queries_for_either_kind_of_top(app):
    """Three plain public roots, three more, then three threads under
    private prompt roots: each step adds the same number of queries to a
    listing (what an entry cost before: its pins, its publish date), and
    none to the sitemap. The rule is part of the listing's own query."""
    requests = {
        "share": lambda: _client(app).get("/api/share/public/anna"),
        "profile": lambda: _client(app).get("/@anna"),
        "feed.xml": lambda: _client(app).get("/@anna/feed.xml"),
        "sitemap": lambda: _client(app).get("/sitemap.xml"),
        "feed": lambda: _client(app, "ben").get("/api/commons/feed"),
    }

    def measure():
        return {name: _statements(app, req)
                for name, req in requests.items()}

    for i in range(3):
        _node("anna", f"# Root {i}\n\nbody", privacy="public",
              slug=f"root-{i}")
    three = measure()
    for i in range(3, 6):
        _node("anna", f"# Root {i}\n\nbody", privacy="public",
              slug=f"root-{i}")
    six = measure()
    for i in range(3):
        _start(app, content=f"# Thread {i}\n\nbody")
    nine = measure()

    assert len(_listed_ids(app)["share"]) == 9
    for name in requests:
        assert nine[name] - six[name] == six[name] - three[name], name
    assert nine["sitemap"] == three["sitemap"] == 1


# ── Page cache ────────────────────────────────────────────────────────


def test_an_edit_of_the_message_drops_the_threads_cached_pages(
        app, fake_redis):
    prompt, message = _start(app)
    slug = message.public_slug
    reply = _node("ben", "BEN-REPLY", parent=message, privacy="public")
    anon = _client(app)
    pages = [f"/@anna/{slug}", f"/@anna/{slug}.md", f"/node/{message.id}",
             f"/node/{reply.id}", "/@anna", "/@anna/feed.xml",
             "/sitemap.xml"]
    for url in pages:
        assert anon.get(url).status_code == 200
    for url in pages:
        assert f"public_html:{url}" in fake_redis.store, url

    resp = _put(app, "anna", message.id,
                content="# Anna on rivers\n\nANNA-NEW-BODY",
                privacy_level="public")

    assert resp.status_code == 200, resp.get_json()
    for url in pages:
        assert f"public_html:{url}" not in fake_redis.store, url
    for url in pages[:6]:
        page = _body(anon.get(url))
        assert "ANNA-PUBLIC-BODY" not in page, url
    assert "ANNA-NEW-BODY" in _body(anon.get(f"/@anna/{slug}"))


def test_a_reply_and_a_takedown_drop_the_threads_cached_pages(
        app, fake_redis):
    prompt, message = _start(app)
    slug = message.public_slug
    anon = _client(app)
    article_keys = [f"public_html:/@anna/{slug}",
                    f"public_html:/node/{message.id}"]
    anon.get(f"/@anna/{slug}")
    anon.get(f"/node/{message.id}")
    reply = _node("ben", "BEN-REPLY", parent=message, privacy="public")
    from backend.utils import public_cache
    public_cache.invalidate_for_node(reply)
    for key in article_keys:
        assert key not in fake_redis.store
    assert "BEN-REPLY" in _body(anon.get(f"/@anna/{slug}"))
    anon.get(f"/node/{message.id}")

    resp = _put(app, "anna", message.id, content=message.get_content(),
                privacy_level="private")

    assert resp.status_code == 200, resp.get_json()
    for key in article_keys:
        assert key not in fake_redis.store
    assert anon.get(f"/@anna/{slug}").status_code == 404
    assert anon.get(f"/node/{message.id}").status_code == 404


def test_the_many_node_and_account_drops_reach_the_messages_pages(
        app, fake_redis):
    from backend.utils import public_cache
    prompt, message = _start(app)
    key = f"public_html:/@anna/{message.public_slug}"

    _client(app).get(f"/@anna/{message.public_slug}")
    assert key in fake_redis.store
    public_cache.invalidate_for_nodes([message.id])
    assert key not in fake_redis.store

    _client(app).get(f"/@anna/{message.public_slug}")
    assert key in fake_redis.store
    public_cache.invalidate_for_user(_user("anna"))
    assert key not in fake_redis.store


# ── The owner's own views ─────────────────────────────────────────────


def test_the_owner_sees_her_thread_as_before(app):
    prompt, message = _start(app)
    anna = _client(app, "anna")

    data = anna.get(f"/api/nodes/{message.id}").get_json()
    assert [a["id"] for a in data["ancestors"]] == [prompt.id]
    assert "ANNA-PROMPT-TEXT" in data["ancestors"][0]["content"]
    assert data["ancestors"][0]["privacy_level"] == "private"
    assert data["privacy_level"] == "public"

    conversation = anna.get(
        f"/api/textmode/from-node/{message.id}").get_json()
    assert conversation["conversation_id"] == prompt.id
    assert [m["id"] for m in conversation["messages"]] == [message.id]

    log = anna.get("/api/log")
    assert log.status_code == 200
    assert _body(log).count("ANNA-PUBLIC-BODY") == 1

    more = anna.post(f"/api/textmode/{prompt.id}/message", json={
        "content": "a second message", "parent_id": message.id})
    assert more.status_code == 202, more.get_json()
    second = _get(more.get_json()["user_node_id"])
    assert second.parent_id == message.id
    assert second.privacy_level == "public"


# ── Every GET route, as a visitor and as another member ───────────────

SKIP_PREFIXES = ("/api/sse", "/static", "/media", "/auth", "/api/admin",
                 "/api/external/twitter", "/api/voice/approval",
                 "/api/voice/refusal", "/api/import/status",
                 "/api/drafts/streaming")


def _sweep(app, viewer, ints, strings, extra):
    """Every GET route as *viewer*; ({url: marks seen}, requests made)."""
    client = _client(app, viewer)
    seen, count = {}, 0
    urls = list(extra)
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.rule.startswith(SKIP_PREFIXES):
            continue
        args = sorted(rule.arguments)
        pools = []
        for a in args:
            conv = rule._converters[a].__class__.__name__
            pools.append(ints if conv == "IntegerConverter" else strings)
        for combo in itertools.product(*pools):
            urls.append(rule.build(dict(zip(args, combo)))[1])
    for url in urls:
        try:
            r = client.get(url)
        except Exception:  # a route that cannot run on sqlite
            continue
        count += 1
        body = _body(r)
        marks = [m for m in PERSONAL + PROMPT_MARKS if m in body]
        if marks:
            seen[url] = (r.status_code, marks)
    return seen, count


def test_no_route_gives_the_prompt_text_to_anyone_but_its_owner(app):
    # Her own public Text-mode thread.
    typed_prompt, typed = _start(app)
    # A second one, its prompt edited inside the thread.
    edited_prompt, edited = _start(
        app, content="# Anna second\n\nANNA-PUBLIC-BODY two")
    r = _put(app, "anna", edited_prompt.id, detach_prompt=True,
             privacy_level="private",
             content="ANNA-EDITED-IN-THREAD {user_profile} {user_memory} "
                     "{user_todo} {user_intentions} {user_recent}")
    assert r.status_code == 200, r.get_json()
    # A recorded public thread, and recorded replies under her own
    # public gleaning and under her AI reply in Ben's public thread.
    r = _record(app, "anna", agentic=True, privacy_level="public",
                ai_usage="chat")
    recorded_prompt = _get(r.get_json()["conversation_id"])
    own = _gleaning("anna", "public")
    r = _record(app, "anna", own, agentic=True, privacy_level="public",
                ai_usage="chat")
    own_prompt = _get(r.get_json()["conversation_id"])
    ben_gleaning = _gleaning("ben", "public")
    annas = _node("anna", "anna replies to ben", parent=ben_gleaning,
                  privacy="public")
    r = _record(app, "anna", _ai_reply("anna", annas, "public"),
                agentic=True, privacy_level="public", ai_usage="chat")
    guest_prompt = _get(r.get_json()["conversation_id"])
    prompts = [typed_prompt, edited_prompt, recorded_prompt, own_prompt,
               guest_prompt]
    # The two inside a public thread are passed through: their messages
    # show in the thread, in Anna's and in Ben's.
    for inside, top in ((own_prompt, own), (guest_prompt, ben_gleaning)):
        tree = _client(app).get(
            f"/api/commons/node/{top.id}").get_data(as_text=True)
        assert "said aloud" in tree and inside.parent_id is not None
    assert {p.privacy_level for p in prompts} == {"private"}
    # Ben quotes each prompt entry from a node of his own.
    r = _client(app, "ben").post("/api/nodes/", json={
        "content": "ben quotes " + " ".join(
            f"{{quote:{p.id}}}" for p in prompts),
        "privacy_level": "private", "ai_usage": "chat"})
    assert r.status_code in (200, 201), r.get_json()
    ben_quoting = r.get_json()["id"]

    # The owner is served the text and the pins (not an empty run).
    own_view = _body(_client(app, "anna").get(
        f"/api/nodes/{typed_prompt.id}"))
    for mark in ("ANNA-PROMPT-TEXT", "ANNA-PROFILE", "ANNA-MEMORY"):
        assert mark in own_view, mark
    assert "ANNA-EDITED-IN-THREAD" in _body(_client(app, "anna").get(
        f"/api/nodes/{edited_prompt.id}"))

    max_id = _db.session.query(_db.func.max(Node.id)).scalar()
    ints = list(range(1, max_id + 2))
    slugs = [n.public_slug for n in Node.query.filter(
        Node.public_slug.isnot(None))]
    strings = ["anna", "ben", "textmode", "voice", "intentions", "memory",
               "scratchpad", "ai_preferences", "profile", "todo"] + slugs
    extra = ["/api/search?q=ANNA", "/api/search?q=profile",
             "/api/search?q=anna", "/api/search/semantic?q=ANNA",
             f"/api/search/neighbors?node_id={typed_prompt.id}",
             "/api/log?scope=all", "/api/commons/feed?per_page=100",
             "/api/nodes/titles?ids=" + ",".join(map(str, ints)),
             f"/api/nodes/{ben_quoting}/resolve-quotes",
             "/api/export/threads?format=json", "/api/export/threads",
             "/api/dashboard/?username=anna", "/api/share/public/anna",
             "/api/log?username=anna", "/api/dashboard/anna"]
    for viewer in (None, "ben"):
        seen, count = _sweep(app, viewer, ints, strings, extra)
        assert count > 500
        assert seen == {}, (viewer, seen)
