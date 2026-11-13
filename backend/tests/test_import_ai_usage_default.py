"""An import's AI usage follows the account's setting (#593).

Peter, 2026-11-10: the import dialog starts at the account's AI usage
setting ("it should simply follow the account's setting"), and a
re-import applies the value the dialog shows to the entries already in
Loore, picked or not ("if chat is selected (even though by default),
chat is used on all entries"). The dialogs send the value they show; a
request without one gets the account's setting too, and a request that
names a value (the iPhone app before its next build sends "none") keeps
it.
"""

import json
import os
import sys
import types
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
sys.modules.setdefault("ffmpeg", MagicMock())

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import User, Node            # noqa: E402
import backend.models as _real_backend_models    # noqa: E402


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

    from backend.routes.import_data import import_bp
    app.register_blueprint(import_bp, url_prefix="/api")
    return app


@pytest.fixture
def app(tmp_path, monkeypatch):
    from backend.utils import twitter_archive
    monkeypatch.setattr(twitter_archive, "STASH_ROOT", tmp_path / "imports")
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

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def hand_offs(app, monkeypatch):
    """The profile hand-offs an import started: (user id, imported tokens).
    The markdown path and the ChatGPT/Claude job both end in one of these;
    the hand-off itself is tested with the importers."""
    from backend.routes import import_data
    calls = []

    def markdown_hand_off(user_obj, earliest_ts, tokens):
        calls.append((user_obj.id, tokens))
        return "profile-task"

    def job_hand_off(user_id, earliest_ts, tokens):
        calls.append((user_id, tokens))
        return "profile-task"

    monkeypatch.setattr(import_data, "_hand_off_profile_update_after_import",
                        markdown_hand_off)
    monkeypatch.setattr(import_data, "_maybe_update_profile_after_import",
                        job_hand_off)
    return calls


def _client_for(app, default_ai_usage):
    u = User(username="alice", approved=True, plan="alpha",
             default_ai_usage=default_ai_usage)
    _db.session.add(u)
    _db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
    return client, u.id


FILES = [
    {"filename_without_ext": "a", "content": "file a content",
     "modified_at": "2024-01-01T12:00:00"},
    {"filename_without_ext": "b", "content": "file b content",
     "modified_at": "2024-01-02T12:00:00"},
]


def _confirm_markdown(client, **extra):
    body = {"files": FILES, "import_type": "separate_nodes",
            "privacy_level": "private"}
    body.update(extra)
    return client.post("/api/import/confirm", json=body)


def _usages(user_id):
    return sorted(n.ai_usage for n in
                  Node.query.filter_by(human_owner_id=user_id).all())


# ── Markdown, Word and PDF (/import/confirm) ─────────────────────────────

@pytest.mark.parametrize("setting", ["none", "chat", "train"])
def test_import_without_a_value_takes_the_account_setting(
        app, hand_offs, setting):
    client, uid = _client_for(app, setting)
    resp = _confirm_markdown(client)
    assert resp.status_code == 201, resp.get_json()
    assert _usages(uid) == [setting, setting]
    if setting == "none":
        # No AI means no AI: nothing is handed to a profile build.
        assert hand_offs == []
        assert resp.get_json()["profile_update_task_id"] is None
    else:
        assert [u for u, _ in hand_offs] == [uid]
        assert resp.get_json()["profile_update_task_id"] == "profile-task"


def test_an_untouched_dialog_sends_the_account_setting_and_starts_the_profile(
        app, hand_offs):
    client, uid = _client_for(app, "chat")
    resp = _confirm_markdown(client, ai_usage="chat")
    assert resp.status_code == 201
    assert _usages(uid) == ["chat", "chat"]
    assert [u for u, _ in hand_offs] == [uid]


def test_a_named_value_wins_over_the_account_setting(app, hand_offs):
    # The iPhone app before its next build sends "none" explicitly.
    client, uid = _client_for(app, "chat")
    resp = _confirm_markdown(client, ai_usage="none")
    assert resp.status_code == 201
    assert _usages(uid) == ["none", "none"]
    assert hand_offs == []


@pytest.mark.parametrize("named", [True, False])
def test_reimport_applies_the_shown_value_to_entries_already_in_loore(
        app, hand_offs, named):
    client, uid = _client_for(app, "chat")
    # Imported before #593, when the dialog started at None.
    first = _confirm_markdown(client, ai_usage="none").get_json()
    assert first["created"] == 2
    assert _usages(uid) == ["none", "none"]

    # The dialog now opens at Chat; the user confirms without touching it.
    again = _confirm_markdown(client, **({"ai_usage": "chat"} if named else {}))
    body = again.get_json()
    assert body["created"] == 0
    assert body["updated"] == 2
    assert body["skipped"] == 0
    assert _usages(uid) == ["chat", "chat"]


# ── ChatGPT and Claude (background job) ──────────────────────────────────

@pytest.fixture
def run_jobs_inline(app, monkeypatch):
    from backend.routes import import_data
    app.import_results = []

    def run_inline(user_id, token, task_id, options):
        app.import_results.append(import_data.run_conversation_import(
            user_id, token, task_id, options))

    monkeypatch.setattr(import_data, "_dispatch_conversation_import",
                        run_inline)
    return app.import_results


def _conversation():
    """A conversation as the analyze step stashes it."""
    messages = [
        {"text": "hello world", "role": "user",
         "created_at": "2023-11-14T22:13:20", "model": "", "mapping_id": "u1"},
        {"text": "hi there, friend", "role": "assistant",
         "created_at": "2023-11-14T22:13:21", "model": "gpt-4o",
         "mapping_id": "a1"},
    ]
    return {"name": "First chat", "created_at": "2023-11-14T22:13:20",
            "default_model": "gpt-4o", "messages": messages,
            "message_count": 2, "token_count": 7}


def _confirm_conversations(client, user_id, source, **extra):
    from backend.routes.import_data import _stash_conversations
    token = _stash_conversations(user_id, source, [_conversation()], 2)
    body = {"import_token": token, "privacy_level": "private"}
    body.update(extra)
    return client.post(f"/api/import/{source}/confirm",
                       data=json.dumps(body),
                       content_type="application/json")


@pytest.mark.parametrize("source", ["chatgpt", "claude"])
def test_conversation_import_without_a_value_takes_the_account_setting(
        app, hand_offs, run_jobs_inline, source):
    client, uid = _client_for(app, "train")
    resp = _confirm_conversations(client, uid, source)
    assert resp.status_code == 202, resp.get_json()
    [result] = run_jobs_inline
    assert result["created"] == 2
    assert _usages(uid) == ["train", "train"]
    assert [u for u, _ in hand_offs] == [uid]


def test_conversation_reimport_applies_the_shown_value(app, hand_offs,
                                                       run_jobs_inline):
    client, uid = _client_for(app, "chat")
    _confirm_conversations(client, uid, "chatgpt", ai_usage="none")
    assert _usages(uid) == ["none", "none"]
    _confirm_conversations(client, uid, "chatgpt", ai_usage="chat")
    assert run_jobs_inline[-1]["updated"] == 2
    assert _usages(uid) == ["chat", "chat"]


# ── Twitter (the task gets the resolved value) ───────────────────────────

def _fake_twitter_task(monkeypatch):
    mod = types.ModuleType("backend.tasks.imports")
    task = MagicMock()
    task.delay.return_value = MagicMock(id="task-123")
    mod.import_twitter_archive = task
    monkeypatch.setitem(sys.modules, "backend.tasks.imports", mod)
    return task


@pytest.mark.parametrize("extra,expected", [
    ({}, "train"),                     # no value: the account's setting
    ({"ai_usage": "none"}, "none"),    # a named value is kept
])
def test_twitter_task_gets_the_resolved_value(app, monkeypatch, extra,
                                              expected):
    from backend.utils import twitter_archive as ta
    client, uid = _client_for(app, "train")
    task = _fake_twitter_task(monkeypatch)
    token = ta.stash_write(uid, [])
    resp = client.post("/api/import/twitter/confirm",
                       json={"import_token": token, **extra})
    assert resp.status_code == 202, resp.get_json()
    assert task.delay.call_args[0][2]["ai_usage"] == expected


# ── Imports are never public (#677) ──────────────────────────────────────
# Controls (private and circles imports as before) are marked as such; the
# refusal tests fail on main, where Public was accepted.

REFUSAL = "Imported entries can't be public. Import them, then make public the ones you choose."


def _privacies(user_id):
    return sorted(n.privacy_level for n in
                  Node.query.filter_by(human_owner_id=user_id).all())


@pytest.mark.parametrize("level", ["public", "bogus", ["public"]])
def test_markdown_import_refuses_public_and_unknown_and_writes_nothing(
        app, hand_offs, level):
    client, uid = _client_for(app, "chat")
    resp = _confirm_markdown(client, privacy_level=level)
    assert resp.status_code == 400
    if level == "public":
        assert resp.get_json()["error"] == REFUSAL
    assert _privacies(uid) == []
    assert hand_offs == []


@pytest.mark.parametrize("level", ["private", "circles"])
def test_markdown_import_control_private_and_circles_unchanged(
        app, hand_offs, level):
    client, uid = _client_for(app, "chat")
    resp = _confirm_markdown(client, privacy_level=level)
    assert resp.status_code == 201
    assert _privacies(uid) == [level, level]


def test_markdown_import_control_without_a_level_is_private(app, hand_offs):
    client, uid = _client_for(app, "chat")
    body = {"files": FILES, "import_type": "separate_nodes"}
    assert client.post("/api/import/confirm", json=body).status_code == 201
    assert _privacies(uid) == ["private", "private"]


@pytest.mark.parametrize("source", ["chatgpt", "claude"])
def test_conversation_import_refuses_public_and_starts_no_job(
        app, hand_offs, run_jobs_inline, source):
    client, uid = _client_for(app, "chat")
    resp = _confirm_conversations(client, uid, source,
                                  privacy_level="public")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == REFUSAL
    assert run_jobs_inline == []
    assert _privacies(uid) == []
    resp = _confirm_conversations(client, uid, source, privacy_level="x")
    assert resp.status_code == 400 and run_jobs_inline == []


@pytest.mark.parametrize("source", ["chatgpt", "claude"])
def test_conversation_import_control_circles_unchanged(
        app, hand_offs, run_jobs_inline, source):
    client, uid = _client_for(app, "chat")
    resp = _confirm_conversations(client, uid, source,
                                  privacy_level="circles")
    assert resp.status_code == 202
    assert _privacies(uid) == ["circles", "circles"]


def test_reimport_cannot_make_imported_entries_public(
        app, hand_offs, run_jobs_inline):
    client, uid = _client_for(app, "chat")
    _confirm_conversations(client, uid, "chatgpt")
    assert _privacies(uid) == ["private", "private"]
    resp = _confirm_conversations(client, uid, "chatgpt",
                                  privacy_level="public")
    assert resp.status_code == 400
    assert _privacies(uid) == ["private", "private"]
    # Control: a re-import can still set Circles on the skipped duplicates.
    _confirm_conversations(client, uid, "chatgpt", privacy_level="circles")
    assert _privacies(uid) == ["circles", "circles"]


def test_control_reimport_lowers_an_entry_made_public_by_hand(
        app, hand_offs):
    # Existing behavior, kept (PR "Needs Peter"): a re-import applies its
    # privacy to duplicates, so a private re-import lowers an entry the
    # user published by hand afterwards.
    client, uid = _client_for(app, "chat")
    _confirm_markdown(client)
    node = Node.query.filter_by(human_owner_id=uid).first()
    node.set_privacy_level("public")
    _db.session.commit()
    _confirm_markdown(client)
    assert _privacies(uid) == ["private", "private"]


def test_apply_settings_to_skipped_refuses_public_and_changes_nothing(
        app, hand_offs):
    from backend.routes.import_data import _apply_settings_to_skipped
    client, uid = _client_for(app, "chat")
    _confirm_markdown(client)
    ids = [n.id for n in Node.query.filter_by(human_owner_id=uid)]
    with pytest.raises(ValueError):
        _apply_settings_to_skipped(ids, "public", "chat")
    assert _privacies(uid) == ["private", "private"]
    # Control: Circles applies.
    assert _apply_settings_to_skipped(ids, "circles", "chat") == 2
    assert _privacies(uid) == ["circles", "circles"]


def test_twitter_confirm_refuses_public_and_queues_nothing(app, monkeypatch):
    from backend.utils import twitter_archive as ta
    client, uid = _client_for(app, "chat")
    task = _fake_twitter_task(monkeypatch)
    token = ta.stash_write(uid, [])
    resp = client.post("/api/import/twitter/confirm",
                       json={"import_token": token,
                             "privacy_level": "public"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == REFUSAL
    task.delay.assert_not_called()
    # Control: Circles is queued as before.
    resp = client.post("/api/import/twitter/confirm",
                       json={"import_token": token,
                             "privacy_level": "circles"})
    assert resp.status_code == 202
    assert task.delay.call_args[0][2]["privacy_level"] == "circles"
