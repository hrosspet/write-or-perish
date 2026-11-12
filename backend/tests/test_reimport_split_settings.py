"""A re-import applies its settings to every entry of a split item (#638).

A Word or PDF document becomes one entry per chapter, and a file or a
message above the per-entry cap a chain of parts; the dedup key sits on
the last entry only. A re-import with another AI usage changed only that
last entry, so after a re-import with None the earlier chapters stayed
readable to AI. Every chapter and part now takes the re-import's
settings, also when the last one was deleted, and the profile hand-off
counts all of them. The entries around the item (the document before it
in the same thread, the user's own replies) keep theirs.

Every text is synthetic.
"""
import io
from datetime import datetime

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


CHANGES = [("none", "chat"), ("chat", "none")]


# ── every chapter of a Word or PDF document ──────────────────────────────

@pytest.mark.parametrize("before,after", CHANGES)
def test_word_reimport_changes_every_chapter(app, before, after):
    u, client = _user(app, f"word_{before}")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    assert _confirm(client, [doc], before).status_code == 201
    nodes = _entries(u)
    assert len(nodes) == 4  # the text before chapter 1, then 3 chapters
    assert _usages(nodes) == [before] * 4

    body = _confirm(client, [doc], after).get_json()
    assert body["created"] == 0
    assert body["updated"] == 1 and body["skipped"] == 0  # one document
    _db.session.expire_all()
    assert _usages(_entries(u)) == [after] * 4


@pytest.mark.parametrize("before,after", CHANGES)
def test_pdf_reimport_changes_every_chapter(app, before, after):
    u, client = _user(app, f"pdf_{before}")
    doc = _analyze(client, "Paper.pdf", _paper())
    assert _confirm(client, [doc], before,
                    whose_writing="mine").status_code == 201
    assert len(_entries(u)) == 4  # the title page's text, then 3 chapters

    body = _confirm(client, [doc], after, whose_writing="mine").get_json()
    assert body["updated"] == 1 and body["skipped"] == 0
    _db.session.expire_all()
    assert _usages(_entries(u)) == [after] * 4


def test_word_reimport_in_one_thread_changes_every_chapter(app):
    u, client = _user(app, "word_thread")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat", import_type="single_thread")
    _confirm(client, [doc], "none", import_type="single_thread")
    _db.session.expire_all()
    assert _usages(_entries(u)) == ["none"] * 4


# ── every part of a file or message above the per-entry cap ─────────────

def _long_text(word):
    # Above the 100,000-character cap: 2 parts, split at newlines
    return f"synthetic {word} line\n" * 6000


@pytest.mark.parametrize("before,after", CHANGES)
def test_long_markdown_file_reimport_changes_every_part(app, before, after):
    u, client = _user(app, f"long_md_{before}")
    f = {"filename_without_ext": "long note", "content": _long_text("note"),
         "modified_at": "2022-05-01T12:00:00"}
    _confirm(client, [f], before)
    assert len(_entries(u)) == 2

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


@pytest.mark.parametrize("before,after", CHANGES)
def test_long_chatgpt_message_reimport_changes_every_part(
        app, hand_offs, before, after):
    u, _client = _user(app, f"long_msg_{before}")
    _import_conversations(u.id, [_long_conversation()], before)
    nodes = _entries(u)
    assert len(nodes) == 3  # the question in 2 parts, the answer

    result = _import_conversations(u.id, [_long_conversation()], after)
    assert result["updated"] == 2 and result["skipped"] == 0
    _db.session.expire_all()
    assert _usages(_entries(u)) == [after] * 3


# ── what lies around the item keeps its settings ─────────────────────────

def test_reimport_of_one_document_leaves_the_document_before_it(app):
    """Two documents imported into one thread: the second's chapters hang
    under the first one's last chapter. A re-import of the second alone
    changes all of its chapters and stops at the first one's key."""
    u, client = _user(app, "two_docs")
    first = _analyze(client, "First.docx", _thesis(2))
    second = dict(_analyze(client, "Second.docx", _thesis(3)),
                  modified_at="2022-01-01T00:00:00")
    _confirm(client, [first, second], "none", import_type="single_thread")
    nodes = _entries(u)
    assert len(nodes) == 3 + 4

    body = _confirm(client, [second], "chat",
                    import_type="single_thread").get_json()
    assert body["updated"] == 1
    _db.session.expire_all()
    assert _usages(_entries(u)) == ["none"] * 3 + ["chat"] * 4


def test_own_reply_under_a_chapter_keeps_its_setting(app):
    """An entry the user wrote under a chapter is not part of the
    document: every chapter changes, the reply does not."""
    u, client = _user(app, "own_reply")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    chapter = _entries(u)[2]
    reply = Node(user_id=u.id, human_owner_id=u.id, parent_id=chapter.id,
                 node_type="user", ai_usage="chat")
    reply.set_content("synthetic reply")
    _db.session.add(reply)
    _db.session.commit()

    _confirm(client, [doc], "none")
    _db.session.expire_all()
    assert Node.query.get(reply.id).ai_usage == "chat"
    assert [n.ai_usage for n in _entries(u) if n.id != reply.id] == [
        "none"] * 4


# ── the last chapter deleted ─────────────────────────────────────────────

def _delete_last_chapter(user):
    last = _entries(user)[-1]
    last.deleted_at = datetime.utcnow()
    _db.session.commit()
    return last


def test_chapters_before_a_deleted_last_chapter_change(app):
    """The key sits on the deleted last chapter; "skip" leaves it deleted,
    and the chapters before it, still in Loore, take the setting."""
    u, client = _user(app, "deleted_last_skip")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    last = _delete_last_chapter(u)

    body = _confirm(client, [doc], "none", on_deleted="skip").get_json()
    assert body["updated"] == 1 and body["skipped"] == 0
    _db.session.expire_all()
    alive = [n for n in _entries(u) if n.deleted_at is None]
    assert _usages(alive) == ["none"] * 3
    assert Node.query.get(last.id).deleted_at is not None


def test_restore_of_a_deleted_last_chapter_changes_the_old_chapters(app):
    """"restore" imports the document again in full (#521); the old
    chapters still in Loore take the setting as well."""
    u, client = _user(app, "deleted_last_restore")
    doc = _analyze(client, "Thesis.docx", _thesis(3))
    _confirm(client, [doc], "chat")
    _delete_last_chapter(u)

    body = _confirm(client, [doc], "none", on_deleted="restore").get_json()
    assert body["created"] == 4
    _db.session.expire_all()
    alive = [n for n in _entries(u) if n.deleted_at is None]
    assert len(alive) == 3 + 4
    assert _usages(alive) == ["none"] * 7


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
