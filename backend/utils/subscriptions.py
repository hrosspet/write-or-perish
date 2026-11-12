"""What a subscription gives an account (#438), without calling Stripe.

The switch is PAYMENTS_ENABLED. Off, nothing here changes what an account
can do. On:

* an account gets AI replies when it is on a paid plan
  (User.on_paid_plan: a plan set by hand, alpha or pro, a trialing or
  active subscription, or an alpha tester's free month) or is an admin.
  Every other account
  ("free", or a subscription that ended) keeps writing, reading and
  export; create_llm_placeholder refuses its replies with
  SubscriptionRequired, which the routes answer as a 402 (or, where the
  writing was saved, as the ``subscription_required`` fields of a 2xx).
* Voice mode and profile updates follow User.on_paid_plan whatever the
  switch: a subscription that exists always counts, so turning payments
  off does not take what people paid for.
* the per-user spend cap of an account whose access comes from its
  subscription is the level's ceiling (subscription_spend_limit_usd).

A Stripe event never writes ``User.plan``: an account set by hand stays
as it is (utils/stripe_billing.apply_subscription).
"""
from datetime import datetime, timedelta

from flask import current_app, has_app_context

from backend.models import SUBSCRIPTION_LEVELS
from backend.utils.timefmt import iso_utc

# The levels a user can choose; the discounted one needs a code.
OFFERED_LEVELS = ("standard", "supporter")


class SubscriptionRequired(Exception):
    """A reply asked for by an account that has no AI replies while
    payments are on. Raised by create_llm_placeholder before any write;
    the app answers it with subscription_required_response (402)."""

    def __init__(self, user=None):
        super().__init__("subscription_required")
        self.message = subscription_required_message(user)


def _config(config=None):
    if config is not None:
        return config
    if has_app_context():
        return current_app.config
    return {}


def payments_enabled(config=None):
    return bool(_config(config).get("PAYMENTS_ENABLED"))


def is_production(config=None):
    """Anything but development, testing and staging (FLASK_ENV)."""
    env = (_config(config).get("DEPLOY_ENV") or "production").lower()
    return env not in ("development", "testing", "staging")


def key_mode(key):
    """"live", "test" or None for a Stripe secret or restricted key."""
    if not key:
        return None
    if key.startswith(("sk_live_", "rk_live_")):
        return "live"
    if key.startswith(("sk_test_", "rk_test_")):
        return "test"
    return None


def check_payments_config(config):
    """At boot: in production, payments stay off unless the key is a live
    one (a sandbox key there would sell nothing real and take test events
    as real). Returns why they were turned off, or None."""
    if not config.get("PAYMENTS_ENABLED") or not is_production(config):
        return None
    if key_mode(config.get("STRIPE_SECRET_KEY")) == "live":
        return None
    config["PAYMENTS_ENABLED"] = False
    return ("PAYMENTS_ENABLED is set, but the Stripe key is not a live "
            "key; payments stay off in production.")


def refuse_without_paid_features(user, config=None):
    """For a route that starts an optional model call (new speech for a
    profile or a reference): the 402 to answer while payments are on and
    the account has no AI replies, else None."""
    if ai_replies_allowed(user, config):
        return None
    return subscription_required_response(user)


def access_state(user, config=None):
    """Where the account stands: "full" while payments are off, for an
    admin and for a plan set by hand; otherwise the subscription's
    ACCESS_STATES ("active", "lapsed", "none")."""
    if not payments_enabled(config) or user is None:
        return "full"
    if getattr(user, "is_admin", False) or user.plan_set_by_hand:
        return "full"
    return user.subscription_access_state


# State "none" (#438; Peter, 2026-11-12: "people shouldn't be let in until
# they have a subscription"): the only routes such an account reaches.
# Everything else under /api and /media answers no_subscription_response.
_OPEN_EXACT = {
    ("GET", "/api/dashboard"),            # who am I (no profile in it)
    ("PATCH", "/api/dashboard/timezone"),
    ("POST", "/api/account/delete"),
    ("POST", "/api/account/delete/confirm"),
    ("POST", "/api/account/restore"),
    ("POST", "/api/account/restore/decline"),
    ("GET", "/api/account/restore"),
}
_OPEN_PREFIXES = ("/api/billing", "/api/terms", "/api/webhooks",
                  "/api/health", "/api/ready", "/api/live")

NO_SUBSCRIPTION_ERROR = "no_subscription"
NO_SUBSCRIPTION_MESSAGE = "This account has no active subscription yet."


def open_without_subscription(method, path):
    """Whether an account in state "none" may call *method* *path*."""
    path = path.rstrip("/") or "/"
    if not (path.startswith("/api") or path.startswith("/media")):
        return True   # sign-in, sign-out, the pages themselves
    if (method, path) in _OPEN_EXACT:
        return True
    return any(path == p or path.startswith(p + "/") for p in _OPEN_PREFIXES)


def no_subscription_response():
    from flask import jsonify
    return jsonify({"error": NO_SUBSCRIPTION_ERROR,
                    "message": NO_SUBSCRIPTION_MESSAGE}), 402


def block_accounts_without_subscription():
    """The app's before_request (#438). Payments on: an approved account
    that never had a subscription (and whose plan was not set by hand) is
    not let in. It reaches only who-am-I, its subscription, the terms and
    deleting itself; every other /api and /media route answers 402
    {"error": "no_subscription"} before the view runs. No-op while
    payments are off."""
    from flask_login import current_user
    if not current_user.is_authenticated:
        return None
    return refuse_not_let_in(current_user)


def refuse_not_let_in(user):
    """402 no_subscription when *user* (the session's, or the account an
    API token belongs to: utils/api_tokens) is not let in and this
    request is not one it may make; None otherwise. The same rule for
    every way a request is authenticated."""
    if not payments_enabled():
        return None
    from flask import request
    if user is None or not getattr(user, "approved", False):
        return None
    if open_without_subscription(request.method, request.path):
        return None
    if access_state(user) != "none":
        return None
    return no_subscription_response()


def ai_replies_allowed(user, config=None):
    """Whether *user* may get an AI reply. Always True while payments
    are off, for admins, and on a paid plan (set by hand or subscribed)."""
    if not payments_enabled(config):
        return True
    if user is None:
        return True
    if getattr(user, "is_admin", False):
        return True
    return bool(user.on_paid_plan)


# What stops, per kind of refused call: (the clause after "so", the
# sentence without a subscription, what still works). Without the paid
# set an account makes no billed call at all (decided under Peter's "no
# AI replies and background jobs", 2026-11-12): replies, transcription,
# search by meaning, todo merges and poll drafts alike.
_PAUSED = {
    "reply": ("Loore doesn't reply",
              "Loore doesn't reply on an account without a subscription.",
              "Your writing is saved."),
    "transcribe": ("Loore doesn't transcribe recordings",
                   "Loore doesn't transcribe recordings on an account "
                   "without a subscription.",
                   "Typing works as before."),
    "search": ("search by meaning is paused",
               "Search by meaning is paused on an account without a "
               "subscription.",
               "Search by words works as before."),
    "ai": ("Loore doesn't use AI for this",
           "Loore doesn't use AI on an account without a subscription.",
           "Your writing is saved."),
    "sync": ("Loore doesn't sync bookmarks from X",
             "Loore doesn't sync bookmarks from X on an account without a "
             "subscription.",
             "Your saved references stay."),
}


def subscription_required_message(user=None, kind="reply"):
    """Why the call is refused, for the web and the iPhone alike: it
    names the state, never where or how to pay (the iPhone app shows it
    as it is). The web adds its link to Settings."""
    so, alone, works = _PAUSED.get(kind, _PAUSED["reply"])
    status = getattr(user, "subscription_status", None) if user else None
    if status == "ended":
        return f"Your subscription has ended, so {so}. {works}"
    if status == "past_due":
        return f"Your last payment didn't go through, so {so}. {works}"
    if user is not None and getattr(user, "is_alpha_tester", False):
        return f"Your free month has ended, so {so}. {works}"
    return f"{alone} {works}"


def subscription_required_payload(user=None, kind="reply"):
    return {
        "error": "subscription_required",
        "message": subscription_required_message(user, kind),
    }


def subscription_required_response(user=None, kind="reply"):
    from flask import jsonify
    return jsonify(subscription_required_payload(user, kind)), 402


def refuse_billed_call(user, kind="ai", config=None):
    """402 subscription_required when *user* may make no billed call (no
    paid set while payments are on); None otherwise."""
    if ai_replies_allowed(user, config):
        return None
    return subscription_required_response(user, kind)


def subscription_required_fields(user, config=None):
    """For a 2xx that saved the writing: the fields that say the reply
    was skipped and why, or None when the reply may go ahead."""
    if ai_replies_allowed(user, config):
        return None
    return {"subscription_required": True,
            "subscription_message": subscription_required_message(user)}


def check_ai_replies(user, config=None):
    """Raise SubscriptionRequired when *user* may not get an AI reply."""
    if not ai_replies_allowed(user, config):
        raise SubscriptionRequired(user)


def level_spend_limit_usd(level, config=None):
    cfg = _config(config)
    if level not in SUBSCRIPTION_LEVELS:
        return None
    value = cfg.get(f"SUBSCRIPTION_SPEND_LIMIT_USD_{level.upper()}")
    return None if value is None else float(value)


def subscription_spend_limit_usd(user, config=None):
    """The monthly ceiling an account's subscription gives it, or None:
    for an account set by hand (its cap stays what it was) and for one
    whose subscription gives nothing. An alpha tester's free month without
    a subscription has the discounted level's ceiling, the level the
    month leads to."""
    if user is None or getattr(user, "plan_set_by_hand", True):
        return None
    if getattr(user, "subscription_grants_access", False):
        return level_spend_limit_usd(user.subscription_level, config)
    if getattr(user, "alpha_free_month_active", False):
        return level_spend_limit_usd("discounted", config)
    return None


# ── The alpha free month (#438) ─────────────────────────────────────────
# Peter, 2026-11-12: alpha testers are "free for one month after the
# opening, then the discounted price". `flask alpha-free-month --apply`
# moves every approved account on the hand-set "alpha" plan to "free" and
# marks it (User.alpha_free_granted_at); the month starts at its first
# request after that (start_alpha_free_month). During it the account has
# everything, without a card; it may subscribe at any time, at the
# discounted price without a code, and the first payment is then due when
# the month ends. After it, without a subscription, the account is lapsed:
# reading, writing and export stay.

# Stripe refuses a Checkout trial that ends less than 48 hours ahead; a
# tester who subscribes in the last two days gets those hours on top.
CHECKOUT_TRIAL_MIN = timedelta(hours=49)


def start_alpha_free_month(user, config=None, now=None):
    """The first request of an alpha tester after the grant starts the
    free month. Returns True when this call started it. A conditional
    write, so two requests at once agree on one end date."""
    if user is None or not payments_enabled(config):
        return False
    if (getattr(user, "alpha_free_granted_at", None) is None
            or user.alpha_free_until is not None):
        return False
    from backend.extensions import db
    from backend.models import User
    cfg = _config(config)
    now = now or datetime.utcnow()
    until = now + timedelta(days=int(cfg.get("ALPHA_FREE_MONTH_DAYS") or 30))
    try:
        written = (User.query
                   .filter(User.id == user.id,
                           User.alpha_free_granted_at.isnot(None),
                           User.alpha_free_until.is_(None))
                   .update({"alpha_free_until": until},
                           synchronize_session=False))
        db.session.commit()
    except Exception:  # noqa: BLE001 - never fail a request over this
        db.session.rollback()
        return False
    db.session.refresh(user)
    return bool(written)


def start_alpha_free_month_hook():
    """before_request: start a signed-in alpha tester's free month."""
    from flask_login import current_user
    if current_user.is_authenticated:
        start_alpha_free_month(current_user)


def alpha_free_month_end(user, config=None, now=None):
    """When an alpha tester's running free month ends (its expected end
    when it has not started yet), or None."""
    if not getattr(user, "alpha_free_month_active", False):
        return None
    if user.alpha_free_until is not None:
        return user.alpha_free_until
    cfg = _config(config)
    now = now or datetime.utcnow()
    return now + timedelta(days=int(cfg.get("ALPHA_FREE_MONTH_DAYS") or 30))


def alpha_checkout_trial_end(user, config=None, now=None):
    """The trial a Checkout gives an alpha tester who subscribes during
    the free month: until the month ends (at least CHECKOUT_TRIAL_MIN
    ahead), so the first payment is due then. None once the month is
    over, or when the account used a trial already: a later subscription
    is charged at checkout."""
    if getattr(user, "subscription_trial_used", False):
        return None
    end = alpha_free_month_end(user, config, now)
    if end is None:
        return None
    now = now or datetime.utcnow()
    return max(end, now + CHECKOUT_TRIAL_MIN)


def alpha_free_month_candidates(usernames=None):
    """Accounts the free month goes to: approved, not admins, on the
    hand-set "alpha" plan, not being deleted, not given it already.
    *usernames* narrows the list."""
    from backend.models import User
    q = User.query.filter(User.approved.is_(True),
                          User.is_admin.isnot(True),
                          User.plan == "alpha",
                          User.deleted_at.is_(None),
                          User.alpha_free_granted_at.is_(None))
    if usernames:
        q = q.filter(User.username.in_(list(usernames)))
    return q.order_by(User.id).all()


def grant_alpha_free_month(users, now=None):
    """Move *users* from "alpha" to "free" with a free month that starts
    at their next request. Commits. Returns how many changed."""
    from backend.extensions import db
    now = now or datetime.utcnow()
    for user in users:
        user.plan = "free"
        user.alpha_free_granted_at = now
        user.alpha_free_until = None
    db.session.commit()
    return len(users)


def undo_alpha_free_month(usernames=None):
    """Put alpha testers back on the hand-set "alpha" plan and forget
    their free month (a subscription they started stays theirs and keeps
    renewing until they cancel it). Commits. Returns how many changed."""
    from backend.extensions import db
    from backend.models import User
    q = User.query.filter(User.alpha_free_granted_at.isnot(None))
    if usernames:
        q = q.filter(User.username.in_(list(usernames)))
    users = q.all()
    for user in users:
        user.plan = "alpha"
        user.alpha_free_granted_at = None
        user.alpha_free_until = None
    db.session.commit()
    return len(users)


def checkout_levels(user):
    """The levels the account may choose without a code: an alpha tester
    has the discounted one too, first."""
    if getattr(user, "is_alpha_tester", False):
        return ("discounted",) + OFFERED_LEVELS
    return OFFERED_LEVELS


def level_price_cents(level, config=None):
    cfg = _config(config)
    return int(cfg.get(f"SUBSCRIPTION_PRICE_CENTS_{level.upper()}") or 0)


def subscription_state(user, config=None):
    """The account's subscription as the clients show it (web Settings,
    iPhone Settings). Ids and payment details stay on the server."""
    cfg = _config(config)
    state = access_state(user, cfg)
    alpha = bool(user.is_alpha_tester)
    return {
        # "set_by_hand" (also admins), "active", "lapsed", "none".
        "state": "set_by_hand" if state == "full" else state,
        "level": user.subscription_level,
        "status": user.subscription_status or "none",
        "current_period_end": iso_utc(user.subscription_period_end),
        "trial_end": iso_utc(user.subscription_trial_end),
        "cancel_at_period_end": bool(user.subscription_cancel_at_period_end),
        # An alpha tester's free month takes the trial's place.
        "trial_available": not user.subscription_trial_used and not alpha,
        "trial_days": int(cfg.get("SUBSCRIPTION_TRIAL_DAYS") or 0),
        # The alpha free month: whether this is an alpha tester, whether
        # the month still runs, and when it ends (an expected date while
        # it has not started).
        "alpha_tester": alpha,
        "free_month_active": bool(user.alpha_free_month_active),
        "free_month_until": iso_utc(
            alpha_free_month_end(user, cfg) or user.alpha_free_until)
        if alpha else None,
        "plan_set_by_hand": bool(user.plan_set_by_hand),
        "ai_replies": ai_replies_allowed(user, cfg),
        # Live at Stripe (it renews) but giving nothing: a failed payment
        # past its grace days, or withdrawn after a dispute or a refund.
        "access": bool(user.subscription_grants_access),
        "has_customer": bool(user.stripe_customer_id),
    }


def user_payload_fields(user, config=None):
    """Extra fields of the signed-in user while payments are on; none
    while they are off, so the payload is exactly what it was."""
    if not payments_enabled(config):
        return {}
    return {
        "payments_enabled": True,
        "subscription": subscription_state(user, config),
    }
