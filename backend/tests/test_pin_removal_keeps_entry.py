"""Editing an entry so that a personal placeholder disappears keeps the
entry (#786), also with the edit form's base_content (#799).

An entry carries pins (NodeContextArtifact): one row per personal
document its text names ({user_profile}, {user_todo}, {user_intentions}
and so on), plus the prompt version a session's first entry resolves
from. An edit that drops a placeholder deletes that pin row
(sync_context_artifacts). The pin's relationship to its node carried
cascade="all,delete-orphan", so deleting the pin deleted the NODE: on
Postgres the save answered 500 and nothing was saved; on sqlite the
entry itself was deleted. The cascade is on the node's side now:
deleting a node deletes its pins, and deleting a pin deletes nothing
else (not the node, its replies, its edit history, the prompt version or
the document version the pin pointed at). An edit removes only the pins
of placeholders the old text had and the new text lacks, so the versions
an AI reply's turn read with a tool stay pinned on the reply.

Through the real PUT /nodes/<id> route and the 30-day clean-up's purge,
on sqlite and, when LOORE_TEST_POSTGRES_URL names a throwaway database
(it creates and drops every table in it), on Postgres too, as
test_artifact_name_cleanup.py.
"""
import os
import sys
import warnings
from datetime import datetime, timedelta
from types import SimpleNamespace
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
from flask import Flask, g  # noqa: E402
from sqlalchemy.exc import SAWarning  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login  # noqa: E402
from backend.extensions import db as _db  # noqa: E402
from backend.models import (  # noqa: E402
    Node, NodeContextArtifact, NodeVersion, User, UserArtifact,
    UserProfile, UserPrompt, UserTodo,
)
import backend.models as _real_backend_models  # noqa: E402

PG_URL = os.environ.get("LOORE_TEST_POSTGRES_URL")
DB_URLS = ["sqlite:///:memory:"] + ([PG_URL] if PG_URL else [])

PROMPT_TEXT = ("You are Loore.\n{user_profile}\n{user_intentions}\n"
               "{user_todo}")
ENTRY_TEXT = "Notes for today. {user_profile} {user_todo} {user_intentions}"
# The placeholder an edit drops -> the pin type that goes with it.
DROPPED = {
    "user_profile": "profile",
    "user_todo": "todo",
    "user_intentions": "user_artifact",
}


def _make_app(db_uri):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_uri
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return _db.session.get(User, int(user_id))

    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
    return app


@pytest.fixture(params=DB_URLS, ids=lambda u: u.split(":", 1)[0])
def app(request):
    # Other test files may leave flask_login swapped for a MagicMock,
    # which turns every edit into a 403 through backend.utils.privacy's
    # current_user (as test_detached_prompt_stays_agentic.py).
    _affected = lambda k: (  # noqa: E731
        k == "flask_login"
        or k.startswith("backend.routes")
        or k == "backend.models"
        or k == "backend.utils.privacy"
    )
    saved = {k: sys.modules[k] for k in list(sys.modules) if _affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for _k in [k for k in list(sys.modules)
               if k.startswith("backend.routes")
               or k == "backend.utils.privacy"]:
        del sys.modules[_k]

    app = _make_app(request.param)
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


def _client(app, user):
    g.pop("_login_user", None)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return client


def _node(user, content, parent=None):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                privacy_level="private", ai_usage="chat", token_count=1,
                parent_id=parent.id if parent else None)
    node.set_content(content)
    _db.session.add(node)
    _db.session.flush()
    return node


@pytest.fixture
def world(app):
    """Alice with every personal document a prompt names, and a prompt
    version that names three of them."""
    alice = User(username="alice", approved=True)
    _db.session.add(alice)
    _db.session.flush()
    profile = UserProfile(user_id=alice.id, generated_by="test",
                          ai_usage="chat")
    profile.set_content("Alice writes in the mornings.")
    todo = UserTodo(user_id=alice.id, generated_by="test", ai_usage="chat")
    todo.set_content("- [ ] finish the chapter")
    intentions = UserArtifact(user_id=alice.id, kind="intentions",
                              title="Intentions", generated_by="test",
                              ai_usage="chat")
    intentions.set_content("Write every day.")
    memory = UserArtifact(user_id=alice.id, kind="memory", title="Memory",
                          generated_by="test", ai_usage="chat")
    memory.set_content("Prefers short replies.")
    prompt = UserPrompt(user_id=alice.id, prompt_key="textmode",
                        title="Text mode", generated_by="default")
    prompt.set_content(PROMPT_TEXT)
    _db.session.add_all([profile, todo, intentions, memory, prompt])
    _db.session.commit()
    return SimpleNamespace(alice=alice, profile=profile, todo=todo,
                           intentions=intentions, memory=memory,
                           prompt=prompt)


def _prompt_entry(world):
    """A session's first entry, pinned as a Text-mode session pins it,
    with a reply and a reply to that."""
    from backend.utils.context_artifacts import attach_context_artifacts
    root = _node(world.alice, "")
    attach_context_artifacts(root.id, world.alice.id,
                             prompt_record=world.prompt)
    reply = _node(world.alice, "A reply.", parent=root)
    answer = _node(world.alice, "An answer to the reply.", parent=reply)
    _db.session.commit()
    return root, reply, answer


def _plain_entry(world, parent=None):
    """An entry whose own text names three documents."""
    from backend.utils.context_artifacts import sync_context_artifacts
    entry = _node(world.alice, ENTRY_TEXT, parent=parent)
    sync_context_artifacts(entry.id, world.alice.id, ENTRY_TEXT)
    reply = _node(world.alice, "A reply.", parent=entry)
    _db.session.commit()
    return entry, reply


def _pins(node_id):
    return {(p.artifact_type, p.artifact_id): p.id
            for p in NodeContextArtifact.query.filter_by(node_id=node_id)}


def _documents_kept(world):
    """No pin deletion touched a prompt or document version."""
    _db.session.expire_all()
    assert _db.session.get(UserPrompt, world.prompt.id) is not None
    assert _db.session.get(UserProfile, world.profile.id) is not None
    assert _db.session.get(UserTodo, world.todo.id) is not None
    assert _db.session.get(UserArtifact, world.intentions.id) is not None
    assert _db.session.get(UserArtifact, world.memory.id) is not None


def _unchanged(node_id, parent_id, text):
    node = _db.session.get(Node, node_id)
    assert node is not None
    assert node.parent_id == parent_id
    assert node.deleted_at is None
    assert node.get_content() == text


# ── The edit (the issue's case) ─────────────────────────────────────────


def test_prompt_entry_edit_that_drops_the_profile_keeps_the_entry(
        app, world):
    """The person edits a session's first entry in the thread and takes
    {user_profile} out. The edit is saved; the profile pin and the prompt
    reference go; the other pins stay as they were; the replies, the
    prompt version and the documents are untouched."""
    root, reply, answer = _prompt_entry(world)
    root_id, reply_id, answer_id = root.id, reply.id, answer.id
    before = _pins(root_id)
    assert set(before) == {
        ("prompt", world.prompt.id), ("profile", world.profile.id),
        ("todo", world.todo.id), ("user_artifact", world.intentions.id),
        ("user_artifact", world.memory.id)}
    new_text = "You are Loore, but terser.\n{user_intentions}\n{user_todo}"

    resp = _client(app, world.alice).put(f"/nodes/{root_id}", json={
        "content": new_text, "privacy_level": "private", "ai_usage": "chat",
        "detach_prompt": True,
    })

    assert resp.status_code == 200, resp.get_json()
    _db.session.expire_all()
    root = _db.session.get(Node, root_id)
    assert root is not None
    assert root.get_content() == new_text
    assert root.prompt_key == "textmode"
    after = _pins(root_id)
    kept = {k: v for k, v in before.items()
            if k[0] not in ("prompt", "profile")}
    assert after == kept                     # same rows, not re-made
    versions = NodeVersion.query.filter_by(node_id=root_id).all()
    assert [v.get_content() for v in versions] == [PROMPT_TEXT]
    _unchanged(reply_id, root_id, "A reply.")
    _unchanged(answer_id, reply_id, "An answer to the reply.")
    _documents_kept(world)


@pytest.mark.parametrize("dropped", [*DROPPED, "all"])
def test_entry_edit_that_drops_a_placeholder_keeps_the_entry(
        app, world, dropped):
    """An entry whose own text names documents (no prompt reference):
    dropping one placeholder, or all of them, removes those pins only."""
    parent = _node(world.alice, "The thread's first entry.")
    entry, reply = _plain_entry(world, parent=parent)
    entry_id, reply_id, parent_id = entry.id, reply.id, parent.id
    before = _pins(entry_id)
    assert {t for t, _ in before} == {"profile", "todo", "user_artifact"}
    if dropped == "all":
        new_text, gone = "Notes for today.", set(DROPPED.values())
    else:
        new_text = ENTRY_TEXT.replace(" {%s}" % dropped, "")
        gone = {DROPPED[dropped]}

    resp = _client(app, world.alice).put(f"/nodes/{entry_id}", json={
        "content": new_text, "privacy_level": "private", "ai_usage": "chat",
    })

    assert resp.status_code == 200, resp.get_json()
    _db.session.expire_all()
    _unchanged(entry_id, parent_id, new_text)
    assert _pins(entry_id) == {k: v for k, v in before.items()
                               if k[0] not in gone}
    _unchanged(reply_id, entry_id, "A reply.")
    _unchanged(parent_id, None, "The thread's first entry.")
    _documents_kept(world)


def _tool_pins(node, world):
    """The pins the retrieval loop (_pin_shown) leaves on the node whose
    turn read the to-do list and a document with a tool."""
    _db.session.add_all([
        NodeContextArtifact(node_id=node.id, artifact_type="todo",
                            artifact_id=world.todo.id),
        NodeContextArtifact(node_id=node.id, artifact_type="user_artifact",
                            artifact_id=world.intentions.id),
    ])


def test_ai_reply_edit_keeps_the_versions_its_turn_read(app, world):
    """An AI reply whose turn read the to-do list and a document with a
    tool carries pins to those versions, so the export names what it
    read. Its text names no placeholder. Its human owner edits it: the
    text is saved and both pins stay (review of #788: an edit removed
    every pin no placeholder in the NEW text needed)."""
    model = User(username="model-account", approved=True)
    _db.session.add(model)
    _db.session.flush()
    question = _node(world.alice, "What is on my list?")
    reply = Node(user_id=model.id, human_owner_id=world.alice.id,
                 node_type="llm", llm_model="test-model",
                 privacy_level="private", ai_usage="chat", token_count=1,
                 parent_id=question.id)
    reply.set_content("I read your list. Here is the answer.")
    _db.session.add(reply)
    _db.session.flush()
    _tool_pins(reply, world)
    follow_up = _node(world.alice, "Thanks.", parent=reply)
    _db.session.commit()
    reply_id, follow_up_id = reply.id, follow_up.id
    before = _pins(reply_id)
    assert set(before) == {("todo", world.todo.id),
                           ("user_artifact", world.intentions.id)}
    new_text = "I read your list. Here is a better answer."

    resp = _client(app, world.alice).put(f"/nodes/{reply_id}", json={
        "content": new_text, "privacy_level": "private", "ai_usage": "chat",
    })

    assert resp.status_code == 200, resp.get_json()
    _db.session.expire_all()
    _unchanged(reply_id, question.id, new_text)
    assert _pins(reply_id) == before
    _unchanged(follow_up_id, reply_id, "Thanks.")
    _documents_kept(world)


def test_edit_removes_only_the_dropped_placeholders_pins(app, world):
    """An entry with {user_profile} in its text and a to-do pin no
    placeholder accounts for: dropping {user_profile} removes the
    profile pin only."""
    from backend.utils.context_artifacts import sync_context_artifacts
    entry = _node(world.alice, "Notes {user_profile}")
    sync_context_artifacts(entry.id, world.alice.id, "Notes {user_profile}")
    _db.session.add(NodeContextArtifact(node_id=entry.id,
                                        artifact_type="todo",
                                        artifact_id=world.todo.id))
    _db.session.commit()
    entry_id = entry.id
    before = _pins(entry_id)
    assert set(before) == {("profile", world.profile.id),
                           ("todo", world.todo.id)}

    resp = _client(app, world.alice).put(f"/nodes/{entry_id}", json={
        "content": "Notes", "privacy_level": "private", "ai_usage": "chat",
    })

    assert resp.status_code == 200, resp.get_json()
    _db.session.expire_all()
    _unchanged(entry_id, None, "Notes")
    assert _pins(entry_id) == {("todo", world.todo.id):
                               before[("todo", world.todo.id)]}
    _documents_kept(world)


# ── Deleting a pin, deleting a node ─────────────────────────────────────


def test_deleting_a_pin_deletes_nothing_else(app, world):
    """The ORM delete of a pin row, as sync_context_artifacts does it."""
    root, reply, _ = _prompt_entry(world)
    root_id, reply_id = root.id, reply.id
    profile_pin = NodeContextArtifact.query.filter_by(
        node_id=root_id, artifact_type="profile").one()
    others = {k: v for k, v in _pins(root_id).items() if k[0] != "profile"}

    _db.session.delete(profile_pin)
    _db.session.commit()

    _db.session.expire_all()
    _unchanged(root_id, None, PROMPT_TEXT)   # still resolves the prompt
    _unchanged(reply_id, root_id, "A reply.")
    assert _pins(root_id) == others
    _documents_kept(world)


def test_deleting_a_node_deletes_its_pins(app, world):
    """A node deleted through the ORM takes its pins with it (no orphan
    rows, no NOT NULL failure from nulling their node_id) and leaves
    another entry's pins and the versions they point at."""
    gone, _ = _plain_entry(world)
    kept, _ = _plain_entry(world)
    leaf = _node(world.alice, "{user_profile}", parent=gone)
    from backend.utils.context_artifacts import sync_context_artifacts
    sync_context_artifacts(leaf.id, world.alice.id, "{user_profile}")
    _db.session.commit()
    leaf_id, kept_id = leaf.id, kept.id
    kept_pins = _pins(kept_id)
    assert _pins(leaf_id)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        _db.session.delete(_db.session.get(Node, leaf_id))
        _db.session.commit()

    _db.session.expire_all()
    assert _db.session.get(Node, leaf_id) is None
    assert _pins(leaf_id) == {}
    assert _pins(kept_id) == kept_pins
    _documents_kept(world)


@pytest.mark.parametrize("pins_loaded", [False, True],
                         ids=["pins-unloaded", "pins-loaded"])
def test_thirty_day_cleanup_purges_a_pinned_entry(app, world, pins_loaded):
    """The clean-up's full purge deletes the node through the ORM, and
    Node.context_artifacts' cascade deletes its pins (the purge's own
    bulk delete of them is gone: with the pins already loaded on the
    node, the cascade deleted them a second time, and a DELETE that
    matches no row warns). The node's pins go, another entry's pins and
    the versions stay. With the pins loaded on the node and without."""
    from backend.tasks.node_cleanup import _full_purge
    entry, reply = _plain_entry(world)
    kept, _ = _plain_entry(world)
    reply_id, kept_id = reply.id, kept.id
    # The reply is the leaf the purge may delete; give it pins too.
    from backend.utils.context_artifacts import sync_context_artifacts
    sync_context_artifacts(reply_id, world.alice.id, ENTRY_TEXT)
    _db.session.get(Node, reply_id).deleted_at = (
        datetime.utcnow() - timedelta(days=31))
    _db.session.commit()
    kept_pins = _pins(kept_id)
    entry_pins = _pins(entry.id)
    assert _pins(reply_id)

    node = _db.session.get(Node, reply_id)
    if pins_loaded:
        assert len(node.context_artifacts) == 4
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        _full_purge(node)
        _db.session.commit()

    _db.session.expire_all()
    assert _db.session.get(Node, reply_id) is None
    assert _pins(reply_id) == {}
    assert _pins(kept_id) == kept_pins
    assert _pins(entry.id) == entry_pins
    _documents_kept(world)


# ── With the edit form's base (#799) ────────────────────────────────────
#
# Both apps' edit forms now send the text they were opened on
# (base_content): the route locks the row and reads it again before the
# edit, and saves only over that text. These are the edits above as the
# forms send them, and the two refusals that must leave the pins alone.


def test_edit_with_its_base_that_drops_the_profile_keeps_the_entry(
        app, world):
    """The first test's edit with the base the form showed (the resolved
    prompt): saved, the same pins go, the entry and its replies stay."""
    root, reply, answer = _prompt_entry(world)
    root_id, reply_id, answer_id = root.id, reply.id, answer.id
    before = _pins(root_id)
    new_text = "You are Loore, but terser.\n{user_intentions}\n{user_todo}"

    resp = _client(app, world.alice).put(f"/nodes/{root_id}", json={
        "content": new_text, "base_content": PROMPT_TEXT,
        "privacy_level": "private", "ai_usage": "chat",
        "detach_prompt": True,
    })

    assert resp.status_code == 200, resp.get_json()
    _db.session.expire_all()
    _unchanged(root_id, None, new_text)
    assert _pins(root_id) == {k: v for k, v in before.items()
                              if k[0] not in ("prompt", "profile")}
    _unchanged(reply_id, root_id, "A reply.")
    _unchanged(answer_id, reply_id, "An answer to the reply.")
    _documents_kept(world)


def test_stale_edit_that_drops_a_placeholder_saves_nothing(app, world):
    """A form opened before another device's edit drops a placeholder:
    refused (409 content_changed); the text, the pins and the edit
    history stay as the other device left them."""
    entry, _ = _plain_entry(world)
    entry_id = entry.id
    client = _client(app, world.alice)
    newer = ENTRY_TEXT.replace(" {user_todo}", "")
    assert client.put(f"/nodes/{entry_id}", json={
        "content": newer, "base_content": ENTRY_TEXT,
        "privacy_level": "private", "ai_usage": "chat",
    }).status_code == 200
    _db.session.expire_all()
    pins = _pins(entry_id)
    versions = NodeVersion.query.filter_by(node_id=entry_id).count()

    resp = client.put(f"/nodes/{entry_id}", json={
        "content": ENTRY_TEXT.replace(" {user_profile}", ""),
        "base_content": ENTRY_TEXT,
        "privacy_level": "private", "ai_usage": "chat",
    })

    assert resp.status_code == 409
    assert resp.get_json()["code"] == "content_changed"
    _db.session.expire_all()
    _unchanged(entry_id, None, newer)
    assert _pins(entry_id) == pins
    assert NodeVersion.query.filter_by(node_id=entry_id).count() == versions
    _documents_kept(world)


def test_stale_settings_only_save_does_not_bring_a_dropped_pin_back(
        app, world):
    """A form opened before another device took {user_todo} out saves only
    a narrower setting, its text untouched: the entry keeps the newer
    text, and the to-do pin that edit removed is not made again from the
    form's older text."""
    entry, _ = _plain_entry(world)
    entry_id = entry.id
    client = _client(app, world.alice)
    newer = ENTRY_TEXT.replace(" {user_todo}", "")
    assert client.put(f"/nodes/{entry_id}", json={
        "content": newer, "base_content": ENTRY_TEXT,
        "privacy_level": "private", "ai_usage": "chat",
    }).status_code == 200
    _db.session.expire_all()
    pins = _pins(entry_id)
    assert "todo" not in {t for t, _ in pins}

    resp = client.put(f"/nodes/{entry_id}", json={
        "content": ENTRY_TEXT, "base_content": ENTRY_TEXT,
        "privacy_level": "private", "ai_usage": "none",
    })

    assert resp.status_code == 200, resp.get_json()
    _db.session.expire_all()
    _unchanged(entry_id, None, newer)
    assert _db.session.get(Node, entry_id).ai_usage == "none"
    assert _pins(entry_id) == pins
    _documents_kept(world)
