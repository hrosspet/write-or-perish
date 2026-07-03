"""The dev-update channel (#207): user-facing changelog, targeted
notifications, and admin polls, served through one surface with shared
read/skip semantics. Skip = show again on next open; read = gone for good.

Everything here is per-user private state. Poll answers cross the privacy
boundary ONLY through the explicit /send endpoint (opt-in 2) — see
PollResponse in models.py.
"""
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user

from backend.extensions import db
from backend.models import (
    ChangelogReadState, UserNotification, Poll, PollResponse,
)
from backend.utils.changelog import parse_changelog, unread_sections_for
from backend.utils.timefmt import iso_utc

logger = logging.getLogger(__name__)

updates_bp = Blueprint("updates_bp", __name__)


def _updates_enabled():
    return current_app.config.get("DEV_UPDATES_V1", True)


def _serialize_response(resp):
    """The user's own view of their poll response (drafts included —
    they're private to the user)."""
    if resp is None:
        return None
    return {
        "status": resp.status,
        "content": resp.get_content(),
        "generated_by": resp.generated_by,
        "draft_requested_at": iso_utc(resp.draft_requested_at),
        "sent_at": iso_utc(resp.sent_at),
    }


def _pending_polls_for(user):
    """Active polls the user hasn't resolved (sent or declined)."""
    responses = {
        r.poll_id: r
        for r in PollResponse.query.filter_by(user_id=user.id).all()
    }
    pending = []
    for poll in Poll.query.filter_by(closed_at=None).order_by(
            Poll.created_at.asc()).all():
        resp = responses.get(poll.id)
        if resp is not None and resp.status in ("sent", "declined"):
            continue
        pending.append({
            "id": poll.id,
            "question": poll.question,
            "created_at": iso_utc(poll.created_at),
            "response": _serialize_response(resp),
        })
    return pending


@updates_bp.route("", methods=["GET"])
@login_required
def get_updates():
    """Everything unread for the current user. All lists empty -> the
    frontend shows nothing."""
    if not _updates_enabled():
        return jsonify(
            {"changelog": [], "notifications": [], "polls": []}), 200

    read_states = ChangelogReadState.query.filter_by(
        user_id=current_user.id).all()
    changelog = [
        {
            "id": s["id"],
            "title": s["title"],
            "date": s["date"].isoformat() if s["date"] else None,
            "body": s["body"],
        }
        for s in unread_sections_for(current_user, read_states)
    ]

    notifications = [
        {
            "id": n.id,
            "type": n.type,
            "title": n.title,
            "body": n.body,
            "link": n.link,
            "created_at": iso_utc(n.created_at),
        }
        for n in UserNotification.query.filter_by(
            user_id=current_user.id, status="unread"
        ).order_by(UserNotification.created_at.desc()).limit(20).all()
    ]

    return jsonify({
        "changelog": changelog,
        "notifications": notifications,
        "polls": _pending_polls_for(current_user),
    }), 200


# ---------------------------------------------------------------------------
# Changelog read/skip
# ---------------------------------------------------------------------------

@updates_bp.route("/changelog/<section_id>/<action>", methods=["POST"])
@login_required
def mark_changelog(section_id, action):
    if action not in ("read", "skip"):
        return jsonify({"error": "Unknown action"}), 404
    if not any(s["id"] == section_id for s in parse_changelog()):
        return jsonify({"error": "Unknown section"}), 404

    state = ChangelogReadState.query.filter_by(
        user_id=current_user.id, section_id=section_id).first()
    if state is None:
        state = ChangelogReadState(
            user_id=current_user.id, section_id=section_id)
        db.session.add(state)
    state.status = "read" if action == "read" else "skipped"
    db.session.commit()
    return jsonify({"section_id": section_id, "status": state.status}), 200


# ---------------------------------------------------------------------------
# Notifications read/skip
# ---------------------------------------------------------------------------

@updates_bp.route("/notifications/<int:notification_id>/<action>",
                  methods=["POST"])
@login_required
def mark_notification(notification_id, action):
    if action not in ("read", "skip"):
        return jsonify({"error": "Unknown action"}), 404
    notification = UserNotification.query.filter_by(
        id=notification_id, user_id=current_user.id).first_or_404()
    if action == "read":
        notification.status = "read"
        notification.read_at = datetime.utcnow()
    else:
        notification.skipped_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"id": notification.id, "status": notification.status}), 200


# ---------------------------------------------------------------------------
# Polls — the user side (two-phase opt-in)
# ---------------------------------------------------------------------------

def _get_active_poll(poll_id):
    poll = Poll.query.get_or_404(poll_id)
    if poll.closed_at is not None:
        return None
    return poll


def _get_or_create_response(poll):
    resp = PollResponse.query.filter_by(
        poll_id=poll.id, user_id=current_user.id).first()
    if resp is None:
        resp = PollResponse(poll_id=poll.id, user_id=current_user.id)
        db.session.add(resp)
    return resp


@updates_bp.route("/polls/<int:poll_id>", methods=["GET"])
@login_required
def get_poll(poll_id):
    """Poll + the user's own response state (polled while a draft is being
    generated)."""
    poll = Poll.query.get_or_404(poll_id)
    resp = PollResponse.query.filter_by(
        poll_id=poll.id, user_id=current_user.id).first()
    return jsonify({
        "id": poll.id,
        "question": poll.question,
        "active": poll.active,
        "response": _serialize_response(resp),
    }), 200


@updates_bp.route("/polls/<int:poll_id>/draft", methods=["POST"])
@login_required
def request_draft(poll_id):
    """Opt-in 1: the user asks the LLM to draft an answer from their
    archive. Refused when their global AI-usage setting opts out."""
    from backend.utils.privacy import AI_ALLOWED
    from backend.utils.spend import user_is_capped

    poll = _get_active_poll(poll_id)
    if poll is None:
        return jsonify({"error": "This poll is closed."}), 409
    if current_user.default_ai_usage not in AI_ALLOWED:
        return jsonify({
            "error": "Your AI-usage setting doesn't allow this. You can "
                     "still write an answer yourself."}), 403
    if user_is_capped(current_user):
        return jsonify({
            "error": "monthly_spend_limit_reached",
            "message": "You've reached your monthly usage limit."}), 402

    resp = _get_or_create_response(poll)
    if resp.status == "sent":
        return jsonify({"error": "Already sent."}), 409
    if resp.status == "drafting":
        return jsonify({"response": _serialize_response(resp)}), 200

    resp.status = "drafting"
    resp.draft_requested_at = datetime.utcnow()
    db.session.commit()

    from backend.tasks.poll_draft import draft_poll_response
    task = draft_poll_response.delay(resp.id)
    resp.draft_task_id = task.id
    db.session.commit()
    return jsonify({"response": _serialize_response(resp)}), 202


@updates_bp.route("/polls/<int:poll_id>/response", methods=["PUT"])
@login_required
def save_response(poll_id):
    """Save a hand-written or edited answer (still private)."""
    poll = _get_active_poll(poll_id)
    if poll is None:
        return jsonify({"error": "This poll is closed."}), 409
    content = (request.get_json() or {}).get("content", "")
    if not content.strip():
        return jsonify({"error": "Answer is empty."}), 400

    resp = _get_or_create_response(poll)
    if resp.status == "sent":
        return jsonify({"error": "Already sent."}), 409
    resp.set_content(content)
    resp.status = "draft"
    resp.generated_by = None  # user-authored/edited now
    db.session.commit()
    return jsonify({"response": _serialize_response(resp)}), 200


@updates_bp.route("/polls/<int:poll_id>/send", methods=["POST"])
@login_required
def send_response(poll_id):
    """Opt-in 2: explicitly share the answer with the admin. The ONLY
    transition that makes a response visible to anyone but the user."""
    poll = _get_active_poll(poll_id)
    if poll is None:
        return jsonify({"error": "This poll is closed."}), 409
    resp = PollResponse.query.filter_by(
        poll_id=poll.id, user_id=current_user.id).first()
    if resp is None or not (resp.content and resp.get_content().strip()):
        return jsonify({"error": "Nothing to send yet."}), 400
    if resp.status == "drafting":
        return jsonify({"error": "Draft still generating."}), 409
    if resp.status == "sent":
        return jsonify({"response": _serialize_response(resp)}), 200

    resp.status = "sent"
    resp.sent_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"response": _serialize_response(resp)}), 200


@updates_bp.route("/polls/<int:poll_id>/decline", methods=["POST"])
@login_required
def decline_poll(poll_id):
    """'No thanks' — permanently dismisses the poll for this user."""
    poll = Poll.query.get_or_404(poll_id)
    resp = _get_or_create_response(poll)
    if resp.status == "sent":
        return jsonify({"error": "Already sent."}), 409
    resp.status = "declined"
    db.session.commit()
    return jsonify({"response": _serialize_response(resp)}), 200
