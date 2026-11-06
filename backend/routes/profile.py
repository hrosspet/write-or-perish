from flask import jsonify, current_app
from flask_login import login_required, current_user
from backend.utils.error_privacy import describe_exception
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
from backend.utils.profile_versions import (
    visible_profiles_query,
    same_profile_text,
    coverage_of,
    current_profile_query,
)

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
    in-pipeline revert (tasks/exports.retip_profile_chain): a new row typed
    'revert' that carries the source version's attribution and ai_usage
    (#191). It is the newest version, so the next update builds on it, and
    a run in progress stops before it saves over it
    (tasks/exports.base_moved, #183)."""
    old = UserProfile.query.get_or_404(version_id)
    if old.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    latest = current_profile_query(current_user.id).first()
    if latest is not None and latest.id == old.id:
        return jsonify({"error": "Already the current version"}), 400

    new_profile = UserProfile(
        user_id=current_user.id,
        generated_by=old.generated_by,
        tokens_used=0,
        privacy_level=old.privacy_level,
        ai_usage=old.ai_usage,
        # The same coverage an edit of it would carry, render time
        # included: copying only the cutoff left an integration's empty
        # render time, so a pinned (pre-filled) account's continue rule
        # measured from the revert and seeded an extra update.
        **coverage_of(current_user, old),
        generation_type="revert",
        parent_profile_id=old.id,
    )
    # Copy the encrypted content directly (no decrypt/re-encrypt round).
    new_profile.content = old.content
    db.session.add(new_profile)
    _user_saved_a_version(new_profile)
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


@profile_bp.route("/<int:profile_id>/tts-chapters", methods=["GET"])
@login_required
def get_tts_chapters(profile_id):
    """Chapter list for a profile's TTS, built from its TTSChunk rows
    exactly as for nodes and references (#145). Chapter titles are
    profile text, so the access check is the audio route's: the owner
    only."""
    profile = UserProfile.query.get_or_404(profile_id)

    if profile.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403

    from backend.models import TTSChunk
    from backend.utils.audio_processing import tts_chapters
    chunks = TTSChunk.query.filter_by(profile_id=profile_id).order_by(
        TTSChunk.chunk_index).all()
    return jsonify({"chapters": tts_chapters(chunks)}), 200


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
    """Create a new profile version the user wrote (generated_by "user").

    When the user already has a profile, the new version follows the
    newest one, like an edit of it (``update_profile``): that version is
    its parent, and the new one carries its source coverage
    (``coverage_of``), so the next update builds on the user's text and
    reads only writing after that version's cutoff. The iPhone app sends a
    POST when a new version arrived while the user was editing ("Save as
    new"). A first profile, written when the user has none, has neither a
    parent nor coverage; it follows the provisional ladder like any first
    profile (tasks/exports.profile_is_provisional)."""
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

    # The profile in use, never a large build's window or group (#545).
    newest = current_profile_query(current_user.id).first()
    profile = UserProfile(
        user_id=current_user.id,
        generated_by="user",
        tokens_used=0,
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        parent_profile_id=newest.id if newest else None,
        **(coverage_of(current_user, newest) if newest else {}),
    )
    profile.set_content(content)

    user_id = current_user.id  # read before a rollback expires it
    try:
        db.session.add(profile)
        db.session.flush()
        if newest is not None and profile.ai_usage in AI_ALLOWED:
            _carry_recent_context(newest, profile)
        _user_saved_a_version(profile)
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
        # The error's text can quote the profile being saved: the response
        # and the log (and so Sentry) get the type only.
        current_app.logger.exception(
            "Profile create failed for user %s: %s", user_id, describe_exception(e))
        return jsonify({"error": "Failed to create profile"}), 500


def _edit_ai_usage(profile):
    """The ai_usage of a version saved from an edit of ``profile``: the
    account setting when the text is written (#191, #346), except that an
    edit of a version marked 'none' stays 'none'. Most of its text was
    written under 'none', and nothing marked None reaches a model."""
    if profile.ai_usage not in AI_ALLOWED:
        return profile.ai_usage
    return current_user.default_ai_usage


def _save_edit_as_new_version(profile, new_content, data, keep_audio=False):
    """A profile edit saved as a new version the user wrote
    (generated_by "user", dated by the edit). The edited version keeps its
    text, ai_usage and audio and stays in the history.

    The new version is the edited one's successor: it links to it as its
    parent, and it covers the same source data, so it carries that
    version's coverage (``coverage_of``; for an integration, the
    continue-rule boundary is that of the chain tip it merged). It is the
    newest version, so the next update builds on it as its previous
    profile (tasks/exports.profile_update_base), and a run in progress
    stops before it saves over it (tasks/exports.base_moved). Voice review,
    2026-10-20 (#183). A version marked 'none' is skipped as a base, as
    before (#346).

    keep_audio: the user chose to keep the existing audio for the edited
    text (the frontend's "keep or regenerate" prompt, #66), so the new
    version reuses the edited version's audio files."""
    new_profile = UserProfile(
        user_id=current_user.id,
        generated_by="user",
        tokens_used=0,
        privacy_level=data.get("privacy_level", profile.privacy_level),
        ai_usage=_edit_ai_usage(profile),
        parent_profile_id=profile.id,
        **coverage_of(current_user, profile),
    )
    new_profile.set_content(new_content)
    edited_id = profile.id  # read before a rollback expires it
    try:
        db.session.add(new_profile)
        db.session.flush()
        if keep_audio:
            _copy_audio(profile, new_profile)
        if new_profile.ai_usage in AI_ALLOWED:
            _carry_recent_context(profile, new_profile)
        _user_saved_a_version(new_profile)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(
            "Profile edit (as a new version of %s) failed: %s",
            edited_id, describe_exception(e))
        return jsonify({"error": "Failed to update profile"}), 500
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


def _carry_recent_context(source, target):
    """A version the user saves on top of ``source`` keeps ``source``'s
    recent-context summary (#183). Chat replies look the summary up by the
    newest profile's id, so without a copy they had none until the summary
    job wrote a new one (10 to 20 minutes), and that job spent a model call
    on writing the summary already covered. The copy covers the same
    writing: same cutoff and save time, the text copied as stored (not
    decrypted), no model call."""
    from backend.models import UserRecentContext
    summary = (UserRecentContext.query
               .filter_by(user_id=source.user_id, profile_id=source.id)
               .order_by(UserRecentContext.created_at.desc()).first())
    if summary is None:
        return
    db.session.add(UserRecentContext(
        user_id=summary.user_id, content=summary.content,
        generated_by=summary.generated_by, tokens_used=0,
        created_at=summary.created_at,
        source_data_cutoff=summary.source_data_cutoff,
        source_tokens_covered=summary.source_tokens_covered,
        profile_id=target.id, ai_usage=summary.ai_usage))


def _user_saved_a_version(version):
    """A version the user saves (an edit, a new profile, a revert) ends a
    rebuild's pending merge of an older version of theirs (#183): the newest
    version the user saved wins, and the next run builds on it. A version
    marked 'none' is never a base, so it ends nothing: the pending merge
    still runs once the account allows AI."""
    if version.ai_usage in AI_ALLOWED:
        current_user.profile_pending_merge_id = None


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

    # Every edit that changes the words is saved as a new version the user
    # wrote, dated by the edit, with the edited version as its predecessor
    # (voice review, 2026-10-20, #183). The edited version keeps its text,
    # its ai_usage and its audio. A change of whitespace only (blank lines,
    # trailing spaces, a trailing newline) is not an edit of the words: it
    # is saved in place and makes no new version, except while the account
    # is set to 'none', when no text change lands in an AI-readable row
    # (#346).
    old_content = profile.get_content()
    text_changed = new_content != old_content
    words_changed = (text_changed
                     and not same_profile_text(old_content, new_content))
    none_edit = (text_changed and profile.ai_usage in AI_ALLOWED
                 and not account_allows_ai(current_user))
    if words_changed or none_edit:
        # A 'none' version gets no speech, so it never takes audio.
        return _save_edit_as_new_version(
            profile, new_content, data,
            keep_audio=(_edit_ai_usage(profile) in AI_ALLOWED
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
        current_app.logger.exception("Profile %s update failed: %s",
                                     profile_id, describe_exception(e))
        return jsonify({"error": "Failed to update profile"}), 500
