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


def test_a_change_of_ai_usage_still_applies_to_prompt_entries(app):
    entry = _node("anna", "anna entry")
    prompt, message = _stored_prompt_entry("anna", entry, "private")

    resp = _put(app, "anna", entry.id, content="anna entry",
                privacy_level="public", ai_usage="train",
                apply_to_descendants=True)

    assert resp.status_code == 200, resp.get_json()
    prompt = _get(prompt.id)
    assert (prompt.privacy_level, prompt.ai_usage) == ("private", "train")
