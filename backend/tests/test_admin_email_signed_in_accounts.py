"""The admin sets a sign-in address only on an account nobody has signed
into (PUT /api/admin/users/<id>/update_email; activity.sign_in_status).

An account someone has used, or one older than the sign-in record, keeps
its address: the route answers 409 ``account_signed_in`` with a text the
admin panel shows, and only the owner's own confirmed change (Account
settings) moves it. A whitelisted or pre-filled account before its
owner's first sign-in still takes the address the admin vouches for, so
Activate & Welcome can reach the person. Uses the fixtures of
test_email_change.py (sqlite in memory, mail captured below the senders).
"""
import logging
import re
from datetime import datetime

from flask import g

from backend.extensions import db as _db
from backend.models import Node
from backend.tests.test_email_change import (  # noqa: F401
    _anonymous, _client, _request, _session_user_id, _token, _user, app,
    mails, real_app)

TEXT = "an entry of alice's own"


def _used(name, with_entry=True):
    """The account as it is once its owner has signed in and written."""
    user = _user(name)
    user.last_seen_at = datetime.utcnow()
    user.accepted_terms_at = datetime.utcnow()
    node_id = None
    if with_entry:
        node = Node(user_id=user.id, human_owner_id=user.id,
                    node_type="user", privacy_level="private")
        node.set_content(TEXT)
        _db.session.add(node)
    _db.session.commit()
    if with_entry:
        node_id = node.id
    return user.id, node_id


def _set_email(app, user_id, email):
    return _client(app, "root").put(
        f"/api/admin/users/{user_id}/update_email", json={"email": email})


def test_an_account_in_use_keeps_its_address(real_app, mails):  # noqa: F811
    from backend.routes.admin import EMAIL_SET_REFUSED_MESSAGE
    alice_id, node_id = _used("alice")

    r = _set_email(real_app, alice_id, "someone.else@example.com")

    assert r.status_code == 409
    assert r.get_json() == {"error": EMAIL_SET_REFUSED_MESSAGE,
                            "code": "account_signed_in"}
    _db.session.expire_all()
    assert _user("alice").email == "alice@example.com"
    # A sign-in link asked for the other address opens no session as alice.
    outsider = _anonymous(real_app)
    assert outsider.post("/auth/magic-link/send", json={
        "email": "someone.else@example.com"}).status_code == 200
    for mail in [m for m in mails if m["to"] == "someone.else@example.com"]:
        token = re.search(r"verify\?token=(\S+)", mail["text"]).group(1)
        g.pop("_login_user", None)
        outsider.get(f"/auth/magic-link/verify?token={token}")
        assert _session_user_id(outsider) != str(alice_id)
    g.pop("_login_user", None)
    assert outsider.get(f"/api/nodes/{node_id}").status_code in (401, 403,
                                                                 404)


def test_clearing_the_address_of_an_account_in_use_is_refused(app):  # noqa: F811
    alice_id, _ = _used("alice", with_entry=False)
    assert _set_email(app, alice_id, "").status_code == 409
    _db.session.expire_all()
    assert _user("alice").email == "alice@example.com"


def test_an_x_only_account_in_use_gets_no_address(real_app, mails):  # noqa: F811
    xonly_id, _ = _used("xonly")

    assert _set_email(real_app, xonly_id,
                      "someone.else@example.com").status_code == 409

    _db.session.expire_all()
    assert _user("xonly").email is None
    g.pop("_login_user", None)
    r = _client(real_app, "root").post(
        f"/api/admin/users/{xonly_id}/activate_and_welcome")
    assert r.status_code == 400
    assert mails == []


def test_an_account_older_than_the_sign_in_record_is_refused(app):  # noqa: F811
    bob = _user("bob")
    bob.created_at = datetime(2026, 1, 15)   # before last_seen_at existed
    _db.session.commit()
    assert _set_email(app, bob.id, "bob.new@example.com").status_code == 409
    _db.session.expire_all()
    assert _user("bob").email == "bob@example.com"


def test_the_owners_pending_change_survives_a_refused_set(app, mails):  # noqa: F811
    c = _client(app, "alice")
    assert _request(c, "alice.new@example.com").status_code == 200
    token, _ = _token(mails)
    alice_id, _ = _used("alice", with_entry=False)

    assert _set_email(app, alice_id, "someone.else@example.com"
                      ).status_code == 409

    _db.session.expire_all()
    assert _user("alice").pending_email == "alice.new@example.com"
    g.pop("_login_user", None)
    r = _client(app, "alice").post("/api/dashboard/email/confirm",
                                   json={"token": token})
    assert r.status_code == 200, r.get_json()
    _db.session.expire_all()
    assert _user("alice").email == "alice.new@example.com"


def test_an_account_nobody_signed_into_takes_the_address(app, caplog):  # noqa: F811
    xonly = _user("xonly")   # whitelisted ahead of its owner's sign-in
    root_id = _user("root").id

    with caplog.at_level(logging.WARNING, logger="backend.routes.admin"):
        r = _set_email(app, xonly.id, " Owner@Example.com ")

    assert r.status_code == 200, r.get_json()
    assert r.get_json()["email"] == "owner@example.com"
    lines = [rec.getMessage() for rec in caplog.records
             if rec.name == "backend.routes.admin"]
    assert f"Admin {root_id} set the email of user {xonly.id}" in lines
    assert not any("example.com" in line for line in lines)
