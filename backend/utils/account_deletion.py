"""Account deletion (#269): the identity layer on top of the data purge
(#268, backend/utils/user_purge.py).

Asking:
* The user asks on the Account page and types their username. An
  account with an email address confirms from a link mailed to it,
  inside a signed-in session of the account (as the email change of
  #260 does); an account without one is scheduled at once.
* An admin deletes from the dashboard: dry-run counts first, the
  username typed, then it runs at once (as the admin purge does).

Scheduling (``schedule_account_deletion``) hides the account at once
(``User.deleted_at``): every session stops working, sign-in links are
invalidated, the public pages answer 404, other members see its public
writing as deleted, and no background job runs for it. The deletion is
a ``UserDataPurge`` job with ``delete_account``, due after the grace
period (an admin's at once). A waiting "Delete all my writing" request
of the user becomes this job.

Signing in during the grace period offers a restore
(``restore_account``): the job is cancelled and the account shown again.
Once the job has started, nothing can be restored.

Deleting (``delete_identity``, called by ``user_purge.run_purge_job``
after the purge has verified that nothing of the user's is left):
* the user's tombstones (entries other people replied under, kept by the
  purge) move to the ``loore-erased`` account, like the cost rows;
* other rows' references to the account are cleared (``node.pinned_by``,
  ``poll.created_by``);
* the rows that exist only for the account are deleted
  (``username_history``, ``api_token``, ``changelog_read_state``);
* the account's handles, current and former, are reserved for
  USERNAME_RESERVE_DAYS (``ReleasedUsername``);
* every foreign key into ``user`` is counted and must be 0, then the
  user row is deleted. The job row (``user_data_purge``) stays, with ids
  and counts only.
A confirmation email goes to the address the account had, after the
commit.

Nothing here reads content: ids, names of tables and counts only.
"""
import logging
import time
from collections import Counter
from datetime import datetime, timedelta

from itsdangerous import BadSignature, SignatureExpired
from sqlalchemy import and_, func, or_

from backend.extensions import db
from backend.models import (
    ApiToken, ChangelogReadState, Node, Poll, ReleasedUsername, User,
    UserDataPurge, UsernameHistory,
)
from backend.utils import user_purge
from backend.utils.user_purge import (
    PURGE_GRACE_DAYS, PurgeIncomplete, active_job, plan_nodes, purge_refusal,
)

logger = logging.getLogger(__name__)

# ── Heuristics (see the PR for why each value) ──────────────────────────
# Peter's deletion rule (LOORE-ESSENCE "Deletion", 2026-10-02): a deleted
# account has a 30-day grace period. The same constant as the data purge.
ACCOUNT_DELETION_GRACE_DAYS = PURGE_GRACE_DAYS
# How long a deleted account's handles stay out of reach of new accounts.
USERNAME_RESERVE_DAYS = 365
# Lifetime of the emailed confirmation link.
DELETION_LINK_SECONDS = 3600
# How long a sign-in into a deleted account may take to answer the
# restore question (the offer lives in the browser's session).
RESTORE_OFFER_SECONDS = 15 * 60

_LINK_SALT = "account-deletion"
RESTORE_SESSION_KEY = "account_restore"


class AccountDeletionRefused(Exception):
    """The account cannot be deleted (now). ``code`` is for the client."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _now():
    return datetime.utcnow()


def format_day(value):
    """5 November 2026 (UTC), for emails."""
    return f"{value.day} {value.strftime('%B %Y')}" if value else ""


def session_user(user_id):
    """flask-login's user loader: the account behind a session or a
    remember cookie, or None. A deleted account in its grace period is
    signed out everywhere at once this way; signing in again offers a
    restore (routes/auth.py)."""
    try:
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    if user is None or user.deleted_at is not None:
        return None
    return user


# ── Who may be deleted ──────────────────────────────────────────────────

def _other_live_admin(user_id):
    return db.session.query(User.id).filter(
        User.is_admin.is_(True), User.id != user_id,
        User.deleted_at.is_(None)).first() is not None


def deletion_refusal(user):
    """(code, message) when *user* must not be deleted, else None: AI and
    system accounts never (deleting an AI account would delete every AI
    reply in Loore), and never the last admin who is not being deleted."""
    reason = purge_refusal(user)
    if reason == "no such account":
        return "not_found", "No such account."
    if reason:
        return "refused", (f"Refused ({reason}): AI and system accounts "
                           "are never deleted.")
    if user.is_admin and not _other_live_admin(user.id):
        return "last_admin", ("This is the last admin account. Make "
                              "another account an admin first.")
    return None


def account_deletion_info(user):
    """What the Account page states about deleting the account."""
    refusal = deletion_refusal(user)
    return {
        "grace_days": ACCOUNT_DELETION_GRACE_DAYS,
        "username_reserve_days": USERNAME_RESERVE_DAYS,
        # An account with an email address confirms from a mailed link.
        "confirm_by_email": bool(user.email),
        "link_expires_in": DELETION_LINK_SECONDS,
        "refusal": ({"code": refusal[0], "message": refusal[1]}
                    if refusal else None),
        # The account signs in with X, and whether this session holds that
        # sign-in's token, which the deletion revokes at X (Peter,
        # 2026-10-09). The dialog's X note depends on both.
        "x_sign_in": bool(user.twitter_id),
        "x_sign_in_revocable": session_x_sign_in_token(user) is not None,
    }


# ── "Sign in with X" (Peter, 2026-10-09: "pls ship the PRs with revoke") ─

def session_x_sign_in_token(user):
    """The OAuth 1.0a token of this request's "Sign in with X", when it is
    the X account *user* signs in with and has not expired; else None.

    flask-dance keeps the token in the browser's session after the
    sign-in; the server stores none. A session can hold another X
    account's token (a shared browser that signed in with X, then by
    email link), so the token's X user id must be the account's."""
    from flask import current_app, has_request_context
    if not has_request_context() or not user.twitter_id:
        return None
    bp = current_app.blueprints.get("twitter")
    try:
        token = bp.token if bp is not None else None
    except Exception:  # noqa: BLE001 - an unreadable token is no token
        return None
    if not isinstance(token, dict):
        return None
    if str(token.get("user_id") or "") != str(user.twitter_id):
        return None
    if not token.get("oauth_token") or not token.get("oauth_token_secret"):
        return None
    expires_at = token.get("expires_at")   # a UTC Unix time (flask-dance)
    if expires_at and float(expires_at) <= time.time():
        return None
    return token


def end_x_sign_in(user):
    """At the user's own deletion request: invalidate this session's
    "Sign in with X" token at X, then drop it from the session.

    Best effort, as the purge's revoke of the bookmark connection: one
    call bounded by X_REVOKE_TIMEOUT_SECONDS; a failure is logged without
    the token and never stops the deletion. X answering 401 means the
    token was already invalid (the user removed Loore on X), a warning.
    A restore stays possible: signing in with X again asks X again.
    Returns True when X confirmed."""
    from flask import current_app
    from backend.utils.external_content import x_invalidate_sign_in_token
    from backend.utils.user_purge import X_REVOKE_TIMEOUT_SECONDS
    token = session_x_sign_in_token(user)
    ok = False
    if token is not None:
        try:
            x_invalidate_sign_in_token(
                token["oauth_token"], token["oauth_token_secret"],
                current_app.config.get("TWITTER_API_KEY"),
                current_app.config.get("TWITTER_API_SECRET"),
                timeout=X_REVOKE_TIMEOUT_SECONDS)
            ok = True
            logger.info("account deletion of user %s: the X sign-in was "
                        "revoked at X", user.id)
        except Exception as e:  # noqa: BLE001 - never stops the deletion
            status = getattr(getattr(e, "response", None), "status_code",
                             None)
            (logger.warning if status == 401 else logger.error)(
                "account deletion of user %s: X did not revoke the X "
                "sign-in (%s%s); the deletion goes ahead", user.id,
                type(e).__name__, f", HTTP {status}" if status else "")
    # As routes/auth.py's _drop_x_token: the next "Sign in with X" in this
    # browser goes through X again (it would fail on the revoked token).
    bp = current_app.blueprints.get("twitter")
    if bp is not None:
        try:
            del bp.token
        except KeyError:
            pass
    return ok


# ── The emailed confirmation link (email accounts) ──────────────────────

def _serializer():
    from backend.utils.magic_link import _get_serializer
    return _get_serializer()


def issue_confirmation_link(user):
    """A token for the confirmation link; its hash and expiry go on the
    user row (a new request replaces the old link). Returns the token."""
    from backend.utils.magic_link import hash_token
    token = _serializer().dumps({"user_id": user.id}, salt=_LINK_SALT)
    user.account_deletion_token_hash = hash_token(token)
    user.account_deletion_expires_at = _now() + timedelta(
        seconds=DELETION_LINK_SECONDS)
    db.session.commit()
    return token


def check_confirmation(user, token):
    """None when *token* confirms *user*'s deletion now, else a reason:
    "invalid_or_expired" or "other_account" (the link belongs to another
    account than the signed-in one)."""
    from backend.utils.magic_link import hash_token
    if not isinstance(token, str) or not token:
        return "invalid_or_expired"
    try:
        payload = _serializer().loads(token, salt=_LINK_SALT,
                                      max_age=DELETION_LINK_SECONDS)
    except (SignatureExpired, BadSignature):
        return "invalid_or_expired"
    if not isinstance(payload, dict) or payload.get("user_id") != user.id:
        return "other_account"
    if (user.account_deletion_token_hash != hash_token(token)
            or user.account_deletion_expires_at is None
            or user.account_deletion_expires_at < _now()):
        return "invalid_or_expired"
    return None


# ── Schedule, restore ───────────────────────────────────────────────────

def _drop_public_pages(user):
    user_purge._drop_public_pages(user)


def _hide(user, now):
    """Hide the account and invalidate everything that signs in to it."""
    user.deleted_at = user.deleted_at or now
    user.magic_link_token_hash = None
    user.magic_link_expires_at = None
    user.pending_email = None
    user.email_change_token_hash = None
    user.email_change_expires_at = None
    user.account_deletion_token_hash = None
    user.account_deletion_expires_at = None


def _own_legacy_ai_replies(user_id):
    """Set ``human_owner_id`` on the user's legacy AI replies (stored
    before the column existed, still NULL), so every check of a hidden
    owner covers them during the grace period. Which replies is the
    purge's own rule (``user_purge._legacy_ai_reply_ids``, the rule of
    ``find_human_owner`` and of the ``backfill-human-owner`` command):
    the nearest ancestor that is not an AI reply is the user's. A reply
    whose chain ends without such an ancestor gets no owner. Only rows
    still NULL change, ``updated_at`` is kept, and the value is the
    user's whether the deletion goes ahead or the account is restored.
    Returns how many rows changed."""
    changed = 0
    for chunk in user_purge._chunks(sorted(
            user_purge._legacy_ai_reply_ids(user_id))):
        changed += Node.query.filter(
            Node.id.in_(chunk), Node.node_type == "llm",
            Node.human_owner_id.is_(None),
        ).update({Node.human_owner_id: user_id,
                  Node.updated_at: Node.updated_at},
                 synchronize_session=False)
    return changed


def schedule_account_deletion(user, *, requested_by_id, source, at=None):
    """Hide the account and schedule its deletion, due at *at* (default:
    after the grace period). A waiting purge of the user (their "Delete
    all my writing", or an earlier account deletion) becomes this job.
    Returns the job. Raises AccountDeletionRefused."""
    now = _now()
    due = at or now + timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)
    # One active job per user: the user row lock serialises requests. For
    # an admin, every admin row is locked (in id order, so two requests
    # cannot deadlock): the last-admin check reads the other admins, and
    # two admins deleting at the same moment must not both pass it.
    if user.is_admin:
        db.session.query(User.id).filter(User.is_admin.is_(True)).order_by(
            User.id).with_for_update().all()
    else:
        db.session.query(User).filter(
            User.id == user.id).with_for_update().one()
    job = active_job(user.id)
    # Checked under the locks.
    refusal = deletion_refusal(user)
    if refusal:
        db.session.rollback()
        raise AccountDeletionRefused(*refusal)
    if job is not None and job.status == "running":
        db.session.commit()
        raise AccountDeletionRefused(
            "already_running", "A deletion of this account's data is "
            "running now. Try again when it has finished.")
    if job is None:
        job = UserDataPurge(user_id=user.id, status="scheduled")
        db.session.add(job)
    job.delete_account = True
    # Everything of the account's goes, also what a waiting "Delete all
    # my writing" did not hide (what the user wrote after it). The
    # writing that request hid stays hidden with the account.
    job.scope = "all"
    job.source = source
    job.requested_by_id = requested_by_id
    job.requested_at = now
    job.scheduled_for = due
    _own_legacy_ai_replies(user.id)
    _hide(user, now)
    db.session.commit()
    _drop_public_pages(user)
    logger.warning("Account deletion of user %s scheduled for %s (job %s, "
                   "%s by %s)", user.id, due, job.id, source, requested_by_id)
    return job


def restore_account(user):
    """Cancel the waiting deletion and show the account again. True when
    the account is live afterwards; False once the deletion has started
    (it can no longer be undone), and for an admin's deletion, which has
    no grace period. The cancel is a conditional update on the job's
    status, so a restore and the beat's claim have one winner. If the
    deletion replaced a "Delete all my writing", the writing that request
    hid is shown again too (restoring cancels both)."""
    if user.deleted_at is None:
        return True
    # A "Delete all my writing" this deletion replaced is cancelled with
    # it, and the writing it hid comes back, in the same transaction.
    cancelled = user_purge.cancel_jobs(
        UserDataPurge.user_id == user.id,
        UserDataPurge.delete_account.is_(True),
        UserDataPurge.source == "self",
        cancelled_by_id=user.id)
    if not cancelled and _deletion_started(user.id):
        db.session.rollback()
        return False
    user.deleted_at = None
    db.session.commit()
    _drop_public_pages(user)
    logger.warning("Account of user %s restored", user.id)
    return True


def _deletion_started(user_id):
    """The account deletion can no longer be undone: it has started, or
    an admin asked for it (due at once, no grace period)."""
    return db.session.query(UserDataPurge.id).filter(
        UserDataPurge.user_id == user_id,
        UserDataPurge.delete_account.is_(True),
        or_(UserDataPurge.status.in_(("running", "failed", "done")),
            and_(UserDataPurge.status == "scheduled",
                 UserDataPurge.source != "self")),
    ).first() is not None


def deletion_job(user_id):
    """The account deletion waiting or running for *user_id*, or None."""
    return UserDataPurge.query.filter(
        UserDataPurge.user_id == user_id,
        UserDataPurge.delete_account.is_(True),
        UserDataPurge.status.in_(UserDataPurge.ACTIVE_STATUSES),
    ).order_by(UserDataPurge.id.desc()).first()


def restore_offer(user):
    """What the restore page shows for a deleted account in its grace
    period: the username, when it is deleted, and whether it can still
    be restored."""
    from backend.models import UserDataPurgeHidden
    from backend.utils.timefmt import iso_utc
    job = deletion_job(user.id)
    return {
        "username": user.username,
        "delete_on": iso_utc(job.scheduled_for) if job else None,
        "restorable": not _deletion_started(user.id),
        # The deletion replaced a "Delete all my writing" whose writing is
        # still hidden: a restore brings that writing back too (#268).
        "writing_comes_back": job is not None and db.session.query(
            UserDataPurgeHidden.id).filter(
            UserDataPurgeHidden.job_id == job.id).first() is not None,
    }


# ── The dry run (admin) ─────────────────────────────────────────────────

def count_account_data(user_id):
    """What deleting the account would delete, change and keep: the
    purge's counts, the identity layer's, and the blast radius (public
    writing, replies in other people's threads). Changes nothing. Raises
    PurgeRefused for an AI or system account."""
    counts = user_purge.purge_user_content(user_id, dry_run=True)
    plan = plan_nodes(user_id)
    owned = plan.owned_select(user_id)
    identity = {
        "user": 1,
        "username_history": UsernameHistory.query.filter_by(
            user_id=user_id).count(),
        "api_token": ApiToken.query.filter_by(user_id=user_id).count(),
        "changelog_read_state": ChangelogReadState.query.filter_by(
            user_id=user_id).count(),
        "node.pinned_by": Node.query.filter(
            Node.pinned_by == user_id, ~Node.id.in_(owned)).count(),
        "poll.created_by": Poll.query.filter(
            Poll.created_by == user_id).count(),
        # Kept as empty placeholders, moved to loore-erased.
        "node_reattributed": len(plan.keep),
    }
    blast = {
        "public_nodes": Node.query.filter(
            Node.id.in_(owned), Node.privacy_level == "public",
            Node.deleted_at.is_(None)).count(),
        "replies_to_others": sum(
            1 for pid in plan.parents.values()
            if pid is not None and pid not in plan.parents),
    }
    return {"counts": counts, "identity": identity, "blast_radius": blast}


# ── The deletion itself ─────────────────────────────────────────────────

def references_to_user(user_id):
    """Rows in any table whose foreign key points at the user row,
    {"table.column": count}. Read from the schema, so a table added later
    with a user foreign key is counted without a change here."""
    user_table = User.__table__
    out = {}
    for table in db.metadata.sorted_tables:
        if table is user_table:
            continue
        for fk in table.foreign_keys:
            if fk.column.table is not user_table:
                continue
            col = fk.parent
            n = db.session.query(func.count()).select_from(table).filter(
                col == user_id).scalar() or 0
            if n:
                out[f"{table.name}.{col.name}"] = n
    return out


def _reserve_usernames(handles, now):
    until = now + timedelta(days=USERNAME_RESERVE_DAYS)
    for handle in sorted(h for h in handles if h):
        row = ReleasedUsername.query.filter_by(username=handle).first()
        if row is None:
            db.session.add(ReleasedUsername(
                username=handle, released_at=now, reserved_until=until))
        else:
            row.released_at = now
            row.reserved_until = max(row.reserved_until, until)


def delete_identity(user_id):
    """Delete the account after its data has been purged. Flushes but
    does not commit: the caller commits it together with the job's
    "done", then calls the returned function for what happens outside
    the database (cache, email). Returns (counts, after_commit). Raises
    PurgeRefused, or PurgeIncomplete when anything of the user's is left
    that the purge should have deleted."""
    from backend.utils.system_accounts import get_erased_system_user

    user = db.session.get(User, user_id)
    refusal = deletion_refusal(user)
    if refusal:
        raise user_purge.PurgeRefused(refusal[1])
    U = user_id
    live = db.session.query(func.count(Node.id)).filter(
        or_(Node.user_id == U, Node.human_owner_id == U),
        Node.deleted_at.is_(None)).scalar()
    if live:
        raise PurgeIncomplete(f"live nodes still left: node={live}")

    erased = get_erased_system_user()
    now = _now()
    counts = Counter()
    # The purge's tombstones (other people's replies hang below them).
    # Only the owner columns change, updated_at is kept.
    for col, key in ((Node.user_id, "node.user_id"),
                     (Node.human_owner_id, "node.human_owner_id")):
        counts[key] += Node.query.filter(col == U).update(
            {col: erased.id, Node.updated_at: Node.updated_at},
            synchronize_session=False)
    counts["node.pinned_by"] += Node.query.filter(Node.pinned_by == U).update(
        {Node.pinned_by: None, Node.updated_at: Node.updated_at},
        synchronize_session=False)
    counts["poll.created_by"] += Poll.query.filter(Poll.created_by == U).update(
        {Poll.created_by: None}, synchronize_session=False)

    handles = {(user.username or "").lower()}
    handles.update(h for (h,) in db.session.query(
        UsernameHistory.old_username).filter(UsernameHistory.user_id == U))
    for model, key in ((UsernameHistory, "username_history"),
                       (ApiToken, "api_token"),
                       (ChangelogReadState, "changelog_read_state")):
        counts[key] += model.query.filter(model.user_id == U).delete(
            synchronize_session=False)
    db.session.flush()

    left = references_to_user(U)
    if left:
        raise PurgeIncomplete("references to the account still left: "
                              + ", ".join(f"{k}={v}"
                                          for k, v in sorted(left.items())))

    email = user.email
    _reserve_usernames(handles, now)
    counts["released_username"] += len([h for h in handles if h])
    db.session.expunge(user)
    counts["user"] += User.query.filter(User.id == U).delete(
        synchronize_session=False)
    db.session.flush()

    def after_commit():
        try:
            from backend.utils import public_cache
            paths = {"/sitemap.xml"}
            for h in handles:
                paths.update({f"/@{h}", f"/@{h}/feed.xml"})
            public_cache.invalidate(*paths)
        except Exception as e:  # noqa: BLE001 - the TTL bounds it
            logger.warning("account deletion of user %s: public page cache "
                           "not dropped (%s)", U, type(e).__name__)
        if email:
            from backend.utils.email import send_account_deleted_email
            send_account_deleted_email(email)

    logger.warning("Account of user %s deleted", U)
    return dict(counts), after_commit


def release_expired_usernames(now=None):
    """Forget handles whose reservation is over (the beat calls it).
    Returns how many rows went."""
    now = now or _now()
    n = ReleasedUsername.query.filter(
        ReleasedUsername.reserved_until <= now).delete(
        synchronize_session=False)
    db.session.commit()
    return n
