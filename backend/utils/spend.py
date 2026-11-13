"""Month-to-date API spend aggregation + spend-limit alerting (issue #85).

Core logic lives here (plain functions, app context assumed active) so it
is unit-testable without importing the Celery app. The hourly beat task in
backend/tasks/spend_monitor.py is a thin wrapper around check_and_alert().

Spend source of truth is APICostLog (cost_microdollars), which every
LLM/transcription/TTS task already writes. Provider attribution derives
from SUPPORTED_MODELS config with a "claude*" prefix fallback so rows for
models that have since been removed from config still count correctly.
"""
import logging
from datetime import date, datetime
from functools import wraps

from sqlalchemy import event, func, or_

from backend.extensions import db
from backend.models import APICostLog, SpendAlert

logger = logging.getLogger(__name__)

# Seconds to defer the per-user cap check after a cost row is inserted, so the
# inserting transaction has committed before we aggregate committed spend.
ENFORCE_CAP_DELAY_SECONDS = 15


class SpendCapExceeded(Exception):
    """Raised when a spend-blocked user would incur a cost-bearing action.
    Registered with a 402 error handler so any HTTP path that hits it
    surfaces the standard monthly_spend_limit_reached payload (→ banner)."""
    pass


def month_start(now=None):
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, 1)


def current_month(now=None):
    """Current calendar month as "YYYY-MM" (UTC)."""
    return (now or datetime.utcnow()).strftime("%Y-%m")


def _month_first_day(month, months_ahead=0):
    """The first day of ``month`` ("YYYY-MM") plus ``months_ahead``."""
    year, mon = (int(part) for part in month.split("-"))
    index = year * 12 + (mon - 1) + months_ahead
    return date(index // 12, index % 12 + 1, 1)


def _next_month(month):
    return _month_first_day(month, 1).strftime("%Y-%m")


def _anthropic_filter(config):
    ids = {
        model_id
        for model_id, cfg in (config.get("SUPPORTED_MODELS") or {}).items()
        if cfg.get("provider") == "anthropic"
    }
    return or_(APICostLog.model_id.in_(ids),
               APICostLog.model_id.like("claude%"))


def get_month_spend_microdollars(config, provider=None, now=None):
    """Month-to-date spend in microdollars, optionally filtered by provider
    ("anthropic" / "openai")."""
    q = db.session.query(
        func.coalesce(func.sum(APICostLog.cost_microdollars), 0)
    ).filter(APICostLog.created_at >= month_start(now))
    if provider == "anthropic":
        q = q.filter(_anthropic_filter(config))
    elif provider == "openai":
        q = q.filter(~_anthropic_filter(config))
    return int(q.scalar() or 0)


def parse_thresholds(raw):
    """Parse "0.5,0.8,0.95" into a sorted list of floats in (0, 1]."""
    thresholds = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            logger.warning("Ignoring invalid spend threshold %r", part)
            continue
        if 0 < value <= 1:
            thresholds.append(value)
    return sorted(set(thresholds))


def check_and_alert(config, now=None, send_email=None):
    """Compare month-to-date Anthropic spend against the configured limit
    and alert once per (month, threshold) crossed.

    Returns a dict summary (also useful as the Celery task result).
    `send_email` is injectable for tests; defaults to the real sender.
    """
    limit_usd = config.get("ANTHROPIC_SPEND_LIMIT_USD") or 0
    if limit_usd <= 0:
        return {"status": "disabled"}

    if send_email is None:
        from backend.utils.email import send_spend_alert_email
        send_email = send_spend_alert_email

    now = now or datetime.utcnow()
    month = now.strftime("%Y-%m")
    spend_usd = get_month_spend_microdollars(
        config, provider="anthropic", now=now) / 1_000_000
    thresholds = parse_thresholds(config.get("SPEND_ALERT_THRESHOLDS"))

    fired = []
    for threshold in thresholds:
        if spend_usd < limit_usd * threshold:
            continue
        already = SpendAlert.query.filter_by(
            provider="anthropic", month=month, threshold=threshold
        ).first()
        if already:
            continue
        try:
            send_email(
                to_email=config.get("SPEND_ALERT_EMAIL"),
                provider="anthropic",
                spend_usd=spend_usd,
                limit_usd=limit_usd,
                threshold=threshold,
            )
        except Exception:
            # Don't record the alert as sent — retry next run.
            logger.exception(
                "Failed to send spend alert (threshold %.2f)", threshold)
            continue
        db.session.add(SpendAlert(
            provider="anthropic", month=month,
            threshold=threshold, spend_usd=spend_usd,
        ))
        db.session.commit()
        fired.append(threshold)

    return {
        "status": "ok",
        "month": month,
        "spend_usd": round(spend_usd, 4),
        "limit_usd": limit_usd,
        "fired": fired,
    }


# --- Per-user monthly hard cap (issue #85 follow-up) -----------------------

def get_user_month_spend_microdollars(user_id, now=None):
    """Month-to-date spend in microdollars for one user, ALL providers."""
    q = db.session.query(
        func.coalesce(func.sum(APICostLog.cost_microdollars), 0)
    ).filter(
        APICostLog.user_id == user_id,
        APICostLog.created_at >= month_start(now),
    )
    return int(q.scalar() or 0)


def user_spend_since_microdollars(user_id, since):
    """One user's spend since *since* (naive UTC), all providers."""
    q = db.session.query(
        func.coalesce(func.sum(APICostLog.cost_microdollars), 0)
    ).filter(
        APICostLog.user_id == user_id,
        APICostLog.created_at >= since,
    )
    return int(q.scalar() or 0)


# --- The trial's own ceiling (#438) ------------------------------------------
# A trialing subscription may use SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD of model
# cost in all, counted from the trial's start: nothing has been paid yet. The
# block is a flag on the account (User.subscription_trial_capped), not a
# month, so it holds across a month boundary inside the trial; it ends with
# the first payment (stripe_billing.handle_event), or with the trial when
# none follows. An admin's per-user limit replaces it, as it replaces the
# level's ceiling.

def alpha_unpaid_since(user):
    """An alpha tester (#438) from the free month's start to their first
    paid invoice, while the month or a subscription not paid yet gives
    them the paid set: the month's start, else None. That whole stretch,
    the time after the month's end before the first charge included,
    counts against one ceiling (trial_limit_usd)."""
    if getattr(user, "alpha_free_granted_at", None) is None:
        return None
    if getattr(user, "subscription_ever_paid", False):
        return None
    start = getattr(user, "alpha_free_start", None)
    if start is None or datetime.utcnow() < start:
        return None
    if (getattr(user, "alpha_free_month_active", False)
            or getattr(user, "subscription_grants_access", False)):
        return start
    return None


def under_trial_ceiling(user):
    """Trialing, or active after the trial before its first payment went
    through: Stripe makes the subscription active at the trial's end and
    charges the first invoice about an hour later, and until then
    nothing has been paid. Also an alpha tester before their first paid
    invoice (alpha_unpaid_since)."""
    if alpha_unpaid_since(user) is not None:
        return True
    status = getattr(user, "subscription_status", None)
    if status == "trialing":
        return True
    return (status == "active"
            and not getattr(user, "subscription_ever_paid", False)
            and getattr(user, "subscription_trial_start", None) is not None)


def trial_limit_usd(user, config):
    if not under_trial_ceiling(user):
        return 0
    if getattr(user, "plan_set_by_hand", True):
        return 0
    if getattr(user, "monthly_spend_limit_usd", None) is not None:
        return 0
    if alpha_unpaid_since(user) is not None:
        # An alpha tester (#438): from the free month's start to the first
        # paid invoice, Standard's ceiling once in all (Peter, 2026-11-13:
        # "the standard limit"), however many calendar months it spans.
        from backend.utils.subscriptions import (
            ALPHA_UNPAID_CEILING_LEVEL, level_spend_limit_usd)
        return float(level_spend_limit_usd(ALPHA_UNPAID_CEILING_LEVEL,
                                           config) or 0)
    if getattr(user, "alpha_free_granted_at", None) is not None:
        return 0
    return float(config.get("SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD") or 0)


def trial_capped(user):
    return bool(getattr(user, "subscription_trial_capped", False)
                and under_trial_ceiling(user))


def enforce_trial_cap(user, config, send_email=None):
    """Set (or clear) the trial block from the spend since the trial's
    start. Commits when it changed. Returns True when the user is
    blocked by it."""
    limit = trial_limit_usd(user, config)
    since = (alpha_unpaid_since(user)
             or getattr(user, "subscription_trial_start", None))
    blocked = False
    spend = 0
    if limit > 0 and since is not None:
        spend = user_spend_since_microdollars(user.id, since)
        blocked = spend >= _usd_to_microdollars(limit)
    if bool(user.subscription_trial_capped) == blocked:
        return blocked
    user.subscription_trial_capped = blocked
    db.session.commit()
    if blocked:
        logger.warning("User %s capped for the trial: $%.2f spent >= $%.2f",
                       user.id, spend / 1e6, limit)
        if send_email is None:
            from backend.utils.email import send_user_spend_block_email
            send_email = send_user_spend_block_email
        try:
            send_email(
                to_email=config.get("SPEND_ALERT_EMAIL") or "signup@loore.org",
                username=user.username, spend_usd=spend / 1e6,
                limit_usd=limit, carried_usd=0)
        except Exception:
            logger.exception("Failed to send trial cap email for user %s",
                             user.id)
    return blocked


def _user_spend_in_month_microdollars(user_id, month):
    """One user's spend in a whole past month ("YYYY-MM"), all providers."""
    start = _month_first_day(month)
    end = _month_first_day(month, 1)
    q = db.session.query(
        func.coalesce(func.sum(APICostLog.cost_microdollars), 0)
    ).filter(
        APICostLog.user_id == user_id,
        APICostLog.created_at >= datetime(start.year, start.month, 1),
        APICostLog.created_at < datetime(end.year, end.month, 1),
    )
    return int(q.scalar() or 0)


def _usd_to_microdollars(usd):
    return int(round((usd or 0) * 1_000_000))


# --- The carry (#594) ------------------------------------------------------
# Peter, 2026-10-22: what a month spends above the user's limit counts
# toward the next month ($50 limit, $60 spent in October: November starts
# with $10 counted). An overflow larger than a month's limit keeps the user
# capped into the following month, and so on until it is used up. A limit
# of 0 (no limit) clears the carry; raising a limit gives the room at once.
#
#     carry(m) = max(0, carry(m-1) + spend(m-1) - limit)
#
# The limit is the one in force when the carry is rolled forward, at the
# month's rollover: no history of limits is kept. A limit raised during a
# month and not lowered again is the number the cap was enforcing when the
# month ended. The carry is stored with the month it is counted in
# (User.spend_carry_microdollars, User.spend_carry_month) and rolled
# forward one month at a time, so an inactive user's carry shrinks by one
# limit per month. A user never rolled (NULL month) starts at 0: no month
# before the first roll is charged afterwards. Cost records are never
# changed.
#
# A month in which the user had no limit at any time carries nothing
# (User.spend_carry_free_month): a limit of 0 clears the carry, also when
# a limit is set again later in the month (the admin lets a job through,
# then restores the limit). The cap within that month is unchanged: its
# real spend counts against the limit set later. A roll that finds no
# limit marks the month, and every write of a limit rolls with the old
# limit first (set_user_spend_limit), so a month that starts without a
# limit or loses it mid-month is marked before its rollover.

def _month_spend_for_carry(user, month):
    """What ``month`` adds to the carry: its spend, or nothing in a month
    the user had no limit at some time."""
    if getattr(user, "spend_carry_free_month", None) == month:
        return 0
    return _user_spend_in_month_microdollars(user.id, month)


def rolled_spend_carry(user, config, now=None):
    """The user's carry into the current month, in microdollars, rolled
    forward from what is stored without writing it. Queries one month of
    cost rows per month the stored carry is behind; none when it is
    current or the user has no limit."""
    month = current_month(now)
    limit = _usd_to_microdollars(get_user_spend_limit_usd(user, config))
    if limit <= 0:
        return 0
    stored = getattr(user, "spend_carry_month", None)
    if stored is None or stored > month:
        return 0
    carry = getattr(user, "spend_carry_microdollars", None) or 0
    while stored < month:
        carry = max(0, carry + _month_spend_for_carry(user, stored) - limit)
        stored = _next_month(stored)
    return carry


def roll_spend_carry(user, config, now=None):
    """Bring the user's stored carry up to the current month and, when the
    carry alone reaches the limit, block the user for the month (silently:
    the admin was emailed when the overflow happened). With no limit in
    force, mark the month as carrying nothing. Idempotent; does not commit.
    Returns True when it changed the row."""
    month = current_month(now)
    carry = rolled_spend_carry(user, config, now)
    changed = False
    if user.spend_carry_month != month or \
            (user.spend_carry_microdollars or 0) != carry:
        user.spend_carry_month = month
        user.spend_carry_microdollars = carry
        changed = True
    limit = _usd_to_microdollars(get_user_spend_limit_usd(user, config))
    if limit <= 0 and user.spend_carry_free_month != month:
        user.spend_carry_free_month = month
        changed = True
    if limit > 0 and carry >= limit and user.spend_blocked_month != month:
        user.spend_blocked_month = month
        changed = True
        logger.warning(
            "User %s capped for %s by the carry: $%.2f carried >= $%.2f "
            "limit", user.id, month, carry / 1e6, limit / 1e6)
    return changed


def refresh_spend_carry(user, config, now=None):
    """roll_spend_carry, committed when it changed something, for a user
    with a limit. For a user without one it does nothing, not even a query:
    the cap checks before every step stay free for them. The hourly roll
    keeps their month current and marks it as carrying nothing, and a limit
    set later goes through set_user_spend_limit, so it starts from 0."""
    if (get_user_spend_limit_usd(user, config) or 0) <= 0:
        return
    if roll_spend_carry(user, config, now):
        db.session.commit()


def set_user_spend_limit(user, limit_usd, config, now=None):
    """The admin's limit edit: the carry into this month is rolled with the
    limit in force when the month began (which marks the month as carrying
    nothing when that was no limit), then the new limit applies and the
    block follows it (reconcile_user_spend_block, which marks the month
    when the new limit is 0). Does not commit."""
    roll_spend_carry(user, config, now)
    user.monthly_spend_limit_usd = limit_usd
    return reconcile_user_spend_block(user, config, now=now)


def clear_spend_carry(user, config, now=None):
    """The admin's clear action: the carry into this month goes to 0 and
    the block follows the month's own spend. Does not commit."""
    user.spend_carry_month = current_month(now)
    user.spend_carry_microdollars = 0
    return reconcile_user_spend_block(user, config, now=now)


def roll_spend_carries(config, now=None):
    """Hourly: roll every user whose carry is not yet for this month. After
    the first run of a month that is only the users created since."""
    from backend.models import User
    month = current_month(now)
    ids = [uid for (uid,) in db.session.query(User.id).filter(or_(
        User.spend_carry_month.is_(None),
        User.spend_carry_month != month)).all()]
    rolled = blocked = 0
    for uid in ids:
        user = db.session.get(User, uid)
        if user is None:
            continue
        try:
            if roll_spend_carry(user, config, now):
                capped = user.spend_blocked_month == month
                db.session.commit()
                rolled += 1
                blocked += capped
        except Exception:
            db.session.rollback()
            logger.exception("Spend carry roll failed for user %s", uid)
    return {"status": "ok", "month": month, "users": len(ids),
            "rolled": rolled, "capped_by_carry": blocked}


def spend_cap_lift_date(user, config, now=None):
    """The date (UTC) a capped user's limit lifts if they spend nothing
    more: the first of next month, or a later first when what is counted
    this month (the carry and this month's spend) exceeds the limit by a
    whole month's limit or more."""
    now = now or datetime.utcnow()
    month = current_month(now)
    limit = _usd_to_microdollars(get_user_spend_limit_usd(user, config))
    if limit <= 0:
        return _month_first_day(month, 1)
    counted = rolled_spend_carry(user, config, now)
    if getattr(user, "spend_carry_free_month", None) != month:
        # A month without a limit at some time carries nothing onward.
        counted += get_user_month_spend_microdollars(user.id, now=now)
    overflow = counted - limit
    months = 1 + (overflow // limit if overflow > 0 else 0)
    return _month_first_day(month, months)


def get_user_spend_limit_usd(user, config):
    """Effective monthly spend cap for a user, in USD.

    The per-user override (User.monthly_spend_limit_usd) wins when set — note a
    stored 0 means "uncapped" for that user, distinct from NULL ("use default").
    NULL falls back to the global PER_USER_MONTHLY_LIMIT_USD. A return of 0 (or
    less) means no cap is in force for this user.
    """
    override = getattr(user, "monthly_spend_limit_usd", None)
    if override is not None:
        return override
    # An account whose access comes from its subscription has the level's
    # ceiling (#438); one set by hand keeps the global default.
    from backend.utils.subscriptions import subscription_spend_limit_usd
    level_limit = subscription_spend_limit_usd(user, config)
    if level_limit is not None:
        return level_limit
    return config.get("PER_USER_MONTHLY_LIMIT_USD") or 0


def reconcile_user_spend_block(user, config, now=None):
    """Set or clear the user's block flag to match what is counted this month
    (the carry and the month-to-date spend) against their effective limit —
    the source of truth for the admin limit editor. Raising a user's limit
    above it unblocks them immediately; lowering it at/below it blocks them;
    a limit of 0 clears the carry (#594). Does NOT commit (the caller owns
    the transaction). Returns a summary dict.
    """
    now = now or datetime.utcnow()
    month = current_month(now)
    roll_spend_carry(user, config, now)
    limit_usd = get_user_spend_limit_usd(user, config)
    if limit_usd <= 0:
        user.spend_carry_microdollars = 0
    carried_usd = (user.spend_carry_microdollars or 0) / 1_000_000
    spend_usd = get_user_month_spend_microdollars(user.id, now=now) / 1_000_000

    if limit_usd > 0 and carried_usd + spend_usd >= limit_usd:
        user.spend_blocked_month = month
        blocked = True
    else:
        # Clear only a current-month block; leave anything else untouched (in
        # practice the flag is always either the current month or already
        # auto-expired, but this keeps the operation strictly scoped).
        if user.spend_blocked_month == month:
            user.spend_blocked_month = None
        blocked = False

    return {
        "blocked": blocked,
        "spend_usd": round(spend_usd, 4),
        "carried_usd": round(carried_usd, 4),
        "limit_usd": limit_usd,
        "month": month,
    }


def user_is_capped(user, now=None):
    """Cheap, query-light check of whether a user is currently spend-blocked.

    Pass a loaded User to avoid any query (the flag is already on the row);
    a bare id triggers a single primary-key lookup. The block auto-expires at
    month rollover because we compare the stored month to the current one.
    """
    from backend.models import User
    if user is None:
        return False
    if not isinstance(user, User):
        user = db.session.get(User, user)
        if user is None:
            return False
    blocked = user.spend_blocked_month
    if bool(blocked) and blocked == current_month(now):
        return True
    # The trial's own ceiling (#438); always False while payments are off.
    return trial_capped(user)


def enforce_user_spend_cap(user_id, config, now=None, send_email=None):
    """Block a user whose month-to-date spend (all providers) plus the carry
    from earlier months (#594) has reached their limit, and alert the admin
    once. Idempotent within a month. Runs in its own transaction (safe to
    call out-of-band). The carry is rolled to this month first.

    `send_email` is injectable for tests; defaults to the real sender.
    """
    from backend.models import User
    now = now or datetime.utcnow()
    month = current_month(now)
    user = db.session.get(User, user_id)
    if user is None:
        return {"status": "no_user", "user_id": user_id}

    # A trial has its own ceiling over the whole trial (#438).
    if (trial_limit_usd(user, config) > 0 or user.subscription_trial_capped) \
            and enforce_trial_cap(user, config, send_email=send_email):
        return {"status": "trial_capped", "user_id": user_id,
                "blocked": True}

    # Per-user override wins over the global default (and 0 = uncapped).
    limit_usd = get_user_spend_limit_usd(user, config)
    if limit_usd <= 0:
        return {"status": "disabled", "user_id": user_id}

    refresh_spend_carry(user, config, now)

    if user.spend_blocked_month == month:
        return {"status": "already_blocked", "user_id": user_id, "month": month}

    carried_usd = (user.spend_carry_microdollars or 0) / 1_000_000
    spend_usd = get_user_month_spend_microdollars(user_id, now=now) / 1_000_000
    if carried_usd + spend_usd < limit_usd:
        return {
            "status": "ok", "user_id": user_id,
            "spend_usd": round(spend_usd, 4),
            "carried_usd": round(carried_usd, 4), "limit_usd": limit_usd,
            "blocked": False,
        }

    # Over the cap. Persist the block first (the critical effect), then alert
    # best-effort — a failed email must not leave the user un-blocked.
    user.spend_blocked_month = month
    db.session.commit()

    if send_email is None:
        from backend.utils.email import send_user_spend_block_email
        send_email = send_user_spend_block_email
    try:
        send_email(
            to_email=config.get("SPEND_ALERT_EMAIL") or "signup@loore.org",
            username=user.username,
            spend_usd=spend_usd,
            limit_usd=limit_usd,
            carried_usd=carried_usd,
        )
    except Exception:
        logger.exception(
            "Failed to send per-user spend block email for user %s", user_id)

    logger.warning(
        "User %s hard-blocked: $%.2f spent + $%.2f carried >= $%.2f cap "
        "for %s", user_id, spend_usd, carried_usd, limit_usd, month)
    return {
        "status": "blocked", "user_id": user_id, "month": month,
        "spend_usd": round(spend_usd, 4),
        "carried_usd": round(carried_usd, 4), "limit_usd": limit_usd,
        "blocked": True,
    }


SPEND_CAP_MESSAGE = ("You've reached your monthly usage limit for the free "
                     "alpha.")
# While payments are on (#438) the limit is not the free alpha's.
SPEND_CAP_MESSAGE_PAID = "You've reached your monthly usage limit."


def spend_cap_message(lifts_on=None, now=None):
    """The 402's text. It names the date only when the cap lifts later than
    the start of next month (a carry over a whole month's limit, #594)."""
    from backend.utils.subscriptions import payments_enabled
    first = SPEND_CAP_MESSAGE_PAID if payments_enabled() else \
        SPEND_CAP_MESSAGE
    next_start = _month_first_day(current_month(now), 1)
    if lifts_on is None or lifts_on <= next_start:
        return f"{first} It resets at the start of next month."
    return (f"{first} It resets on "
            f"{lifts_on.strftime('%B')} {lifts_on.day}.")


def spend_cap_payload(user=None, now=None):
    """The standard 402 body for a capped user: the error code the
    frontend's api.js interceptor keys the spend-cap banner on, the text,
    and ``lifts_on``, the date (UTC, ISO) the cap lifts, when the user is
    known."""
    from flask import current_app
    from backend.models import User
    lifts_on = None
    if isinstance(user, User) and trial_capped(user) and \
            not (user.spend_blocked_month
                 and user.spend_blocked_month == current_month(now)):
        if alpha_unpaid_since(user) is not None:
            # An alpha tester's free month (#438): one ceiling until the
            # first payment.
            return {
                "error": "monthly_spend_limit_reached",
                "message": ("You've reached the usage limit of your free "
                            "month. It lifts with your first payment."),
                "lifts_on": None,
            }
        # The trial's own ceiling (#438): it lifts when the trial ends.
        end = user.subscription_trial_end
        when = f" on {end.strftime('%B')} {end.day}" if end else ""
        return {
            "error": "monthly_spend_limit_reached",
            "message": ("You've reached the usage limit of your trial. "
                        f"It lifts when the trial ends{when}."),
            "lifts_on": end.date().isoformat() if end else None,
        }
    if user is not None and isinstance(user, User):
        try:
            lifts_on = spend_cap_lift_date(user, current_app.config, now)
        except Exception:
            logger.exception("Spend cap lift date failed for user %s",
                             getattr(user, "id", None))
    return {
        "error": "monthly_spend_limit_reached",
        "message": spend_cap_message(lifts_on, now),
        "lifts_on": lifts_on.isoformat() if lifts_on else None,
    }


def spend_cap_response(user=None):
    """The standard 402 for a capped user (see spend_cap_payload). Pass the
    user: without one the body has no lift date and the usual text."""
    from flask import jsonify
    return jsonify(spend_cap_payload(user)), 402


def spend_capped_fields(user):
    """For a 2xx that saved the writing but skipped the reply because the
    user is capped: the flag the clients key the banner on, and the date
    the cap lifts (as the 402's ``lifts_on``)."""
    return {"spend_capped": True,
            "spend_cap_lifts_on": spend_cap_payload(user)["lifts_on"]}


def require_spend_headroom(fn):
    """Route decorator: reject cost-incurring requests from a capped user with
    402 + a clear message. Place below @login_required so current_user is set.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from flask_login import current_user
        if getattr(current_user, "is_authenticated", False) and \
                user_is_capped(current_user):
            return spend_cap_response(current_user)
        return fn(*args, **kwargs)
    return wrapper


@event.listens_for(APICostLog, "after_insert")
def _dispatch_user_cap_check(mapper, connection, target):
    """After any cost row is written, schedule a (deferred, out-of-band)
    per-user cap check. No-op unless the cap is enabled, so tests and the
    default config incur zero broker traffic."""
    try:
        from flask import current_app, has_app_context
        if not has_app_context():
            return
        # Payments on (#438): a subscription has a ceiling of its own even
        # when the global default is 0.
        if ((current_app.config.get("PER_USER_MONTHLY_LIMIT_USD") or 0) <= 0
                and not current_app.config.get("PAYMENTS_ENABLED")):
            return
        if target.user_id is None:
            return
        from backend.tasks.spend_monitor import enforce_user_cap
        enforce_user_cap.apply_async(
            args=[target.user_id], countdown=ENFORCE_CAP_DELAY_SECONDS)
    except Exception:
        logger.exception("Failed to dispatch per-user spend cap check")
