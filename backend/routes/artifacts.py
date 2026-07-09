"""User artifact routes (issue #158).

Artifacts are generic named, versioned documents (memory, scratchpad,
custom kinds). Same append-only versioning contract as AI preferences:
PUT inserts a new row; the latest row per (user, kind) is current.
"""
import re

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from backend.extensions import db
from backend.models import UserArtifact
from backend.utils.timefmt import iso_utc

artifacts_bp = Blueprint("artifacts", __name__)

_KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,47}$')


def _render_desc(desc):
    """Substitute {name} with the current user's username (e.g. the default
    memory description "Durable facts about {name}, ...")."""
    if desc and "{name}" in desc:
        return desc.replace("{name}", current_user.username)
    return desc


def _serialize(artifact, version_number=None, include_content=True):
    data = {
        "id": artifact.id,
        "kind": artifact.kind,
        "title": artifact.title,
        # Fall back to the built-in default for a kind whose row has no
        # description (e.g. an AI write via update_artifact, which didn't set
        # one) so default kinds always present their description — and the
        # edit form prefills it instead of coming up blank.
        "description": _render_desc(
            artifact.description
            or UserArtifact.DEFAULT_DESCRIPTIONS.get(artifact.kind)),
        "generated_by": artifact.generated_by,
        "created_at": iso_utc(artifact.created_at),
        "privacy_level": artifact.privacy_level,
        "ai_usage": artifact.ai_usage,
    }
    if include_content:
        data["content"] = artifact.get_content()
    if version_number is not None:
        data["version_number"] = version_number
    return data


@artifacts_bp.route("/", methods=["GET"])
@login_required
def list_artifacts():
    """Latest version of each artifact kind, defaults included even when
    they don't exist yet (so the UI can always render memory/scratchpad)."""
    latest = UserArtifact.latest_per_kind(current_user.id)
    items = []
    for kind, title in UserArtifact.DEFAULT_KINDS.items():
        if kind in latest:
            continue
        items.append({
            "id": None, "kind": kind, "title": title, "content": "",
            "description": _render_desc(
                UserArtifact.DEFAULT_DESCRIPTIONS.get(kind)),
            "generated_by": None, "created_at": None,
            "privacy_level": "private", "ai_usage": "chat",
        })
    for kind in sorted(latest):
        items.append(_serialize(latest[kind]))
    return jsonify({"artifacts": items}), 200


@artifacts_bp.route("/<kind>", methods=["GET"])
@login_required
def get_artifact(kind):
    artifact = UserArtifact.latest_for(current_user.id, kind)
    if artifact is None:
        if kind in UserArtifact.DEFAULT_KINDS:
            return jsonify({"artifact": {
                "id": None, "kind": kind,
                "title": UserArtifact.DEFAULT_KINDS[kind],
                "description": _render_desc(
                    UserArtifact.DEFAULT_DESCRIPTIONS.get(kind)),
                "content": "", "generated_by": None, "created_at": None,
                "privacy_level": "private", "ai_usage": "chat",
            }}), 200
        return jsonify({"error": "Artifact not found"}), 404

    version_count = UserArtifact.query.filter_by(
        user_id=current_user.id, kind=kind).count()
    return jsonify(
        {"artifact": _serialize(artifact, version_number=version_count)}
    ), 200


@artifacts_bp.route("/<kind>", methods=["PUT"])
@login_required
def update_artifact(kind):
    """Create a new version of an artifact (creates the kind if new)."""
    if not _KIND_RE.match(kind):
        return jsonify({"error": (
            "Invalid kind: use a short lowercase slug "
            "(letters, digits, dashes)."
        )}), 400

    data = request.get_json() or {}
    content = data.get("content")
    if content is None:
        return jsonify({"error": "Content is required"}), 400

    previous = UserArtifact.latest_for(current_user.id, kind)
    title = (data.get("title") or "").strip()
    if not title:
        title = (previous.title if previous
                 else UserArtifact.DEFAULT_KINDS.get(
                     kind, kind.replace("-", " ").title()))

    # Description: explicit value wins; otherwise carry forward the previous
    # version's, falling back to the built-in default for this kind.
    if "description" in data:
        description = (data.get("description") or "").strip() or None
    elif previous is not None:
        description = previous.description
    else:
        description = UserArtifact.DEFAULT_DESCRIPTIONS.get(kind)

    artifact = UserArtifact(
        user_id=current_user.id,
        kind=kind,
        title=title[:128],
        description=(description[:255] if description else None),
        generated_by=data.get("generated_by", "user"),
        tokens_used=0,
    )
    artifact.set_content(content)
    db.session.add(artifact)
    db.session.commit()

    version_count = UserArtifact.query.filter_by(
        user_id=current_user.id, kind=kind).count()
    return jsonify(
        {"artifact": _serialize(artifact, version_number=version_count)}
    ), 200


@artifacts_bp.route("/<kind>/versions", methods=["GET"])
@login_required
def get_artifact_versions(kind):
    rows = UserArtifact.query.filter_by(
        user_id=current_user.id, kind=kind
    ).order_by(UserArtifact.created_at.desc(), UserArtifact.id.desc()).all()

    total = len(rows)
    versions = [
        _serialize(row, version_number=total - i, include_content=False)
        for i, row in enumerate(rows)
    ]
    return jsonify({"versions": versions}), 200


@artifacts_bp.route("/versions/<int:version_id>", methods=["GET"])
@login_required
def get_artifact_version(version_id):
    artifact = UserArtifact.query.get_or_404(version_id)
    if artifact.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify({"artifact": _serialize(artifact)}), 200


@artifacts_bp.route("/<kind>/revert/<int:version_id>", methods=["POST"])
@login_required
def revert_artifact(kind, version_id):
    """Create a new artifact version from a historical one."""
    old = UserArtifact.query.get_or_404(version_id)
    if old.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    if old.kind != kind:
        return jsonify(
            {"error": "Version does not belong to this artifact"}), 400

    latest = UserArtifact.latest_for(current_user.id, kind)
    if latest is not None and latest.id == old.id:
        return jsonify({"error": "Already the current version"}), 400

    new = UserArtifact(
        user_id=current_user.id,
        kind=kind,
        title=old.title,
        description=old.description,
        generated_by="revert",
        tokens_used=0,
        privacy_level=old.privacy_level,
        # A revert reproduces a prior version, so copy its ai_usage (#191).
        ai_usage=old.ai_usage,
    )
    # Copy the encrypted content directly (no decrypt/re-encrypt round).
    new.content = old.content
    db.session.add(new)
    db.session.commit()

    version_count = UserArtifact.query.filter_by(
        user_id=current_user.id, kind=kind).count()
    return jsonify(
        {"artifact": _serialize(new, version_number=version_count)}), 200
