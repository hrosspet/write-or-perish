"""The user's data download is sent while it is built (#591).

``/export/threads`` streams ``iter_user_export``: the header goes out
before any entry is read, a few threads are loaded at a time, and a
failure part-way ends the response without its end, so the browser marks
the download failed. The text is byte for byte the text the legacy
builder (``build_user_export_content(user, filter_ai_usage=False)``)
makes, which these tests compare on small accounts with every feature
the export renders: branches, deleted entries and threads, other
people's replies, pinned artifact versions, quotes, imported tweets.

In-memory SQLite, ENCRYPTION_DISABLED; nothing calls an outside service.
"""
import os
import re
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

os.environ["ENCRYPTION_DISABLED"] = "true"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TWITTER_API_KEY", "fake")
os.environ.setdefault("TWITTER_API_SECRET", "fake")

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())
sys.modules.setdefault("celery.result", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402
from sqlalchemy import event  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    Node, NodeContextArtifact, User, UserArtifact, UserProfile, UserPrompt,
    UserTodo,
)
import backend.models as _real_backend_models  # noqa: E402

T0 = datetime(2026, 3, 1, 9, 0, 0)
EXPORT_DATE = re.compile(r"\*\*Export Date:\*\* [^\n]*")


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)

    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.export_data import export_bp
    app.register_blueprint(export_bp, url_prefix="/api")
    return app


@pytest.fixture
def app():
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[_k]

    app = _make_app()
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    current = [k for k in list(sys.modules) if _affected(k)]
    for k in current:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod
    # Restore the package attributes the fresh imports re-pointed (see
    # test_export_subtree_access.py).
    for k in set(current) | set(saved):
        pkg_name, _, attr = k.rpartition(".")
        pkg = sys.modules.get(pkg_name)
        if pkg is None or not attr:
            continue
        if k in saved:
            setattr(pkg, attr, saved[k])
        elif hasattr(pkg, attr):
            delattr(pkg, attr)


@pytest.fixture
def ex(app):
    """The export module the app's route uses."""
    from backend.routes import export_data
    return export_data


def _user(name):
    u = User(username=name, twitter_id=f"{name}-twitter-id")
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def alice(app):
    return _user("alice")


@pytest.fixture
def bob(app):
    return _user("bob")


_clock = {"n": 0}


def _node(user, content, parent=None, privacy="private", deleted=False,
          node_type="user", origin=None, owner=None, prompt_key=None):
    _clock["n"] += 1
    node = Node(
        user_id=user.id,
        human_owner_id=(owner or user).id,
        parent_id=parent.id if parent else None,
        node_type=node_type,
        llm_model="claude-x" if node_type == "llm" else None,
        privacy_level=privacy, ai_usage="chat", token_count=5,
        origin=origin, prompt_key=prompt_key,
        created_at=T0 + timedelta(minutes=_clock["n"]),
    )
    node.set_content(content)
    if deleted:
        node.deleted_at = T0 + timedelta(days=30)
    _db.session.add(node)
    _db.session.commit()
    return node


def _pin(node, artifact_type, row):
    _db.session.add(NodeContextArtifact(
        node_id=node.id, artifact_type=artifact_type, artifact_id=row.id))
    _db.session.commit()


def _version(model, user, **kw):
    _clock["n"] += 1
    row = model(user_id=user.id, generated_by="test",
                created_at=T0 + timedelta(minutes=_clock["n"]), **kw)
    row.content = kw.get("content") or f"{model.__name__} text {_clock['n']}"
    _db.session.add(row)
    _db.session.commit()
    return row


def _session(user, llm, prompt, pins, turns=4):
    """An agentic session: its system entry pins *prompt* and *pins*;
    turns alternate the user's entries and AI replies."""
    root = _node(user, "", prompt_key=prompt.prompt_key)
    _pin(root, "prompt", prompt)
    for artifact_type, row in pins:
        _pin(root, artifact_type, row)
    parent = root
    for t in range(turns):
        parent = (_node(llm, f"reply {t} of {root.id}", parent=parent,
                        node_type="llm", owner=user) if t % 2
                  else _node(user, f"turn {t} of {root.id}", parent=parent))
    return root


def _rich_account(alice, bob):
    """Every shape the full export renders, in one account."""
    llm = _user("claude-x")
    voice = [_version(UserPrompt, alice, prompt_key="voice",
                      title="Voice prompt", content=f"voice v{i}")
             for i in range(2)]
    profiles = [_version(UserProfile, alice, content=f"profile v{i}")
                for i in range(3)]
    todos = [_version(UserTodo, alice, content=f"todo v{i}")
             for i in range(2)]
    memory = [_version(UserArtifact, alice, kind="memory", title="Memory",
                       content=f"memory v{i}") for i in range(2)]
    gone = _version(UserArtifact, alice, kind="scratchpad",
                    title="Scratchpad", content="deleted scratchpad")
    gone.deleted_at = T0 + timedelta(days=2)
    bobs_profile = _version(UserProfile, bob, content="BOBS PROFILE")

    # Flat imported tweets: a run, broken by a thread, then another run.
    for i in range(3):
        _node(alice, f"tweet {i}", origin="twitter")
    # A branching thread with a quote and a deleted entry in the middle.
    root = _node(alice, "branching root")
    a = _node(alice, "branch a", parent=root)
    _node(llm, "reply under a", parent=a, node_type="llm", owner=alice)
    b = _node(alice, "branch b", parent=root, deleted=True)
    _node(alice, f"under deleted b, quoting {{quote:{a.id}}}", parent=b)
    _node(bob, "BOB PUBLIC REPLY", parent=root, privacy="public")
    _node(bob, "BOB PRIVATE REPLY", parent=root)
    for i in range(2):
        _node(alice, f"late tweet {i}", origin="twitter")
    # A tweet thread (a tweet with a self-reply is not flat).
    tw = _node(alice, "tweet with a reply", origin="twitter")
    _node(alice, "self reply", parent=tw, origin="twitter")
    # A fully deleted thread (left out) and a deleted root with a live
    # reply (kept, as a placeholder).
    gone_root = _node(alice, "GONE ROOT", deleted=True)
    _node(alice, "GONE REPLY", parent=gone_root, deleted=True)
    dead_root = _node(alice, "DEAD ROOT", deleted=True)
    _node(alice, "alive under a deleted root", parent=dead_root)
    # Sessions pinning artifact versions, also a deleted one, and a
    # reply by bob pinning his own profile (left out of the preamble).
    _session(alice, llm, voice[0], [("profile", profiles[0]),
                                    ("todo", todos[0]),
                                    ("user_artifact", memory[0])])
    s2 = _session(alice, llm, voice[1], [("profile", profiles[2]),
                                         ("todo", todos[1]),
                                         ("user_artifact", memory[1]),
                                         ("user_artifact", gone)])
    bob_reply = _node(bob, "bob in alice's session", parent=s2,
                      privacy="public")
    _pin(bob_reply, "profile", bobs_profile)
    # A session in a deleted thread pins a version nothing else pins.
    hidden = _session(alice, llm, voice[0], [("profile", profiles[1])])
    for n in Node.query.filter(Node.id >= hidden.id).all():
        n.deleted_at = T0 + timedelta(days=40)
    _db.session.commit()
    # A last run of tweets ends the export.
    for i in range(2):
        _node(alice, f"final tweet {i}", origin="twitter")


def _legacy(ex, user):
    """The text the download had before #591."""
    content = ex.build_user_export_content(user, filter_ai_usage=False)
    return content if content else ex.NO_THREADS_TEXT


def _streamed(ex, user):
    return "".join(ex.iter_user_export(user))


def _same(a, b):
    return EXPORT_DATE.sub("", a) == EXPORT_DATE.sub("", b)


def _client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True
    return client


# ── the text is the legacy text ────────────────────────────────────────

@pytest.mark.parametrize("batch", [1, 2, 3, 100])
def test_streamed_text_is_the_legacy_text(app, ex, alice, bob, monkeypatch,
                                          batch):
    """Byte for byte, also with batches that split tweet runs and
    threads apart, a key window smaller than a thread, and a piece sent
    after every entry."""
    _rich_account(alice, bob)
    monkeypatch.setattr(ex, "EXPORT_STREAM_BATCH", batch)
    monkeypatch.setattr(ex, "EXPORT_DEK_WINDOW", 2)
    monkeypatch.setattr(ex, "EXPORT_FLUSH_CHARS", 1)

    legacy = _legacy(ex, alice)
    streamed = _streamed(ex, alice)
    assert _same(streamed, legacy)
    # The fixture renders what it is meant to.
    assert "# Tweets by alice (imported from Twitter/X) — 3 tweets" in legacy
    assert "# Tweets by alice (imported from Twitter/X) — 2 tweets" in legacy
    # #592's separator names the entry the branch replies to.
    assert "**BRANCH: another reply to " in legacy
    assert "[Node deleted by author]" in legacy
    assert "[Reply not accessible]" in legacy
    assert "BOB PUBLIC REPLY" in legacy
    assert "BOB PRIVATE REPLY" not in legacy
    assert "GONE ROOT" not in legacy
    assert "## System Prompts Referenced" in legacy
    assert "profile v0" in legacy and "profile v2" in legacy
    assert "profile v1" not in legacy      # pinned only in a deleted thread
    assert "deleted scratchpad" not in legacy
    assert "BOBS PROFILE" not in legacy
    assert "branch a" in legacy.split("quoting")[1]   # the quote resolved
    assert legacy.endswith("*End of Export*")


def test_download_is_the_legacy_text(app, ex, alice, bob, monkeypatch):
    """Through the route: same text, sent in more than one piece, under
    the file name the web app used to give it."""
    _rich_account(alice, bob)
    monkeypatch.setattr(ex, "EXPORT_STREAM_BATCH", 2)
    monkeypatch.setattr(ex, "EXPORT_FLUSH_CHARS", 200)
    legacy = _legacy(ex, alice)

    resp = _client(app, alice).get("/api/export/threads", buffered=False)
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    day = datetime.utcnow().strftime("%Y-%m-%d")
    assert resp.headers["Content-Disposition"] == (
        f'attachment; filename="loore-export-{day}.txt"')
    assert resp.headers["X-Accel-Buffering"] == "no"
    assert "Content-Length" not in resp.headers
    pieces = [p.decode() if isinstance(p, bytes) else p
              for p in resp.response]
    resp.close()
    assert len(pieces) > 3
    assert _same("".join(pieces), legacy)


def test_no_threads(app, ex, alice):
    assert _streamed(ex, alice) == "No threads found to export."
    gone = _node(alice, "only a deleted thread", deleted=True)
    _node(alice, "and its deleted reply", parent=gone, deleted=True)
    assert _legacy(ex, alice) == "No threads found to export."
    assert _streamed(ex, alice) == "No threads found to export."


def test_only_tweets(app, ex, alice, monkeypatch):
    monkeypatch.setattr(ex, "EXPORT_STREAM_BATCH", 2)
    for i in range(5):
        _node(alice, f"tweet {i}", origin="twitter")
    assert _same(_streamed(ex, alice), _legacy(ex, alice))


# ── sent while it is built ─────────────────────────────────────────────

def test_header_goes_out_before_any_entry_is_read(app, ex, alice, bob,
                                                  monkeypatch):
    _rich_account(alice, bob)
    rendered = []
    real = ex._format_node_text

    def counting(node, *a, **kw):
        rendered.append(node.id)
        return real(node, *a, **kw)
    monkeypatch.setattr(ex, "_format_node_text", counting)

    pieces = ex.iter_user_export(alice)
    first = next(pieces)
    assert first.startswith("# Loore - Thread Export\n")
    assert "**Total Threads:**" in first
    assert rendered == []
    rest = "".join(pieces)
    assert rendered and rest.endswith("*End of Export*")


def test_a_failure_part_way_ends_the_download_without_its_end(
        app, ex, alice, monkeypatch):
    """The response is cut, never ended: the app server then closes the
    connection without the response's last chunk, and the browser marks
    the download failed (rule: problems show)."""
    for i in range(4):
        root = _node(alice, f"thread {i}")
        _node(alice, f"reply {i}", parent=root)
    monkeypatch.setattr(ex, "EXPORT_FLUSH_CHARS", 1)
    real = ex.iter_node_tree
    calls = []

    def failing(*a, **kw):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("database went away")
        return real(*a, **kw)
    monkeypatch.setattr(ex, "iter_node_tree", failing)

    resp = _client(app, alice).get("/api/export/threads", buffered=False)
    assert resp.status_code == 200
    received = []
    with pytest.raises(RuntimeError, match="database went away"):
        for piece in resp.response:
            received.append(piece.decode() if isinstance(piece, bytes)
                            else piece)
    text = "".join(received)
    assert "thread 1" in text            # sent before the failure
    assert "thread 3" not in text
    assert "*End of Export*" not in text


def test_head_builds_nothing(app, ex, alice, monkeypatch):
    """The web app checks with HEAD that the download can start."""
    _node(alice, "a thread")
    monkeypatch.setattr(ex, "iter_user_export", MagicMock(
        side_effect=AssertionError("built for a HEAD request")))
    resp = _client(app, alice).head("/api/export/threads")
    assert resp.status_code == 200
    assert resp.get_data() == b""
    # Signed out (the fixture's app context keeps the last signed-in user
    # in g between requests).
    from flask import g
    g.pop("_login_user", None)
    assert app.test_client().head(
        "/api/export/threads").status_code in (302, 401)


# ── the batched alive check ────────────────────────────────────────────

def test_alive_check_is_one_query_per_batch(app, ex, alice):
    live = _node(alice, "live root")
    deep = _node(alice, "deleted root", deleted=True)
    mid = _node(alice, "deleted middle", parent=deep, deleted=True)
    _node(alice, "live grandchild", parent=mid)
    gone = _node(alice, "deleted thread", deleted=True)
    _node(alice, "deleted reply", parent=gone, deleted=True)
    lone = _node(alice, "deleted lone root", deleted=True)
    roots = [live.id, deep.id, gone.id, lone.id]

    statements = []

    def count(conn, cursor, statement, *a):
        statements.append(statement)
    event.listen(_db.engine, "before_cursor_execute", count)
    try:
        alive = ex._threads_with_alive_node(roots, batch=3)
    finally:
        event.remove(_db.engine, "before_cursor_execute", count)

    assert alive == {live.id, deep.id}
    assert len(statements) == 2          # 4 threads, 3 per query
    assert alive == {r for r in roots if ex._thread_has_alive_node(r)}
    assert ex._threads_with_alive_node([]) == set()


def test_full_export_checks_only_threads_with_a_deleted_root(
        app, ex, alice, monkeypatch):
    for i in range(3):
        _node(alice, f"live {i}")
    dead = _node(alice, "deleted", deleted=True)
    _node(alice, "live reply", parent=dead)
    asked = []
    real = ex._threads_with_alive_node

    def recording(root_ids, *a, **kw):
        root_ids = list(root_ids)
        asked.append(root_ids)
        return real(root_ids, *a, **kw)
    monkeypatch.setattr(ex, "_threads_with_alive_node", recording)
    assert _same(_streamed(ex, alice), _legacy(ex, alice))
    assert asked == [[dead.id], [dead.id]]


# ── data keys are unwrapped a window at a time ─────────────────────────

def test_keys_are_unwrapped_a_window_ahead(app, ex, alice, monkeypatch):
    """A cold key costs a KMS call: the download unwraps the keys of the
    next window of entries in one batch, also inside one thread larger
    than the window, so no wait is longer than a window's."""
    root = _node(alice, "e0")
    parent = root
    for i in range(1, 7):
        parent = _node(alice, f"e{i}", parent=parent)
    _node(alice, "second thread")

    cached, batches = set(), []
    monkeypatch.setattr(ex, "dek_cached", lambda c: c in cached)

    def prefetch(contents):
        contents = list(contents)
        batches.append(contents)
        cached.update(contents)
    monkeypatch.setattr(ex, "prefetch_deks", prefetch)
    monkeypatch.setattr(ex, "EXPORT_DEK_WINDOW", 3)

    text = _streamed(ex, alice)
    assert batches == [["e0", "e1", "e2"], ["e3", "e4", "e5"],
                       ["e6", "second thread"]]
    assert "e6" in text


# ── access in the preamble ─────────────────────────────────────────────

def test_preamble_leaves_out_pins_on_entries_the_user_cannot_access(
        app, ex, alice, bob):
    """The preamble query keeps only entries the user can access: a
    profile pinned only on the user's deleted session entry (in a thread
    kept for its live replies), and the prompt text pinned on another
    user's private reply in the user's public thread, stay out of the
    file (from the review of #629)."""
    llm = _user("claude-x")
    prompt = _version(UserPrompt, alice, prompt_key="voice",
                      title="Voice prompt", content="ALICE PROMPT")
    profile = _version(UserProfile, alice, content="PROFILE ON DELETED ROOT")
    bobs_prompt = _version(UserPrompt, bob, prompt_key="voice",
                           title="Bob prompt", content="BOB PRIVATE PROMPT")
    root = _session(alice, llm, prompt, [("profile", profile)])
    root.deleted_at = T0 + timedelta(days=3)
    _db.session.commit()
    pub = _node(alice, "public root", privacy="public")
    _pin(_node(bob, "bob private", parent=pub), "prompt", bobs_prompt)
    legacy = _legacy(ex, alice)
    assert "PROFILE ON DELETED ROOT" not in legacy
    assert "BOB PRIVATE PROMPT" not in legacy
    assert _same(_streamed(ex, alice), legacy)


# ── other requests on the worker get a turn ────────────────────────────

def test_export_pauses_with_a_real_sleep(app, ex, alice, monkeypatch):
    """Under the gevent worker a 0 s sleep does not let the loop poll
    sockets, so the export's pause must sleep longer than 0."""
    for i in range(3):
        root = _node(alice, f"thread {i}")
        _node(alice, f"reply {i}", parent=root)
    sleeps = []
    monkeypatch.setattr(ex.time, "sleep", sleeps.append)
    monkeypatch.setattr(ex, "EXPORT_PAUSE_EVERY_SECONDS", 0)
    text = _streamed(ex, alice)
    assert text.endswith("*End of Export*")
    assert len(sleeps) > 6
    assert all(s == ex.EXPORT_PAUSE_SECONDS > 0 for s in sleeps)
