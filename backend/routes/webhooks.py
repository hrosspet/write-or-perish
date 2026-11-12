"""Inbound webhooks. Currently: GitHub issue-close notifications.

Closes the loop opened by Voice-mode issue submission (#207 channel +
propose_github_issue): every Loore-created issue carries a
``loore:{username}`` label, so when the developer closes it on GitHub we
can tell the submitting user through the dev-update channel — a targeted
UserNotification they see next time they open Loore.

Unlike the rest of the API this endpoint is called by GitHub, not by a
logged-in user; authentication is the webhook HMAC signature
(X-Hub-Signature-256, keyed by GITHUB_WEBHOOK_SECRET).
"""
import hashlib
import hmac
import logging

from flask import Blueprint, current_app, jsonify, request

from backend.models import User
from backend.utils.notifications import notify_user

logger = logging.getLogger(__name__)

webhooks_bp = Blueprint("webhooks", __name__)

LOORE_LABEL_PREFIX = "loore:"


def _verify_signature(secret, payload, signature_header):
    """Constant-time check of GitHub's HMAC-SHA256 payload signature."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


def _clip(text, limit):
    """Keep the notification title inside its 200-char column even for
    long issue titles."""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _submitter_from_labels(labels):
    """Extract the Loore username from an issue's ``loore:{username}``
    label. Returns None for issues not created through Loore."""
    for label in labels or []:
        name = label.get("name", "")
        if name.startswith(LOORE_LABEL_PREFIX):
            return name[len(LOORE_LABEL_PREFIX):]
    return None


@webhooks_bp.route("/github", methods=["POST"])
def github_webhook():
    secret = current_app.config.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        # Fail closed: without a shared secret we cannot authenticate
        # GitHub, so the endpoint is effectively disabled.
        return jsonify({"error": "webhook not configured"}), 503

    if not _verify_signature(
            secret, request.get_data(),
            request.headers.get("X-Hub-Signature-256")):
        logger.warning("GitHub webhook: bad or missing signature")
        return jsonify({"error": "invalid signature"}), 403

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return jsonify({"status": "pong"})
    if event != "issues":
        return jsonify({"status": "ignored", "reason": "event"})

    payload = request.get_json(silent=True) or {}
    if payload.get("action") != "closed":
        return jsonify({"status": "ignored", "reason": "action"})

    issue = payload.get("issue") or {}
    username = _submitter_from_labels(issue.get("labels"))
    if not username:
        return jsonify({"status": "ignored", "reason": "not a loore issue"})

    user = User.query.filter_by(username=username).first()
    if user is None:
        logger.warning(
            "GitHub webhook: no user for label loore:%s (issue #%s)",
            username, issue.get("number"))
        return jsonify({"status": "ignored", "reason": "unknown user"})

    number = issue.get("number")
    title = issue.get("title") or f"issue #{number}"
    link = issue.get("html_url")

    # Title-only, no body: the title is the user's own issue title
    # quoted back to them; the verdict is carried by `type` (the modal
    # renders it as an eyebrow label) and "Take a look" has the details.
    # The issue title is the submitting user's own content going back to
    # its author — the "no user content in notifications" rule guards
    # against leaks to OTHER users, which this is not.
    issue_ref = f'"{_clip(title, 150)}" (#{number})'
    if issue.get("state_reason") == "completed":
        notify_user(
            user.id,
            type="fix_ready",
            title=issue_ref,
            link=link,
            replace_unread=False,
        )
    else:
        # not_planned / duplicate — be honest rather than silent.
        notify_user(
            user.id,
            type="issue_declined",
            title=issue_ref,
            link=link,
            replace_unread=False,
        )

    logger.info(
        "GitHub webhook: notified user %s about closed issue #%s (%s)",
        user.id, number, issue.get("state_reason"))
    return jsonify({"status": "notified"})


# ── Stripe (#438) ───────────────────────────────────────────────────────
# Called by Stripe, authenticated by the Stripe-Signature header (keyed by
# STRIPE_WEBHOOK_SECRET). Each event is applied once: its id goes into
# stripe_event in the same transaction as what it changed, so a redelivery
# (or two deliveries at once) is answered "duplicate". An event names the
# subscription that changed; its state is read from Stripe then
# (utils/stripe_billing.handle_event), so the order events arrive in does
# not matter. An event Loore does not handle is acknowledged with 200; a
# failure worth a retry (Stripe unreachable) answers 503 without
# recording the event, and Stripe sends it again.

def _verified_stripe_event(config):
    """(event, None) for a correctly signed event, else (None, the
    answer: 404 while payments are off and no secret is set, 503 while
    they are on without one, 400 for a bad signature or body)."""
    import json

    import stripe
    secret = config.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        if not config.get("PAYMENTS_ENABLED"):
            return None, (jsonify({"error": "Not found"}), 404)
        return None, (jsonify({"error": "webhook not configured"}), 503)
    try:
        text = request.get_data().decode("utf-8")
        stripe.WebhookSignature.verify_header(
            text, request.headers.get("Stripe-Signature"), secret,
            stripe.Webhook.DEFAULT_TOLERANCE)
        event = json.loads(text)
    except (stripe.SignatureVerificationError, UnicodeDecodeError,
            ValueError):
        logger.warning("Stripe webhook: bad or missing signature")
        return None, (jsonify({"error": "invalid signature"}), 400)
    if not isinstance(event, dict) or not event.get("id"):
        return None, (jsonify({"error": "invalid event"}), 400)
    return event, None


@webhooks_bp.route("/stripe", methods=["POST"])
def stripe_webhook():
    from sqlalchemy.exc import IntegrityError

    from backend.extensions import db
    from backend.models import StripeEvent
    from backend.utils import stripe_billing

    config = current_app.config
    event, refused = _verified_stripe_event(config)
    if refused is not None:
        return refused
    event_id = event["id"]
    etype = event.get("type") or ""

    if db.session.get(StripeEvent, event_id) is not None:
        return jsonify({"status": "duplicate"})
    db.session.add(StripeEvent(id=event_id, type=etype[:128]))
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "duplicate"})

    if etype not in stripe_billing.HANDLED_EVENTS:
        db.session.commit()
        return jsonify({"status": "ignored"})
    try:
        outcome = stripe_billing.handle_event(
            stripe_billing.get_gateway(config), event, config)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "duplicate"})
    except Exception as e:  # noqa: BLE001 - Stripe retries the event
        db.session.rollback()
        logger.error("Stripe webhook: %s %s not applied: %s", etype,
                     event_id, type(e).__name__)
        return jsonify({"error": "not applied, retry"}), 503
    logger.info("Stripe webhook: %s %s %s", etype, event_id, outcome)
    return jsonify({"status": outcome})
