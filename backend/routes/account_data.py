"""The user's own "Delete all my writing" (#268).

Asking schedules a purge after the grace period (PURGE_GRACE_DAYS); until
it starts the user can cancel it. The purge itself runs in Celery (see
backend/utils/user_purge.py). The account stays: login, username,
settings and plan.

Replaces DELETE /api/delete_my_data, which deleted only the user's own
node rows (not imports or AI replies, nor any dependent table) and
failed on Postgres's foreign keys.
"""
import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from backend.utils.user_purge import (
    PurgeRefused, active_job, cancel_purge, deletion_status, schedule_purge,
)

logger = logging.getLogger(__name__)

account_data_bp = Blueprint("account_data_bp", __name__)


@account_data_bp.route("/data", methods=["GET"])
@login_required
def get_data_deletion():
    """The user's scheduled, running or last finished deletion."""
    return jsonify(deletion_status(current_user.id)), 200


@account_data_bp.route("/data", methods=["DELETE"])
@login_required
def request_data_deletion():
    """Schedule the deletion of all the user's writing.

    Body: {"confirm": "<username>"} — the user types their username in
    the dialog; anything else is refused, so no stray request deletes."""
    data = request.get_json(silent=True) or {}
    typed = (data.get("confirm") or "").strip()
    if not typed or typed.lower() != (current_user.username or "").lower():
        return jsonify({"error": "Type your username to confirm.",
                        "code": "confirm_mismatch"}), 400
    try:
        job, created = schedule_purge(
            current_user, requested_by_id=current_user.id, source="self")
    except PurgeRefused:
        return jsonify({"error": "This account cannot be deleted here.",
                        "code": "purge_refused"}), 403
    if created:
        logger.info("User %s scheduled a data purge (job %s) for %s",
                    current_user.id, job.id, job.scheduled_for)
    return jsonify(deletion_status(current_user.id)), 202 if created else 200


@account_data_bp.route("/data/cancel", methods=["POST"])
@login_required
def cancel_data_deletion():
    """Cancel the deletion while it waits out the grace period."""
    if cancel_purge(current_user.id, current_user.id):
        logger.info("User %s cancelled their data purge", current_user.id)
        return jsonify(deletion_status(current_user.id)), 200
    job = active_job(current_user.id)
    if job is not None and job.status == "running":
        return jsonify({"error": "The deletion has already started and "
                                 "can no longer be cancelled.",
                        "code": "already_running"}), 409
    return jsonify({"error": "No deletion is scheduled.",
                    "code": "not_scheduled"}), 404
