"""The user's own "Delete all my writing" (#268).

Asking hides all the user's writing at once and schedules its purge after
the grace period (PURGE_GRACE_DAYS); until the purge starts, "Restore my
writing" brings back exactly what the request hid (Peter, 2026-10-09).
The purge itself runs in Celery (see backend/utils/user_purge.py). The
account stays: login, username, settings and plan.

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
    """Hide all the user's writing now and schedule its deletion after
    the grace period.

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
        from backend.utils.user_purge import hidden_counts
        logger.info("User %s scheduled a data purge (job %s) for %s; "
                    "hidden now: %s", current_user.id, job.id,
                    job.scheduled_for, hidden_counts(job.id))
    return jsonify(deletion_status(current_user.id)), 202 if created else 200


@account_data_bp.route("/data/restore", methods=["POST"])
@account_data_bp.route("/data/cancel", methods=["POST"])
@login_required
def restore_data():
    """Restore my writing: cancel the deletion while it waits out the
    grace period and show again exactly what the request hid, in one
    transaction (a restore that fails changes nothing)."""
    if cancel_purge(current_user.id, current_user.id):
        logger.info("User %s restored their writing (data purge "
                    "cancelled)", current_user.id)
        return jsonify(deletion_status(current_user.id)), 200
    job = active_job(current_user.id)
    if job is not None and job.status == "running":
        return jsonify({"error": "Your writing is being deleted now and "
                                 "can no longer be restored.",
                        "code": "already_running"}), 409
    return jsonify({"error": "There is nothing to restore.",
                    "code": "not_scheduled"}), 404
