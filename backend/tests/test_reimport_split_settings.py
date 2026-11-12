"""A re-import applies its settings to every entry of a split item (#638).

A Word or PDF document becomes one entry per chapter, and a file or a
message above the per-entry cap a chain of parts; the dedup key sits on
the last entry only. A re-import with another AI usage changed only that
last entry, so after a re-import with None the earlier chapters stayed
readable to AI. Every chapter and part now takes the re-import's
settings, and the profile hand-off counts all of them.

Only entries certainly part of the item change. An item imported since
#638 is the entries sharing its split_item token. One imported before it
("legacy" below, simulated by clearing the tokens) is proven by the exact
dates its import wrote (parts 1 ms apart, chapters 1 s apart), walking up
from the keyed entry and stopping at the first entry that fails. Entries
around the item (an unrelated entry above it, the document before it in
the same thread, a copy whose key was given up, the user's own replies)
keep their settings.

Every text is synthetic.
"""
import io
from datetime import datetime, timedelta

import pytest

from backend.tests.test_chatgpt_import import (  # noqa: F401
    app, _db, _login, _make_user)
from backend.tests.test_docx_import import _thesis
from backend.tests.test_pdf_import import _paper
from backend.tests.test_reimport_profile_handoff import (  # noqa: F401
    hand_offs, _import_conversations)
from backend.models import Node


@pytest.fixture(autouse=True)
def _no_profile_jobs(hand_offs):  # noqa: F811
    """Every import here records its profile hand-off instead of
    starting one."""
    return hand_offs


def _user(app, name):
    u = _make_user(name)
    u.default_ai_usage = "chat"
    _db.session.commit()
    client = app.test_client()
    _login(client, u.id)
    return u, client


def _analyze(client, name, data):
    r = client.post("/api/import/analyze",
                    data={"zip_file": (io.BytesIO(data), name)},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["files"][0]


def _confirm(client, files, ai_usage, **kw):
    body = {"files": files, "import_type": "separate_nodes",
            "privacy_level": "private", "ai_usage": ai_usage, **kw}
    return client.post("/api/import/confirm", json=body)


def _entries(user):
    return (Node.query.filter_by(human_owner_id=user.id)
            .order_by(Node.id).all())


def _usages(nodes):
    return [n.ai_usage for n in nodes]


def _make_legacy(user):
    """The entries as an import from before the split_item token left
    them."""
    for n in _entries(user):
        n.split_item = None
    _db.session.commit()


def _imported(user, legacy):
    if legacy:
        _make_legacy(user)


def _unrelated(user, ai_usage, origin="docx", **kw):
    """An entry of the same author, type and origin, but no part of any
    document imported here."""
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             ai_usage=ai_usage, origin=origin, **kw)
    n.set_content("synthetic unrelated entry")
    _db.session.add(n)
    _db.session.commit()
    return n


CHANGES = [("none", "chat"), ("chat", "none")]
LEGACY = [pytest.param(False, id="token"), pytest.param(True, id="legacy")]


# ── every chapter of a Word or PDF document ──────────────────────────────

@pytest.mark.parametrize("legacy", LEGACY)
@pytest.mark.parametrize("before,after", CHANGES)
def test_word_reimport_changes_every_chapter(app, before, after, legacy):
    u, client = _user(app, f"word_{before}_{legacy}")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    assert _confirm(client, [doc], before).status_code == 201
    nodes = _entries(u)
    assert len(nodes) == 4  # the text before chapter 1, then 3 chapters
    assert _usages(nodes) == [before] * 4
    _imported(u, legacy)

    body = _confirm(client, [doc], after).get_json()
    assert body["created"] == 0
    assert body["updated"] == 1 and body["skipped"] == 0  # one document
    _db.session.expire_all()
    assert _usages(_entries(u)) == [after] * 4


@pytest.mark.parametrize("legacy", LEGACY)
@pytest.mark.parametrize("before,after", CHANGES)
def test_pdf_reimport_changes_every_chapter(app, before, after, legacy):
    u, client = _user(app, f"pdf_{before}_{legacy}")
    doc = _analyze(client, "Paper.pdf", _paper())
    assert _confirm(client, [doc], before,
                    whose_writing="mine").status_code == 201
    assert len(_entries(u)) == 4  # the title page's text, then 3 chapters
    _imported(u, legacy)

    body = _confirm(client, [doc], after, whose_writing="mine").get_json()
    assert body["updated"] == 1 and body["skipped"] == 0
    _db.session.expire_all()
    assert _usages(_entries(u)) == [after] * 4


@pytest.mark.parametrize("legacy", LEGACY)
def test_word_reimport_in_one_thread_changes_every_chapter(app, legacy):
    u, client = _user(app, f"word_thread_{legacy}")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat", import_type="single_thread")
    _imported(u, legacy)
    _confirm(client, [doc], "none", import_type="single_thread")
    _db.session.expire_all()
    assert _usages(_entries(u)) == ["none"] * 4


def test_a_document_s_entries_share_one_token(app):
    u, client = _user(app, "tokens")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    note = {"filename_without_ext": "short note", "content": "synthetic",
            "modified_at": "2022-05-01T12:00:00"}
    _confirm(client, [doc, note], "chat")
    nodes = _entries(u)
    tokens = {n.split_item for n in nodes[:4]}
    assert len(tokens) == 1 and None not in tokens
    assert nodes[4].split_item is None  # a file saved as one entry


# ── every part of a file or message above the per-entry cap ─────────────

def _long_text(word):
    # Above the 100,000-character cap: 2 parts, split at newlines
    return f"synthetic {word} line\n" * 6000


@pytest.mark.parametrize("legacy", LEGACY)
@pytest.mark.parametrize("before,after", CHANGES)
def test_long_markdown_file_reimport_changes_every_part(
        app, before, after, legacy):
    u, client = _user(app, f"long_md_{before}_{legacy}")
    f = {"filename_without_ext": "long note", "content": _long_text("note"),
         "modified_at": "2022-05-01T12:00:00"}
    _confirm(client, [f], before)
    assert len(_entries(u)) == 2
    _imported(u, legacy)

    body = _confirm(client, [f], after).get_json()
    assert body["updated"] == 1 and body["skipped"] == 0
    _db.session.expire_all()
    assert _usages(_entries(u)) == [after] * 2


def _long_conversation():
    return {
        "name": "Synthetic long chat",
        "created_at": "2022-02-01T10:00:00",
        "messages": [
            {"text": _long_text("question"), "role": "user",
             "created_at": "2022-02-01T10:00:00", "model": "",
             "mapping_id": "long-u"},
            {"text": "synthetic answer", "role": "assistant",
             "created_at": "2022-02-01T10:00:05", "model": "gpt-4o",
             "mapping_id": "long-a"},
        ],
        "message_count": 2,
    }


@pytest.mark.parametrize("legacy", LEGACY)
@pytest.mark.parametrize("before,after", CHANGES)
def test_long_chatgpt_message_reimport_changes_every_part(
        app, before, after, legacy):
    u, _client = _user(app, f"long_msg_{before}_{legacy}")
    _import_conversations(u.id, [_long_conversation()], before)
    nodes = _entries(u)
    assert len(nodes) == 3  # the question in 2 parts, the answer
    assert nodes[0].split_item and nodes[0].split_item == nodes[1].split_item
    _imported(u, legacy)

    result = _import_conversations(u.id, [_long_conversation()], after)
    assert result["updated"] == 2 and result["skipped"] == 0
    _db.session.expire_all()
    assert _usages(_entries(u)) == [after] * 3


# ── entries that are not part of the document keep their settings ───────

@pytest.mark.parametrize("legacy", LEGACY)
def test_unrelated_entry_above_the_document_keeps_its_setting(app, legacy):
    """An entry of the same author, type and origin, with no key, right
    above the document's first chapter."""
    u, client = _user(app, f"unrelated_above_{legacy}")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "none")
    first = _entries(u)[0]
    other = _unrelated(u, "none")
    first.parent_id = other.id
    _db.session.commit()
    _imported(u, legacy)

    _confirm(client, [doc], "chat")
    _db.session.expire_all()
    assert Node.query.get(other.id).ai_usage == "none"
    assert [n.ai_usage for n in _entries(u) if n.id != other.id] == [
        "chat"] * 4


@pytest.mark.parametrize("legacy", LEGACY)
def test_released_copy_above_the_document_keeps_its_setting(app, legacy):
    """Two documents in one thread, the second under the first one's last
    chapter. That chapter is deleted and the first document restored, so
    the old chapter gives up its key (#521). A re-import of the second
    alone does not reach the first one's old chapters."""
    u, client = _user(app, f"released_above_{legacy}")
    first = _analyze(client, "First.docx", _thesis(2))
    second = dict(_analyze(client, "Second.docx", _thesis(3)),
                  modified_at="2022-01-01T00:00:00")
    _confirm(client, [first, second], "none", import_type="single_thread")
    old_first = _entries(u)[:3]
    old_first[-1].deleted_at = datetime.utcnow()
    _db.session.commit()
    _imported(u, legacy)
    _confirm(client, [first], "none", import_type="single_thread",
             on_deleted="restore")
    assert Node.query.get(old_first[-1].id).source_key is None

    body = _confirm(client, [second], "chat",
                    import_type="single_thread").get_json()
    assert body["updated"] == 1
    _db.session.expire_all()
    assert _usages(Node.query.get(n.id) for n in old_first[:2]) == [
        "none"] * 2
    second_entries = [n for n in _entries(u)
                      if n.created_at >= datetime(2022, 1, 1)]
    assert _usages(second_entries) == ["chat"] * 4


@pytest.mark.parametrize("legacy", LEGACY)
def test_document_above_without_a_key_keeps_its_setting(app, legacy):
    """Two documents in one thread, the second under the first; the
    first one's last chapter holds no key. A re-import of the second
    changes its own chapters only."""
    u, client = _user(app, f"two_docs_{legacy}")
    first = _analyze(client, "First.docx", _thesis(2))
    second = dict(_analyze(client, "Second.docx", _thesis(3)),
                  modified_at="2022-01-01T00:00:00")
    _confirm(client, [first, second], "none", import_type="single_thread")
    nodes = _entries(u)
    assert len(nodes) == 3 + 4
    nodes[2].source_key = None
    _db.session.commit()
    _imported(u, legacy)

    body = _confirm(client, [second], "chat",
                    import_type="single_thread").get_json()
    assert body["updated"] == 1
    _db.session.expire_all()
    assert _usages(_entries(u)) == ["none"] * 3 + ["chat"] * 4


@pytest.mark.parametrize("legacy", LEGACY)
def test_reimport_of_one_document_leaves_the_document_before_it(app, legacy):
    """The same with the first document's key in place."""
    u, client = _user(app, f"two_keyed_docs_{legacy}")
    first = _analyze(client, "First.docx", _thesis(2))
    second = dict(_analyze(client, "Second.docx", _thesis(3)),
                  modified_at="2022-01-01T00:00:00")
    _confirm(client, [first, second], "none", import_type="single_thread")
    _imported(u, legacy)

    _confirm(client, [second], "chat", import_type="single_thread")
    _db.session.expire_all()
    assert _usages(_entries(u)) == ["none"] * 3 + ["chat"] * 4


@pytest.mark.parametrize("legacy", LEGACY)
def test_own_reply_under_a_chapter_keeps_its_setting(app, legacy):
    """An entry the user wrote under a chapter is not part of the
    document: every chapter changes, the reply does not."""
    u, client = _user(app, f"own_reply_{legacy}")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    chapter = _entries(u)[2]
    reply = Node(user_id=u.id, human_owner_id=u.id, parent_id=chapter.id,
                 node_type="user", ai_usage="chat")
    reply.set_content("synthetic reply")
    _db.session.add(reply)
    _db.session.commit()
    _imported(u, legacy)

    _confirm(client, [doc], "none")
    _db.session.expire_all()
    assert Node.query.get(reply.id).ai_usage == "chat"
    assert [n.ai_usage for n in _entries(u) if n.id != reply.id] == [
        "none"] * 4


# ── a deleted chapter ────────────────────────────────────────────────────

def _delete(node):
    node.deleted_at = datetime.utcnow()
    _db.session.commit()
    return node


@pytest.mark.parametrize("legacy", LEGACY)
def test_chapters_before_a_deleted_last_chapter_change(app, legacy):
    """The key sits on the deleted last chapter; "skip" leaves it deleted,
    and the chapters before it, still in Loore, take the setting."""
    u, client = _user(app, f"deleted_last_skip_{legacy}")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    last = _delete(_entries(u)[-1])
    _imported(u, legacy)

    body = _confirm(client, [doc], "none", on_deleted="skip").get_json()
    assert body["updated"] == 1 and body["skipped"] == 0
    _db.session.expire_all()
    alive = [n for n in _entries(u) if n.deleted_at is None]
    assert _usages(alive) == ["none"] * 3
    assert Node.query.get(last.id).deleted_at is not None


def test_restore_of_a_deleted_last_chapter_changes_the_old_chapters(app):
    """"restore" imports the document again in full (#521); the old
    chapters still in Loore, named by the old copy's token, take the
    setting as well."""
    u, client = _user(app, "deleted_last_restore")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    _delete(_entries(u)[-1])

    body = _confirm(client, [doc], "none", on_deleted="restore").get_json()
    assert body["created"] == 4
    _db.session.expire_all()
    alive = [n for n in _entries(u) if n.deleted_at is None]
    assert len(alive) == 3 + 4
    assert _usages(alive) == ["none"] * 7


# ── what an import from before the token cannot prove stays as it is ────

def test_legacy_restore_leaves_the_old_chapters(app):
    """Without a token nothing walks up from a key that was given up.
    Control: main leaves them too (the first version of this fix did
    not)."""
    u, client = _user(app, "legacy_restore")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    _delete(_entries(u)[-1])
    _make_legacy(u)

    _confirm(client, [doc], "none", on_deleted="restore")
    _db.session.expire_all()
    alive = [n for n in _entries(u) if n.deleted_at is None]
    assert _usages(alive) == ["chat"] * 3 + ["none"] * 4


def test_legacy_walk_stops_at_a_deleted_chapter(app):
    """Control: main changes only the last chapter as well (the first
    version of this fix walked through the deleted one)."""
    u, client = _user(app, "legacy_deleted_middle")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    nodes = _entries(u)
    _delete(nodes[2])
    _make_legacy(u)

    _confirm(client, [doc], "none")
    _db.session.expire_all()
    assert _usages(Node.query.get(n.id) for n in (nodes[0], nodes[1],
                                                  nodes[3])) == [
        "chat", "chat", "none"]


def test_legacy_walk_stops_where_the_dates_do_not_prove_a_chapter(app):
    """A chapter dated one second and a moment before the next one is not
    proven to be the chapter before it."""
    u, client = _user(app, "legacy_dates")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    nodes = _entries(u)
    nodes[1].created_at -= timedelta(microseconds=1)
    _db.session.commit()
    _make_legacy(u)

    _confirm(client, [doc], "none")
    _db.session.expire_all()
    assert _usages(_entries(u)) == ["chat", "chat", "none", "none"]


# ── the user's own change of one chapter ─────────────────────────────────

def test_a_chapter_the_user_changed_takes_the_reimport_setting(app):
    """The dialog's value applies to every entry of the archive already in
    Loore (Peter, 2026-11-10, #593), as it does for an entry not split."""
    u, client = _user(app, "chapter_edit")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    chapter = _entries(u)[2]
    chapter.ai_usage = "none"
    _db.session.commit()

    body = _confirm(client, [doc], "chat").get_json()
    assert body["updated"] == 1
    _db.session.expire_all()
    assert _usages(_entries(u)) == ["chat"] * 4


# ── the profile hand-off counts every chapter ────────────────────────────

def test_none_to_chat_hands_off_from_the_first_chapter(app, hand_offs):
    u, client = _user(app, "word_hand_off")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "none")
    assert hand_offs == []
    _confirm(client, [doc], "chat")
    nodes = _entries(u)
    assert [(uid, ts) for uid, ts, _t in hand_offs] == [
        (u.id, nodes[0].created_at)]
    assert hand_offs[0][2] == sum(n.token_count for n in nodes)


# ── the request's settings are checked ───────────────────────────────────

BAD_SETTINGS = [
    pytest.param({"ai_usage": "everything"}, "ai_usage", id="ai_usage"),
    pytest.param({"ai_usage": ["chat"]}, "ai_usage", id="ai_usage-list"),
    pytest.param({"privacy_level": "world"}, "privacy_level", id="privacy"),
]


@pytest.mark.parametrize("bad,field", BAD_SETTINGS)
def test_file_import_refuses_unknown_settings(app, bad, field):
    u, client = _user(app, f"bad_files_{field}_{len(str(bad))}")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "none")
    r = client.post("/api/import/confirm", json={
        "files": [doc], "import_type": "separate_nodes",
        "privacy_level": "private", "ai_usage": "chat", **bad})
    assert r.status_code == 400
    assert r.get_json()["error"] == f"Invalid {field}"
    _db.session.expire_all()
    assert {(n.ai_usage, n.privacy_level) for n in _entries(u)} == {
        ("none", "private")}


@pytest.mark.parametrize("route", ["twitter", "chatgpt", "claude"])
@pytest.mark.parametrize("bad,field", BAD_SETTINGS)
def test_archive_imports_refuse_unknown_settings(app, route, bad, field):
    _u, client = _user(app, f"bad_{route}_{field}_{len(str(bad))}")
    r = client.post(f"/api/import/{route}/confirm", json={
        "import_token": "synthetic-token", "import_type": "separate_nodes",
        "privacy_level": "private", "ai_usage": "chat", **bad})
    assert r.status_code == 400
    assert r.get_json()["error"] == f"Invalid {field}"
