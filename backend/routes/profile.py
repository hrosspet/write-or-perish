from flask import jsonify, current_app
from flask_login import login_required, current_user
from backend.models import UserProfile
from backend.extensions import db
from backend.utils.timefmt import iso_utc
from pathlib import Path

from flask import Blueprint

# Privacy utilities
from backend.utils.privacy import (
    validate_privacy_level,
    PrivacyLevel,
    AI_ALLOWED,
    account_allows_ai,
    speech_allowed,
    SPEECH_REFUSED_MESSAGE,
)
from backend.utils.api_keys import get_openai_chat_key
from backend.utils.spend import require_spend_headroom
from backend.utils.profile_versions import visible_profiles_query

profile_bp = Blueprint("profile", __name__)

AUDIO_STORAGE_ROOT = "data/audio"


@profile_bp.route("/versions", methods=["GET"])
@login_required
def get_profile_versions():
    """List the user's profile versions (pipeline intermediates hidden)."""
    profiles = visible_profiles_query(current_user.id).all()

    versions = []
    total = len(profiles)
    for i, profile in enumerate(profiles):
        versions.append({
            "id": profile.id,
            "generated_by": profile.generated_by,
            "tokens_used": profile.tokens_used,
            "created_at": iso_utc(profile.created_at),
            "version_number": total - i,
            "source_tokens_used": profile.source_tokens_used,
            "source_origin_stats": profile.source_origin_stats,
            "source_data_cutoff": (
                iso_utc(profile.source_data_cutoff)
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
            "created_at": iso_utc(profile.created_at),
        }
    }), 200


@profile_bp.route("/revert/<int:version_id>", methods=["POST"])
@login_required
def revert_profile(version_id):
    """Create a new profile version from a historical one. Mirrors the
    in-pipeline revert (tasks/exports.py): a new row typed 'revert' that
    carries the source version's attribution and ai_usage (#191)."""
    old = UserProfile.query.get_or_404(version_id)
    if old.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    latest = UserProfile.query.filter_by(
        user_id=current_user.id
    ).order_by(UserProfile.created_at.desc()).first()
    if latest is not None and latest.id == old.id:
        return jsonify({"error": "Already the current version"}), 400

    new_profile = UserProfile(
        user_id=current_user.id,
        generated_by=old.generated_by,
        tokens_used=0,
        privacy_level=old.privacy_level,
        ai_usage=old.ai_usage,
        source_tokens_used=old.source_tokens_used,
        source_data_cutoff=old.source_data_cutoff,
        generation_type="revert",
        parent_profile_id=old.id,
    )
    # Copy the encrypted content directly (no decrypt/re-encrypt round).
    new_profile.content = old.content
    db.session.add(new_profile)
    db.session.commit()

    return jsonify({
        "profile": {
            "id": new_profile.id,
            "content": new_profile.get_content(),
            "generated_by": new_profile.generated_by,
            "tokens_used": new_profile.tokens_used,
            "created_at": iso_utc(new_profile.created_at),
            "privacy_level": new_profile.privacy_level,
            "ai_usage": new_profile.ai_usage,
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
@require_spend_headroom
def generate_tts(profile_id):
    """Trigger TTS generation for the user profile."""
    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    if profile.audio_tts_url:
        return jsonify({"message": "TTS already available", "tts_url": profile.audio_tts_url}), 200

    # A profile version saved while the account's AI usage was 'none' is
    # not sent to the speech model.
    if not speech_allowed(profile):
        return jsonify({"error": SPEECH_REFUSED_MESSAGE}), 403

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


@profile_bp.route("/", methods=["POST"])
@login_required
def create_profile():
    """Create a new user-generated profile."""
    from flask import request

    data = request.get_json()
    content = data.get("content")

    if not content:
        return jsonify({"error": "Content is required"}), 400

    if not content.strip():
        return jsonify({"error": "Content cannot be empty"}), 400

    privacy_level = data.get("privacy_level", PrivacyLevel.PRIVATE)
    # A profile's ai_usage comes only from the account setting at creation
    # (#191, #346); the request cannot set it.
    ai_usage = current_user.default_ai_usage

    if not validate_privacy_level(privacy_level):
        return jsonify({"error": f"Invalid privacy_level: {privacy_level}"}), 400

    profile = UserProfile(
        user_id=current_user.id,
        generated_by="user",
        tokens_used=0,
        privacy_level=privacy_level,
        ai_usage=ai_usage
    )
    profile.set_content(content)

    try:
        db.session.add(profile)
        db.session.commit()

        return jsonify({
            "message": "Profile created successfully",
            "profile": {
                "id": profile.id,
                "content": profile.get_content(),
                "generated_by": profile.generated_by,
                "tokens_used": profile.tokens_used,
                "created_at": iso_utc(profile.created_at),
                "privacy_level": profile.privacy_level,
                "ai_usage": profile.ai_usage
            }
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to create profile", "details": str(e)}), 500


def _save_edit_as_new_version(profile, new_content, data, keep_audio=False):
    """A profile edit saved as a new version the user wrote
    (generated_by "user"), with the account's ai_usage. The edited version
    keeps its text, ai_usage and audio. The new version covers the same
    source data as the edited one, so it carries that version's source
    figures, and it links to it as its parent.

    Two cases use it:
    - an edit made while the account is set to 'none' (#346): the version
      is marked 'none', and the update pipeline skips it as a base
      (tasks/exports.profile_update_base);
    - an edit of a generated version (#183): the pipeline treats it as the
      user's own text, so incremental updates build on it and full
      rebuilds keep it (tasks/exports.place_user_written_profile).

    keep_audio: the user chose to keep the existing audio for the edited
    text (the frontend's "keep or regenerate" prompt, #66), so the new
    version reuses the edited version's audio files."""
    new_profile = UserProfile(
        user_id=current_user.id,
        generated_by="user",
        tokens_used=0,
        privacy_level=data.get("privacy_level", profile.privacy_level),
        ai_usage=current_user.default_ai_usage,
        source_tokens_used=profile.source_tokens_used,
        source_data_cutoff=profile.source_data_cutoff,
        source_origin_stats=profile.source_origin_stats,
        source_rendered_at=profile.source_rendered_at,
        parent_profile_id=profile.id,
    )
    new_profile.set_content(new_content)
    try:
        db.session.add(new_profile)
        db.session.flush()
        if keep_audio:
            _copy_audio(profile, new_profile)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update profile", "details": str(e)}), 500
    return jsonify({
        "message": "Profile updated successfully",
        "profile": {
            "id": new_profile.id,
            "content": new_profile.get_content(),
            "generated_by": new_profile.generated_by,
            "tokens_used": new_profile.tokens_used,
            "created_at": iso_utc(new_profile.created_at),
            "privacy_level": new_profile.privacy_level,
            "ai_usage": new_profile.ai_usage
        }
    }), 200


def _copy_audio(source, target):
    """Point ``target`` at ``source``'s finished speech: the scalar URL and
    the per-chunk rows the streaming player reads. The files are shared;
    they belong to the same user, and clearing audio never deletes files
    (utils/audio_storage.clear_tts_artifacts)."""
    from backend.models import TTSChunk
    if not source.audio_tts_url or source.tts_task_status != "completed":
        return
    target.audio_tts_url = source.audio_tts_url
    target.tts_task_status = "completed"
    target.tts_task_progress = 100
    for chunk in TTSChunk.query.filter_by(profile_id=source.id).order_by(
            TTSChunk.chunk_index).all():
        db.session.add(TTSChunk(
            profile_id=target.id, chunk_index=chunk.chunk_index,
            section_index=chunk.section_index,
            section_title=chunk.section_title, audio_url=chunk.audio_url,
            duration=chunk.duration, status=chunk.status,
            completed_at=chunk.completed_at))


def _edit_needs_new_version(profile):
    """Whether a text edit of this AI-readable version is saved as a new
    version (``_save_edit_as_new_version``) rather than in place:

    - while the account is set to 'none', so the text does not land in an
      AI-readable row (#346);
    - when the version is generated: the edit is the user's own text, so
      the profile jobs keep it instead of overwriting it, and the generated
      version stays in the history (#183; voice review, 2026-10-02: a job
      that regenerates something the user edited keeps the edits and
      refreshes only its own part).

    A version the user wrote, or one already marked 'none', is edited in
    place."""
    if profile.ai_usage not in AI_ALLOWED:
        return False
    return (not account_allows_ai(current_user)
            or profile.generated_by != "user")


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

    if "privacy_level" in data and not validate_privacy_level(data["privacy_level"]):
        return jsonify({"error": f"Invalid privacy_level: {data['privacy_level']}"}), 400

    # An edit made while the account is set to 'none' (#346), or an edit of
    # a generated version (#183), is saved as a new version the user wrote;
    # the edited version keeps its text, its ai_usage and its audio.
    text_changed = new_content != profile.get_content()
    if text_changed and _edit_needs_new_version(profile):
        # A 'none' version gets no speech, so it never takes audio.
        return _save_edit_as_new_version(
            profile, new_content, data,
            keep_audio=(account_allows_ai(current_user)
                        and not data.get("regenerate_tts")))

    # Editing the text makes generated TTS audio stale. The frontend asks
    # the user whether to keep or regenerate and only sends
    # regenerate_tts=true when they choose to regenerate; we then clear the
    # audio so fresh TTS is generated on the next request (#66).
    if data.get("regenerate_tts") and text_changed:
        from backend.utils.audio_storage import clear_tts_artifacts
        clear_tts_artifacts(profile)

    profile.set_content(new_content)

    # Handle privacy settings updates (optional)
    if "privacy_level" in data:
        profile.privacy_level = data["privacy_level"]

    # ai_usage is not editable: it comes from the account setting when the
    # text is written (#346).

    try:
        db.session.commit()
        return jsonify({
            "message": "Profile updated successfully",
            "profile": {
                "id": profile.id,
                "content": profile.get_content(),
                "generated_by": profile.generated_by,
                "tokens_used": profile.tokens_used,
                "created_at": iso_utc(profile.created_at),
                "privacy_level": profile.privacy_level,
                "ai_usage": profile.ai_usage
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update profile", "details": str(e)}), 500