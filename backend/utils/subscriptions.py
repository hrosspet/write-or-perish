"""What a subscription gives an account (#438), without calling Stripe.

The switch is PAYMENTS_ENABLED. Off, nothing here changes what an account
can do. On:

* an account gets AI replies when it is on a paid plan
  (User.on_paid_plan: a plan set by hand, alpha or pro, or a trialing,
  active or past-due subscription) or is an admin. Every other account
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
    if not payments_enabled():
        return None
    from flask import request
    from flask_login import current_user
    if not current_user.is_authenticated or not current_user.approved:
        return None
    if open_without_subscription(request.method, request.path):
        return None
    if access_state(current_user) != "none":
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


def subscription_required_message(user=None):
    """Why there is no reply, for the web and the iPhone alike: it names
    the state, never where or how to pay (the iPhone app shows it as it
    is). The web adds its link to Settings."""
    status = getattr(user, "subscription_status", None) if user else None
    if status == "ended":
        return ("Your subscription has ended, so Loore doesn't reply. "
                "Your writing is saved.")
    if status == "past_due":
        return ("Your last payment didn't go through, so Loore doesn't "
                "reply. Your writing is saved.")
    return ("Loore doesn't reply on an account without a subscription. "
            "Your writing is saved.")


def subscription_required_payload(user=None):
    return {
        "error": "subscription_required",
        "message": subscription_required_message(user),
    }


def subscription_required_response(user=None):
    from flask import jsonify
    return jsonify(subscription_required_payload(user)), 402


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
    whose subscription gives nothing."""
    if user is None or getattr(user, "plan_set_by_hand", True):
        return None
    if not getattr(user, "subscription_grants_access", False):
        return None
    return level_spend_limit_usd(user.subscription_level, config)


def level_price_cents(level, config=None):
    cfg = _config(config)
    return int(cfg.get(f"SUBSCRIPTION_PRICE_CENTS_{level.upper()}") or 0)


def subscription_state(user, config=None):
    """The account's subscription as the clients show it (web Settings,
    iPhone Settings). Ids and payment details stay on the server."""
    cfg = _config(config)
    state = access_state(user, cfg)
    return {
        # "set_by_hand" (also admins), "active", "lapsed", "none".
        "state": "set_by_hand" if state == "full" else state,
        "level": user.subscription_level,
        "status": user.subscription_status or "none",
        "current_period_end": iso_utc(user.subscription_period_end),
        "trial_end": iso_utc(user.subscription_trial_end),
        "cancel_at_period_end": bool(user.subscription_cancel_at_period_end),
        "trial_available": not user.subscription_trial_used,
        "trial_days": int(cfg.get("SUBSCRIPTION_TRIAL_DAYS") or 0),
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
