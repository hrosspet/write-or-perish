"""The signed-in user's subscription (#438): what it is, Checkout, the
customer portal. Every route answers 404 while PAYMENTS_ENABLED is off.

Only the web's Settings page calls these. The iPhone app shows the state
from the user object and offers no way to buy (Peter, 2026-11-12).
"""
import logging
from functools import wraps

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from backend.extensions import db
from backend.models import SUBSCRIPTION_GRANTING_STATUSES
from backend.utils import discount_codes, stripe_billing
from backend.utils.subscriptions import (
    checkout_levels, level_price_cents, payments_enabled, subscription_state)

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing_bp", __name__)

UNAVAILABLE_MESSAGE = ("Subscriptions are not available right now. "
                       "Please try again later.")


def payments_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not payments_enabled():
            return jsonify({"error": "Not found"}), 404
        if not current_user.approved and not _blocked_may(current_user):
            # Waitlisted accounts subscribe once they are let in.
            return jsonify({"error": "not_approved",
                            "message": "Your account is not approved "
                                       "yet."}), 403
        return fn(*args, **kwargs)
    return wrapper


def _blocked_may(user):
    """An account the admin blocked may still read its subscription and
    open Stripe's page to cancel it or change the card (#697,
    utils/approval.BLOCKED_OPEN); never start a checkout or a sync."""
    from backend.utils.approval import is_blocked, open_while_blocked
    return is_blocked(user) and open_while_blocked(request.method,
                                                   request.path)


def _too_many(kind):
    """None, or the 429 once the account used the hour's checkouts or
    syncs (#697, utils/billing_limits). Counts this request otherwise."""
    from backend.utils import billing_limits
    try:
        billing_limits.check_and_count(current_user, kind)
    except billing_limits.TooManyBillingRequests:
        message = (billing_limits.TOO_MANY_CHECKOUTS_MESSAGE
                   if kind == "checkout" else "Please try again later.")
        return jsonify({"error": "too_many_requests",
                        "message": message}), 429
    return None


def _text(data, key):
    """A string field of the body, stripped; None when it is something
    else (a number, a list), which the routes answer with 400."""
    value = data.get(key)
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else None


def _unavailable(exc):
    logger.error("Billing unavailable: %s", type(exc).__name__)
    return jsonify({"error": "billing_unavailable",
                    "message": UNAVAILABLE_MESSAGE}), 503


def _state():
    cfg = current_app.config
    return {
        "subscription": subscription_state(current_user, cfg),
        # An alpha tester is offered the discounted level too, first.
        "levels": [{"level": level,
                    "amount_cents": level_price_cents(level, cfg),
                    "currency": cfg.get("SUBSCRIPTION_CURRENCY", "usd")}
                   for level in checkout_levels(current_user)],
        "contact_email": cfg.get("SUBSCRIPTION_CONTACT_EMAIL"),
    }


@billing_bp.route("", methods=["GET"])
@billing_bp.route("/", methods=["GET"])
@login_required
@payments_required
def get_billing():
    return jsonify(_state())


def _checkout_level(user, data):
    """(level, code, None), or (None, None, the refusal). A code takes no
    use here: it is counted when its subscription is created."""
    raw = _text(data, "code")
    if raw is None:
        return None, None, (jsonify({"error": "code_not_valid",
                                     "message": discount_codes.NOT_VALID_MESSAGE}),
                            400)
    if not raw:
        level = data.get("level")
        # The discounted level without a code: alpha testers only.
        if level not in checkout_levels(user):
            return None, None, (jsonify({"error": "invalid_level",
                                         "message": "Choose a level."}), 400)
        return level, None, None
    try:
        code = discount_codes.check_code(user, raw, current_app.config)
    except discount_codes.TooManyAttempts:
        return None, None, (jsonify({
            "error": "too_many_attempts",
            "message": discount_codes.TOO_MANY_MESSAGE}), 429)
    except discount_codes.CodeNotValid:
        return None, None, (jsonify({
            "error": "code_not_valid",
            "message": discount_codes.NOT_VALID_MESSAGE}), 400)
    return "discounted", code, None


@billing_bp.route("/checkout", methods=["POST"])
@login_required
@payments_required
def start_checkout():
    """Body: {"level": "standard"|"supporter"} or {"code": "..."} (a code
    opens the discounted level). Answers {"url"} of the Stripe-hosted
    Checkout."""
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    user = current_user
    if user.plan_set_by_hand:
        return jsonify({
            "error": "plan_set_by_hand",
            "message": "Your account has the paid features already; there "
                       "is nothing to pay."}), 409
    if user.subscription_status in SUBSCRIPTION_GRANTING_STATUSES:
        return jsonify({
            "error": "already_subscribed",
            "message": "You have a subscription already. Manage it from "
                       "the link in Settings."}), 409
    level, code, refused = _checkout_level(user, data)
    if refused is not None:
        return refused
    too_many = _too_many("checkout")
    if too_many is not None:
        return too_many
    try:
        gateway = stripe_billing.get_gateway()
        session = stripe_billing.create_checkout(
            gateway, user, level, code=code)
    except stripe_billing.CheckoutAlreadyCompleted:
        # The earlier checkout was finished; the webhook is on its way.
        return jsonify({
            "error": "already_subscribed",
            "message": "Your checkout is complete; your subscription is "
                       "starting. Reload this page in a moment."}), 409
    except Exception as e:  # noqa: BLE001 - not configured, Stripe down
        db.session.rollback()
        return _unavailable(e)
    return jsonify({"url": session.get("url")})


@billing_bp.route("/portal", methods=["POST"])
@login_required
@payments_required
def open_portal():
    """{"url"} of Stripe's customer portal: change the card, invoices,
    cancel. Sessions live for minutes, so each press makes a new one."""
    if not current_user.stripe_customer_id:
        return jsonify({"error": "no_subscription",
                        "message": "There is no subscription to manage."}), 409
    try:
        session = stripe_billing.create_portal(
            stripe_billing.get_gateway(), current_user)
    except Exception as e:  # noqa: BLE001
        return _unavailable(e)
    return jsonify({"url": session.get("url")})


@billing_bp.route("/sync", methods=["POST"])
@login_required
@payments_required
def sync():
    """Read the account's subscription from Stripe now, without waiting
    for the webhook: the return page after Checkout calls it with the
    session id. A session of another account is ignored."""
    data = request.get_json(silent=True)
    data = data if isinstance(data, dict) else {}
    session_id = _text(data, "session_id")
    if session_id is None:
        return jsonify({"error": "invalid_session"}), 400
    too_many = _too_many("sync")
    if too_many is not None:
        return too_many
    user = current_user
    try:
        gateway = stripe_billing.get_gateway()
        sub_id = None
        if session_id.startswith("cs_"):
            session = gateway.retrieve_checkout_session(session_id)
            if session.get("client_reference_id") == str(user.id):
                sub_id = session.get("subscription")
                if isinstance(sub_id, dict):
                    sub_id = sub_id.get("id")
        sub_id = sub_id or user.stripe_subscription_id
        if sub_id:
            stripe_billing.sync_subscription(gateway, sub_id)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return _unavailable(e)
    db.session.refresh(user)
    return jsonify(_state())
