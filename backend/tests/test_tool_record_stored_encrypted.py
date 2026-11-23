"""A reply's tool record (Node.tool_calls_meta) is stored like the entry's
text: encrypted, with each entry's name and status beside it
(backend/utils/tool_record.py).

What these pin: the stored value does not hold the record's text; code
reads and writes the same JSON text as before; rows written in the
earlier form (the list itself) read back unchanged; the SQL filters on
entry names still match; another viewer's answer needs no decryption;
a reply's text and record are written and read with one data key; the
owner-run script converts earlier rows in batches."""
import importlib.util
import json
import os
import sys
import types

import pytest
from flask import Flask
from sqlalchemy import text

from backend.extensions import db
from backend.models import Node, User
from backend.utils import encryption

QUERY = "what I wrote about the move to Lisbon"
RECORD = [
    {"name": "search", "status": "success",
     "input": {"query": QUERY}, "result_count": 3},
    {"name": "update_artifact", "status": "success", "kind": "lisbon-plan",
     "input": {"title": "Lisbon plan", "description": "Steps for the move"},
     "artifact_id": 7},
    {"name": "_batch", "batch_id": "msgbatch_1", "status": "submitted"},
    {"name": "_mode", "source_mode": "voice"},
]
RECORD_TEXT = json.dumps(RECORD)
PRIVATE_WORDS = (QUERY, "Lisbon", "lisbon-plan", "Steps for the move",
                 "msgbatch_1", "voice")


class _KeyService:
    """Stands in for the key service one level below the data-key cache,
    so the cache works as in production and calls are counted."""

    def __init__(self):
        self.wraps = 0
        self.unwraps = 0

    def encrypt(self, request):
        self.wraps += 1
        return types.SimpleNamespace(ciphertext=b"w:" + request["plaintext"])

    def decrypt(self, request):
        self.unwraps += 1
        return types.SimpleNamespace(plaintext=request["ciphertext"][2:])

    def forget(self):
        """A process that has read nothing yet."""
        encryption._dek_cache.clear()
        del encryption._dek_cache_order[:]
        self.wraps = self.unwraps = 0


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "false")
    monkeypatch.setenv("GCP_KMS_KEY_NAME",
                       "projects/t/locations/l/keyRings/r/cryptoKeys/k")
    service = _KeyService()
    monkeypatch.setattr(encryption, "_kms_encrypt", service.encrypt)
    monkeypatch.setattr(encryption, "_kms_decrypt", service.decrypt)
    service.forget()
    yield service
    service.forget()


@pytest.fixture
def app(keys):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _user(name="ada"):
    user = User(username=name)
    db.session.add(user)
    db.session.flush()
    return user


def _reply(user, record=RECORD_TEXT, privacy="private", content="A reply."):
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="llm",
                llm_model="m", privacy_level=privacy)
    node.set_content(content)
    node.tool_calls_meta = record
    db.session.add(node)
    db.session.commit()
    return node


def _stored(node_id):
    return db.session.execute(
        text("SELECT tool_calls_meta FROM node WHERE id = :i"),
        {"i": node_id}).scalar()


def _store_as_before(node_id, value):
    """What a row holds that was written before records were encrypted."""
    db.session.execute(
        text("UPDATE node SET tool_calls_meta = :v WHERE id = :i"),
        {"v": value, "i": node_id})
    db.session.commit()
    db.session.expire_all()


# -- what is stored ----------------------------------------------------------

def test_the_stored_record_holds_names_and_statuses_and_no_other_text(app):
    node = _reply(_user())
    stored = _stored(node.id)
    for words in PRIVATE_WORDS:
        assert words not in stored
    held = json.loads(stored)
    assert held["shown"] == [
        {"name": "search", "status": "success"},
        {"name": "update_artifact", "status": "success"},
        {"name": "_batch", "status": "submitted"},
        {"name": "_mode"},
    ]
    assert held["sealed"].startswith("ENC:v2:")


def test_the_record_reads_back_as_the_text_that_was_written(app):
    node = _reply(_user())
    db.session.expire_all()
    assert Node.query.get(node.id).tool_calls_meta == RECORD_TEXT


def test_a_public_replys_record_is_stored_encrypted_too(app):
    node = _reply(_user(), privacy="public", content="Public words.")
    assert node.content == "Public words."      # text: stored as it is
    stored = _stored(node.id)
    for words in PRIVATE_WORDS:
        assert words not in stored
    assert Node.query.get(node.id).tool_calls_meta == RECORD_TEXT


def test_a_row_from_before_reads_back_unchanged(app):
    node = _reply(_user(), record=None)
    _store_as_before(node.id, RECORD_TEXT)
    assert Node.query.get(node.id).tool_calls_meta == RECORD_TEXT
    assert _stored(node.id) == RECORD_TEXT      # reading rewrites nothing


@pytest.mark.parametrize("odd", [
    "not json at all",
    json.dumps({"v": 1, "shown": [], "sealed": "ENC:v2:AAAA:BBBB"}),
    json.dumps({"v": 1, "shown": [], "plain": "[]"}),
    json.dumps([{"name": "search", "input": {"query": "ENC:v2:AAAA:BBBB"}}]),
])
def test_text_that_looks_like_a_stored_value_reads_back_as_written(app, odd):
    node = _reply(_user(), record=odd)
    db.session.expire_all()
    assert Node.query.get(node.id).tool_calls_meta == odd
    assert json.loads(_stored(node.id))["sealed"].startswith("ENC:v2:")


def test_with_encryption_off_the_list_is_stored_as_it_is(app, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_DISABLED", "true")
    node = _reply(_user())
    assert _stored(node.id) == RECORD_TEXT
    looks_stored = json.dumps({"v": 1, "shown": [], "sealed": "x"})
    other = _reply(_user("bo"), record=looks_stored)
    db.session.expire_all()
    assert Node.query.get(other.id).tool_calls_meta == looks_stored


def test_setting_the_same_record_again_writes_nothing(app):
    node = _reply(_user())
    before = _stored(node.id)
    node.tool_calls_meta = RECORD_TEXT
    db.session.commit()
    assert _stored(node.id) == before


def test_clearing_the_record_clears_the_stored_value(app):
    node = _reply(_user())
    node.tool_calls_meta = None
    db.session.commit()
    assert _stored(node.id) is None
    other = _reply(_user("bo"))
    Node.query.filter(Node.id == other.id).update(
        {Node.tool_calls_meta: None}, synchronize_session=False)
    db.session.commit()
    assert _stored(other.id) is None
    assert Node.query.get(other.id).tool_calls_meta is None


def test_the_clean_up_of_a_deleted_reply_leaves_no_stored_record(app):
    """The clean-up clears the columns it names with setattr, and finds
    its rows with ``getattr(Node, name).isnot(None)``."""
    from backend.tasks.node_cleanup import _KEPT_TEXT_COLUMNS, _holds_text
    assert "tool_calls_meta" in _KEPT_TEXT_COLUMNS
    node = _reply(_user())
    node.content = None
    db.session.commit()
    assert Node.query.filter(_holds_text()).count() == 1
    for name in _KEPT_TEXT_COLUMNS:
        setattr(node, name, None)
    db.session.commit()
    assert _stored(node.id) is None
    assert Node.query.filter(_holds_text()).count() == 0


# -- readers -----------------------------------------------------------------

def test_sql_filters_on_entry_names_match_both_forms(app):
    user = _user()
    new = _reply(user)
    old = _reply(user, record=None)
    _store_as_before(old.id, RECORD_TEXT)
    plain = _reply(user, record=json.dumps(
        [{"name": "search", "status": "success",
          "input": {"query": "a _batch of propose_todo notes"}}]))
    both = {new.id, old.id}

    def ids(condition):
        return {n.id for n in Node.query.filter(condition).all()}

    assert ids(Node.tool_calls_meta.like('%"_batch"%')) == both
    assert ids(Node.tool_calls_meta.like('%"update_artifact"%')) == both
    assert ids(Node.tool_calls_meta.contains('"_mode"', autoescape=True)) \
        == both
    assert ids(Node.tool_calls_meta.isnot(None)) == both | {plain.id}
    # A word inside a query matched the earlier form's text; the stored
    # form holds names only.
    assert ids(Node.tool_calls_meta.like('%propose_todo%')) == set()


def test_the_owner_gets_the_record_in_full_and_others_names_and_statuses(app):
    from backend.utils.tool_meta import tool_calls_meta_for
    owner, other = _user(), _user("bo")
    for node in (_reply(owner), _reply(owner, record=None)):
        if node.tool_calls_meta is None:
            _store_as_before(node.id, RECORD_TEXT)
            node = Node.query.get(node.id)
        assert tool_calls_meta_for(node, owner.id) == RECORD
        for viewer in (other.id, None):
            assert tool_calls_meta_for(node, viewer) == [
                {"name": "search", "status": "success"},
                {"name": "update_artifact", "status": "success"},
                {"name": "_batch", "status": "submitted"},
                {"name": "_mode"},
            ]


def test_another_viewers_answer_asks_the_key_service_nothing(app, keys):
    from backend.utils.tool_meta import tool_calls_meta_for
    owner, other = _user(), _user("bo")
    node = _reply(owner, privacy="public")
    db.session.expire_all()
    keys.forget()
    node = Node.query.get(node.id)
    assert tool_calls_meta_for(node, other.id)[0] == {
        "name": "search", "status": "success"}
    assert (keys.wraps, keys.unwraps) == (0, 0)


def test_a_name_that_is_not_a_short_word_still_reaches_another_viewer(app):
    from backend.utils.tool_meta import tool_calls_meta_for
    owner, other = _user(), _user("bo")
    long_name = "n" * 200
    record = [{"name": long_name, "status": 7}, {"name": "search"}]
    node = _reply(owner, record=json.dumps(record))
    assert long_name not in _stored(node.id)
    assert tool_calls_meta_for(node, other.id) == [
        {"name": long_name, "status": 7}, {"name": "search"}]


def test_the_chain_query_finds_a_glean_marker_in_both_forms(app):
    from backend.utils.ca_feed import READ_FURTHER_MARKER
    from backend.utils.llm_nodes import _has_read_marker
    from backend.utils.thread_size import _is_gleaning_meta
    user = _user()
    marked = json.dumps([{"name": READ_FURTHER_MARKER}])
    new = _reply(user, record=marked)
    old = _reply(user, record=None)
    _store_as_before(old.id, marked)
    unmarked = _reply(user)
    rows = {r.id: r for r in db.session.query(
        Node.id, Node.tool_calls_meta).all()}
    for node_id, expected in ((new.id, True), (old.id, True),
                              (unmarked.id, False)):
        assert _has_read_marker(rows[node_id]) is expected
        assert _has_read_marker(Node.query.get(node_id)) is expected
        assert _is_gleaning_meta(rows[node_id].tool_calls_meta) is expected


def test_the_ancestor_walk_finds_markers_and_decrypts_nothing(app, keys):
    from backend.utils.ca_feed import READ_FURTHER_MARKER
    from backend.utils.llm_nodes import _Chain
    user = _user()
    top = _reply(user, record=json.dumps([{"name": READ_FURTHER_MARKER}]))
    below = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
                 parent_id=top.id)
    below.set_content("A follow-up.")
    db.session.add(below)
    db.session.commit()
    db.session.expire_all()
    keys.forget()
    chain = _Chain(below)
    assert [n.id for n in chain.nodes] == [below.id, top.id]
    assert top.id in chain.reads
    assert (keys.wraps, keys.unwraps) == (0, 0)


def test_checks_for_a_marker_or_a_tools_entry_decrypt_nothing(app, keys):
    from backend.utils.ca_feed import (
        READ_FURTHER_MARKER, is_read_reply, read_reply_ids,
    )
    from backend.utils.glean import is_gleaning
    from backend.utils.proposals import _has_tool_entry
    user = _user()
    batch = _reply(user)
    marked = _reply(user, record=json.dumps([{"name": READ_FURTHER_MARKER}]))
    plain = _reply(user, record=json.dumps(
        [{"name": "search", "input": {"query": '"_batch" and "_read"'}}]))
    old = _reply(user, record=None)
    _store_as_before(old.id, RECORD_TEXT)
    db.session.expire_all()
    keys.forget()
    nodes = [Node.query.get(n.id) for n in (batch, marked, plain, old)]
    batch, marked, plain, old = nodes
    assert [is_read_reply(n) for n in nodes] == [True, False, False, True]
    assert read_reply_ids(nodes) == {batch.id, old.id}
    assert is_gleaning(marked) and is_gleaning(batch)
    assert _has_tool_entry(batch, "update_artifact")
    assert _has_tool_entry(old, "update_artifact")
    assert not _has_tool_entry(plain, "update_artifact")
    assert (keys.wraps, keys.unwraps) == (0, 0)


def test_a_selected_column_reads_with_the_modules_reader(app):
    from backend.utils import tool_record
    user = _user()
    new = _reply(user)
    old = _reply(user, record=None)
    _store_as_before(old.id, RECORD_TEXT)
    for node_id in (new.id, old.id):
        selected = db.session.query(Node.tool_calls_meta).filter(
            Node.id == node_id).scalar()
        assert tool_record.read(selected) == RECORD_TEXT
        assert [e.get("name") for e in tool_record.shown_entries(selected)] \
            == ["search", "update_artifact", "_batch", "_mode"]


# -- key-service calls -------------------------------------------------------

def test_a_replys_text_and_record_are_written_with_one_key(app, keys):
    user = _user()
    keys.forget()
    node = _reply(user)
    assert (keys.wraps, keys.unwraps) == (1, 0)
    # A later change of the record (a proposal applied) asks for nothing.
    node.tool_calls_meta = json.dumps(RECORD + [{"name": "x"}])
    db.session.commit()
    assert (keys.wraps, keys.unwraps) == (1, 0)


def test_reading_text_and_record_in_a_new_process_is_one_call(app, keys):
    node = _reply(_user())
    db.session.expire_all()
    keys.forget()
    node = Node.query.get(node.id)
    assert node.get_content() == "A reply."
    assert node.tool_calls_meta == RECORD_TEXT
    assert node.tool_calls_meta == RECORD_TEXT
    assert (keys.wraps, keys.unwraps) == (0, 1)


def test_a_record_written_before_the_text_keeps_one_key_per_reply(app, keys):
    """The reply task writes the record while the reply is still being
    made, many times, then the text and the record once more."""
    user = _user()
    keys.forget()
    node = Node(user_id=user.id, human_owner_id=user.id, node_type="llm",
                llm_model="m", tool_calls_meta=json.dumps([{"name": "_client"}]))
    db.session.add(node)
    for step in range(5):
        node.tool_calls_meta = json.dumps(RECORD[:1] * (step + 1))
    assert keys.wraps == 1
    node.set_content("The answer.")
    node.tool_calls_meta = RECORD_TEXT
    db.session.commit()
    assert (keys.wraps, keys.unwraps) == (2, 0)
    db.session.expire_all()
    keys.forget()
    node = Node.query.get(node.id)
    assert (node.get_content(), node.tool_calls_meta) == (
        "The answer.", RECORD_TEXT)
    assert keys.unwraps == 1


# -- the script for earlier rows ---------------------------------------------

def _script():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "scripts", "store_tool_records_encrypted.py")
    spec = importlib.util.spec_from_file_location("_store_tool_records", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _earlier_rows(user, count):
    ids = []
    for i in range(count):
        node = _reply(user, record=None, content=f"Reply {i}.")
        _store_as_before(node.id, json.dumps(
            RECORD + [{"name": "n", "i": i}]))
        ids.append(node.id)
    return ids


def test_the_script_counts_without_writing_by_default(app):
    script = _script()
    ids = _earlier_rows(_user(), 3)
    before = [_stored(i) for i in ids]
    assert script.survey() == (3, 0)
    counts = script.convert(pause=0)
    assert counts["found"] == 3 and counts["converted"] == 0
    assert [_stored(i) for i in ids] == before


def test_the_script_converts_in_batches_with_one_key_each(app, keys):
    script = _script()
    user = _user()
    ids = _earlier_rows(user, 5)
    already = _reply(user)
    texts = {i: Node.query.get(i).tool_calls_meta for i in ids}
    held = {i: Node.query.get(i).content for i in ids}
    stamps = {i: Node.query.get(i).updated_at for i in ids}
    kept = _stored(already.id)
    db.session.expire_all()
    keys.forget()

    counts = script.convert(apply=True, batch_size=2, pause=0)

    assert counts["converted"] == 5 and counts["changed_meanwhile"] == 0
    assert (keys.wraps, keys.unwraps) == (3, 0)     # 3 batches, no reads
    assert script.survey() == (0, 6)
    assert _stored(already.id) == kept
    db.session.expire_all()
    for i in ids:
        for words in PRIVATE_WORDS:
            assert words not in _stored(i)
        node = Node.query.get(i)
        assert node.tool_calls_meta == texts[i]
        assert node.content == held[i]              # the text: untouched
        assert node.updated_at == stamps[i]
    # A second run finds nothing.
    assert script.convert(apply=True, pause=0)["found"] == 0


def test_the_script_continues_after_a_stop(app):
    script = _script()
    ids = _earlier_rows(_user(), 5)
    first = script.convert(apply=True, batch_size=2, limit=2, pause=0)
    assert first["converted"] == 2 and first["last_id"] == ids[1]
    assert script.survey() == (3, 2)
    rest = script.convert(apply=True, batch_size=2,
                          after_id=first["last_id"], pause=0)
    assert rest["converted"] == 3
    assert script.survey() == (0, 5)


def test_a_record_the_app_changed_meanwhile_keeps_the_apps_write(
        app, monkeypatch):
    script = _script()
    (node_id,) = _earlier_rows(_user(), 1)
    newer = json.dumps([{"name": "search", "status": "error"}])
    real = script.tool_record.store

    def store_after_the_app_wrote(value, **kw):
        db.session.execute(
            text("UPDATE node SET tool_calls_meta = :v WHERE id = :i"),
            {"v": newer, "i": node_id})
        return real(value, **kw)

    monkeypatch.setattr(script.tool_record, "store",
                        store_after_the_app_wrote)
    counts = script.convert(apply=True, pause=0)
    assert counts["converted"] == 0 and counts["changed_meanwhile"] == 1
    db.session.expire_all()
    assert _stored(node_id) == newer


def test_the_script_can_bring_the_earlier_form_back(app):
    script = _script()
    ids = _earlier_rows(_user(), 3)
    before = {i: _stored(i) for i in ids}
    script.convert(apply=True, batch_size=2, pause=0)
    assert script.undo(pause=0)["converted"] == 0      # dry run
    counts = script.undo(apply=True, batch_size=2, pause=0)
    assert counts["converted"] == 3
    assert {i: _stored(i) for i in ids} == before
