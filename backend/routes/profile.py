from flask import jsonify, current_app
from flask_login import login_required, current_user
from backend.models import UserProfile
from backend.extensions import db
from pathlib import Path

from flask import Blueprint

profile_bp = Blueprint("profile", __name__)

AUDIO_STORAGE_ROOT = "data/audio"


@profile_bp.route("/<int:profile_id>/audio", methods=["GET"])
@login_required
def get_audio(profile_id):
    """Return JSON with URL for TTS audio associated with a profile."""
    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    if not profile.audio_tts_url:
        return jsonify({"error": "No audio found for this profile"}), 404

    return jsonify({
        "tts_url": profile.audio_tts_url,
    })


@profile_bp.route("/<int:profile_id>/tts", methods=["POST"])
@login_required
def generate_tts(profile_id):
    """Trigger TTS generation for the user profile."""
    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    if profile.audio_tts_url:
        return jsonify({"message": "TTS already available", "tts_url": profile.audio_tts_url}), 200

    if not current_app.config.get("OPENAI_API_KEY"):
        return jsonify({"error": "TTS not configured (missing API key)"}), 500

    # Enqueue async TTS generation task
    from backend.tasks.tts import generate_tts_audio_for_profile

    profile.tts_task_status = 'pending'
    profile.tts_task_progress = 0
    db.session.commit()

    task = generate_tts_audio_for_profile.delay(profile.id, str(AUDIO_STORAGE_ROOT))

    profile.tts_task_id = task.id
    db.session.commit()

    current_app.logger.info(f"Enqueued TTS generation task {task.id} for profile {profile.id}")

    return jsonify({
        "message": "TTS generation started",
        "task_id": task.id
    }), 202


@profile_bp.route("/<int:profile_id>/tts-status", methods=["GET"])
@login_required
def get_tts_status(profile_id):
    """Get the current TTS generation status for a profile."""
    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    if profile.tts_task_id:
        # Check task state in Celery
        from backend.celery_app import celery
        task = celery.AsyncResult(profile.tts_task_id)

        if task.state == 'SUCCESS':
            # Ensure our DB record reflects completion.
            if profile.tts_task_status != 'completed':
                profile.tts_task_status = 'completed'
                db.session.commit()
        elif task.state in ['FAILURE', 'REVOKED']:
             if profile.tts_task_status != 'failed':
                profile.tts_task_status = 'failed'
                db.session.commit()


    response_data = {
        "status": profile.tts_task_status,
        "progress": profile.tts_task_progress or 0,
        "task_id": profile.tts_task_id,
        "profile": {
            "id": profile.id,
        }
    }

    if profile.tts_task_status == 'completed':
        response_data['profile']['audio_tts_url'] = profile.audio_tts_url

    return jsonify(response_data)