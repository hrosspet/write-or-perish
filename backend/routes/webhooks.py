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

    # Title-only, no body: the issue title says which one, the verdict
    # prefix says what happened, and "Take a look" carries the details.
    # The issue title is the submitting user's own content going back to
    # its author — the "no user content in notifications" rule guards
    # against leaks to OTHER users, which this is not.
    issue_ref = f'"{_clip(title, 120)}" (#{number})'
    if issue.get("state_reason") == "completed":
        notify_user(
            user.id,
            type="fix_ready",
            title=f"Fixed: {issue_ref}",
            link=link,
            replace_unread=False,
        )
    else:
        # not_planned / duplicate — be honest rather than silent.
        notify_user(
            user.id,
            type="issue_declined",
            title=f"Closed without a fix: {issue_ref}",
            link=link,
            replace_unread=False,
        )

    logger.info(
        "GitHub webhook: notified user %s about closed issue #%s (%s)",
        user.id, number, issue.get("state_reason"))
    return jsonify({"status": "notified"})
