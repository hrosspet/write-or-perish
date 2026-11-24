"""A Private reply under one's own Public entry (Peter, 2026-11-24:
"they shouldn't be refused, they should adhere to the form's selection").

Under a public node of the user's own (one they wrote, or an AI reply
they asked for, a Glean included) a typed reply sent to POST /nodes/ and
a recorded one saved through save-as-node are saved at the level the
form sent, as POST /textmode/from-node saves a typed one. Under someone
else's public node the rule of #228 stays: the reply must be public.

The second half checks that such a reply stays unreadable to everyone
else on every surface: the public thread JSON, the server-rendered
pages, another member's view of the node above (children and counts),
the public listings and feeds, and the chain an AI reply of another
person is built from.

Route-level tests; the generation task is mocked.
"""
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
# Keep the (best-effort, no-op here) draft→node audio move out of the repo.
os.environ["AUDIO_STORAGE_PATH"] = tempfile.mkdtemp(prefix="wop-audio-test-")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())
sys.modules.setdefault("ffmpeg", MagicMock())

_mock_llm_task_module = MagicMock()
_mock_task_result = MagicMock()
_mock_task_result.id = "fake-task-id"
_mock_llm_task_module.generate_llm_response.delay.return_value = (
    _mock_task_result
)

import pytest  # noqa: E402
from flask import Flask, g  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node, Draft     # noqa: E402
import backend.models as _real_backend_models    # noqa: E402

TYPED = "TYPED-PRIVATE-NOTE"
RECORDED = "RECORDED-PRIVATE-NOTE"
CIRCLES = "TYPED-CIRCLES-NOTE"
SECRETS = (TYPED, RECORDED, CIRCLES)


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = True
    # No build dir -> the minimal fallback shell; no broker -> cache off.
    app.config["FRONTEND_BUILD_DIR"] = None
    app.config["PUBLIC_BASE_URL"] = "https://loore.org"
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    }

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.nodes import nodes_bp
    from backend.routes.drafts import drafts_bp
    from backend.routes.textmode import textmode_bp
    from backend.routes.commons import commons_bp
    from backend.routes.share import share_bp
    from backend.routes.public_pages import public_pages_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(drafts_bp, url_prefix="/api/drafts")
    app.register_blueprint(textmode_bp, url_prefix="/api/textmode")
    app.register_blueprint(commons_bp, url_prefix="/api/commons")
    app.register_blueprint(share_bp, url_prefix="/api/share")
    app.register_blueprint(public_pages_bp)
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
        or k == "backend.tasks.llm_completion"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}

    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    sys.modules["backend.tasks.llm_completion"] = _mock_llm_task_module
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]
    _mock_llm_task_module.generate_llm_response.delay.reset_mock()

    app = _make_app()
    with app.app_context():
        _db.create_all()
        for name in ("ben", "anna"):
            _db.session.add(User(
                username=name, default_ai_usage="chat", approved=True,
                plan="alpha", public_sharing_enabled=True))
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


def _user(username):
    return User.query.filter_by(username=username).first()


def _client(app, username=None):
    # Flask-Login caches the user on g: a second client in one test would
    # otherwise act as the first.
    g.pop("_login_user", None)
    client = app.test_client()
    if username is not None:
        with client.session_transaction() as sess:
            sess["_user_id"] = str(_user(username).id)
            sess["_fresh"] = True
    return client


def _entry(username, content, parent=None, privacy="public", slug=None):
    user = _user(username)
    node = Node(user_id=user.id, human_owner_id=user.id,
                parent_id=parent.id if parent else None, node_type="user",
                privacy_level=privacy, ai_usage="chat", public_slug=slug)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _glean(owner, parent, privacy="public"):
    """An AI reply *owner* asked for under *parent* (a Glean's shape)."""
    llm_user = User.query.filter_by(username="gpt-5").first()
    if llm_user is None:
        llm_user = User(twitter_id="llm-gpt-5", username="gpt-5")
        _db.session.add(llm_user)
        _db.session.flush()
    node = Node(user_id=llm_user.id, human_owner_id=_user(owner).id,
                parent_id=parent.id, node_type="llm", llm_model="gpt-5",
                llm_task_status="completed", privacy_level=privacy,
                ai_usage="chat")
    node.set_content("Picks for today.")
    _db.session.add(node)
    _db.session.commit()
    return node


def _type(app, username, parent, content, privacy, ai_usage="none"):
    return _client(app, username).post("/api/nodes/", json={
        "content": content, "parent_id": parent.id,
        "privacy_level": privacy, "ai_usage": ai_usage})


def _record(app, username, parent, content, **body):
    """*username* saves a finished recording made under *parent*."""
    draft = Draft(user_id=_user(username).id, node_id=None,
                  parent_id=parent.id, session_id=str(uuid.uuid4()),
                  streaming_status="completed", streaming_total_chunks=1,
                  streaming_completed_chunks=1, privacy_level="public",
                  ai_usage="chat")
    draft.set_content(content)
    _db.session.add(draft)
    _db.session.commit()
    return _client(app, username).post(
        f"/api/drafts/streaming/{draft.session_id}/save-as-node",
        json={"content": content, **body})


# ── The two paths take the form's level under one's own node ──────────


@pytest.mark.parametrize("level", ["private", "circles"])
@pytest.mark.parametrize("parent_kind", ["entry", "glean"])
def test_a_typed_reply_under_ones_own_public_node_takes_the_forms_level(
        app, level, parent_kind):
    entry = _entry("ben", "A public piece.", slug="a-public-piece")
    parent = entry if parent_kind == "entry" else _glean("ben", entry)

    resp = _type(app, "ben", parent, TYPED, level)

    assert resp.status_code == 201, resp.get_json()
    node = _db.session.get(Node, resp.get_json()["id"])
    assert (node.parent_id, node.privacy_level) == (parent.id, level)
    assert node.public_slug is None


@pytest.mark.parametrize("level", ["private", "circles"])
@pytest.mark.parametrize("parent_kind", ["entry", "glean"])
def test_a_recorded_reply_under_ones_own_public_node_takes_the_forms_level(
        app, level, parent_kind):
    entry = _entry("ben", "A public piece.", slug="a-public-piece")
    parent = entry if parent_kind == "entry" else _glean("ben", entry)

    resp = _record(app, "ben", parent, RECORDED, privacy_level=level,
                   ai_usage="chat")

    assert resp.status_code == 201, resp.get_json()
    node = _db.session.get(Node, resp.get_json()["id"])
    assert (node.parent_id, node.privacy_level) == (parent.id, level)
    assert node.public_slug is None
    assert Draft.query.count() == 0


@pytest.mark.parametrize("level", ["private", "circles"])
def test_a_recorded_reply_with_the_text_mode_prompt_keeps_it_private(
        app, level):
    """The reply box under a Glean records with `agentic`: the prompt
    node goes between the Glean and the message, and is private, as
    from-node makes it for a typed message."""
    glean = _glean("ben", _entry("ben", "A public piece."))

    resp = _record(app, "ben", glean, RECORDED, privacy_level=level,
                   ai_usage="chat", agentic=True)

    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()
    prompt = _db.session.get(Node, data["conversation_id"])
    message = _db.session.get(Node, data["user_node_id"])
    assert (prompt.parent_id, prompt.privacy_level) == (glean.id, "private")
    assert (message.parent_id, message.privacy_level) == (prompt.id, level)


@pytest.mark.parametrize("level", ["private", "circles"])
def test_the_audio_upload_path_takes_the_forms_level_too(
        app, level, monkeypatch):
    """POST /nodes/ with an audio file applies the same rule."""
    import io
    import types
    fake = types.ModuleType("backend.tasks.transcription")
    fake.transcribe_audio = MagicMock()
    fake.transcribe_audio.delay.return_value.id = "task-1"
    monkeypatch.setitem(sys.modules, "backend.tasks.transcription", fake)
    entry = _entry("ben", "A public piece.")
    other = _entry("anna", "Anna's public piece.")
    client = _client(app, "ben")

    def upload(parent):
        return client.post("/api/nodes/", data={
            "audio_file": (io.BytesIO(b"RIFF....WAVE"), "note.wav"),
            "parent_id": str(parent.id), "privacy_level": level,
            "ai_usage": "chat"}, content_type="multipart/form-data")

    refused = upload(other)
    assert refused.status_code == 400
    assert refused.get_json()["code"] == "public_reply_required"

    resp = upload(entry)
    assert resp.status_code == 201, resp.get_json()
    node = _db.session.get(Node, resp.get_json()["id"])
    assert (node.parent_id, node.privacy_level) == (entry.id, level)


def test_from_node_saves_the_same_reply_at_the_same_level(app):
    """The third path, for comparison: a typed reply with AI usage Chat
    under one's own Glean (POST /textmode/from-node)."""
    glean = _glean("ben", _entry("ben", "A public piece."))

    resp = _client(app, "ben").post(
        f"/api/textmode/from-node/{glean.id}",
        json={"content": TYPED, "privacy_level": "private",
              "ai_usage": "chat", "auto_generate": False})

    assert resp.status_code == 202, resp.get_json()
    message = _db.session.get(Node, resp.get_json()["user_node_id"])
    assert message.privacy_level == "private"


# ── Under someone else's public node: the #228 rule, unchanged ────────


@pytest.mark.parametrize("level", ["private", "circles"])
@pytest.mark.parametrize("parent_kind", ["entry", "glean"])
def test_under_someone_elses_public_node_both_paths_are_refused(
        app, level, parent_kind):
    entry = _entry("ben", "A public piece.", slug="a-public-piece")
    parent = entry if parent_kind == "entry" else _glean("ben", entry)

    typed = _type(app, "anna", parent, TYPED, level)
    recorded = _record(app, "anna", parent, RECORDED, privacy_level=level,
                       ai_usage="chat")

    for resp in (typed, recorded):
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["code"] == "public_reply_required"
    assert Node.query.filter(Node.user_id == _user("anna").id).count() == 0
    # The recording stays, so it can be saved as Public.
    assert Draft.query.count() == 1


def test_ones_own_thread_does_not_make_someone_elses_node_ones_own(app):
    """Only the node above counts: Anna's public reply in Ben's thread
    is hers, so Ben's Private reply under it is refused."""
    entry = _entry("ben", "A public piece.")
    annas = _entry("anna", "A public reply.", parent=entry)

    resp = _type(app, "ben", annas, TYPED, "private")

    assert resp.status_code == 400
    assert resp.get_json()["code"] == "public_reply_required"


def test_ones_own_node_in_someone_elses_thread_counts(app):
    """And the other way round: Ben's public reply in Anna's thread is
    his, so his Private reply under it is saved private."""
    annas = _entry("anna", "Anna's public piece.")
    bens = _entry("ben", "A public reply.", parent=annas)

    resp = _type(app, "ben", bens, TYPED, "private")

    assert resp.status_code == 201, resp.get_json()
    assert _db.session.get(
        Node, resp.get_json()["id"]).privacy_level == "private"


def test_a_public_reply_under_someone_elses_public_node_is_saved(app):
    entry = _entry("ben", "A public piece.")

    resp = _type(app, "anna", entry, "A public reply.", "public")

    assert resp.status_code == 201, resp.get_json()


# ── The private reply stays unreadable to everyone else ───────────────


@pytest.fixture
def thread(app):
    """Ben's public piece (with a permalink) and his public Glean under
    it; Anna's public reply under the piece; Ben's Private and Circles
    replies made through the two paths: typed under the piece and under
    the Glean, recorded (with the Text-mode prompt) under the Glean."""
    entry = _entry("ben", "A public piece.", slug="a-public-piece")
    glean = _glean("ben", entry)
    annas = _entry("anna", "Anna's public reply.", parent=entry)
    typed = _type(app, "ben", entry, TYPED, "private")
    circles = _type(app, "ben", glean, CIRCLES, "circles")
    recorded = _record(app, "ben", glean, RECORDED, privacy_level="private",
                       ai_usage="chat", agentic=True)
    for resp in (typed, circles, recorded):
        assert resp.status_code == 201, resp.get_json()
    rec = recorded.get_json()
    return {
        "entry": entry, "glean": glean, "annas": annas,
        "typed": _db.session.get(Node, typed.get_json()["id"]),
        "circles": _db.session.get(Node, circles.get_json()["id"]),
        "prompt": _db.session.get(Node, rec["conversation_id"]),
        "recorded": _db.session.get(Node, rec["user_node_id"]),
    }


def _hidden_ids(t):
    return {t["typed"].id, t["circles"].id, t["prompt"].id,
            t["recorded"].id}


def _assert_no_secret(text):
    for secret in SECRETS:
        assert secret not in text


def test_ben_sees_his_replies(app, thread):
    """The setup is what the other tests think it is."""
    page = _client(app, "ben").get(f"/api/nodes/{thread['entry'].id}")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    for secret in SECRETS:
        assert secret in text


def test_the_public_thread_json_leaves_them_out(app, thread):
    anon = _client(app)
    for node in (thread["entry"], thread["glean"], thread["annas"]):
        resp = anon.get(f"/api/commons/node/{node.id}")
        assert resp.status_code == 200
        _assert_no_secret(resp.get_data(as_text=True))

    body = anon.get(f"/api/commons/node/{thread['entry'].id}").get_json()
    children = body["thread"]["children"]
    assert sorted(c["id"] for c in children) == sorted(
        [thread["glean"].id, thread["annas"].id])
    glean = next(c for c in children if c["id"] == thread["glean"].id)
    assert glean["children"] == []

    for nid in _hidden_ids(thread):
        assert anon.get(f"/api/commons/node/{nid}").status_code == 404


def test_the_server_rendered_pages_leave_them_out(app, thread):
    anon = _client(app)
    for path in (f"/node/{thread['entry'].id}",
                 f"/node/{thread['glean'].id}",
                 "/@ben/a-public-piece", "/@ben/a-public-piece.md",
                 "/@ben", "/@ben/feed.xml", "/sitemap.xml"):
        resp = anon.get(path)
        assert resp.status_code == 200, path
        _assert_no_secret(resp.get_data(as_text=True))
    for nid in _hidden_ids(thread):
        resp = anon.get(f"/node/{nid}")
        assert resp.status_code == 404
        _assert_no_secret(resp.get_data(as_text=True))


def test_another_member_sees_neither_them_nor_their_count(app, thread):
    anna = _client(app, "anna")

    page = anna.get(f"/api/nodes/{thread['entry'].id}")
    assert page.status_code == 200
    _assert_no_secret(page.get_data(as_text=True))
    data = page.get_json()
    assert sorted(c["id"] for c in data["children"]) == sorted(
        [thread["glean"].id, thread["annas"].id])
    assert data["child_count"] == 2
    glean = next(c for c in data["children"]
                 if c["id"] == thread["glean"].id)
    assert glean.get("children", []) == []
    assert glean.get("child_count", 0) == 0
    assert glean.get("descendant_count", 0) == 0

    page = anna.get(f"/api/nodes/{thread['glean'].id}")
    assert page.status_code == 200
    _assert_no_secret(page.get_data(as_text=True))
    assert page.get_json()["children"] == []
    assert page.get_json()["child_count"] == 0

    # From her own reply: the piece above counts only what she can read.
    page = anna.get(f"/api/nodes/{thread['annas'].id}")
    assert page.status_code == 200
    _assert_no_secret(page.get_data(as_text=True))
    above = next(a for a in page.get_json()["ancestors"]
                 if a["id"] == thread["entry"].id)
    assert above["child_count"] == 2

    for nid in _hidden_ids(thread):
        resp = anna.get(f"/api/nodes/{nid}")
        assert resp.status_code == 404
        _assert_no_secret(resp.get_data(as_text=True))


def test_another_member_cannot_build_on_them(app, thread):
    anna = _client(app, "anna")
    for node in (thread["typed"], thread["recorded"]):
        resp = anna.post("/api/nodes/", json={
            "content": "a reply", "parent_id": node.id,
            "privacy_level": "public", "ai_usage": "chat"})
        assert resp.status_code in (403, 404)
        assert anna.post(f"/api/nodes/{node.id}/llm",
                         json={}).status_code in (403, 404)


def test_the_listings_and_feeds_leave_them_out(app, thread):
    anna = _client(app, "anna")
    feed = anna.get("/api/commons/feed")
    assert feed.status_code == 200
    _assert_no_secret(feed.get_data(as_text=True))
    item = next(i for i in feed.get_json()["items"]
                if i["id"] == thread["entry"].id)
    assert item["reply_count"] == 2

    listing = _client(app).get("/api/share/public/ben")
    assert listing.status_code == 200
    _assert_no_secret(listing.get_data(as_text=True))
    assert [s["id"] for s in listing.get_json()["shares"]] == [
        thread["entry"].id]


def test_an_ai_reply_of_someone_else_never_reads_them(app, thread):
    """The chain another person's AI reply is built from is the path
    above their node, as they can read it. A public reply of Ben's under
    his own Private one, with Anna's reply under that: her chain stops
    below the Private one, and the piece above it is cut off with it."""
    from backend.tests.test_context_artifact_pinning import _load_node_chain
    anna_id = _user("anna").id

    chain = _load_node_chain(thread["annas"], anna_id)
    assert [n.id for n in chain] == [thread["entry"].id, thread["annas"].id]

    bens_public = _entry("ben", "A public follow-up.", parent=thread["typed"])
    annas_reply = _type(app, "anna", bens_public, "Anna again.", "public",
                        ai_usage="chat")
    assert annas_reply.status_code == 201, annas_reply.get_json()
    annas_node = _db.session.get(Node, annas_reply.get_json()["id"])

    chain = _load_node_chain(annas_node, anna_id)
    assert [n.id for n in chain] == [bens_public.id, annas_node.id]
    for node in chain:
        _assert_no_secret(node.get_content())


# ── With the checklist tick (#795, train #787) ────────────────────────


def test_a_tick_keeps_a_private_reply_private_and_refuses_a_stale_page(app):
    """#798 checks a tick's base_content on PUT /nodes/<id>; this PR lets
    a Private reply sit under one's own public entry. A tick on that
    reply and on the public entry above saves only over the text the page
    showed, and changes no level: the reply stays Private, the entry
    Public, and Anna still gets nothing of the reply."""
    checklist = "- [ ] deploy\n- [ ] write the note"
    entry = _entry("ben", checklist, slug="a-public-list")
    resp = _type(app, "ben", entry, TYPED + "\n" + checklist, "private")
    assert resp.status_code == 201, resp.get_json()
    reply_id = resp.get_json()["id"]
    reply_text = TYPED + "\n" + checklist
    ben = _client(app, "ben")

    ticked = reply_text.replace("- [ ] deploy", "- [x] deploy")
    r = ben.put(f"/api/nodes/{reply_id}",
                json={"content": ticked, "base_content": reply_text})
    assert r.status_code == 200, r.get_json()

    # A second page still shows the text before that tick.
    stale = reply_text.replace("- [ ] write the note", "- [x] write the note")
    r = ben.put(f"/api/nodes/{reply_id}",
                json={"content": stale, "base_content": reply_text})
    assert r.status_code == 409
    assert r.get_json()["code"] == "content_changed"

    on_entry = checklist.replace("- [ ] deploy", "- [x] deploy")
    r = ben.put(f"/api/nodes/{entry.id}",
                json={"content": on_entry, "base_content": checklist})
    assert r.status_code == 200, r.get_json()

    _db.session.expire_all()
    reply = _db.session.get(Node, reply_id)
    above = _db.session.get(Node, entry.id)
    assert (reply.get_content(), reply.privacy_level) == (ticked, "private")
    assert (above.get_content(), above.privacy_level) == (on_entry, "public")
    assert reply.public_slug is None

    anna = _client(app, "anna")
    assert anna.get(f"/api/nodes/{reply_id}").status_code in (403, 404)
    page = anna.get(f"/api/nodes/{entry.id}")
    assert page.status_code == 200
    _assert_no_secret(page.get_data(as_text=True))
    assert anna.put(f"/api/nodes/{reply_id}", json={
        "content": stale, "base_content": ticked}).status_code in (403, 404)
