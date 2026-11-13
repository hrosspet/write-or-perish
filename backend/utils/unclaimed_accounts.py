"""Pre-filled accounts nobody claimed are deleted after a period (#685).

Peter, 2026-11-13: an account Loore opened and pre-filled for a person
before they joined (from their public posts), and that nobody has signed
in to, is deleted from Loore UNCLAIMED_ACCOUNT_DAYS (90) days after it was
opened, with everything in it: "90 days makes sense to remove it from prod
Loore. I wouldn't remove it from the cloud bucket". The privacy page
states the same. Material kept outside Loore accounts (the cloud bucket,
the shared Community Archive copy) is not touched.

Which accounts (``assess``), all of these:

* pre-filled: ``User.prefilled_handle`` is set. The admin's Community
  Archive and X API pre-fills set it (tasks/imports._import_prefill_rows);
  an account loaded from a bundle is in scope once the load sets it too.
  A whitelisted account that was never pre-filled holds no posts and is
  not deleted here.
* nobody used it (``person_signals``): no sign-in on record and nothing
  only a signed-in person can leave (entries of their own, settings they
  chose, answers, reads, tokens...). Any one signal keeps the account for
  good: it is theirs.
* opened on or after LAST_SEEN_SINCE (2026-08-29). Before that a sign-in
  left no record, so such an account is never deleted here; they are
  counted for the owner instead ("before_record").
* not excluded (``_exclusions``): an admin, an AI or system account, a
  plan only an admin sets, an account named in a tester list, an account
  with a subscription or payment record, an account whose deletion
  already waits or runs.
* its date has passed (``delete_on``): the period after it was opened,
  and not before an admin's "Keep" date (``User.unclaimed_keep_until``,
  which Activate & Welcome also sets for its link's 30 days).

Opened (``Assessment.opened_at``): ``User.prefilled_at``, the time this
server last made the account ready ahead of its owner (the admin's
pre-fills and scripts/load_user.py set it to now, every time: preparing an
account again opens it again). Accounts pre-filled before that column
existed fall back to ``User.created_at``, or to when their first
pre-filled entry was stored if that is later (a row whitelisted earlier;
the earliest ``updated_at`` of its fetched entries).

Only what the person does counts as a sign of use. Fields the admin or the
system write (approval, plan, the alpha free month, a time zone a loader
copies) do not; a mail scanner that opens a sign-in link does count, which
errs towards keeping the account.

Deleting: the account deletion of #269
(``account_deletion.schedule_account_deletion``) with its date set to
now, so the purge (#268) deletes everything the account holds: entries,
profile, intentions and other documents, embeddings, files and the X API
dumps of the pre-filled handle; cost records stay, detached from the
person. There is no 30-day restore window (nobody ever had the account),
no email (they never joined) and the handle is not held back (no page of
theirs ever existed, and the person whose handle it is may sign up
later). The purge checks once more under the user row lock before it
deletes anything (``recheck_before_purge``): a sign-in that landed in
between wins, and the account is shown again.

The nightly task (backend/tasks/unclaimed_accounts.py) is a dry run
unless UNCLAIMED_ACCOUNT_DELETION_ENABLED: it then only reports. Real
runs delete at most MAX_DELETIONS_PER_RUN accounts, oldest date first.

Ids, timestamps and counts only: nothing here reads or decrypts content,
and no model is called.
"""
import logging
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from sqlalchemy import and_, func, or_

from backend.extensions import db
from backend.models import (
    APICostLog, ApiToken, ArtifactView, ChangelogReadState, Draft,
    ExternalAccount, ExternalItem, FeedPick, Node, NodeVersion, PollResponse,
    ReferenceAction, ShareDraft, User, UserArtifact, UserDataPurge,
    UserFeedback, UsernameHistory, UserNotification, UserProfile, UserPrompt,
    UserTodo,
)

logger = logging.getLogger(__name__)

# ── Heuristics (see the PR for why each value) ──────────────────────────
# Accounts deleted per nightly run at most. A second guard after the dry
# run: a wrong rule loses at most this many accounts in one night before
# anyone reads the log, and a backlog of 100 due accounts clears in 10
# nights.
MAX_DELETIONS_PER_RUN = 10

# The purge job's source (UserDataPurge.source) for these deletions.
UNCLAIMED_SOURCE = "unclaimed"
# unclaimed_keep_until for an account an admin keeps for good.
KEEP_FOREVER = datetime(9999, 12, 31)
# Plans a new account gets without an admin: "alpha" (the default) and
# "free" (the default while payments are on, #438). Any other plan was
# set by hand.
DEFAULT_PLANS = ("alpha", "free")
# Node.provenance of entries Loore fetched (models.Node; the same as
# profile_providers.PUBLIC_PROVENANCES). Pre-fills from before the column
# existed (2026-09-21) left it NULL on their tweets.
FETCHED_PROVENANCES = ("prefill_ca", "prefill_x")
# generated_by of a version the person saved (an edit, a revert).
PERSON_VERSIONS = ("user", "revert")


def period_days(config=None):
    from flask import current_app
    config = config if config is not None else current_app.config
    return int(config.get("UNCLAIMED_ACCOUNT_DAYS") or 90)


def deletion_enabled(config=None):
    from flask import current_app
    config = config if config is not None else current_app.config
    return bool(config.get("UNCLAIMED_ACCOUNT_DELETION_ENABLED"))


# Where the period counts from (Assessment.basis).
BASIS_LABELS = {
    "prefilled": "pre-filled or loaded",
    "opened": "account opened (pre-filled before pre-fill dates were kept)",
    "first_entry": "first pre-filled entry stored (pre-filled before "
                   "pre-fill dates were kept; the row is older)",
}


class Assessment(NamedTuple):
    user_id: int
    username: str
    # "due": would be deleted now. "waiting": in scope, its date is later.
    # "used": someone used the account; never deleted. "kept": excluded
    # for good. "before_record": opened before the sign-in record.
    status: str
    opened_at: Optional[datetime]
    delete_on: Optional[datetime]
    # Why it is used, kept or waiting past the period (short labels).
    reasons: tuple = ()
    keep_until: Optional[datetime] = None
    # What opened_at is (a BASIS_LABELS key).
    basis: Optional[str] = None

    def why(self, days):
        """One line for the lists: why the account is due, or what it
        waits for; the reasons otherwise."""
        if self.status not in ("due", "waiting") or self.opened_at is None:
            return "; ".join(self.reasons)
        start = (f"{days} days counted from {self.opened_at:%Y-%m-%d} "
                 f"({BASIS_LABELS.get(self.basis, self.basis)})")
        tail = f"; {'; '.join(self.reasons)}" if self.reasons else ""
        if self.status == "due":
            return (f"no sign of a person and not excluded; {start}; date "
                    f"{self.delete_on:%Y-%m-%d} passed{tail}")
        return f"{start}; date {self.delete_on:%Y-%m-%d}{tail}"


# ── Signals that a person used the account ──────────────────────────────

# Signals on the user row itself, each set only by a signed-in person: a
# sign-in (an X or email-link sign-in, or any app request, since
# 2026-08-29), the terms, an answer, a setting they chose.
_COLUMN_SIGNALS = (
    ("signed in", lambda u: u.last_seen_at is not None),
    ("accepted the terms", lambda u: (
        u.accepted_terms_at is not None or bool(u.accepted_terms_version))),
    # The handle is stamped by an X sign-in or Connect X, never by the
    # whitelist (which stamps only the X id).
    ("signed in with X", lambda u: (
        bool(u.twitter_handle) or u.x_connected_at is not None)),
    ("answered the tweet-seed question", lambda u: (
        u.prefill_consent is not None or u.prefill_consent_at is not None)),
    ("asked to change the email", lambda u: (
        bool(u.pending_email) or bool(u.email_change_token_hash))),
    ("asked to delete the account",
     lambda u: bool(u.account_deletion_token_hash)),
    ("answered the profile-models question", lambda u: (
        bool(u.profile_both_providers)
        or u.profile_both_providers_answered_at is not None)),
    ("chose a Glean setting", lambda u: u.glean_enabled is not None),
    ("turned a setting on", lambda u: bool(
        u.craft_mode or u.public_sharing_enabled
        or u.external_content_enabled)),
    ("changed a default", lambda u: (
        (u.default_ai_usage or "chat") != "chat"
        or (u.default_privacy_level or "private") != "private")),
    # Not the time zone: the browser reports it, but scripts/load_user.py
    # copies one from a bundle too, and the two cannot be told apart.
)


def _column_signals(user):
    return [label for label, found in _COLUMN_SIGNALS if found(user)]


def _own_nodes(user_id):
    return or_(Node.user_id == user_id, Node.human_owner_id == user_id)


def _fetched():
    """An entry Loore fetched for the pre-fill: a tweet with a fetched
    provenance, or none (pre-fills before the column existed)."""
    return and_(Node.origin == "twitter",
                or_(Node.provenance.is_(None),
                    Node.provenance.in_(FETCHED_PROVENANCES)))


def _not_fetched():
    """The exact complement of _fetched(), spelled out so a NULL origin
    counts as not fetched (NOT of a NULL comparison would drop it)."""
    return or_(Node.origin.is_(None), Node.origin != "twitter",
               and_(Node.provenance.isnot(None),
                    Node.provenance.notin_(FETCHED_PROVENANCES)))


def _row_signals(user_id):
    """Signals in other tables: anything there exists only because a
    signed-in person did something. Exists checks on ids; hidden rows (a
    waiting content deletion) count too."""
    from backend.utils.activity import USER_REQUEST_TYPES
    from backend.utils.hidden_rows import including_hidden_rows

    checks = (
        ("an entry of their own",
         lambda: db.session.query(Node.id).filter(
             _own_nodes(user_id), _not_fetched())),
        ("edited an entry",
         lambda: db.session.query(NodeVersion.id).join(
             Node, NodeVersion.node_id == Node.id).filter(
             _own_nodes(user_id))),
        ("asked the AI, recorded or listened",
         lambda: db.session.query(APICostLog.id).filter(
             APICostLog.user_id == user_id,
             APICostLog.request_type.in_(USER_REQUEST_TYPES))),
        ("a draft", lambda: Draft.query.with_entities(Draft.id).filter(
            Draft.user_id == user_id)),
        ("a share draft",
         lambda: ShareDraft.query.with_entities(ShareDraft.id).filter(
             ShareDraft.user_id == user_id)),
        # Not the "default" rows a job copies from the file defaults.
        ("a prompt of their own",
         lambda: UserPrompt.query.with_entities(UserPrompt.id).filter(
             UserPrompt.user_id == user_id,
             UserPrompt.generated_by.in_(PERSON_VERSIONS))),
        ("sent feedback",
         lambda: UserFeedback.query.with_entities(UserFeedback.id).filter(
             UserFeedback.user_id == user_id)),
        ("answered a poll",
         lambda: PollResponse.query.with_entities(PollResponse.id).filter(
             PollResponse.user_id == user_id)),
        ("opened a document",
         lambda: db.session.query(ArtifactView.id).filter(
             ArtifactView.user_id == user_id)),
        ("an API token",
         lambda: db.session.query(ApiToken.id).filter(
             ApiToken.user_id == user_id)),
        ("read the updates",
         lambda: db.session.query(ChangelogReadState.id).filter(
             ChangelogReadState.user_id == user_id)),
        ("read a notification",
         lambda: db.session.query(UserNotification.id).filter(
             UserNotification.user_id == user_id,
             or_(UserNotification.read_at.isnot(None),
                 UserNotification.status != "unread"))),
        ("renamed the account",
         lambda: db.session.query(UsernameHistory.id).filter(
             UsernameHistory.user_id == user_id)),
        ("connected X",
         lambda: db.session.query(ExternalAccount.id).filter(
             ExternalAccount.user_id == user_id)),
        ("saved a reference",
         lambda: ExternalItem.query.with_entities(ExternalItem.id).filter(
             ExternalItem.user_id == user_id)),
        ("a Glean pick",
         lambda: FeedPick.query.with_entities(FeedPick.id).filter(
             FeedPick.user_id == user_id)),
        ("acted on a reference",
         lambda: ReferenceAction.query.with_entities(
             ReferenceAction.id).filter(ReferenceAction.user_id == user_id)),
        ("saved their profile",
         lambda: UserProfile.query.with_entities(UserProfile.id).filter(
             UserProfile.user_id == user_id,
             UserProfile.generated_by.in_(PERSON_VERSIONS))),
        ("saved their todo list",
         lambda: UserTodo.query.with_entities(UserTodo.id).filter(
             UserTodo.user_id == user_id,
             UserTodo.generated_by.in_(PERSON_VERSIONS))),
        ("saved a document",
         lambda: UserArtifact.query.with_entities(UserArtifact.id).filter(
             UserArtifact.user_id == user_id,
             or_(UserArtifact.generated_by.in_(PERSON_VERSIONS),
                 UserArtifact.updated_at.isnot(None)))),
        ("asked to delete their content",
         lambda: db.session.query(UserDataPurge.id).filter(
             UserDataPurge.user_id == user_id,
             UserDataPurge.source == "self")),
    )
    out = []
    with including_hidden_rows():
        for label, query in checks:
            if query().first() is not None:
                out.append(label)
                # One is enough to keep the account; the rest cost queries.
                break
    return out


def person_signals(user, *, rows=True):
    """Why somebody has used this account, as short labels; empty when
    nothing says so. *rows* False checks the user row only (cheap)."""
    out = _column_signals(user)
    if out or not rows:
        return out
    return _row_signals(user.id)


# ── Exclusions ──────────────────────────────────────────────────────────

def _tester_ids(config):
    ids = set()
    for key in ("GLEAN_USER_IDS", "PROFILE_BATCH_USER_IDS"):
        ids.update(config.get(key) or ())
    return ids


def _exclusions(user, config, active_job_ids, ignore_own_deletion=False):
    """Why the account is never deleted here (for as long as it holds).
    *ignore_own_deletion*: the purge's own check of an account its job
    has already hidden."""
    from backend.utils.system_accounts import SYSTEM_USERNAMES
    out = []
    if user.is_admin:
        out.append("admin")
    if (user.username in SYSTEM_USERNAMES
            or (user.twitter_id or "").startswith("llm")):
        out.append("AI or system account")
    if (user.plan or "alpha") not in DEFAULT_PLANS:
        out.append(f"plan set by hand ({user.plan})")
    if user.id in _tester_ids(config):
        out.append("named in a tester list")
    # Payments (#438): read by name, so this holds before and after those
    # columns exist. Only what a person's checkout leads to: the alpha
    # free month (alpha_free_granted_at) is given by a command to approved
    # accounts, pre-filled ones included, and says nothing about a person.
    if (getattr(user, "stripe_customer_id", None)
            or getattr(user, "stripe_subscription_id", None)
            or getattr(user, "subscription_status", None) not in (None, "none")
            or getattr(user, "subscription_ever_paid", False)):
        out.append("subscription or payment record")
    if not ignore_own_deletion and (
            user.deleted_at is not None or user.id in active_job_ids):
        out.append("a deletion already waits or runs")
    if user.unclaimed_keep_until is not None and \
            user.unclaimed_keep_until >= KEEP_FOREVER:
        out.append("kept for good by an admin")
    return out


# ── Dates ───────────────────────────────────────────────────────────────

def first_prefill_at(user_id):
    """When the account's first pre-filled entry was stored: the earliest
    ``updated_at`` of its fetched entries (set at insert, only ever moved
    later). None without any."""
    return db.session.query(func.min(Node.updated_at)).filter(
        _own_nodes(user_id), _fetched()).scalar()


def _postponed(user, base):
    """(delete_on, reasons): the period's end, moved to the Keep date (an
    admin's, or Activate & Welcome's) when that is later. Not a pending
    sign-in link itself: a later link request replaces its expiry."""
    delete_on, reasons = base, []
    keep = user.unclaimed_keep_until
    if keep is not None and keep > delete_on:
        delete_on = keep
        reasons.append(f"kept until {keep:%Y-%m-%d} (an admin's Keep or "
                       "Activate & Welcome)")
    return delete_on, reasons


def assess(user, now, *, config, active_job_ids=frozenset(),
           ignore_own_deletion=False):
    """The Assessment of one pre-filled account at *now*. The queries
    (other tables, the pre-fill date of an older account) run only once
    its date has passed: before that it cannot be due, and its listed date
    is the earliest it could be deleted."""
    from backend.utils.activity import LAST_SEEN_SINCE
    period = timedelta(days=period_days(config))
    if user.prefilled_at is not None:
        opened, basis = user.prefilled_at, "prefilled"
    else:
        opened, basis = user.created_at, "opened"

    def result(status, reasons=(), opened_at=opened, delete_on=None,
               basis=basis):
        return Assessment(user.id, user.username, status, opened_at,
                          delete_on, tuple(reasons), user.unclaimed_keep_until,
                          basis)

    signals = person_signals(user, rows=False)
    if signals:
        return result("used", signals)
    excluded = _exclusions(user, config, active_job_ids, ignore_own_deletion)
    if excluded:
        return result("kept", excluded)
    # Whether a sign-in could have gone unrecorded depends on when the row
    # existed, whatever was done to it later.
    if user.created_at is None or user.created_at < LAST_SEEN_SINCE:
        return result("before_record")

    delete_on, reasons = _postponed(user, opened + period)
    if delete_on > now:
        return result("waiting", reasons, delete_on=delete_on)

    signals = person_signals(user)
    if signals:
        return result("used", signals)
    if basis == "opened":
        first = first_prefill_at(user.id)
        if first is not None and first > opened:
            opened, basis = first, "first_entry"
            delete_on, reasons = _postponed(user, opened + period)
    return result("waiting" if delete_on > now else "due", reasons,
                  opened_at=opened, delete_on=delete_on, basis=basis)


def active_job_user_ids():
    return {uid for (uid,) in db.session.query(UserDataPurge.user_id).filter(
        UserDataPurge.status.in_(UserDataPurge.ACTIVE_STATUSES))}


def candidates():
    """Every pre-filled account that still exists and is not hidden."""
    return User.query.filter(
        User.prefilled_handle.isnot(None),
        User.deleted_at.is_(None),
    ).order_by(User.id).all()


def report(now=None, *, config=None):
    """Every pre-filled account's Assessment, due ones first (oldest date
    first), then waiting ones by date, then the rest by id."""
    from flask import current_app
    config = config if config is not None else current_app.config
    now = now or datetime.utcnow()
    jobs = active_job_user_ids()
    out = [assess(u, now, config=config, active_job_ids=jobs)
           for u in candidates()]
    order = {"due": 0, "waiting": 1, "kept": 2, "before_record": 3, "used": 4}
    return sorted(out, key=lambda a: (
        order[a.status], a.delete_on or datetime.max, a.user_id))


def summary(assessments):
    """Counts and ids per status (ids only: no handles in logs)."""
    out = {}
    for a in assessments:
        out.setdefault(a.status, []).append(a.user_id)
    return {k: {"count": len(v), "ids": v} for k, v in out.items()}


# ── Deleting ────────────────────────────────────────────────────────────

def delete_one(user_id, now, *, config):
    """Schedule the deletion of one due account, due at once, after the
    full check again under the user row lock (a sign-in that lands
    meanwhile waits for the lock and then finds the account hidden, which
    offers a restore). Returns the job, or None when the account is not
    due any more (nothing changed)."""
    from backend.utils.account_deletion import schedule_account_deletion
    # populate_existing: the report loaded this row earlier in the run; a
    # sign-in since must be seen.
    user = db.session.query(User).filter(
        User.id == user_id).populate_existing().with_for_update().one_or_none()
    if user is None or user.prefilled_handle is None:
        db.session.rollback()
        return None
    a = assess(user, now, config=config,
               active_job_ids=active_job_user_ids())
    if a.status != "due":
        db.session.rollback()
        logger.info("unclaimed account %s: not due any more (%s); kept",
                    user_id, a.status)
        return None
    return schedule_account_deletion(
        user, requested_by_id=None, source=UNCLAIMED_SOURCE, at=now)


def recheck_before_purge(job, now=None, *, config=None):
    """The purge's own check, right before it deletes anything of an
    unclaimed account (``user_purge.run_purge_job``): the user row is
    locked and the account assessed again. Still unclaimed and due: True,
    and the lock holds until the purge's first commit. Otherwise (a
    sign-in landed after the account was hidden, an admin's Keep, an
    exclusion): the job is cancelled and the account shown again, in this
    one transaction, and False. The sign-in wins."""
    from flask import current_app
    config = config if config is not None else current_app.config
    # The job was scheduled when the account was due and runs no earlier.
    now = now or max(datetime.utcnow(), job.scheduled_for)
    user = db.session.query(User).filter(
        User.id == job.user_id).populate_existing().with_for_update(
    ).one_or_none()
    if user is None:
        return True
    a = assess(user, now, config=config, ignore_own_deletion=True)
    if a.status == "due":
        return True
    n = UserDataPurge.query.filter(
        UserDataPurge.id == job.id,
        UserDataPurge.status.in_(UserDataPurge.ACTIVE_STATUSES),
    ).update({UserDataPurge.status: "cancelled",
              UserDataPurge.cancelled_at: now,
              UserDataPurge.error: f"kept at the last check: {a.status}"},
             synchronize_session=False)
    if n:
        user.deleted_at = None
    db.session.commit()
    if n:
        from backend.utils import user_purge
        user_purge.settle_restore([job.id])
        user_purge._drop_public_pages(user)
        logger.warning("Unclaimed account %s: kept at the purge's last check "
                       "(%s); job %s cancelled, account shown again",
                       user.id, a.status, job.id)
    return False


def run(now=None, *, config=None, enabled=None, limit=None):
    """One nightly pass. Dry run unless *enabled* (default: the config
    switch): it reports what it would delete and changes nothing. Real:
    schedules the deletion of at most *limit* due accounts, oldest date
    first; a failure in one account is logged as an error and the rest go
    on. Returns counts and ids."""
    from flask import current_app
    config = config if config is not None else current_app.config
    now = now or datetime.utcnow()
    enabled = deletion_enabled(config) if enabled is None else enabled
    limit = MAX_DELETIONS_PER_RUN if limit is None else limit

    assessments = report(now, config=config)
    due = [a.user_id for a in assessments if a.status == "due"]
    # Per due account: where its period counts from and when it ended
    # (ids and dates, no handles).
    due_detail = [
        f"{a.user_id} (counted from {a.opened_at:%Y-%m-%d}, {a.basis}; "
        f"date {a.delete_on:%Y-%m-%d})"
        for a in assessments if a.status == "due"]
    out = {
        "dry_run": not enabled,
        "period_days": period_days(config),
        "counts": {k: v["count"] for k, v in summary(assessments).items()},
        "due": due,
        "due_detail": due_detail,
        "tonight": due[:limit],
        "before_record": [a.user_id for a in assessments
                          if a.status == "before_record"],
        "scheduled": [], "skipped": [], "failed": [],
    }
    from backend.utils.activity import LAST_SEEN_SINCE
    since = LAST_SEEN_SINCE.date().isoformat()
    if not enabled:
        logger.warning(
            "Unclaimed pre-filled accounts (dry run, nothing deleted): %d "
            "due (no sign of a person, not excluded, period over) %s, a "
            "real run would delete %s; %d waiting; %d opened before %s, "
            "never deleted here", len(due), "; ".join(due_detail),
            out["tonight"], out["counts"].get("waiting", 0),
            len(out["before_record"]), since)
        return out

    for user_id in out["tonight"]:
        try:
            job = delete_one(user_id, now, config=config)
        except Exception as e:  # noqa: BLE001 - the next account goes on
            db.session.rollback()
            logger.error("Unclaimed account %s: its deletion was not "
                         "scheduled (%s)", user_id, type(e).__name__)
            out["failed"].append(user_id)
            continue
        if job is None:
            out["skipped"].append(user_id)
        else:
            out["scheduled"].append(user_id)
    logger.warning(
        "Unclaimed pre-filled accounts: deletion scheduled for %s (purged "
        "within minutes); %d due left for later runs; %d waiting; %d "
        "opened before %s, never deleted here; failed %s",
        out["scheduled"], max(len(due) - len(out["tonight"]), 0),
        out["counts"].get("waiting", 0), len(out["before_record"]),
        since, out["failed"])
    return out


# ── The admin's Keep ────────────────────────────────────────────────────

def parse_until(value):
    """A Keep value from the admin: "forever", "clear" / empty / None
    (cleared) or a date (YYYY-MM-DD: kept to the end of that day, UTC).
    Raises ValueError."""
    value = (value or "").strip().lower()
    if value == "forever":
        return KEEP_FOREVER
    if value in ("clear", "none", ""):
        return None
    day = datetime.strptime(value, "%Y-%m-%d")
    return day.replace(hour=23, minute=59, second=59)


def set_keep(user, until):
    """Set (a datetime, KEEP_FOREVER) or clear (None) the admin's Keep on
    *user*. Commits. Returns the stored value."""
    user.unclaimed_keep_until = until
    db.session.commit()
    return user.unclaimed_keep_until


def keep_at_least(user, until):
    """Keep *user* at least until *until* (Activate & Welcome): a later
    Keep stays. The caller commits."""
    if user.unclaimed_keep_until is None or user.unclaimed_keep_until < until:
        user.unclaimed_keep_until = until


# Which accounts a bulk Keep takes besides a list of ids (keep_many).
BULK_SCOPES = {"due": ("due",), "upcoming": ("due", "waiting")}


def keep_many(*, until, user_ids=None, scope=None, now=None, config=None):
    """The admin's Keep on several accounts at once (accounts are
    prepared and invited in waves): the ids given, or every account of
    *scope* ("due", or "upcoming" = due and waiting) in the report now.
    Unknown ids are skipped. One commit. Returns the ids changed."""
    if scope is not None:
        statuses = BULK_SCOPES[scope]
        user_ids = [a.user_id for a in report(now, config=config)
                    if a.status in statuses]
    ids = sorted({int(i) for i in (user_ids or ())})
    users = User.query.filter(User.id.in_(ids)).all() if ids else []
    for user in users:
        user.unclaimed_keep_until = until
    db.session.commit()
    return sorted(u.id for u in users)


def assessment_json(a, days=None):
    from backend.utils.timefmt import iso_utc
    forever = a.keep_until is not None and a.keep_until >= KEEP_FOREVER
    return {
        "user_id": a.user_id, "username": a.username, "status": a.status,
        "opened_at": iso_utc(a.opened_at),
        # Where the period counts from, and why the account is due (or
        # what it waits for).
        "counted_from": BASIS_LABELS.get(a.basis),
        "why": a.why(days if days is not None else period_days()),
        "delete_on": None if (a.delete_on is None or a.delete_on >= KEEP_FOREVER)
        else iso_utc(a.delete_on),
        "reasons": list(a.reasons),
        "keep_until": None if a.keep_until is None or forever
        else iso_utc(a.keep_until),
        "kept_for_good": forever,
    }
