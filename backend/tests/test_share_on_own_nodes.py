""":::share blocks on the user's own nodes: Save to shares saves the clicked
node's own blocks, for its owner only, on every way the node gets or
changes its text.

The paths: an entry typed (POST /api/nodes/), a recorded entry once its
transcript is stored, an edit (PUT /api/nodes/<id>) that adds, changes or
removes a block, an AI reply the user wrote a block into, and imported
text. The guarantees: only the node's owner saves; writing or importing
the tag creates nothing until the click; a save makes a PRIVATE draft and
nothing is public until the Share page's publish; an edit never changes a
saved or published share.

Fixture pattern as test_forum.py (share + nodes blueprints, sqlite,
encryption off).
"""
import json
import os
import sys
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
from flask import Flask, g  # noqa: E402

for _mod in ["flask_login", "backend.models", "backend.extensions"]:
    if _mod in sys.modules and isinstance(sys.modules[_mod], MagicMock):
        del sys.modules[_mod]

import flask_login as _real_flask_login          # noqa: E402
from backend.extensions import db as _db         # noqa: E402
from backend.models import (  # noqa: E402
    Draft, Node, ShareDraft, User,
)
import backend.models as _real_backend_models    # noqa: E402


def _make_app(share_v1=True):
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SHARE_V1"] = share_v1
    app.config["DEFAULT_LLM_MODEL"] = "gpt-5"
    app.config["SUPPORTED_MODELS"] = {
        "gpt-5": {"provider": "openai", "api_model": "gpt-5"},
    }

    _db.init_app(app)
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from backend.routes.share import share_bp
    from backend.routes.nodes import nodes_bp
    from backend.routes.commons import commons_bp
    app.register_blueprint(share_bp, url_prefix="/api/share")
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    app.register_blueprint(commons_bp, url_prefix="/api/commons")
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
        owner = User(username="owner", default_ai_usage="chat",
                     public_sharing_enabled=True)
        other = User(username="other", default_ai_usage="chat",
                     public_sharing_enabled=True)
        model = User(username="gpt-5", twitter_id="llm-gpt-5")
        _db.session.add_all([owner, other, model])
        _db.session.commit()
        yield app
        _db.session.remove()
        _db.drop_all()

    for k in [k for k in list(sys.modules) if _affected(k)]:
        del sys.modules[k]
    sys.modules.update(saved)


def _user(username):
    return User.query.filter_by(username=username).first()


def _client_for(app, username):
    # Flask-Login keeps the loaded user on g for the whole app context the
    # fixture holds: drop it so this client is its own user.
    g.pop("_login_user", None)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_user(username).id)
        sess["_fresh"] = True
    return client


def _entry(username, content, parent=None, privacy="private", **extra):
    user = _user(username)
    node = Node(user_id=user.id, human_owner_id=user.id,
                parent_id=parent.id if parent else None, node_type="user",
                privacy_level=privacy, ai_usage="chat", **extra)
    node.set_content(content)
    _db.session.add(node)
    _db.session.commit()
    return node


def _ai_reply(for_username, content, parent, proposes_share=False,
              privacy="private"):
    """An AI reply written for *for_username*; *proposes_share*: the reply
    task found its :::share block and left the pending proposal (its
    propose_share entry and share_pending Draft), as _auto_create_drafts
    does."""
    owner = _user(for_username)
    node = Node(user_id=_user("gpt-5").id, human_owner_id=owner.id,
                parent_id=parent.id, node_type="llm", llm_model="gpt-5",
                llm_task_status="completed", privacy_level=privacy,
                ai_usage="chat")
    node.set_content(content)
    _db.session.add(node)
    _db.session.flush()
    if proposes_share:
        node.tool_calls_meta = json.dumps([{
            "name": "propose_share", "status": "success",
            "apply_status": "pending_approval"}])
        draft = Draft(user_id=owner.id, parent_id=node.id,
                      label="share_pending")
        draft.set_content("")
        _db.session.add(draft)
    _db.session.commit()
    return node


def _save(client, node_id, index=None):
    body = {"node_id": node_id}
    if index is not None:
        body["share_index"] = index
    return client.post("/api/share/save-proposal", json=body)


def _shares():
    return [(s.get_content(), s.share_type, s.status, s.public_node_id)
            for s in ShareDraft.query.order_by(ShareDraft.id).all()]


def _pending_count(node_id=None):
    q = Draft.query.filter_by(label="share_pending")
    if node_id is not None:
        q = q.filter_by(parent_id=node_id)
    return q.count()


def _meta_entry(node_id):
    meta = json.loads(Node.query.get(node_id).tool_calls_meta or "[]")
    return next((e for e in meta if e.get("name") == "propose_share"), None)


AI_PROPOSAL = (
    "Here is a piece you could share.\n\n"
    ":::share insight\nThe AI's wording of it.\n:::\n")
OWN_BLOCK = ":::share need\nLooking for a reading partner.\n:::\n"


# ── Typed entries ─────────────────────────────────────────────────────────

def test_typed_entry_saves_its_own_block_only_on_click(app):
    """Typing the tag creates nothing; the click saves a private draft of
    exactly the block, with its type word."""
    client = _client_for(app, "owner")
    resp = client.post("/api/nodes/", json={
        "content": "My note.\n\n" + OWN_BLOCK, "privacy_level": "private",
        "ai_usage": "chat"})
    assert resp.status_code == 201
    node_id = resp.get_json()["id"]
    # Writing the tag alone: no share, no pending proposal, nothing public.
    assert ShareDraft.query.count() == 0
    assert _pending_count() == 0
    assert Node.query.filter_by(privacy_level="public").count() == 0

    resp = _save(client, node_id, 0)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "completed"
    assert _shares() == [("Looking for a reading partner.", "need",
                          "draft", None)]
    # Saving is not publishing.
    assert Node.query.filter_by(privacy_level="public").count() == 0


def test_typed_entry_under_pending_ai_proposal_saves_the_entrys_block(app):
    """The user's own block is saved, not the AI's pending proposal above
    it (the server used to walk up to that proposal and save its text),
    and the AI's proposal stays pending for its own Save button."""
    owner = _user("owner")
    root = _entry("owner", "Thinking about reading groups.")
    proposal = _ai_reply("owner", AI_PROPOSAL, root, proposes_share=True)
    client = _client_for(app, "owner")
    resp = client.post("/api/nodes/", json={
        "content": "I'd rather say it myself:\n\n" + OWN_BLOCK,
        "parent_id": proposal.id, "privacy_level": "private",
        "ai_usage": "chat"})
    assert resp.status_code == 201
    entry_id = resp.get_json()["id"]

    resp = _save(client, entry_id, 0)
    assert resp.status_code == 200, resp.get_json()
    assert _shares() == [("Looking for a reading partner.", "need",
                          "draft", None)]
    assert ShareDraft.query.first().source_node_id == entry_id
    # The AI's proposal is untouched: still pending, nothing saved there.
    assert _pending_count(proposal.id) == 1
    assert _meta_entry(proposal.id)["apply_status"] == "pending_approval"
    assert "saved_indexes" not in _meta_entry(proposal.id)

    # And its own button still saves the AI's wording.
    resp = _save(client, proposal.id, 0)
    assert resp.status_code == 200
    assert [s[0] for s in _shares()] == [
        "Looking for a reading partner.", "The AI's wording of it."]
    assert _pending_count(proposal.id) == 0
    assert owner.id == ShareDraft.query.all()[1].user_id


# ── Recorded entries ──────────────────────────────────────────────────────

def test_recorded_entry_with_block_saves_like_a_typed_one(app):
    """A recorded entry is the user's own entry once its transcript is
    stored; the tag is usually put in by an edit of the transcript."""
    node = _entry("owner", "[Voice note – transcription pending]",
                  audio_original_url="/media/x/original.webm",
                  transcription_status="pending")
    # The transcription task stores the transcript.
    node.set_content("So the thing I keep coming back to is reading.")
    node.transcription_status = "completed"
    _db.session.commit()
    client = _client_for(app, "owner")
    assert _save(client, node.id, 0).status_code == 400  # no block yet

    resp = client.put(f"/api/nodes/{node.id}", json={
        "content": node.get_content() + "\n\n" + OWN_BLOCK})
    assert resp.status_code == 200
    assert ShareDraft.query.count() == 0 and _pending_count() == 0

    resp = _save(client, node.id, 0)
    assert resp.status_code == 200
    assert _shares() == [("Looking for a reading partner.", "need",
                          "draft", None)]


# ── Edits ─────────────────────────────────────────────────────────────────

def test_edit_adds_block_to_ai_reply_then_owner_saves_it(app):
    """An AI reply with no pending proposal (the user wrote the block in
    by an edit) used to answer "No pending share found"."""
    root = _entry("owner", "A question.")
    reply = _ai_reply("owner", "An answer without any share.", root)
    client = _client_for(app, "owner")
    resp = client.put(f"/api/nodes/{reply.id}", json={
        "content": "An answer without any share.\n\n" + OWN_BLOCK})
    assert resp.status_code == 200

    resp = _save(client, reply.id, 0)
    assert resp.status_code == 200, resp.get_json()
    assert _shares() == [("Looking for a reading partner.", "need",
                          "draft", None)]
    assert _meta_entry(reply.id)["saved_indexes"] == [0]


def test_edit_changing_a_saved_block_offers_save_again(app):
    """After a save, an edit of the block's text makes it a block not yet
    saved: Save saves the new text as a new draft, and the share saved
    before keeps the text it was saved with."""
    node = _entry("owner", OWN_BLOCK)
    client = _client_for(app, "owner")
    assert _save(client, node.id, 0).status_code == 200

    resp = client.put(f"/api/nodes/{node.id}", json={
        "content": ":::share need\nLooking for TWO reading partners.\n:::\n"})
    assert resp.status_code == 200
    entry = next(e for e in resp.get_json()["node"]["tool_calls_meta"]
                 if e["name"] == "propose_share")
    assert entry["saved_indexes"] == []

    resp = _save(client, node.id, 0)
    assert resp.status_code == 200, resp.get_json()
    assert [s[0] for s in _shares()] == [
        "Looking for a reading partner.",
        "Looking for TWO reading partners."]


def test_edit_keeps_saved_state_of_unchanged_blocks(app):
    """Removing one block moves the others up; a saved block stays saved
    wherever it now stands, and the unsaved one keeps its button."""
    node = _entry("owner", (
        ":::share insight\nFirst.\n:::\n\n"
        ":::share need\nSecond.\n:::\n\n"
        ":::share offering\nThird.\n:::\n"))
    client = _client_for(app, "owner")
    assert _save(client, node.id, 2).status_code == 200  # "Third."

    resp = client.put(f"/api/nodes/{node.id}", json={
        "content": (":::share need\nSecond.\n:::\n\n"
                    ":::share offering\nThird.\n:::\n")})
    assert resp.status_code == 200
    assert _meta_entry(node.id)["saved_indexes"] == [1]
    assert _meta_entry(node.id)["apply_status"] == "partial"

    assert _save(client, node.id, 1).status_code == 400  # Already saved
    resp = _save(client, node.id, 0)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "completed"
    assert [s[0] for s in _shares()] == ["Third.", "Second."]


def test_edit_changing_only_the_type_word_offers_save_again(app):
    node = _entry("owner", OWN_BLOCK)
    client = _client_for(app, "owner")
    assert _save(client, node.id, 0).status_code == 200
    client.put(f"/api/nodes/{node.id}", json={
        "content": OWN_BLOCK.replace("need", "offering")})
    assert _meta_entry(node.id)["saved_indexes"] == []
    assert _save(client, node.id, 0).status_code == 200
    assert [s[1] for s in _shares()] == ["need", "offering"]


def test_edit_removing_the_block_leaves_no_pending_share(app):
    """An AI proposal the owner edits the block out of: its pending
    proposal goes (a spoken "yes, save that" can no longer act on text
    that is gone), and nothing is saved."""
    root = _entry("owner", "A question.")
    proposal = _ai_reply("owner", AI_PROPOSAL, root, proposes_share=True)
    client = _client_for(app, "owner")
    resp = client.put(f"/api/nodes/{proposal.id}", json={
        "content": "Here is a piece you could share."})
    assert resp.status_code == 200
    assert _pending_count(proposal.id) == 0
    assert ShareDraft.query.count() == 0
    assert _save(client, proposal.id).status_code == 400  # nothing to save


def test_edit_keeping_the_block_keeps_the_pending_proposal(app):
    """A checklist tick or any edit around the block leaves the AI's
    proposal pending, and the block is saved as edited (the user's edit
    wins)."""
    root = _entry("owner", "A question.")
    proposal = _ai_reply("owner", AI_PROPOSAL, root, proposes_share=True)
    client = _client_for(app, "owner")
    resp = client.put(f"/api/nodes/{proposal.id}", json={
        "content": AI_PROPOSAL.replace("The AI's wording", "My wording")})
    assert resp.status_code == 200
    assert _pending_count(proposal.id) == 1
    assert _save(client, proposal.id, 0).status_code == 200
    assert _shares()[0][0] == "My wording of it."
    assert _pending_count(proposal.id) == 0


def test_edit_after_publishing_changes_nothing_public(app):
    """What is public is what the user published: a later edit of the
    entry's block changes neither the share nor its public node."""
    node = _entry("owner", OWN_BLOCK)
    client = _client_for(app, "owner")
    share_id = _save(client, node.id, 0).get_json()["share"]["id"]
    resp = client.post(f"/api/share/{share_id}/publish")
    assert resp.status_code == 200
    public_id = resp.get_json()["public_node_id"]

    client.put(f"/api/nodes/{node.id}", json={
        "content": ":::share need\nSomething else entirely.\n:::\n"})
    assert Node.query.get(public_id).get_content() == (
        "Looking for a reading partner.")
    assert ShareDraft.query.get(share_id).get_content() == (
        "Looking for a reading partner.")
    assert ShareDraft.query.get(share_id).status == "published"


# ── Only the owner ────────────────────────────────────────────────────────

def test_non_owner_cannot_save_from_someone_elses_entry(app):
    """Another user's public entry with a block, readable by everyone:
    only its author may save it."""
    node = _entry("other", OWN_BLOCK, privacy="public")
    client = _client_for(app, "owner")
    resp = _save(client, node.id, 0)
    assert resp.status_code == 404
    assert ShareDraft.query.count() == 0


def test_non_owner_cannot_save_from_someone_elses_ai_reply(app):
    """Another user's AI reply in a public thread, even one proposing a
    share: only the user it was written for may save it."""
    root = _entry("other", "A public question.", privacy="public")
    reply = _ai_reply("other", AI_PROPOSAL, root, proposes_share=True,
                      privacy="public")
    client = _client_for(app, "owner")
    assert _save(client, reply.id, 0).status_code == 404
    assert ShareDraft.query.count() == 0
    assert _pending_count(reply.id) == 1


def test_thread_owner_cannot_save_another_persons_reply_in_their_thread(app):
    """Text someone else wrote, in the user's own public thread."""
    root = _entry("owner", "My public question.", privacy="public")
    reply = _entry("other", OWN_BLOCK, parent=root, privacy="public")
    client = _client_for(app, "owner")
    assert _save(client, reply.id, 0).status_code == 404
    assert ShareDraft.query.count() == 0


def test_deleted_entry_cannot_be_saved(app):
    from datetime import datetime
    node = _entry("owner", OWN_BLOCK)
    node.deleted_at = datetime.utcnow()
    _db.session.commit()
    client = _client_for(app, "owner")
    assert _save(client, node.id, 0).status_code == 404
    assert ShareDraft.query.count() == 0


# ── Imported text ─────────────────────────────────────────────────────────

def test_imported_text_with_the_tag_waits_for_the_click(app):
    """Text that arrives by an import (here a chat archive: the user's
    message and the assistant's) creates no share and no pending proposal
    by itself; the owner's click saves a private draft, as for typed text,
    and nothing is ever published by it."""
    root = _entry("owner", "Imported.\n\n" + OWN_BLOCK, origin="chatgpt")
    owner = _user("owner")
    assistant = Node(user_id=_user("gpt-5").id, human_owner_id=owner.id,
                     parent_id=root.id, node_type="llm",
                     llm_model="chatgpt", origin="chatgpt",
                     privacy_level="private", ai_usage="chat")
    assistant.set_content(":::share insight\nFrom the archive.\n:::\n")
    _db.session.add(assistant)
    _db.session.commit()
    assert ShareDraft.query.count() == 0
    assert _pending_count() == 0

    client = _client_for(app, "owner")
    assert _save(client, root.id, 0).status_code == 200
    assert _save(client, assistant.id, 0).status_code == 200
    assert _shares() == [
        ("Looking for a reading partner.", "need", "draft", None),
        ("From the archive.", "insight", "draft", None)]
    assert Node.query.filter_by(privacy_level="public").count() == 0


# ── Flags ─────────────────────────────────────────────────────────────────

def test_save_needs_the_users_public_sharing_opt_in(app):
    node = _entry("owner", OWN_BLOCK)
    owner = _user("owner")
    owner.public_sharing_enabled = False
    _db.session.commit()
    client = _client_for(app, "owner")
    assert _save(client, node.id, 0).status_code == 404
    assert ShareDraft.query.count() == 0


def test_save_is_off_while_share_v1_is_off(app):
    node = _entry("owner", OWN_BLOCK)
    app.config["SHARE_V1"] = False
    try:
        client = _client_for(app, "owner")
        assert _save(client, node.id, 0).status_code == 404
        assert ShareDraft.query.count() == 0
    finally:
        app.config["SHARE_V1"] = True


# ── Who gets the saved-share state ────────────────────────────────────────

def _meta_as(app, username, node_id, path=""):
    """The tool record GET /api/nodes/<id><path> gives *username*."""
    resp = _client_for(app, username).get(f"/api/nodes/{node_id}{path}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json().get("tool_calls_meta")


def _names(meta):
    return [m.get("name") for m in meta or []]


def test_saved_share_state_of_a_public_entry_goes_to_its_owner_only(app):
    """An entry's saved-share state is returned to its owner only: another
    signed-in user and an anonymous reader of the public entry get it as
    if no share had been saved, before and after the owner's save."""
    node = _entry("owner", OWN_BLOCK, privacy="public")
    assert _meta_as(app, "other", node.id) is None

    assert _save(_client_for(app, "owner"), node.id, 0).status_code == 200

    assert _meta_as(app, "other", node.id) is None
    g.pop("_login_user", None)
    anon = app.test_client().get(f"/api/commons/node/{node.id}")
    assert anon.status_code == 200
    assert "propose_share" not in anon.get_data(as_text=True)
    assert "saved_indexes" not in anon.get_data(as_text=True)
    owners = _meta_as(app, "owner", node.id)
    assert _names(owners) == ["propose_share"]
    assert owners[0]["saved_indexes"] == [0]

    # An edit keeps it the owner's.
    resp = _client_for(app, "owner").put(f"/api/nodes/{node.id}", json={
        "content": "Lead-in.\n\n" + OWN_BLOCK})
    assert resp.status_code == 200
    assert _meta_as(app, "other", node.id) is None
    assert _meta_as(app, "owner", node.id)[0]["saved_indexes"] == [0]


def test_saved_share_state_of_a_public_ai_reply_goes_to_its_owner_only(app):
    """A public AI reply with no share proposal of its own, saved from by
    the user it was written for: other readers get its record as before
    the save, on the thread page and on the reply's status."""
    root = _entry("owner", "A public question.", privacy="public")
    reply = _ai_reply("owner", "An answer.\n\n" + OWN_BLOCK, root,
                      privacy="public")
    reply.tool_calls_meta = json.dumps([{"name": "_mode",
                                         "source_mode": "text"}])
    _db.session.commit()
    before = _meta_as(app, "other", reply.id)

    assert _save(_client_for(app, "owner"), reply.id, 0).status_code == 200

    assert _meta_as(app, "other", reply.id) == before == [{"name": "_mode"}]
    status = _meta_as(app, "other", reply.id, "/llm-status")
    assert "propose_share" not in _names(status)
    assert "propose_share" in _names(_meta_as(app, "owner", reply.id))


def test_an_ai_proposal_reads_the_same_to_other_readers_after_the_save(app):
    """The AI's own proposal entry (name and status) is shown to every
    reader of the reply, as before; the save adds nothing they see."""
    root = _entry("owner", "A public question.", privacy="public")
    proposal = _ai_reply("owner", AI_PROPOSAL, root, proposes_share=True,
                         privacy="public")
    before = _meta_as(app, "other", proposal.id)
    assert before == [{"name": "propose_share", "status": "success"}]

    assert _save(_client_for(app, "owner"), proposal.id, 0).status_code == 200

    assert _meta_as(app, "other", proposal.id) == before
    owners = _meta_as(app, "owner", proposal.id)
    assert owners[0]["apply_status"] == "completed"
    assert owners[0]["saved_indexes"] == [0]


def test_saved_share_state_written_earlier_on_an_entry_is_the_owners(app):
    """An entry saved from before this change holds the state with no
    owner-only mark: other readers still do not get it."""
    node = _entry("owner", OWN_BLOCK, privacy="public")
    node.tool_calls_meta = json.dumps([{
        "name": "propose_share", "apply_status": "completed",
        "share_id": 1, "share_ids": [1], "saved_indexes": [0]}])
    _db.session.commit()
    assert _meta_as(app, "other", node.id) is None
    assert _names(_meta_as(app, "owner", node.id)) == ["propose_share"]
