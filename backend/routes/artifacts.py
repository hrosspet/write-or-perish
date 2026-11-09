"""User artifact routes (issue #158).

Artifacts are generic named, versioned documents (memory, scratchpad,
custom kinds). Same append-only versioning contract as AI preferences:
PUT inserts a new row; the latest row per (user, kind) is current. PATCH
(checklist clicks) may edit the latest row in place — see patch_artifact.
DELETE soft-deletes every version of a custom artifact (#603) — see
backend/utils/artifact_deletion.py. Every route reads live versions only.
"""
import re

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from backend.extensions import db
from backend.models import NodeContextArtifact, UserArtifact
from backend.utils.timefmt import iso_utc

# The answer for a deleted kind: the client says "This artifact was
# deleted." instead of offering an empty artifact under its name (#603).
_DELETED = {"error": "This artifact was deleted.", "deleted": True}

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
        # Whether the user may delete it (#603): the clients show Delete
        # from this, so the rule lives on the server only.
        "deletable": UserArtifact.is_deletable_kind(artifact.kind),
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
    they don't exist yet (so the UI can always render memory/scratchpad).

    ``deleted_kinds``: the user's kinds that were deleted and not made
    again (#603), so a link to one says it was deleted."""
    from backend.utils.artifact_deletion import deleted_kinds
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
            "privacy_level": "private",
            "ai_usage": current_user.default_ai_usage,
            "deletable": False,
        })
    for kind in sorted(latest):
        items.append(_serialize(latest[kind]))
    return jsonify({
        "artifacts": items,
        "deleted_kinds": deleted_kinds(current_user.id),
    }), 200


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
                "privacy_level": "private",
                "ai_usage": current_user.default_ai_usage,
                "deletable": False,
            }}), 200
        from backend.utils.artifact_deletion import deleted_kinds
        if kind in deleted_kinds(current_user.id):
            return jsonify(_DELETED), 404
        return jsonify({"error": "Artifact not found"}), 404

    version_count = UserArtifact.version_count(current_user.id, kind)
    return jsonify(
        {"artifact": _serialize(artifact, version_number=version_count)}
    ), 200


@artifacts_bp.route("/<kind>/viewed", methods=["POST"])
@login_required
def artifact_viewed(kind):
    """View ping from the artifacts page (once per kind per visit) — feeds
    the admin Activity tab. Fire-and-forget on the client; errors here
    must never surface to the user."""
    from backend.models import ArtifactView
    kind = (kind or "").strip()[:48]
    if not kind:
        return "", 204
    try:
        db.session.add(ArtifactView(user_id=current_user.id, kind=kind))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return "", 204


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
        # The user's global default, like the todo list and the profile
        # (#326): the row's ai_usage is what the training key reads.
        ai_usage=current_user.default_ai_usage,
    )
    artifact.set_content(content)
    db.session.add(artifact)
    db.session.commit()

    # A kind made again after a delete counts from v1 (#603).
    version_count = UserArtifact.version_count(current_user.id, kind)
    return jsonify(
        {"artifact": _serialize(artifact, version_number=version_count)}
    ), 200


@artifacts_bp.route("/<kind>", methods=["PATCH"])
@login_required
def patch_artifact(kind):
    """Save a checklist toggle or a "+" insert from the artifacts page.

    Like the todo list's PATCH, it edits the latest version in place, so
    ticking boxes doesn't add a version per click. The append-only
    contract still holds where it matters: when the latest version came
    from the AI (or a revert), or a session has pinned it (#191: a pin
    means "this exact version was in that session's context"), the
    change becomes a new user version instead, and later clicks edit
    that one.
    """
    data = request.get_json() or {}
    content = data.get("content")
    if content is None:
        return jsonify({"error": "Content is required"}), 400

    latest = UserArtifact.latest_for(current_user.id, kind)
    if latest is None:
        # A checklist click on a page left open after the delete in
        # another tab saves nothing (#603).
        return jsonify({"error": "Artifact not found"}), 404

    pinned = NodeContextArtifact.query.filter_by(
        artifact_type="user_artifact", artifact_id=latest.id,
    ).first() is not None
    if latest.generated_by in ("user", "manual") and not pinned:
        artifact = latest
    else:
        artifact = UserArtifact(
            user_id=current_user.id,
            kind=kind,
            title=latest.title,
            description=latest.description,
            generated_by="user",
            tokens_used=0,
            ai_usage=current_user.default_ai_usage,
        )
        db.session.add(artifact)
    artifact.set_content(content)
    db.session.commit()

    version_count = UserArtifact.version_count(current_user.id, kind)
    return jsonify(
        {"artifact": _serialize(artifact, version_number=version_count)}
    ), 200


@artifacts_bp.route("/<kind>/versions", methods=["GET"])
@login_required
def get_artifact_versions(kind):
    rows = UserArtifact.live().filter_by(
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
    artifact = UserArtifact.get_live(version_id)
    if artifact is None:
        return jsonify({"error": "Version not found"}), 404
    if artifact.user_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify({"artifact": _serialize(artifact)}), 200


@artifacts_bp.route("/versions/<int:version_id>/changes", methods=["GET"])
@login_required
def get_artifact_version_changes(version_id):
    """What a version changed, for "Show changes" on an artifact write in
    a reply's Actions taken (#604): the line diff from the version before
    it to this one, built here from the version ids the write stored.

    ``?from=<id>``: the version to diff against (the write's
    previous_artifact_id); without it, the version just before this one.
    The caller's own versions only, so another user's id reads as not
    found; there is no admin path (admins never read content through the
    API).

    200: {"kind", "title", "version_number", "previous_version_number"
    (null for a creation), "lines": [{"type": "same"|"del"|"add",
    "text"}]}. 404: no such version of the caller's, or ``from`` is not
    another version of the same artifact.
    """
    from backend.utils.artifact_versions import (
        line_diff, previous_version, stored_version, version_number)

    artifact = stored_version(version_id, current_user.id)
    if artifact is None:
        return jsonify({"error": "Version not found"}), 404
    from_id = request.args.get("from", type=int)
    if from_id is not None:
        previous = stored_version(from_id, current_user.id)
        if (previous is None or previous.kind != artifact.kind
                or previous.id == artifact.id):
            return jsonify({"error": "Version not found"}), 404
    else:
        previous = previous_version(artifact)
    return jsonify({
        "kind": artifact.kind,
        "title": artifact.title,
        "version_number": version_number(artifact),
        "previous_version_number": (
            version_number(previous) if previous is not None else None),
        "lines": line_diff(
            (previous.get_content() or "") if previous is not None else None,
            artifact.get_content() or ""),
    }), 200


@artifacts_bp.route("/<kind>/revert/<int:version_id>", methods=["POST"])
@login_required
def revert_artifact(kind, version_id):
    """Create a new artifact version from a historical one."""
    # A deleted version can't come back as a new one (#603).
    old = UserArtifact.get_live(version_id)
    if old is None:
        return jsonify({"error": "Version not found"}), 404
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

    version_count = UserArtifact.version_count(current_user.id, kind)
    return jsonify(
        {"artifact": _serialize(new, version_number=version_count)}), 200


@artifacts_bp.route("/<kind>", methods=["DELETE"])
@login_required
def delete_artifact(kind):
    """Delete a custom artifact with all its versions (#603).

    A soft delete, as for nodes: every version is hidden at once from
    every reader (the list, the history, the AI's index and tools,
    exports), and the daily cleanup deletes the rows after
    SOFT_DELETE_GRACE_DAYS. Built-in kinds and the saved-references
    digest are refused. Only the owner's own versions are touched: the
    query is scoped by the caller's id, so anyone else's kind of the
    same name is not found.

    200: {"deleted": kind, "versions": n, "grace_days": 30}.
    403: a built-in kind or the digest. 404: no live version of it,
    with "deleted": true when the user deleted it already (a repeated
    delete, e.g. from a second device, can be taken as done).
    """
    from backend.constants import SOFT_DELETE_GRACE_DAYS
    from backend.utils.artifact_deletion import (
        deleted_kinds, soft_delete_artifact)

    if not UserArtifact.is_deletable_kind(kind):
        return jsonify({"error": (
            "Built-in artifacts can't be deleted.")}), 403
    count = soft_delete_artifact(current_user.id, kind)
    if not count:
        db.session.rollback()
        if kind in deleted_kinds(current_user.id):
            return jsonify(_DELETED), 404
        return jsonify({"error": "Artifact not found"}), 404
    db.session.commit()
    return jsonify({
        "deleted": kind,
        "versions": count,
        "grace_days": SOFT_DELETE_GRACE_DAYS,
    }), 200
