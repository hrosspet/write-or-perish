"""Tests for account deletion (#269): backend/utils/account_deletion.py,
the self-service, restore and admin endpoints, and the hidden state
during the grace period.

Reuses the fixture world of test_user_purge.py (#268): alice has a row in
every table the purge touches, bob is the control. sqlite in-memory with
foreign keys ON, so deleting the user row while anything still points at
it fails here as it would on Postgres. Celery, provider batches and mail
delivery are stubbed; no model API is called.
"""
import re
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from flask import Flask

from backend.tests.test_user_purge import (  # noqa: F401 - fixtures
    T0, _add, _bob_snapshot, _client, _db, _node, _real_backend_models,
    _real_flask_login, stubs, world,
)
from backend.models import (
    ApiToken, Draft, Node, Poll, ReleasedUsername,
    ShareDraft, User, UserDataPurge, UsernameHistory,
)
from backend.utils import account_deletion as acc
from backend.utils import user_purge as up
from backend.utils.system_accounts import ERASED_SYSTEM_USERNAME

FRONTEND = "https://app.test"


def _make_app():
    from flask_login import LoginManager

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["FRONTEND_URL"] = FRONTEND
    app.config["SHARE_V1"] = True
    app.config["FRONTEND_BUILD_DIR"] = None
    app.config["PUBLIC_BASE_URL"] = "https://loore.org"
    _db.init_app(app)
    lm = LoginManager(app)
    # The production loader (backend/__init__.py uses the same function).
    lm.user_loader(acc.session_user)

    from backend.routes.account_data import account_data_bp
    from backend.routes.account_deletion import account_deletion_bp
    from backend.routes.admin import admin_bp
    from backend.routes.auth import auth_bp
    from backend.routes.commons import commons_bp
    from backend.routes.dashboard import dashboard_bp
    from backend.routes.log import log_bp
    app.register_blueprint(dashboard_bp, url_prefix="/api/dashboard")
    from backend.routes.public_pages import public_pages_bp
    app.register_blueprint(log_bp, url_prefix="/api")
    app.register_blueprint(account_data_bp, url_prefix="/api/account")
    app.register_blueprint(account_deletion_bp, url_prefix="/api/account")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(commons_bp, url_prefix="/api/commons")
    app.register_blueprint(public_pages_bp)
    return app


@pytest.fixture
def app():
    affected = lambda k: (  # noqa: E731
        k == "flask_login" or k.startswith("backend.routes")
        or k == "backend.models")
    saved = {k: sys.modules[k] for k in list(sys.modules) if affected(k)}
    sys.modules["flask_login"] = _real_flask_login
    sys.modules["backend.models"] = _real_backend_models
    for k in [k for k in list(sys.modules) if k.startswith("backend.routes")]:
        del sys.modules[k]

    app = _make_app()
    with app.app_context():
        with _db.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        _db.create_all()
        yield app
        _db.session.remove()
        with _db.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        _db.drop_all()

    for k in [k for k in list(sys.modules) if affected(k)]:
        if k not in saved:
            del sys.modules[k]
    for k, mod in saved.items():
        sys.modules[k] = mod


@pytest.fixture
def mail(monkeypatch):
    """Every mail sent: (to, subject, text)."""
    import backend.utils.email as email
    sent = []
    monkeypatch.setattr(email, "_deliver", lambda to, subject, text, html:
                        sent.append((to, subject, text)))
    return sent


def _dispatcher(monkeypatch, stubs):
    mod = MagicMock()
    mod.dispatch = lambda job_id, token: stubs.dispatched.append((job_id, token))
    monkeypatch.setitem(sys.modules, "backend.tasks.user_purge", mod)


def _schedule(user, **kw):
    kw.setdefault("requested_by_id", user.id)
    kw.setdefault("source", "self")
    return acc.schedule_account_deletion(user, **kw)


def _run_due(job, at):
    """Let the beat claim the job at *at* and run it."""
    tokens = []
    up.dispatch_due_jobs(lambda j, t: tokens.append(t), now=at)
    assert tokens, "the due job was not claimed"
    return up.run_purge_job(job.id, tokens[-1])


def _user_columns():
    """(table, column) for every column that names an account: foreign
    keys into user, and the requester columns kept without one."""
    out = []
    for table in _db.metadata.sorted_tables:
        if table.name == "user":
            continue
        for col in table.columns:
            if any(fk.column.table.name == "user" for fk in col.foreign_keys):
                out.append((table, col))
    return out


# ── Asking ──────────────────────────────────────────────────────────────

def test_an_email_account_confirms_from_the_link_in_its_own_session(
        app, world, stubs, mail):
    a = world.alice
    c = _client(app, a)
    r = c.post("/api/account/delete", json={"confirm": "bob"})
    assert r.status_code == 400 and r.get_json()["code"] == "confirm_mismatch"
    assert c.post("/api/account/delete").status_code == 400

    r = c.post("/api/account/delete", json={"confirm": "Alice"})
    assert r.status_code == 202 and r.get_json()["status"] == "confirm_email"
    # Nothing happens before the link is used.
    assert _db.session.get(User, a.id).deleted_at is None
    assert UserDataPurge.query.count() == 0
    to, subject, text = mail[-1]
    assert to == "alice@example.com"
    assert subject == "Confirm the deletion of your Loore account"
    link = re.search(r"(https://app\.test/confirm-account-deletion\?token=\S+)",
                     text).group(1)
    token = link.split("token=", 1)[1]

    # Another account's session cannot use it; nor can a wrong token.
    r = _client(app, world.bob).post("/api/account/delete/confirm",
                                     json={"token": token})
    assert r.status_code == 403 and r.get_json()["reason"] == "other_account"
    r = c.post("/api/account/delete/confirm", json={"token": token + "x"})
    assert r.status_code in (400, 403)
    assert UserDataPurge.query.count() == 0

    other_session = _client(app, a)
    r = c.post("/api/account/delete/confirm", json={"token": token})
    assert r.status_code == 202 and r.get_json()["status"] == "scheduled"
    job = UserDataPurge.query.one()
    assert job.delete_account and job.source == "self"
    assert job.status == "scheduled"
    due = job.scheduled_for - datetime.utcnow()
    assert timedelta(days=29, hours=23) < due <= timedelta(days=30)
    assert _db.session.get(User, a.id).deleted_at is not None
    # Signed out here and in every other session.
    assert c.get("/api/account/data").status_code == 401
    assert other_session.get("/api/account/data").status_code == 401
    assert mail[-1][1] == "Your Loore account will be deleted"
    assert "sign in to Loore before then" in mail[-1][2]


def test_an_expired_link_confirms_nothing(app, world, stubs, mail):
    a = world.alice
    c = _client(app, a)
    c.post("/api/account/delete", json={"confirm": "alice"})
    token = mail[-1][2].split("token=", 1)[1].split()[0]
    user = _db.session.get(User, a.id)
    user.account_deletion_expires_at = datetime.utcnow() - timedelta(seconds=1)
    _db.session.commit()
    r = c.post("/api/account/delete/confirm", json={"token": token})
    assert r.status_code == 400
    assert UserDataPurge.query.count() == 0


def test_an_account_without_email_is_scheduled_at_once(app, world, stubs, mail):
    a = world.alice
    a.email = None
    _db.session.commit()
    c = _client(app, a)
    r = c.post("/api/account/delete", json={"confirm": "alice"})
    assert r.status_code == 202 and r.get_json()["status"] == "scheduled"
    assert UserDataPurge.query.one().delete_account
    assert c.get("/api/account/data").status_code == 401
    assert mail == []


# ── Hidden at once ──────────────────────────────────────────────────────

def test_the_account_is_hidden_at_once(app, world, stubs):
    from backend.utils.api_tokens import TOKEN_MARKER
    from backend.utils import api_tokens
    from backend.utils.magic_link import hash_token
    from backend.utils.privacy import accessible_nodes_filter, can_user_access_node
    from backend.utils.serialization import serialize_node_status
    from backend.utils.username_history import resolve_public_handle

    a, b = world.alice, world.bob
    a.public_sharing_enabled = b.public_sharing_enabled = True
    a.magic_link_token_hash = "pending-link"
    a.magic_link_expires_at = datetime.utcnow() + timedelta(minutes=5)
    plain = TOKEN_MARKER + "secret"
    _add(ApiToken(user_id=a.id, name="clip", token_hash=hash_token(plain),
                  prefix="s"))
    _db.session.commit()
    A1 = _db.session.get(Node, world.ids["A1"])
    B1 = _db.session.get(Node, world.ids["B1"])
    assert api_tokens.resolve_api_token(plain, "external:write") is not None
    assert can_user_access_node(A1, b.id)
    assert resolve_public_handle("alice")[0] is not None
    assert a.id in {u.id for u in User.profile_eligible_query()}

    _schedule(a)
    _db.session.expire_all()
    A1 = _db.session.get(Node, world.ids["A1"])
    B1 = _db.session.get(Node, world.ids["B1"])
    user = _db.session.get(User, a.id)
    assert acc.session_user(a.id) is None
    assert user.magic_link_token_hash is None
    assert api_tokens.resolve_api_token(plain, "external:write") is None
    assert a.id not in {u.id for u in User.profile_eligible_query()}
    assert resolve_public_handle("alice") == (None, False)
    # Bob sees her public entry as deleted, without her name, and his own
    # reply below it as before.
    assert not can_user_access_node(A1, b.id)
    status = serialize_node_status(A1, b.id)
    assert status["deleted"] is True and status["username"] is None
    assert can_user_access_node(B1, b.id)
    visible = {n.id for n in Node.query.filter(
        accessible_nodes_filter(Node, b.id))}
    assert world.ids["A1"] not in visible and world.ids["B1"] in visible
    # Nothing was deleted yet.
    assert Node.query.filter_by(user_id=a.id).count() == 7


def test_other_members_logs_do_not_show_a_hidden_accounts_reply(
        app, world, stubs):
    """Bob deleted the root of his thread; his Log card falls back to the
    newest node still alive under it, which was alice's public reply.
    Once alice's account is hidden, that reply counts as deleted there
    too: neither her words nor her name appear in bob's Log."""
    a7 = _db.session.get(Node, world.ids["A7"])
    a7.privacy_level = "public"
    a7.set_content("alice hidden words")
    _db.session.get(Node, world.ids["B3"]).deleted_at = datetime.utcnow()
    _db.session.commit()
    c = _client(app, world.bob)

    def cards():
        return c.get("/api/log").get_json()["nodes"]
    assert any(n["preview"].startswith("alice hidden words")
               and n["username"] == "alice" for n in cards())
    _schedule(world.alice)
    for n in cards():
        assert "alice hidden words" not in (n["preview"] or "")
        assert n["username"] != "alice"


def test_nobody_can_build_on_a_hidden_accounts_entry_or_read_it_into_ai(
        app, world, stubs):
    """Bob's public reply B1 sits under alice's public entry A1. While her
    account is hidden, A1 cannot be replied to or linked (404, like a
    node bob cannot see), and an AI reply for bob under B1 does not
    read it: A1 stays in the chain as a deleted placeholder, as after
    the purge, and its text is never sent."""
    from backend.tests.test_context_artifact_pinning import _load_node_chain
    from backend.utils.node_deletion import assert_parent_alive
    from backend.utils.privacy import (
        can_user_see_node_or_tombstone, shown_as_deleted)
    b = world.bob
    A1 = _db.session.get(Node, world.ids["A1"])
    B1 = _db.session.get(Node, world.ids["B1"])
    assert can_user_see_node_or_tombstone(A1, b.id)
    assert assert_parent_alive(A1.id, b.id) is None
    assert [n.id for n in _load_node_chain(B1, b.id)] == [A1.id, B1.id]

    _schedule(world.alice)
    _db.session.expire_all()
    A1 = _db.session.get(Node, world.ids["A1"])
    B1 = _db.session.get(Node, world.ids["B1"])
    assert not can_user_see_node_or_tombstone(A1, b.id)
    resp, status = assert_parent_alive(A1.id, b.id)
    assert status == 404
    assert [n.id for n in _load_node_chain(B1, b.id)] == [A1.id, B1.id]
    assert shown_as_deleted(A1, b.id) and not shown_as_deleted(B1, b.id)
    # Nobody builds an AI reply on A1 itself either.
    with pytest.raises(ValueError):
        _load_node_chain(A1, b.id)


def test_a_gone_authors_placeholders_carry_no_name(app, world, stubs):
    """In the grace period and after the deletion alike, other people see
    the account's entries as deleted placeholders with no name (neither
    hers nor loore-erased), in threads and in quotes, and its replies
    do not count."""
    from backend.utils.quotes import get_quote_data
    from backend.utils.serialization import serialize_node_status
    from backend.utils.thread_tree import alive_child_counts
    b = world.bob
    a1 = world.ids["A1"]
    assert alive_child_counts([a1])[a1] == 3      # A2 (hers), B1, LB
    job = _schedule(world.alice)

    def check():
        _db.session.expire_all()
        node = _db.session.get(Node, a1)
        status = serialize_node_status(node, b.id)
        assert status["deleted"] is True and status["deleted_at"]
        assert status["username"] is None
        quote = get_quote_data([a1], b.id)[a1]
        assert quote["deleted"] is True
        assert quote["username"] is None and quote["user_id"] is None
    check()
    assert alive_child_counts([a1])[a1] == 2      # B1, LB
    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "done"
    check()


def _reply_under_alices_public_reply(world):
    """Bob's public root B3, alice's public reply A7, bob's reply BX."""
    A7 = _db.session.get(Node, world.ids["A7"])
    A7.privacy_level = "public"
    A7.set_content("alice's words in bob's thread")
    BX = _node(world.bob, A7, owner=world.bob.id, privacy="public",
               text="bob answers her")
    _db.session.commit()
    return BX.id


def test_an_ai_reply_below_a_hidden_accounts_reply_keeps_the_thread(
        app, world, stubs):
    """The context of bob's AI reply under BX is [B3, A7, BX] before the
    request, in the grace period (A7 as a deleted placeholder) and after
    the purge (A7 a tombstone): the same thread every time."""
    from backend.tests.test_context_artifact_pinning import _load_node_chain
    from backend.utils.llm_nodes import _Chain
    from backend.utils.privacy import shown_as_deleted
    b = world.bob
    bx = _reply_under_alices_public_reply(world)
    expected = [world.ids["B3"], world.ids["A7"], bx]

    def chain():
        _db.session.expire_all()
        return _load_node_chain(_db.session.get(Node, bx), b.id)

    assert [n.id for n in chain()] == expected
    job = _schedule(world.alice)
    nodes = chain()
    assert [n.id for n in nodes] == expected
    assert [shown_as_deleted(n, b.id) for n in nodes] == [False, True, False]
    # The reply check walks the same chain: past A7 to bob's root, whose
    # ai_usage then decides, as after the purge.
    B3 = _db.session.get(Node, world.ids["B3"])
    B3.ai_usage = "none"
    _db.session.commit()
    assert _Chain(_db.session.get(Node, bx)).unreadable(b.id).id == B3.id
    B3.ai_usage = "chat"
    _db.session.commit()

    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "done"
    nodes = chain()
    assert [n.id for n in nodes] == expected
    assert [shown_as_deleted(n, b.id) for n in nodes] == [False, True, False]


def test_exports_show_a_hidden_accounts_reply_without_a_name(
        app, world, stubs):
    """Bob's own export (the download, the background export and his
    recent-context input) shows alice's reply in his thread as a deleted
    placeholder without a name, in the grace period and after the purge,
    with and without a token budget."""
    from backend.routes.export_data import build_user_export_content
    b = world.bob
    _reply_under_alices_public_reply(world)

    def exports():
        _db.session.expire_all()
        bob = _db.session.get(User, b.id)
        return [build_user_export_content(bob, filter_ai_usage=False),
                build_user_export_content(bob, filter_ai_usage=True),
                build_user_export_content(bob, max_tokens=50_000,
                                          filter_ai_usage=True),
                build_user_export_content(
                    bob, filter_ai_usage=True,
                    created_after=datetime(2000, 1, 1))]

    for text in exports():
        assert "User (alice)" in text and "alice's words" in text
    job = _schedule(world.alice)

    def check():
        for text in exports():
            assert "alice" not in text
            assert ERASED_SYSTEM_USERNAME not in text
            assert "[Node deleted by author]" in text
            assert "bob answers her" in text
    check()
    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "done"
    check()


def test_node_json_does_not_name_a_hidden_parents_author_by_id(
        app, world, stubs):
    """parent_user_id under a hidden account's placeholder is None: on
    the focal node, its ancestors and the children below the
    placeholder, in the grace period and after the purge."""
    from backend.routes.nodes import nodes_bp
    app.register_blueprint(nodes_bp, url_prefix="/api/nodes")
    a_id, b = world.alice.id, world.bob
    bx = _reply_under_alices_public_reply(world)
    c = _client(app, b)

    def ids_named():
        r = c.get(f"/api/nodes/{bx}")
        assert r.status_code == 200
        focal = r.get_json()
        r = c.get(f"/api/nodes/{world.ids['B3']}")
        assert r.status_code == 200
        a7 = next(ch for ch in r.get_json()["children"]
                  if ch["id"] == world.ids["A7"])
        below = [ch["parent_user_id"] for ch in a7["children"]]
        return ([focal["parent_user_id"]]
                + [x.get("parent_user_id") for x in focal["ancestors"]]
                + [x.get("user_id") for x in focal["ancestors"]]
                + below)

    assert a_id in ids_named()
    job = _schedule(world.alice)
    assert a_id not in ids_named()
    assert ids_named()[0] is None
    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "done"
    erased = User.query.filter_by(username=ERASED_SYSTEM_USERNAME).one()
    named = ids_named()
    assert a_id not in named and erased.id not in named


def test_old_ai_replies_without_an_owner_are_hidden_with_the_account(
        app, world, stubs):
    """An AI reply stored before replies had a human owner counts as the
    user's when its nearest ancestor that is not an AI reply is theirs
    (the purge's rule). The request gives such replies their owner, so
    they are hidden with the account; a restore leaves the owner as it
    is. Replies whose chain leads to another person's entry, or to no
    entry at all, get no owner."""
    from backend.tests.test_context_artifact_pinning import _load_node_chain
    from backend.utils.privacy import (
        accessible_nodes_filter, can_user_access_node, shown_as_deleted)
    a, b, llm = world.alice, world.bob, world.llm
    for k in ("A1", "A2", "L2"):
        _db.session.get(Node, world.ids[k]).privacy_level = "public"
    L2 = _db.session.get(Node, world.ids["L2"])
    BL = _node(b, L2, owner=b.id, privacy="public")
    # An AI reply under an AI reply, and one with no entry above it.
    L4 = _node(llm, L2, owner=None, node_type="llm", privacy="public")
    orphan = _node(llm, None, owner=None, node_type="llm", privacy="public")
    _db.session.commit()
    ids = {"L2": L2.id, "L4": L4.id, "L3": world.ids["L3"],
           "orphan": orphan.id, "BL": BL.id}
    assert can_user_access_node(_db.session.get(Node, ids["L2"]), b.id)

    _schedule(a)
    _db.session.expire_all()

    def owner(k):
        return _db.session.get(Node, ids[k]).human_owner_id
    assert owner("L2") == owner("L4") == a.id
    assert owner("L3") is None          # under bob's entry
    assert owner("orphan") is None      # no entry above it
    L2 = _db.session.get(Node, ids["L2"])
    assert not can_user_access_node(L2, b.id)
    visible = {n.id for n in Node.query.filter(
        accessible_nodes_filter(Node, b.id))}
    assert ids["L2"] not in visible and ids["L4"] not in visible
    assert ids["BL"] in visible
    nodes = _load_node_chain(_db.session.get(Node, ids["BL"]), b.id)
    assert [n.id for n in nodes] == [
        world.ids["A1"], world.ids["A2"], ids["L2"], ids["BL"]]
    assert [shown_as_deleted(n, b.id) for n in nodes] == [
        True, True, True, False]

    assert acc.restore_account(_db.session.get(User, a.id))
    _db.session.expire_all()
    assert owner("L2") == owner("L4") == a.id
    assert owner("L3") is None and owner("orphan") is None
    assert can_user_access_node(_db.session.get(Node, ids["L2"]), b.id)


def test_a_hidden_account_has_no_member_page(app, world, stubs):
    c = _client(app, world.bob)
    assert c.get("/api/dashboard/alice").status_code == 200
    _schedule(world.alice)
    r = c.get("/api/dashboard/alice")
    unknown = c.get("/api/dashboard/nobody")
    assert r.status_code == unknown.status_code == 404
    assert r.get_data() == unknown.get_data()


def test_an_admin_deletes_their_own_account_on_the_account_page(
        app, world, stubs, monkeypatch):
    _dispatcher(monkeypatch, stubs)
    _add(User(username="second", approved=True, is_admin=True))
    _db.session.commit()
    c = _client(app, world.admin)
    for url in (f"/api/admin/users/{world.admin.id}/delete_account?dry_run=1",
                f"/api/admin/users/{world.admin.id}/delete_account"):
        r = c.post(url, json={"confirm_username": "admin"})
        assert r.status_code == 409 and r.get_json()["code"] == "own_account"
    assert UserDataPurge.query.count() == 0
    assert _db.session.get(User, world.admin.id).deleted_at is None


def test_the_data_cancel_does_not_undo_an_account_deletion(app, world, stubs):
    job = _schedule(world.alice)
    assert up.cancel_purge(world.alice.id, world.alice.id) is False
    assert _db.session.get(UserDataPurge, job.id).status == "scheduled"


def _thread_ids(c, node_id):
    def ids(n):
        return [n["id"]] + [i for ch in n["children"] for i in ids(ch)]
    return ids(c.get(f"/api/commons/node/{node_id}").get_json()["thread"])


def test_public_pages_answer_404_from_the_request_to_the_end(app, world, stubs):
    a, b = world.alice, world.bob
    a.public_sharing_enabled = b.public_sharing_enabled = True
    # Her public reply in bob's public thread.
    _db.session.get(Node, world.ids["A7"]).privacy_level = "public"
    _db.session.commit()
    c = app.test_client()
    assert c.get("/@alice/birds").status_code == 200
    assert c.get("/@alice").status_code == 200
    assert "/@alice/birds" in c.get("/sitemap.xml").get_data(as_text=True)
    assert c.get("/api/commons/permalink/alice/birds").status_code == 200
    assert world.ids["A7"] in _thread_ids(c, world.ids["B3"])

    job = _schedule(a)
    assert world.ids["A7"] not in _thread_ids(c, world.ids["B3"])
    for url in ("/@alice/birds", "/@alice", "/@alice/feed.xml",
                f"/node/{world.ids['A1']}", "/api/commons/permalink/alice/birds",
                f"/api/commons/node/{world.ids['A1']}"):
        assert c.get(url).status_code == 404, url
    assert "/@alice" not in c.get("/sitemap.xml").get_data(as_text=True)
    # Bob's public reply still renders on its own.
    assert c.get(f"/api/commons/node/{world.ids['B1']}").status_code == 200

    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "done"
    for url in ("/@alice/birds", "/@alice", "/@alice/feed.xml",
                f"/node/{world.ids['A1']}", "/api/commons/permalink/alice/birds",
                f"/api/commons/node/{world.ids['A1']}"):
        assert c.get(url).status_code in (404, 410), url
    assert c.get("/sitemap.xml").status_code == 200
    assert c.get(f"/api/commons/node/{world.ids['B1']}").status_code == 200


# ── Restore ─────────────────────────────────────────────────────────────

def _sign_in_by_link(c, mail):
    assert c.post("/auth/magic-link/send",
                  json={"email": "alice@example.com"}).status_code == 200
    token = re.search(r"token=(\S+)", mail[-1][2]).group(1)
    return c.get(f"/auth/magic-link/verify?token={token}")


def test_signing_in_during_the_grace_period_offers_a_restore(
        app, world, stubs, mail):
    a = world.alice
    job = _schedule(a)
    c = app.test_client()
    r = _sign_in_by_link(c, mail)
    assert r.status_code == 302
    assert r.headers["Location"] == f"{FRONTEND}/account-restore"
    # Not signed in: only the question is open.
    assert c.get("/api/account/data").status_code == 401
    r = c.get("/api/account/restore")
    assert r.status_code == 200
    offer = r.get_json()
    assert offer["username"] == "alice" and offer["restorable"] is True
    assert offer["delete_on"]

    r = c.post("/api/account/restore")
    assert r.status_code == 200 and r.get_json()["status"] == "restored"
    assert c.get("/api/account/data").status_code == 200
    _db.session.expire_all()
    assert _db.session.get(User, a.id).deleted_at is None
    assert _db.session.get(UserDataPurge, job.id).status == "cancelled"
    assert up.dispatch_due_jobs(lambda j, t: pytest.fail("restored"),
                                now=datetime.utcnow() + timedelta(days=40)) == []


def test_signing_in_with_x_offers_a_restore(app, world, stubs, monkeypatch):
    import backend.routes.auth as auth
    fake = MagicMock()
    fake.authorized = True
    fake.get.return_value = MagicMock(
        ok=True, json=lambda: {"id": 1001, "screen_name": "alicebirds"})
    monkeypatch.setattr(auth, "twitter", fake)
    _schedule(world.alice)
    c = app.test_client()
    r = c.get("/auth/login")
    assert r.status_code == 302
    assert r.headers["Location"] == f"{FRONTEND}/account-restore"
    assert c.get("/api/account/data").status_code == 401
    assert c.get("/api/account/restore").get_json()["username"] == "alice"


def test_keeping_it_deleted_drops_the_offer(app, world, stubs, mail):
    _schedule(world.alice)
    c = app.test_client()
    _sign_in_by_link(c, mail)
    assert c.post("/api/account/restore/decline").status_code == 200
    assert c.get("/api/account/restore").status_code == 404
    assert c.post("/api/account/restore").status_code == 404
    assert _db.session.get(User, world.alice.id).deleted_at is not None


def test_the_offer_is_short_lived(app, world, stubs, mail):
    _schedule(world.alice)
    c = app.test_client()
    _sign_in_by_link(c, mail)
    with c.session_transaction() as s:
        offer = dict(s[acc.RESTORE_SESSION_KEY])
        offer["at"] = time.time() - acc.RESTORE_OFFER_SECONDS - 1
        s[acc.RESTORE_SESSION_KEY] = offer
    assert c.get("/api/account/restore").status_code == 404


def test_no_restore_once_the_deletion_has_started(app, world, stubs, mail):
    job = _schedule(world.alice)
    c = app.test_client()
    _sign_in_by_link(c, mail)
    assert up.claim_job(job.id, "runner", now=job.scheduled_for)
    assert c.get("/api/account/restore").get_json()["restorable"] is False
    r = c.post("/api/account/restore")
    assert r.status_code == 409 and r.get_json()["code"] == "already_started"
    assert _db.session.get(User, world.alice.id).deleted_at is not None
    assert c.get("/api/account/data").status_code == 401


# ── The purge and the identity layer ────────────────────────────────────

def test_after_the_grace_period_nothing_of_the_account_is_left(
        app, world, stubs, mail):
    a, b = world.alice, world.bob
    a_id = a.id
    _add(UsernameHistory(user_id=a_id, old_username="alice_old"))
    w_poll = _add(Poll(question="Hers?", created_by=a_id))
    B3 = _db.session.get(Node, world.ids["B3"])
    B3.pinned_by, B3.pinned_at = a_id, T0
    _db.session.commit()
    job = _schedule(a)
    now = datetime.utcnow()

    # Nothing during the grace period.
    assert up.dispatch_due_jobs(lambda j, t: pytest.fail("early"),
                                now=now + timedelta(days=29)) == []
    assert Node.query.filter_by(user_id=a_id).count() == 7

    assert _run_due(job, now + timedelta(days=30, minutes=1)) == "done"
    _db.session.expire_all()
    assert _db.session.get(User, a_id) is None
    # Verified by counting: no column anywhere names her any more.
    for table, col in _user_columns():
        n = _db.session.query(_db.func.count()).select_from(table).filter(
            col == a_id).scalar()
        assert n == 0, f"{table.name}.{col.name}"
    assert acc.references_to_user(a_id) == {}
    for model in (up.ProfileBatchJob, up.PollDraftBatchJob,
                  up.ExternalDigestBatchJob, up.RecentContextBatchJob):
        for j in model.query:
            assert all(i.get("user_id") != a_id for i in j.items or []), model
    # The job row stays, with ids and counts only.
    job = _db.session.get(UserDataPurge, job.id)
    assert job.status == "done" and job.user_id == a_id
    assert job.counts["user"] == 1 and job.counts["node"] == 6
    assert all(isinstance(v, int) for v in job.counts.values())

    # Her tombstones stay for bob's replies, without her.
    erased = User.query.filter_by(username=ERASED_SYSTEM_USERNAME).one()
    for k in ("A1", "A4", "A5"):
        n = _db.session.get(Node, world.ids[k])
        assert (n.user_id, n.human_owner_id) == (erased.id, erased.id), k
        assert n.content is None and n.deleted_at is not None
    assert _db.session.get(Node, world.ids["B1"]).parent_id == world.ids["A1"]
    assert _db.session.get(Node, world.ids["B2"]).parent_id == world.ids["A5"]
    # Other rows that named her lose only that column.
    B3 = _db.session.get(Node, world.ids["B3"])
    assert B3.pinned_by is None and B3.pinned_at == T0
    assert _db.session.get(Poll, w_poll.id).created_by is None
    assert UsernameHistory.query.count() == 0

    # Her handles are kept from new accounts.
    from backend.utils.reserved_usernames import (
        derive_available_username, validate_username)
    assert validate_username("alice") == "That username is reserved."
    assert validate_username("ALICE_OLD") == "That username is reserved."
    assert derive_available_username("alice") == "alice2"
    row = ReleasedUsername.query.filter_by(username="alice").one()
    assert row.reserved_until - row.released_at == timedelta(
        days=acc.USERNAME_RESERVE_DAYS)
    # The confirmation goes to the address she had.
    assert mail[-1][:2] == ("alice@example.com",
                            "Your Loore account has been deleted")
    assert b.username == "bob"


def test_other_users_data_is_untouched(app, world, stubs):
    before = _bob_snapshot(world)
    job = _schedule(world.alice)
    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "done"
    after = _bob_snapshot(world)
    # The same columns as the data purge clears (#268), and nothing else.
    for row in before["nodes"]:
        if row["id"] == world.ids["B4"]:
            row["linked_node_id"] = None
            row["continuation_node_id"] = None
    for row in before["drafts"]:
        if row["id"] == world.DB1.id:
            row["parent_id"] = None
    for row in before["shares"]:
        if row["id"] == world.SB.id:
            row["source_node_id"] = None
    assert after == before
    assert _db.session.get(Draft, world.DB1.id).updated_at == T0
    assert _db.session.get(ShareDraft, world.SB.id).updated_at == T0
    for f in world.files_bob:
        assert f.exists(), f
    for f in world.files_alice:
        assert not f.exists(), f


def test_the_deletion_never_decrypts(app, world, stubs, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the deletion decrypted content")
    import backend.utils.encryption as enc
    monkeypatch.setattr(enc, "decrypt_content", boom)
    monkeypatch.setattr(_real_backend_models, "decrypt_content", boom)
    acc.count_account_data(world.alice.id)
    job = _schedule(world.alice)
    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "done"
    assert _db.session.get(User, world.alice.id) is None


def test_a_leftover_reference_fails_the_run_and_keeps_the_account(
        app, world, stubs, monkeypatch):
    """If anything still points at the account after the identity layer,
    the run fails (retried, then marked failed) and the user row stays."""
    real = acc.references_to_user
    monkeypatch.setattr(acc, "references_to_user",
                        lambda uid: {"some_table.user_id": 1})
    job = _schedule(world.alice)
    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "error"
    job = _db.session.get(UserDataPurge, job.id)
    assert job.status == "running" and job.error.startswith("PurgeIncomplete")
    assert _db.session.get(User, world.alice.id) is not None
    monkeypatch.setattr(acc, "references_to_user", real)
    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "done"
    assert _db.session.get(User, world.alice.id) is None


def test_a_waiting_writing_deletion_becomes_the_account_deletion(
        app, world, stubs):
    a = world.alice
    data_job, _ = up.schedule_purge(a, requested_by_id=a.id, source="self",
                                    at=datetime.utcnow() + timedelta(days=3))
    job = _schedule(a)
    assert job.id == data_job.id and UserDataPurge.query.count() == 1
    assert job.delete_account
    assert job.scheduled_for > datetime.utcnow() + timedelta(days=29)
    # Restoring cancels both.
    assert acc.restore_account(_db.session.get(User, a.id))
    assert _db.session.get(UserDataPurge, job.id).status == "cancelled"


def test_released_handles_are_forgotten_after_the_reservation(app, world):
    now = datetime.utcnow()
    _add(ReleasedUsername(username="gone", released_at=now - timedelta(days=400),
                          reserved_until=now - timedelta(days=35)))
    _add(ReleasedUsername(username="recent", released_at=now,
                          reserved_until=now + timedelta(days=300)))
    _db.session.commit()
    from backend.utils.reserved_usernames import validate_username
    assert validate_username("gone") is None
    assert validate_username("recent") == "That username is reserved."
    assert acc.release_expired_usernames() == 1
    assert [r.username for r in ReleasedUsername.query] == ["recent"]


# ── Refusals ────────────────────────────────────────────────────────────

def test_the_last_admin_cannot_be_deleted(app, world, stubs, monkeypatch):
    _dispatcher(monkeypatch, stubs)
    admin = world.admin
    c = _client(app, admin)
    r = c.post("/api/account/delete", json={"confirm": "admin"})
    assert r.status_code == 409 and r.get_json()["code"] == "last_admin"
    assert _db.session.get(User, admin.id).deleted_at is None

    second = _add(User(username="second", approved=True, is_admin=True))
    _db.session.commit()
    # With another live admin it works...
    r = c.post("/api/account/delete", json={"confirm": "admin"})
    assert r.status_code == 202
    # ...and then the other one is the last.
    c2 = _client(app, second)
    r = c2.post("/api/account/delete", json={"confirm": "second"})
    assert r.status_code == 409 and r.get_json()["code"] == "last_admin"
    assert acc.deletion_refusal(_db.session.get(User, second.id))[0] == "last_admin"
    # The admin endpoint never acts on the caller's own account.
    r = c2.post(f"/api/admin/users/{second.id}/delete_account",
                json={"confirm_username": "second"})
    assert r.status_code == 409 and r.get_json()["code"] == "own_account"


def test_the_last_admin_is_also_checked_when_the_deletion_runs(
        app, world, stubs):
    second = _add(User(username="second", approved=True, is_admin=True))
    _db.session.commit()
    job = _schedule(second)
    admin = _db.session.get(User, world.admin.id)
    admin.deleted_at = datetime.utcnow()     # meanwhile hidden as well
    _db.session.commit()
    assert _run_due(job, datetime.utcnow() + timedelta(days=31)) == "refused"
    assert _db.session.get(User, second.id) is not None
    assert _db.session.get(UserDataPurge, job.id).status == "failed"


def test_ai_and_system_accounts_are_refused(app, world, stubs):
    from backend.utils.system_accounts import get_erased_system_user
    erased = get_erased_system_user()
    c = _client(app, world.admin)
    for target in (world.llm, erased):
        for url in (f"/api/admin/users/{target.id}/delete_account?dry_run=1",
                    f"/api/admin/users/{target.id}/delete_account"):
            r = c.post(url, json={"confirm_username": target.username})
            assert r.status_code == 409 and r.get_json()["code"] == "refused"
        with pytest.raises(acc.AccountDeletionRefused):
            _schedule(target)
    assert UserDataPurge.query.count() == 0
    assert _db.session.get(User, world.llm.id).deleted_at is None


# ── Admin ───────────────────────────────────────────────────────────────

def test_admin_dry_run_shows_counts_only_then_deletes_at_once(
        app, world, stubs, monkeypatch, mail):
    _dispatcher(monkeypatch, stubs)
    a_id = world.alice.id
    assert _client(app, world.bob).post(
        f"/api/admin/users/{a_id}/delete_account?dry_run=1").status_code == 403

    c = _client(app, world.admin)
    r = c.post(f"/api/admin/users/{a_id}/delete_account?dry_run=1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["counts"]["node"] == 6 and body["counts"]["node_tombstoned"] == 3
    assert body["identity"] == {
        "user": 1, "username_history": 0, "api_token": 1,
        "changelog_read_state": 1, "node.pinned_by": 0, "poll.created_by": 0,
        "node_reattributed": 3}
    # Her public entry A1; her replies to bob's entries B3 and B2.
    assert body["blast_radius"] == {"public_nodes": 1, "replies_to_others": 2}
    text = r.get_data(as_text=True)
    for words in ("I write about birds", "old root", "bob clip", "draft 2"):
        assert words not in text
    assert UserDataPurge.query.count() == 0
    assert _db.session.get(User, a_id).deleted_at is None

    r = c.post(f"/api/admin/users/{a_id}/delete_account",
               json={"confirm_username": "alic"})
    assert r.status_code == 400
    r = c.post(f"/api/admin/users/{a_id}/delete_account",
               json={"confirm_username": "alice"})
    assert r.status_code == 202
    job = UserDataPurge.query.one()
    assert job.delete_account and job.source == "admin"
    assert job.requested_by_id == world.admin.id
    assert job.scheduled_for <= datetime.utcnow()
    assert _db.session.get(User, a_id).deleted_at is not None
    assert len(stubs.dispatched) == 1
    assert up.run_purge_job(job.id, stubs.dispatched[0][1]) == "done"
    assert _db.session.get(User, a_id) is None
    r = c.get(f"/api/admin/users/{a_id}/purge_data")
    assert r.get_json()["job"]["delete_account"] is True
    assert r.get_json()["job"]["status"] == "done"


def test_admin_purge_data_does_not_bring_an_account_deletion_forward(
        app, world, stubs, monkeypatch):
    _dispatcher(monkeypatch, stubs)
    job = _schedule(world.alice)
    due = job.scheduled_for
    c = _client(app, world.admin)
    r = c.post(f"/api/admin/users/{world.alice.id}/purge_data",
               json={"confirm_username": "alice"})
    assert r.status_code == 409
    assert r.get_json()["code"] == "account_deletion_scheduled"
    job = _db.session.get(UserDataPurge, job.id)
    assert job.status == "scheduled" and job.scheduled_for == due
    assert stubs.dispatched == []


def test_admin_delete_brings_a_users_own_request_forward(
        app, world, stubs, monkeypatch):
    _dispatcher(monkeypatch, stubs)
    job = _schedule(world.alice)
    c = _client(app, world.admin)
    r = c.post(f"/api/admin/users/{world.alice.id}/delete_account",
               json={"confirm_username": "alice"})
    assert r.status_code == 202
    job = _db.session.get(UserDataPurge, job.id)
    assert UserDataPurge.query.count() == 1
    assert job.source == "admin" and job.requested_by_id == world.admin.id
    assert job.status == "running" and len(stubs.dispatched) == 1
