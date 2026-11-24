"""The author names of imported ChatGPT / Claude replies are reserved,
and an import finds its author account by twitter_id ("llm-chatgpt"),
never by username: replies are stored under that account even when a
person's account holds the name.

Real create_app (fixture from test_error_text_privacy), sqlite in memory.
"""
from flask import g

from backend.extensions import db
from backend.models import Node, User
from backend.tests.test_error_text_privacy import app  # noqa: F401

CONV = {"name": "c", "created_at": "2026-01-01T00:00:00",
        "messages": [
            {"role": "user", "text": "my question",
             "mapping_id": "m1", "created_at": "2026-01-01T00:00:00"},
            {"role": "assistant", "text": "the private answer",
             "mapping_id": "m2", "created_at": "2026-01-01T00:00:01",
             "model": "gpt-5"},
        ]}


def _make_user(username, **kwargs):
    u = User(username=username, approved=True, plan="alpha", **kwargs)
    db.session.add(u)
    db.session.commit()
    return u


def _client(app, user_id):
    g.pop("_login_user", None)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return c


def _import(user_id, conv=CONV):
    from backend.routes.import_data import create_conversation_nodes
    create_conversation_nodes(user_id, "chatgpt", [(0, conv)],
                              "private", "chat", None)


def test_the_names_cannot_be_taken(app):
    from backend.utils.magic_link import generate_unique_username
    from backend.utils.reserved_usernames import validate_username
    someone = _make_user("someone")
    c = _client(app, someone.id)
    for name in ("chatgpt", "ChatGPT", "chat_gpt", "claude_web"):
        r = c.put("/api/dashboard/user", json={"username": name})
        assert r.status_code == 400, name
        assert r.get_json()["error"] == "That username is reserved."
    assert generate_unique_username("chatgpt@example.com") == "chatgpt2"
    assert validate_username("claude-web") is not None


def test_import_while_a_person_holds_the_name(app):
    holder = _make_user("chatgpt")       # took the name before it was reserved
    importer = _make_user("importer")
    holder_id, importer_id = holder.id, importer.id
    _import(importer_id)
    reply = Node.query.filter_by(node_type="llm").one()
    author = db.session.get(User, reply.user_id)
    assert author.twitter_id == "llm-chatgpt"
    assert author.id != holder_id
    assert author.username == "chatgpt-import"
    assert reply.human_owner_id == importer_id
    r = _client(app, holder_id).get(f"/api/nodes/{reply.id}")
    assert r.status_code in (403, 404)
    assert db.session.get(User, holder_id).username == "chatgpt"

    # The next import finds the same author account.
    later = dict(CONV, messages=[
        dict(m, mapping_id=m["mapping_id"] + "b") for m in CONV["messages"]])
    _import(importer_id, later)
    authors = {n.user_id for n in Node.query.filter_by(node_type="llm")}
    assert authors == {author.id}


def test_import_uses_the_existing_author_account(app):
    existing = _make_user("chatgpt", twitter_id="llm-chatgpt")
    importer = _make_user("importer")
    _import(importer.id)
    reply = Node.query.filter_by(node_type="llm").one()
    assert reply.user_id == existing.id
    assert User.query.filter_by(twitter_id="llm-chatgpt").count() == 1


def test_first_import_makes_the_author_under_the_sources_name(app):
    importer = _make_user("importer")
    _import(importer.id)
    author = User.query.filter_by(twitter_id="llm-chatgpt").one()
    assert author.username == "chatgpt"
