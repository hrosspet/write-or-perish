"""Pre-filled accounts nobody claimed are deleted after the period (#685;
backend/utils/unclaimed_accounts.py, the nightly task, the admin's report
and Keep, `flask unclaimed-accounts`).

Reuses the app of test_account_deletion.py (sqlite in-memory with foreign
keys ON, the auth, account and admin routes) and the purge stubs of
test_user_purge.py: Celery, provider batches and mail delivery are
stubbed, and no model API is called.
"""
import logging
import re
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.tests.test_account_deletion import (  # noqa: F401 - fixtures
    FRONTEND, _run_due, app, mail,
)
from backend.tests.test_user_purge import (  # noqa: F401 - fixtures
    _add, _client, _db, _file, _real_backend_models, stubs,
)
from backend.models import (
    APICostLog, ApiToken, ArtifactView, ChangelogReadState, Draft,
    ExternalAccount, Node, NodeVersion, ReleasedUsername, User, UserArtifact,
    UserDataPurge, UsernameHistory, UserNotification, UserProfile,
    UserPrompt,
)
from backend.utils import account_deletion as acc
from backend.utils import unclaimed_accounts as ua
from backend.utils.system_accounts import ERASED_SYSTEM_USERNAME

OPENED = datetime(2026, 9, 10, 12, 0)
DAY = timedelta(days=1)
DAY_89 = OPENED + 89 * DAY
DAY_91 = OPENED + 91 * DAY


def _fetched_node(user, i, *, stored=None):
    n = Node(user_id=user.id, human_owner_id=user.id, node_type="user",
             privacy_level="private", ai_usage="chat", token_count=3,
             origin="twitter", provenance="prefill_ca",
             source_key=f"twitter:{user.username}-{i}",
             created_at=OPENED - 400 * DAY,
             updated_at=stored or OPENED + timedelta(minutes=5))
    n.set_content("a public post")
    return _add(n)


def _prefilled(username, *, opened=OPENED, posts=3, **kw):
    """An account the admin opened and pre-filled, that nobody used: its
    fetched posts, the profile and intentions built from them, an
    automation cost row."""
    kw.setdefault("twitter_id", f"x-{username}")
    u = _add(User(username=username, approved=False,
                  prefilled_handle=kw.pop("handle", username),
                  profile_force_batch=True, created_at=opened, **kw))
    for i in range(posts):
        _fetched_node(u, i, stored=opened + timedelta(minutes=5))
    _add(UserProfile(user_id=u.id, content="profile", generated_by="claude"))
    _add(UserArtifact(user_id=u.id, kind="intentions", title="Intentions",
                      content="i", generated_by="claude"))
    _add(APICostLog(user_id=u.id, model_id="m", request_type="profile_batch",
                    cost_microdollars=5, request_ref="batch:1"))
    _db.session.commit()
    return u


def _status(user, now=DAY_91, **kw):
    _db.session.expire_all()
    u = _db.session.get(User, user.id)
    return ua.assess(u, now, config=kw.pop("config", None) or {},
                     active_job_ids=ua.active_job_user_ids(), **kw)


def _run(now=DAY_91, **kw):
    _db.session.expire_all()
    return ua.run(now, **kw)


# ── Which accounts ──────────────────────────────────────────────────────

def test_an_unclaimed_account_at_day_91_dry_run_records_then_real_deletes(
        app, stubs, mail):
    """The dry run reports and changes nothing; the real run deletes the
    account with everything in it, through the account deletion (#269)."""
    a = _prefilled("alicebirds", email="alice@example.com")
    a_id = a.id
    dump = _file(stubs.data / "x-api" / "alicebirds-2026-09-10T12-00-00Z.jsonl",
                 b"{}")
    stash = _file(stubs.data / "ca-prefill" / f"{a_id}-123" / "page.jsonl",
                  b"{}")

    out = _run()
    assert out["dry_run"] is True
    assert out["due"] == [a_id] and out["tonight"] == [a_id]
    assert out["scheduled"] == []
    assert UserDataPurge.query.count() == 0
    assert _db.session.get(User, a_id).deleted_at is None
    assert dump.exists() and stash.exists()

    out = _run(enabled=True)
    assert out["scheduled"] == [a_id] and out["dry_run"] is False
    job = UserDataPurge.query.filter_by(user_id=a_id).one()
    assert job.delete_account and job.source == ua.UNCLAIMED_SOURCE
    assert job.scope == "all" and job.requested_by_id is None
    # Due at once: no 30-day window, nobody to restore it.
    assert job.scheduled_for == DAY_91
    assert _db.session.get(User, a_id).deleted_at is not None

    assert _run_due(job, DAY_91 + timedelta(minutes=1)) == "done"
    _db.session.expire_all()
    assert _db.session.get(User, a_id) is None
    assert Node.query.filter(
        (Node.user_id == a_id) | (Node.human_owner_id == a_id)).count() == 0
    assert UserProfile.query.filter_by(user_id=a_id).count() == 0
    assert UserArtifact.query.filter_by(user_id=a_id).count() == 0
    assert not dump.exists() and not stash.exists()
    # Cost records stay, detached from the person.
    erased = User.query.filter_by(username=ERASED_SYSTEM_USERNAME).one()
    row = APICostLog.query.filter_by(request_type="profile_batch").one()
    assert row.user_id == erased.id and row.request_ref is None
    # Nothing was sent to the person, and the handle is free for them.
    assert mail == []
    assert ReleasedUsername.query.count() == 0
    from backend.utils.reserved_usernames import derive_available_username
    assert derive_available_username("alicebirds") == "alicebirds"


def test_day_89_is_not_deleted(app, stubs):
    a = _prefilled("early")
    s = _status(a, DAY_89)
    assert s.status == "waiting"
    assert s.delete_on == OPENED + 90 * DAY
    out = _run(DAY_89, enabled=True)
    assert out["due"] == [] and out["scheduled"] == []
    assert UserDataPurge.query.count() == 0


def test_an_account_without_a_prefilled_handle_is_left_alone(app, stubs):
    u = _prefilled("plain")
    u.prefilled_handle = None
    _db.session.commit()
    out = _run(enabled=True)
    assert out["due"] == [] and out["counts"] == {}
    assert _db.session.get(User, u.id).deleted_at is None


def _llm_reply(u):
    llm = User.query.filter_by(username="claude-x").first() or _add(
        User(username="claude-x", twitter_id="llm-claude-x"))
    n = Node(user_id=llm.id, human_owner_id=u.id, node_type="llm",
             privacy_level="private", ai_usage="chat", token_count=1)
    n.set_content("reply")
    _add(n)


def _own_entry(u, **kw):
    n = Node(user_id=u.id, human_owner_id=u.id, node_type="user",
             privacy_level="private", ai_usage="chat", token_count=1, **kw)
    n.set_content("mine")
    _add(n)


SIGNALS = {
    "signed in": lambda u: setattr(u, "last_seen_at", OPENED + DAY),
    "accepted the terms": lambda u: setattr(u, "accepted_terms_at", OPENED),
    "signed in with X": lambda u: setattr(u, "twitter_handle", "alicebirds"),
    "answered the tweet seed": lambda u: setattr(u, "prefill_consent", "yes"),
    "chose a Glean setting": lambda u: setattr(u, "glean_enabled", False),
    "time zone": lambda u: setattr(u, "timezone", "Europe/Prague"),
    "changed a default": lambda u: setattr(u, "default_ai_usage", "none"),
    "asked to change the email": lambda u: setattr(u, "pending_email", "a@b.c"),
    "an entry written in Loore": lambda u: _own_entry(u),
    "an uploaded archive": lambda u: _own_entry(
        u, origin="twitter", provenance="archive_upload",
        source_key="twitter:upload-1"),
    "an AI reply asked for": _llm_reply,
    "an edited post": lambda u: _add(NodeVersion(
        node_id=Node.query.filter_by(human_owner_id=u.id).first().id,
        content="before")),
    "asked the AI": lambda u: _add(APICostLog(
        user_id=u.id, model_id="m", request_type="conversation",
        cost_microdollars=1)),
    "opened a document": lambda u: _add(ArtifactView(user_id=u.id,
                                                     kind="intentions")),
    "an API token": lambda u: _add(ApiToken(user_id=u.id, name="c",
                                            token_hash="h", prefix="p")),
    "read the updates": lambda u: _add(ChangelogReadState(user_id=u.id,
                                                          section_id="s1")),
    "a draft": lambda u: _add(Draft(user_id=u.id, content="d")),
    "saved the profile": lambda u: _add(UserProfile(
        user_id=u.id, content="mine", generated_by="user")),
    "read a notification": lambda u: _add(UserNotification(
        user_id=u.id, type="profile_ready", title="t", status="read",
        read_at=OPENED + DAY)),
    "connected X": lambda u: _add(ExternalAccount(
        user_id=u.id, provider="twitter", handle="h")),
    "renamed": lambda u: _add(UsernameHistory(user_id=u.id,
                                              old_username="old_name")),
    "a prompt of their own": lambda u: _add(UserPrompt(
        user_id=u.id, prompt_key="voice", title="v", content="p",
        generated_by="user")),
    "asked to delete their content": lambda u: _add(UserDataPurge(
        user_id=u.id, source="self", status="cancelled",
        requested_at=OPENED, scheduled_for=OPENED + 30 * DAY)),
}


@pytest.mark.parametrize("signal", sorted(SIGNALS))
def test_any_sign_of_a_person_keeps_the_account_for_good(app, stubs, signal):
    u = _prefilled("someone")
    SIGNALS[signal](u)
    _db.session.commit()
    s = _status(u)
    assert s.status == "used", s
    assert s.reasons
    out = _run(enabled=True)
    assert out["scheduled"] == [] and out["due"] == []
    assert _db.session.get(User, u.id).deleted_at is None


CONTROLS = {
    "a cost row of automation": lambda u: _add(APICostLog(
        user_id=u.id, model_id="x-api", request_type="x_prefill",
        cost_microdollars=1)),
    "a prompt copied from the defaults": lambda u: _add(UserPrompt(
        user_id=u.id, prompt_key="profile", title="p", content="p",
        generated_by="default")),
    "an unread notification": lambda u: _add(UserNotification(
        user_id=u.id, type="profile_ready", title="t")),
    "an admin's earlier purge": lambda u: _add(UserDataPurge(
        user_id=u.id, source="admin", status="cancelled",
        requested_at=OPENED, scheduled_for=OPENED + 30 * DAY)),
    "an approval by the admin": lambda u: setattr(u, "approved", True),
    "a pre-fill from before the provenance column": lambda u: _own_entry(
        u, origin="twitter", source_key="twitter:legacy-1"),
}


@pytest.mark.parametrize("control", sorted(CONTROLS))
def test_what_automation_leaves_is_not_a_sign_of_a_person(
        app, stubs, control):
    """Control: none of these is left by a person, so the account is
    still due (the signals above are not too broad)."""
    u = _prefilled("nobody")
    CONTROLS[control](u)
    _db.session.commit()
    assert _status(u).status == "due"


EXCLUSIONS = {
    "admin": lambda u, app: setattr(u, "is_admin", True),
    "plan set by hand": lambda u, app: setattr(u, "plan", "pro"),
    "tester list": lambda u, app: app.config.__setitem__(
        "GLEAN_USER_IDS", {u.id}),
    "kept for good": lambda u, app: setattr(
        u, "unclaimed_keep_until", ua.KEEP_FOREVER),
    "a waiting purge": lambda u, app: _add(UserDataPurge(
        user_id=u.id, source="admin", status="scheduled", scope="hidden",
        requested_at=DAY_89, scheduled_for=DAY_89 + 30 * DAY)),
    "AI account": lambda u, app: setattr(u, "twitter_id", "llm-model"),
}


@pytest.mark.parametrize("exclusion", sorted(EXCLUSIONS))
def test_each_exclusion_keeps_the_account(app, stubs, exclusion):
    u = _prefilled("excluded")
    EXCLUSIONS[exclusion](u, app)
    _db.session.commit()
    s = ua.assess(_db.session.get(User, u.id), DAY_91, config=app.config,
                  active_job_ids=ua.active_job_user_ids())
    assert s.status == "kept", s
    out = _run(enabled=True, config=app.config)
    assert out["scheduled"] == []


def test_a_subscription_keeps_the_account(app, stubs, monkeypatch):
    """Payments (#438) add these columns; the check reads them by name."""
    u = _prefilled("subscriber")
    monkeypatch.setattr(User, "stripe_customer_id", "cus_1", raising=False)
    assert _status(u).status == "kept"
    monkeypatch.setattr(User, "stripe_customer_id", None, raising=False)
    monkeypatch.setattr(User, "alpha_free_granted_at", OPENED, raising=False)
    assert _status(u).status == "kept"


def test_an_admins_keep_and_an_open_invitation_postpone_the_date(app, stubs):
    u = _prefilled("promised")
    u.unclaimed_keep_until = OPENED + 120 * DAY
    _db.session.commit()
    s = _status(u)
    assert s.status == "waiting" and s.delete_on == OPENED + 120 * DAY
    assert "kept by an admin" in s.reasons
    assert _status(u, OPENED + 121 * DAY).status == "due"

    v = _prefilled("invited", email="inv@example.com")
    # Activate & Welcome on day 80: a 30-day sign-in link.
    v.magic_link_expires_at = OPENED + 110 * DAY
    _db.session.commit()
    s = _status(v)
    assert s.status == "waiting" and s.delete_on == OPENED + 110 * DAY
    assert _status(v, OPENED + 111 * DAY).status == "due"


def test_accounts_opened_before_the_record_are_counted_never_deleted(
        app, stubs):
    old = _prefilled("oldtimer", opened=datetime(2026, 8, 20))
    out = _run(datetime(2026, 12, 1), enabled=True)
    assert out["before_record"] == [old.id]
    assert out["counts"] == {"before_record": 1}
    assert out["scheduled"] == [] and out["due"] == []
    assert _db.session.get(User, old.id).deleted_at is None


def test_a_row_opened_before_its_prefill_counts_from_the_prefill(app, stubs):
    """A whitelisted row pre-filled 30 days later: the period starts with
    the pre-fill. A second pre-fill later does not restart it."""
    u = _prefilled("later", posts=0)
    for i in range(2):
        _fetched_node(u, i, stored=OPENED + 30 * DAY)
    _db.session.commit()
    s = _status(u)
    assert s.status == "waiting"
    assert s.opened_at == OPENED + 30 * DAY
    assert s.delete_on == OPENED + 120 * DAY
    # A re-pre-fill on day 100 adds posts; the date stays.
    _fetched_node(u, 9, stored=OPENED + 100 * DAY)
    _db.session.commit()
    assert _status(u, OPENED + 121 * DAY).status == "due"


# ── Running it ──────────────────────────────────────────────────────────

def test_a_real_run_deletes_at_most_the_nightly_limit_oldest_first(
        app, stubs, monkeypatch):
    monkeypatch.setattr(ua, "MAX_DELETIONS_PER_RUN", 3)
    users = [_prefilled(f"user{i}", opened=OPENED + i * timedelta(hours=1))
             for i in range(5)]
    out = _run(DAY_91 + DAY, enabled=True)
    assert out["scheduled"] == [u.id for u in users[:3]]
    out = _run(DAY_91 + DAY, enabled=True)
    assert out["scheduled"] == [u.id for u in users[3:]]
    assert UserDataPurge.query.count() == 5


def test_running_twice_schedules_each_account_once(app, stubs):
    u = _prefilled("twice")
    assert _run(enabled=True)["scheduled"] == [u.id]
    assert _run(enabled=True)["scheduled"] == []
    job = UserDataPurge.query.one()
    assert _run_due(job, DAY_91 + timedelta(minutes=1)) == "done"
    out = _run(enabled=True)
    assert out["scheduled"] == [] and out["counts"] == {}
    assert UserDataPurge.query.count() == 1


def test_a_failure_in_one_account_does_not_stop_the_rest(
        app, stubs, monkeypatch, caplog):
    first, second, third = (_prefilled(f"acct{i}", opened=OPENED + i * DAY)
                            for i in range(3))
    real = acc.schedule_account_deletion

    def flaky(user, **kw):
        if user.id == second.id:
            raise RuntimeError("boom")
        return real(user, **kw)
    monkeypatch.setattr(acc, "schedule_account_deletion", flaky)
    with caplog.at_level(logging.ERROR, logger=ua.logger.name):
        out = _run(DAY_91 + 5 * DAY, enabled=True)
    assert out["scheduled"] == [first.id, third.id]
    assert out["failed"] == [second.id]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(str(second.id) in r.getMessage() for r in errors)
    assert "acct1" not in caplog.text   # ids only, no handles
    assert _db.session.get(User, second.id).deleted_at is None


def test_a_sign_in_found_at_the_last_check_keeps_the_account(
        app, stubs, monkeypatch):
    """The report saw the account due; a sign-in landed before its turn.
    The check under the row lock sees it and nothing is scheduled."""
    u = _prefilled("justintime")
    real = ua.report

    def report_then_sign_in(*a, **kw):
        out = real(*a, **kw)
        _db.session.execute(User.__table__.update().where(
            User.id == u.id).values(last_seen_at=DAY_91))
        _db.session.commit()
        return out
    monkeypatch.setattr(ua, "report", report_then_sign_in)
    out = _run(enabled=True)
    assert out["tonight"] == [u.id]
    assert out["scheduled"] == [] and out["skipped"] == [u.id]
    assert UserDataPurge.query.count() == 0


def test_signing_in_before_the_purge_runs_offers_a_restore(
        app, stubs, monkeypatch):
    import backend.routes.auth as auth
    u = _prefilled("racer", twitter_id="555")
    _run(enabled=True)
    fake = MagicMock()
    fake.authorized = True
    fake.get.return_value = MagicMock(
        ok=True, json=lambda: {"id": 555, "screen_name": "racer"})
    monkeypatch.setattr(auth, "twitter", fake)
    c = app.test_client()
    r = c.get("/auth/login")
    assert r.headers["Location"] == f"{FRONTEND}/account-restore"
    assert c.post("/api/account/restore").get_json()["status"] == "restored"
    _db.session.expire_all()
    assert _db.session.get(User, u.id).deleted_at is None
    # Theirs now: out of scope for good.
    assert _status(u, DAY_91 + 365 * DAY).status == "used"


def test_signing_in_after_the_deletion_is_a_new_signup(
        app, stubs, monkeypatch):
    import backend.routes.auth as auth
    u = _prefilled("returner", twitter_id="777")
    old_id = u.id
    _run(enabled=True)
    assert _run_due(UserDataPurge.query.one(),
                    DAY_91 + timedelta(minutes=1)) == "done"
    fake = MagicMock()
    fake.authorized = True
    fake.get.return_value = MagicMock(
        ok=True, json=lambda: {"id": 777, "screen_name": "returner"})
    monkeypatch.setattr(auth, "twitter", fake)
    r = app.test_client().get("/auth/login")
    assert r.headers["Location"] == f"{FRONTEND}/dashboard"
    new = User.query.filter_by(twitter_id="777").one()
    # A fresh, empty account under their own handle, waiting for approval
    # like any new sign-up.
    assert new.id != old_id and new.username == "returner"
    assert new.approved is False and new.prefilled_handle is None
    assert Node.query.filter_by(human_owner_id=new.id).count() == 0


def test_nothing_outside_the_account_is_touched(app, stubs):
    a = _prefilled("target")
    other = _prefilled("waits", opened=OPENED + 60 * DAY)
    member = _add(User(username="member", approved=True,
                       last_seen_at=OPENED, created_at=OPENED))
    _own_entry(member)
    _db.session.commit()
    keep = [
        _file(stubs.data / "x-api" / "waits-2026-09-10T12-00-00Z.jsonl"),
        _file(stubs.data / "community-archive" / "export" / "tweets.parquet"),
        _file(stubs.data / "ca-prefill" / f"{other.id}-9" / "page.jsonl"),
        _file(stubs.audio / "user" / str(member.id) / "node" / "1" / "a.webm"),
    ]
    before = {m.__name__: m.query.count()
              for m in (Node, UserProfile, UserArtifact)}
    out = _run(enabled=True)
    assert out["scheduled"] == [a.id]
    assert _run_due(UserDataPurge.query.one(),
                    DAY_91 + timedelta(minutes=1)) == "done"
    _db.session.expire_all()
    for f in keep:
        assert f.exists(), f
    assert _db.session.get(User, other.id).deleted_at is None
    assert _db.session.get(User, member.id) is not None
    assert Node.query.count() == before["Node"] - 3
    assert UserProfile.query.count() == before["UserProfile"] - 1
    assert UserArtifact.query.count() == before["UserArtifact"] - 1


def test_no_model_call_and_no_decryption(app, stubs, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the deletion decrypted content")
    import backend.utils.encryption as enc
    monkeypatch.setattr(enc, "decrypt_content", boom)
    monkeypatch.setattr(_real_backend_models, "decrypt_content", boom)
    _prefilled("quiet")
    _db.session.commit()
    costs = APICostLog.query.count()
    _run()
    _run(enabled=True)
    assert _run_due(UserDataPurge.query.one(),
                    DAY_91 + timedelta(minutes=1)) == "done"
    assert stubs.cancelled == [] and stubs.revoked == []
    # No new cost row: nothing called a provider.
    assert APICostLog.query.count() == costs


# ── Sign-ins are recorded ───────────────────────────────────────────────

def test_a_sign_in_by_email_link_is_recorded(app, stubs, mail):
    """The link sign-in stamps last_seen itself, as the X sign-in does, so
    the record does not depend on the app loading afterwards."""
    u = _prefilled("emailer", email="emailer@example.com")
    c = app.test_client()
    assert c.post("/auth/magic-link/send",
                  json={"email": "emailer@example.com"}).status_code == 200
    token = re.search(r"token=(\S+)", mail[-1][2]).group(1)
    c.get(f"/auth/magic-link/verify?token={token}")
    _db.session.expire_all()
    assert _db.session.get(User, u.id).last_seen_at is not None
    assert _status(u).status == "used"


# ── The admin's report and Keep, the CLI, the beat ──────────────────────

def test_the_admin_sees_due_waiting_and_kept_accounts_and_can_keep_one(
        app, stubs, monkeypatch):
    # A shorter period, so an account opened after the sign-in record
    # began can be due today.
    app.config["UNCLAIMED_ACCOUNT_DAYS"] = 30
    admin = _add(User(username="boss", approved=True, is_admin=True))
    due = _prefilled("dueone", opened=datetime.utcnow() - 40 * DAY)
    waiting = _prefilled("waitone", opened=datetime.utcnow() - 10 * DAY)
    _prefilled("oldone", opened=datetime(2026, 8, 1))
    used = _prefilled("usedone", opened=datetime.utcnow() - 40 * DAY)
    used.last_seen_at = datetime.utcnow()
    _db.session.commit()

    c = _client(app, admin)
    data = c.get("/api/admin/unclaimed_accounts").get_json()
    assert data["deletion_enabled"] is False and data["period_days"] == 30
    by = {a["user_id"]: a for a in data["accounts"]}
    assert set(by) == {due.id, waiting.id}
    assert by[due.id]["status"] == "due" and by[due.id]["delete_on"]
    assert by[waiting.id]["status"] == "waiting"
    assert data["used_count"] == 1
    assert data["before_record"]["count"] == 1

    r = c.put(f"/api/admin/users/{due.id}/unclaimed_keep",
              json={"until": "2099-01-31"})
    assert r.status_code == 200
    acct = r.get_json()["account"]
    assert acct["status"] == "waiting" and acct["delete_on"].startswith(
        "2099-01-31")
    r = c.put(f"/api/admin/users/{due.id}/unclaimed_keep",
              json={"until": "forever"})
    assert r.get_json()["account"]["kept_for_good"] is True
    assert r.get_json()["account"]["status"] == "kept"
    r = c.put(f"/api/admin/users/{due.id}/unclaimed_keep", json={"until": None})
    assert r.get_json()["account"]["status"] == "due"
    assert c.put(f"/api/admin/users/{due.id}/unclaimed_keep",
                 json={"until": "next week"}).status_code == 400
    # Not for members.
    member = _add(User(username="member2", approved=True))
    _db.session.commit()
    assert _client(app, member).get(
        "/api/admin/unclaimed_accounts").status_code == 403


def test_the_cli_prints_counts_and_ids_without_handles(app, stubs):
    from backend.unclaimed_cli import unclaimed_accounts_command
    app.cli.add_command(unclaimed_accounts_command)
    app.config["UNCLAIMED_ACCOUNT_DAYS"] = 30
    due = _prefilled("clihandle", opened=datetime.utcnow() - 40 * DAY)
    old = _prefilled("cliold", opened=datetime(2026, 8, 1))
    runner = app.test_cli_runner()
    result = runner.invoke(args=["unclaimed-accounts"])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert f"id {due.id}" in result.output
    assert "Due now: 1" in result.output
    assert str(old.id) in result.output and "never deleted automatically): 1" \
        in result.output
    assert "clihandle" not in result.output and "cliold" not in result.output

    result = runner.invoke(args=["unclaimed-accounts", "--keep", str(due.id),
                                 "--until", "forever"])
    assert result.exit_code == 0 and "for good" in result.output
    _db.session.expire_all()
    assert _db.session.get(User, due.id).unclaimed_keep_until == ua.KEEP_FOREVER


def test_the_task_runs_nightly_on_the_background_queue():
    import ast
    from backend.celery_queues import TASK_QUEUES
    from backend.tests.test_beat_schedule import SOURCE, _schedule_entries
    expr = _schedule_entries()["delete-unclaimed-accounts"]
    assert isinstance(expr, ast.Call) and expr.func.attr == "crontab"
    name = "backend.tasks.unclaimed_accounts.delete_unclaimed_accounts"
    assert TASK_QUEUES[name] == "background"
    assert "from backend.tasks import unclaimed_accounts" in SOURCE.read_text()


def test_deletion_is_off_by_default():
    from backend.config import Config
    assert Config.UNCLAIMED_ACCOUNT_DELETION_ENABLED is False
    assert Config.UNCLAIMED_ACCOUNT_DAYS == 90
