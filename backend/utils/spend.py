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
# month, so it holds across a month boundary inside the trial and past its
# end until the first payment, which lifts it (stripe_billing.handle_event).
# A trial that ends without a payment lifts nothing: the account is then
# not let in (a failed first payment) or lapsed (cancelled), with no AI
# replies either way. An admin's per-user limit replaces it, as it
# replaces the level's ceiling.

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


def trial_ceiling_start(user):
    """Where the trial's or an alpha tester's free month's ceiling
    (trial_limit_usd) counts the spend from: the free month's start, else
    the trial's; None for neither."""
    return (alpha_unpaid_since(user)
            or getattr(user, "subscription_trial_start", None))


def trial_capped(user):
    return bool(getattr(user, "subscription_trial_capped", False)
                and under_trial_ceiling(user))


def enforce_trial_cap(user, config, send_email=None):
    """Set (or clear) the trial block from the spend since the trial's
    start. Commits when it changed. Returns True when the user is
    blocked by it."""
    limit = trial_limit_usd(user, config)
    since = trial_ceiling_start(user)
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


# --- The ceiling of a paid period -------------------------------------------
# An account whose ceiling is its subscription level's has it once per
# paid period (Stripe's current period of the subscription,
# User.subscription_period_start), not once per calendar month: a period
# that spans two calendar months gets one ceiling, not two.
#
# Which accounts: access from a subscription (trialing or active), no
# admin per-user limit, no plan set by hand, and a known period start.
# Every other account keeps the calendar month, with everything about it
# unchanged: a plan set by hand, an admin's limit, an alpha tester's free
# month without a subscription, and a subscription whose period start is
# not stored yet (no event from Stripe since the column exists).
#
# The first paid period after a trial counts from the trial's start: the
# trial's ceiling is part of the first period's, not on top of it (Peter,
# 2026-11-12: "$30 for the free trial period, and if the user pays, extend
# this to the subscription's cap").
#
# The #594 rule holds per period: what a period spends above the ceiling
# counts toward the next one (User.spend_period_carry_microdollars, for
# the period starting at User.spend_period_start). The block is the start
# of the period it is for (User.spend_blocked_period_start), so it ends
# by itself when Stripe starts the next period. While an account is
# counted per period its calendar-month block is not read, and its
# months carry nothing (roll_spend_carry marks them).

def ceiling_period_start(user):
    """Where the paid period's ceiling counts the spend from, or None for
    an account whose ceiling is counted over the calendar month."""
    if user is None:
        return None
    if getattr(user, "monthly_spend_limit_usd", None) is not None:
        return None
    if getattr(user, "plan_set_by_hand", True):
        return None
    if not getattr(user, "subscription_grants_access", False):
        return None
    start = getattr(user, "subscription_period_start", None)
    if not isinstance(start, datetime):
        return None
    trial_start = getattr(user, "subscription_trial_start", None)
    trial_end = getattr(user, "subscription_trial_end", None)
    if (isinstance(trial_start, datetime) and isinstance(trial_end, datetime)
            and trial_start <= start <= trial_end):
        # The trial itself, or the period that began when it ended.
        return trial_start
    return start


def _user_spend_between_microdollars(user_id, start, end):
    """One user's spend from *start* up to, not including, *end*."""
    q = db.session.query(
        func.coalesce(func.sum(APICostLog.cost_microdollars), 0)
    ).filter(
        APICostLog.user_id == user_id,
        APICostLog.created_at >= start,
        APICostLog.created_at < end,
    )
    return int(q.scalar() or 0)


def rolled_period_carry(user, config, start):
    """The carry into the period that starts at *start*, in microdollars,
    from what is stored, without writing it: what was counted since the
    stored period's start, above one ceiling. 0 for an account never
    counted per period, and with no limit."""
    stored = getattr(user, "spend_period_start", None)
    carry = getattr(user, "spend_period_carry_microdollars", None) or 0
    if stored == start:
        return carry
    limit = _usd_to_microdollars(get_user_spend_limit_usd(user, config))
    if stored is None or stored > start or limit <= 0:
        return 0
    return max(0, carry + _user_spend_between_microdollars(
        user.id, stored, start) - limit)


def roll_period_carry(user, config, start):
    """Bring the stored period carry to the period that starts at *start*
    and, when the carry alone reaches the ceiling, block the account for
    the period. Does not commit. Returns True when it changed the row."""
    if user.spend_period_start == start:
        return False
    carry = rolled_period_carry(user, config, start)
    user.spend_period_start = start
    user.spend_period_carry_microdollars = carry
    limit = _usd_to_microdollars(get_user_spend_limit_usd(user, config))
    if limit > 0 and carry >= limit:
        user.spend_blocked_period_start = start
        logger.warning(
            "User %s capped for the period from %s by the carry: $%.2f "
            "carried >= $%.2f limit", user.id, start.isoformat(),
            carry / 1e6, limit / 1e6)
    return True


def carried_spend_microdollars(user, config, now=None):
    """What earlier periods (or months) spent above the limit and is
    counted now, without writing anything."""
    start = ceiling_period_start(user)
    if start is not None:
        return rolled_period_carry(user, config, start)
    return rolled_spend_carry(user, config, now)


def counted_spend_microdollars(user, config, now=None):
    """What counts against the user's limit now: the carry and the spend
    of the paid period, or of the calendar month for an account counted
    by the month."""
    start = ceiling_period_start(user)
    if start is not None:
        return (rolled_period_carry(user, config, start)
                + user_spend_since_microdollars(user.id, start))
    return (rolled_spend_carry(user, config, now)
            + get_user_month_spend_microdollars(user.id, now=now))


def _ceiling_blocked(user, now=None):
    """The stored block of the limit itself (not the trial's): for the
    current paid period, or for the current calendar month."""
    start = ceiling_period_start(user)
    if start is not None:
        return user.spend_blocked_period_start == start
    blocked = user.spend_blocked_month
    return bool(blocked) and blocked == current_month(now)


def _add_months(day, months):
    """*day* (a date) *months* later, on the same day of the month or
    the month's last."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = index // 12, index % 12 + 1
    last = (_month_first_day(f"{year:04d}-{month:02d}", 1)
            - _month_first_day(f"{year:04d}-{month:02d}")).days
    return date(year, month, min(day.day, last))


def _period_lift_date(user, config, start):
    """The date (UTC) the period's block lifts if nothing more is spent:
    the day the next period starts, or a later period's when what is
    counted exceeds the ceiling by a whole ceiling or more. None for a
    subscription that ends with this period (nothing starts again)."""
    end = getattr(user, "subscription_period_end", None)
    if end is None or getattr(user, "subscription_cancel_at_period_end",
                              False):
        return None
    limit = _usd_to_microdollars(get_user_spend_limit_usd(user, config))
    if limit <= 0:
        return end.date()
    counted = (rolled_period_carry(user, config, start)
               + user_spend_since_microdollars(user.id, start))
    overflow = counted - limit
    return _add_months(end.date(), overflow // limit if overflow > 0 else 0)


def _period_state(user, config, start):
    """(carried, spend) of the period in USD, with the carry rolled on
    the row. Does not commit."""
    roll_period_carry(user, config, start)
    carried_usd = (user.spend_period_carry_microdollars or 0) / 1_000_000
    spend_usd = user_spend_since_microdollars(user.id, start) / 1_000_000
    return carried_usd, spend_usd


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
    if ceiling_period_start(user) is not None:
        # Counted per paid period: the months carry nothing.
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
    # No monthly limit in force: none at all, or the ceiling is counted
    # per paid period. Either way the month carries nothing.
    by_period = ceiling_period_start(user) is not None
    if (limit <= 0 or by_period) and user.spend_carry_free_month != month:
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
    period_start = ceiling_period_start(user)
    if period_start is not None:
        # Counted per paid period: the day the next period starts.
        return _period_lift_date(user, config, period_start)
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
    period_start = ceiling_period_start(user)
    if period_start is not None:
        # Counted per paid period: the block is the period's.
        carried_usd, spend_usd = _period_state(user, config, period_start)
        blocked = limit_usd > 0 and carried_usd + spend_usd >= limit_usd
        if blocked:
            user.spend_blocked_period_start = period_start
        elif user.spend_blocked_period_start == period_start:
            user.spend_blocked_period_start = None
        return {
            "blocked": blocked,
            "spend_usd": round(spend_usd, 4),
            "carried_usd": round(carried_usd, 4),
            "limit_usd": limit_usd,
            "month": month,
            "period_start": period_start.isoformat(),
        }
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
    month rollover because we compare the stored month to the current one;
    for an account counted per paid period (ceiling_period_start) it is the
    period's block, which ends when the next period starts.
    """
    from backend.models import User
    if user is None:
        return False
    if not isinstance(user, User):
        user = db.session.get(User, user)
        if user is None:
            return False
    if _ceiling_blocked(user, now):
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

    period_start = ceiling_period_start(user)
    if period_start is not None:
        return _enforce_period_cap(user, config, period_start, limit_usd,
                                   send_email)

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


def _enforce_period_cap(user, config, start, limit_usd, send_email=None):
    """enforce_user_spend_cap for an account counted per paid period:
    block it when the period's carry and spend have reached the ceiling,
    and alert the admin once per period."""
    if roll_period_carry(user, config, start):
        db.session.commit()
    period = start.isoformat()
    if user.spend_blocked_period_start == start:
        return {"status": "already_blocked", "user_id": user.id,
                "period_start": period}
    carried_usd = (user.spend_period_carry_microdollars or 0) / 1_000_000
    spend_usd = user_spend_since_microdollars(user.id, start) / 1_000_000
    if carried_usd + spend_usd < limit_usd:
        return {
            "status": "ok", "user_id": user.id,
            "spend_usd": round(spend_usd, 4),
            "carried_usd": round(carried_usd, 4), "limit_usd": limit_usd,
            "blocked": False, "period_start": period,
        }

    # The block first, then the alert (as for the month).
    user.spend_blocked_period_start = start
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
            "Failed to send per-user spend block email for user %s", user.id)

    logger.warning(
        "User %s hard-blocked: $%.2f spent + $%.2f carried >= $%.2f cap "
        "for the period from %s", user.id, spend_usd, carried_usd,
        limit_usd, period)
    return {
        "status": "blocked", "user_id": user.id, "period_start": period,
        "spend_usd": round(spend_usd, 4),
        "carried_usd": round(carried_usd, 4), "limit_usd": limit_usd,
        "blocked": True,
    }


SPEND_CAP_MESSAGE = ("You've reached your monthly usage limit for the free "
                     "alpha.")
# While payments are on (#438) the limit is not the free alpha's.
SPEND_CAP_MESSAGE_PAID = "You've reached your monthly usage limit."


def spend_cap_message(lifts_on=None, now=None, by_period=False):
    """The 402's text. It names the date only when the cap lifts later than
    the start of next month (a carry over a whole month's limit, #594).
    For an account counted per paid period (``by_period``) it names the
    day the next period starts, or no reset when the subscription ends
    with this period."""
    from backend.utils.subscriptions import payments_enabled
    first = SPEND_CAP_MESSAGE_PAID if payments_enabled() else \
        SPEND_CAP_MESSAGE
    if by_period:
        if lifts_on is None:
            return first
        return (f"{first} It resets on "
                f"{lifts_on.strftime('%B')} {lifts_on.day}.")
    next_start = _month_first_day(current_month(now), 1)
    if lifts_on is None or lifts_on <= next_start:
        return f"{first} It resets at the start of next month."
    return (f"{first} It resets on "
            f"{lifts_on.strftime('%B')} {lifts_on.day}.")


# The ceilings that only a first payment lifts (#438): the trial's, and an
# alpha tester's free month's. Not a date: the trial's end lifts nothing
# (a trial that ends without a payment leaves the account not let in, or
# lapsed), so no date is named and ``lifts_on`` is null. They name the
# state and never where or how to pay (the iPhone app shows them too).
TRIAL_CAP_MESSAGE = ("You've reached the usage limit of your trial. It "
                     "lifts with your first payment.")
FREE_MONTH_CAP_MESSAGE = ("You've reached the usage limit of your free "
                          "month. It lifts with your first payment.")


def cap_lifts_with_payment(user, now=None):
    """Capped by the trial's or the free month's ceiling, and not (also)
    by this month's: only the first payment lifts it. *user* is a User or
    an id."""
    from backend.models import User
    if user is not None and not isinstance(user, User):
        user = db.session.get(User, user)
    return (isinstance(user, User) and trial_capped(user)
            and not _ceiling_blocked(user, now))


def spend_cap_payload(user=None, now=None):
    """The standard 402 body for a capped user: the error code the
    frontend's api.js interceptor keys the spend-cap banner on, the text,
    and ``lifts_on``, the date (UTC, ISO) the cap lifts, when the user is
    known. A cap only the first payment lifts (the trial's, the free
    month's) has no date and says so: ``lifts_with_payment``, a key sent
    only then (so nothing differs while payments are off), for the
    clients' own texts."""
    from flask import current_app
    from backend.models import User
    lifts_on = None
    by_period = False
    if isinstance(user, User) and cap_lifts_with_payment(user, now):
        return trial_cap_payload(user)
    if user is not None and isinstance(user, User):
        by_period = ceiling_period_start(user) is not None
        try:
            lifts_on = spend_cap_lift_date(user, current_app.config, now)
        except Exception:
            logger.exception("Spend cap lift date failed for user %s",
                             getattr(user, "id", None))
    return {
        "error": "monthly_spend_limit_reached",
        "message": spend_cap_message(lifts_on, now, by_period=by_period),
        "lifts_on": lifts_on.isoformat() if lifts_on else None,
    }


def trial_cap_payload(user):
    """The 402 body for a ceiling only the first payment lifts (the
    trial's, an alpha tester's free month's; trial_limit_usd)."""
    alpha = alpha_unpaid_since(user) is not None
    return {
        "error": "monthly_spend_limit_reached",
        "message": FREE_MONTH_CAP_MESSAGE if alpha else TRIAL_CAP_MESSAGE,
        "lifts_on": None,
        "lifts_with_payment": True,
    }


def spend_cap_response(user=None):
    """The standard 402 for a capped user (see spend_cap_payload). Pass the
    user: without one the body has no lift date and the usual text."""
    from flask import jsonify
    return jsonify(spend_cap_payload(user)), 402


def spend_capped_fields(user):
    """For a 2xx that saved the writing but skipped the reply because the
    user is capped: the flag the clients key the banner on, and the date
    the cap lifts (as the 402's ``lifts_on``), or, for a cap only the
    first payment lifts, ``spend_cap_lifts_with_payment``."""
    payload = spend_cap_payload(user)
    fields = {"spend_capped": True,
              "spend_cap_lifts_on": payload["lifts_on"]}
    if payload.get("lifts_with_payment"):
        fields["spend_cap_lifts_with_payment"] = True
    return fields


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


def _per_user_cap_on(config):
    """Whether a per-user ceiling can apply: the global default is set,
    or payments are on (#438: a subscription has a ceiling of its own even
    when the global default is 0)."""
    return ((config.get("PER_USER_MONTHLY_LIMIT_USD") or 0) > 0
            or bool(config.get("PAYMENTS_ENABLED")))


def refresh_user_cap(user_id, config):
    """Set the account's block from the spend recorded so far
    (enforce_user_spend_cap, in the caller's run), when a per-user ceiling
    can apply. The reply task asks it when it starts, before it reads the
    flag: a reply queued behind others of the same account then sees what
    they cost, not only the block the deferred check
    (_dispatch_user_cap_check) sets later, behind the same queue. A
    failure is logged and the flag is left as it was."""
    if not _per_user_cap_on(config):
        return None
    try:
        return enforce_user_spend_cap(user_id, config)
    except Exception:
        db.session.rollback()
        logger.exception("Per-user spend cap check failed for user %s",
                         user_id)
        return None


@event.listens_for(APICostLog, "after_insert")
def _dispatch_user_cap_check(mapper, connection, target):
    """After any cost row is written, schedule a (deferred, out-of-band)
    per-user cap check. No-op unless the cap is enabled, so tests and the
    default config incur zero broker traffic."""
    try:
        from flask import current_app, has_app_context
        if not has_app_context():
            return
        if not _per_user_cap_on(current_app.config):
            return
        if target.user_id is None:
            return
        from backend.tasks.spend_monitor import enforce_user_cap
        enforce_user_cap.apply_async(
            args=[target.user_id], countdown=ENFORCE_CAP_DELAY_SECONDS)
    except Exception:
        logger.exception("Failed to dispatch per-user spend cap check")
