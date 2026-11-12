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
    OFFERED_LEVELS, level_price_cents, payments_enabled, subscription_state)

logger = logging.getLogger(__name__)

billing_bp = Blueprint("billing_bp", __name__)

UNAVAILABLE_MESSAGE = ("Subscriptions are not available right now. "
                       "Please try again later.")


def payments_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not payments_enabled():
            return jsonify({"error": "Not found"}), 404
        return fn(*args, **kwargs)
    return wrapper


def _unavailable(exc):
    logger.error("Billing unavailable: %s", type(exc).__name__)
    return jsonify({"error": "billing_unavailable",
                    "message": UNAVAILABLE_MESSAGE}), 503


def _state():
    cfg = current_app.config
    return {
        "subscription": subscription_state(current_user, cfg),
        "levels": [{"level": level,
                    "amount_cents": level_price_cents(level, cfg),
                    "currency": cfg.get("SUBSCRIPTION_CURRENCY", "usd")}
                   for level in OFFERED_LEVELS],
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
    raw = (data.get("code") or "").strip()
    if not raw:
        level = data.get("level")
        if level not in OFFERED_LEVELS:
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
    data = request.get_json(silent=True) or {}
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
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
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
