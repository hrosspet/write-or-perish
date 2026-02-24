from flask import jsonify, current_app
from flask_login import login_required, current_user
from backend.models import UserProfile
from backend.extensions import db
from pathlib import Path

from flask import Blueprint

# Privacy utilities
from backend.utils.privacy import (
    validate_privacy_level,
    validate_ai_usage,
    PrivacyLevel,
    AIUsage
)
from backend.utils.api_keys import get_openai_chat_key

profile_bp = Blueprint("profile", __name__)

AUDIO_STORAGE_ROOT = "data/audio"


@profile_bp.route("/versions", methods=["GET"])
@login_required
def get_profile_versions():
    """List all profile versions for the current user."""
    profiles = UserProfile.query.filter_by(
        user_id=current_user.id
    ).order_by(UserProfile.created_at.desc()).all()

    versions = []
    total = len(profiles)
    for i, profile in enumerate(profiles):
        versions.append({
            "id": profile.id,
            "generated_by": profile.generated_by,
            "tokens_used": profile.tokens_used,
            "created_at": profile.created_at.isoformat(),
            "version_number": total - i,
            "source_tokens_used": profile.source_tokens_used,
            "source_data_cutoff": (
                profile.source_data_cutoff.isoformat()
                if profile.source_data_cutoff else None
            ),
            "generation_type": profile.generation_type,
        })

    return jsonify({"versions": versions}), 200


@profile_bp.route("/versions/<int:version_id>", methods=["GET"])
@login_required
def get_profile_version(version_id):
    """Get a specific profile version's content."""
    profile = UserProfile.query.get_or_404(version_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    return jsonify({
        "profile": {
            "id": profile.id,
            "content": profile.get_content(),
            "generated_by": profile.generated_by,
            "tokens_used": profile.tokens_used,
            "created_at": profile.created_at.isoformat(),
        }
    }), 200


@profile_bp.route("/<int:profile_id>/audio", methods=["GET"])
@login_required
def get_audio(profile_id):
    """Return JSON with URL for TTS audio associated with a profile."""
    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    # If audio exists, return it
    if profile.audio_tts_url:
        return jsonify({
            "tts_url": profile.audio_tts_url,
        })

    # Check if TTS generation is in progress
    if profile.tts_task_status in ['pending', 'processing']:
        return jsonify({
            "status": "generating",
            "message": "TTS generation in progress",
            "progress": profile.tts_task_progress or 0,
            "task_id": profile.tts_task_id
        }), 202  # 202 Accepted - request accepted but not yet completed

    # No audio and no generation in progress
    return jsonify({"error": "No audio found for this profile"}), 404


@profile_bp.route("/<int:profile_id>/tts", methods=["POST"])
@login_required
def generate_tts(profile_id):
    """Trigger TTS generation for the user profile."""
    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    if profile.audio_tts_url:
        return jsonify({"message": "TTS already available", "tts_url": profile.audio_tts_url}), 200

    if not get_openai_chat_key(current_app.config):
        return jsonify({"error": "TTS not configured (missing API key)"}), 500

    # Enqueue async TTS generation task
    from backend.tasks.tts import generate_tts_audio_for_profile

    profile.tts_task_status = 'pending'
    profile.tts_task_progress = 0
    db.session.commit()

    task = generate_tts_audio_for_profile.delay(profile.id, str(AUDIO_STORAGE_ROOT), requesting_user_id=current_user.id)

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


@profile_bp.route("/<int:profile_id>", methods=["PUT"])
@login_required
def update_profile(profile_id):
    """Update the content of a user profile."""
    from flask import request

    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.get_json()
    new_content = data.get("content")

    if new_content is None:
        return jsonify({"error": "Content is required"}), 400

    if not new_content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400

    profile.set_content(new_content)

    # Handle privacy settings updates (optional)
    if "privacy_level" in data:
        privacy_level = data["privacy_level"]
        if not validate_privacy_level(privacy_level):
            return jsonify({"error": f"Invalid privacy_level: {privacy_level}"}), 400
        profile.privacy_level = privacy_level

    if "ai_usage" in data:
        ai_usage = data["ai_usage"]
        if not validate_ai_usage(ai_usage):
            return jsonify({"error": f"Invalid ai_usage: {ai_usage}"}), 400
        profile.ai_usage = ai_usage

    try:
        db.session.commit()
        return jsonify({
            "message": "Profile updated successfully",
            "profile": {
                "id": profile.id,
                "content": profile.get_content(),
                "generated_by": profile.generated_by,
                "tokens_used": profile.tokens_used,
                "created_at": profile.created_at.isoformat(),
                "privacy_level": profile.privacy_level,
                "ai_usage": profile.ai_usage
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update profile", "details": str(e)}), 500