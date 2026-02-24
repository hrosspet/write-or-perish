from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from backend.models import UserPrompt
from backend.extensions import db
from backend.utils.prompts import (
    PROMPT_DEFAULTS, load_default_prompt, default_prompt_hash,
)

prompts_bp = Blueprint("prompts", __name__)


def _edit_count(prompt_key):
    """Number of user edits (DB rows) for this prompt."""
    return UserPrompt.query.filter_by(
        user_id=current_user.id, prompt_key=prompt_key
    ).count()


def _is_default_updated(prompt):
    """Check whether the file default has changed since this row was created.

    Returns True when the user should be notified (customised or reverted rows
    whose underlying default has moved on).  Returns False for ``generated_by
    == "default"`` rows because those are auto-upgraded transparently.
    """
    if prompt.generated_by == "default":
        return False
    current = default_prompt_hash(prompt.prompt_key)
    if current is None:
        return False
    return prompt.based_on_default_hash != current


def _serialize_prompt(prompt, version_number):
    return {
        "id": prompt.id,
        "prompt_key": prompt.prompt_key,
        "title": prompt.title,
        "content": prompt.get_content(),
        "generated_by": prompt.generated_by,
        "created_at": prompt.created_at.isoformat(),
        "version_number": version_number,
        "default_updated": _is_default_updated(prompt),
    }


@prompts_bp.route("/", methods=["GET"])
@login_required
def list_prompts():
    """List all prompt keys with their latest version for the current user."""
    prompts = []
    for key, meta in PROMPT_DEFAULTS.items():
        latest = UserPrompt.query.filter_by(
            user_id=current_user.id, prompt_key=key
        ).order_by(UserPrompt.created_at.desc()).first()

        if latest:
            content = latest.get_content()
            prompts.append({
                "prompt_key": key,
                "title": meta['title'],
                "preview": content[:150] if content else "",
                "version_number": _edit_count(key),
                "generated_by": latest.generated_by,
                "created_at": latest.created_at.isoformat(),
                "default_updated": _is_default_updated(latest),
            })
        else:
            content = load_default_prompt(key)
            prompts.append({
                "prompt_key": key,
                "title": meta['title'],
                "preview": content[:150] if content else "",
                "version_number": 0,
                "generated_by": "default",
                "created_at": None,
                "default_updated": False,
            })

    return jsonify({"prompts": prompts}), 200


@prompts_bp.route("/<prompt_key>", methods=["GET"])
@login_required
def get_prompt(prompt_key):
    """Get the active version of a specific prompt."""
    if prompt_key not in PROMPT_DEFAULTS:
        return jsonify({"error": "Unknown prompt key"}), 404

    meta = PROMPT_DEFAULTS[prompt_key]
    latest = UserPrompt.query.filter_by(
        user_id=current_user.id, prompt_key=prompt_key
    ).order_by(UserPrompt.created_at.desc()).first()

    if latest:
        return jsonify({
            "prompt": _serialize_prompt(latest, _edit_count(prompt_key))
        }), 200
    else:
        content = load_default_prompt(prompt_key)
        return jsonify({
            "prompt": {
                "id": None,
                "prompt_key": prompt_key,
                "title": meta['title'],
                "content": content,
                "generated_by": "default",
                "created_at": None,
                "version_number": 0,
                "default_updated": False,
            }
        }), 200


@prompts_bp.route("/<prompt_key>", methods=["PUT"])
@login_required
def update_prompt(prompt_key):
    """Save a new version of a prompt."""
    if prompt_key not in PROMPT_DEFAULTS:
        return jsonify({"error": "Unknown prompt key"}), 404

    data = request.get_json() or {}
    content = data.get("content")

    if content is None or not content.strip():
        return jsonify({"error": "Content is required"}), 400

    meta = PROMPT_DEFAULTS[prompt_key]
    prompt = UserPrompt(
        user_id=current_user.id,
        prompt_key=prompt_key,
        title=meta['title'],
        generated_by="user",
        based_on_default_hash=default_prompt_hash(prompt_key),
    )
    prompt.set_content(content)
    db.session.add(prompt)
    db.session.commit()

    return jsonify({
        "prompt": _serialize_prompt(prompt, _edit_count(prompt_key))
    }), 200


@prompts_bp.route("/<prompt_key>/versions", methods=["GET"])
@login_required
def get_prompt_versions(prompt_key):
    """List all versions for a specific prompt, including the file default."""
    if prompt_key not in PROMPT_DEFAULTS:
        return jsonify({"error": "Unknown prompt key"}), 404

    all_versions = UserPrompt.query.filter_by(
        user_id=current_user.id, prompt_key=prompt_key
    ).order_by(UserPrompt.created_at.desc()).all()

    total = len(all_versions)
    versions = []
    for i, p in enumerate(all_versions):
        versions.append({
            "id": p.id,
            "generated_by": p.generated_by,
            "created_at": p.created_at.isoformat(),
            "version_number": total - i,
        })

    # Append the file default as v0
    versions.append({
        "id": "default",
        "generated_by": "default",
        "created_at": None,
        "version_number": 0,
    })

    return jsonify({"versions": versions}), 200


@prompts_bp.route("/<prompt_key>/default", methods=["GET"])
@login_required
def get_default_prompt(prompt_key):
    """Get the original file default content for a prompt."""
    if prompt_key not in PROMPT_DEFAULTS:
        return jsonify({"error": "Unknown prompt key"}), 404

    content = load_default_prompt(prompt_key)
    return jsonify({
        "prompt": {
            "id": "default",
            "content": content,
            "generated_by": "default",
            "created_at": None,
        }
    }), 200


@prompts_bp.route("/<prompt_key>/versions/<int:version_id>", methods=["GET"])
@login_required
def get_prompt_version(prompt_key, version_id):
    """Get content of a specific version."""
    prompt = UserPrompt.query.get_or_404(version_id)

    if prompt.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    if prompt.prompt_key != prompt_key:
        return jsonify({"error": "Version does not belong to this prompt"}), 400

    return jsonify({
        "prompt": {
            "id": prompt.id,
            "content": prompt.get_content(),
            "generated_by": prompt.generated_by,
            "created_at": prompt.created_at.isoformat(),
        }
    }), 200


@prompts_bp.route("/<prompt_key>/revert/<int:version_id>", methods=["POST"])
@login_required
def revert_prompt(prompt_key, version_id):
    """Create a new version from a historical one."""
    if prompt_key not in PROMPT_DEFAULTS:
        return jsonify({"error": "Unknown prompt key"}), 404

    old_prompt = UserPrompt.query.get_or_404(version_id)

    if old_prompt.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    if old_prompt.prompt_key != prompt_key:
        return jsonify({"error": "Version does not belong to this prompt"}), 400

    meta = PROMPT_DEFAULTS[prompt_key]
    new_prompt = UserPrompt(
        user_id=current_user.id,
        prompt_key=prompt_key,
        title=meta['title'],
        generated_by="revert",
        based_on_default_hash=default_prompt_hash(prompt_key),
    )
    new_prompt.content = old_prompt.content
    db.session.add(new_prompt)
    db.session.commit()

    return jsonify({
        "prompt": _serialize_prompt(new_prompt, _edit_count(prompt_key))
    }), 200


@prompts_bp.route("/<prompt_key>/revert-to-default", methods=["POST"])
@login_required
def revert_to_default(prompt_key):
    """Create a new version from the file default."""
    if prompt_key not in PROMPT_DEFAULTS:
        return jsonify({"error": "Unknown prompt key"}), 404

    content = load_default_prompt(prompt_key)
    if not content:
        return jsonify({"error": "Default prompt not found"}), 404

    meta = PROMPT_DEFAULTS[prompt_key]
    new_prompt = UserPrompt(
        user_id=current_user.id,
        prompt_key=prompt_key,
        title=meta['title'],
        generated_by="default",
        based_on_default_hash=default_prompt_hash(prompt_key),
    )
    new_prompt.set_content(content)
    db.session.add(new_prompt)
    db.session.commit()

    return jsonify({
        "prompt": _serialize_prompt(new_prompt, _edit_count(prompt_key))
    }), 200


@prompts_bp.route("/<prompt_key>/acknowledge-default", methods=["POST"])
@login_required
def acknowledge_default(prompt_key):
    """Dismiss the 'default updated' notification.

    Updates the stored hash on the latest DB row so the user won't be
    notified again until the next file-default change.
    """
    if prompt_key not in PROMPT_DEFAULTS:
        return jsonify({"error": "Unknown prompt key"}), 404

    latest = UserPrompt.query.filter_by(
        user_id=current_user.id, prompt_key=prompt_key
    ).order_by(UserPrompt.created_at.desc()).first()

    if not latest:
        return jsonify({"error": "No prompt to acknowledge"}), 404

    latest.based_on_default_hash = default_prompt_hash(prompt_key)
    db.session.commit()

    return jsonify({
        "prompt": _serialize_prompt(latest, _edit_count(prompt_key))
    }), 200
