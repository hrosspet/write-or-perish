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


def _serialize(artifact, version_number=None, include_content=True):
    data = {
        "id": artifact.id,
        "kind": artifact.kind,
        "title": artifact.title,
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

    artifact = UserArtifact(
        user_id=current_user.id,
        kind=kind,
        title=title[:128],
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
