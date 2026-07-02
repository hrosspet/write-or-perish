"""Share routes (Feature 3 — Upload v1, dark behind SHARE_V1).

Consent model, structurally enforced:
- The AI proposes a share in conversation (### Share headings). Confirming
  there (Save button / apply_share tool) only creates a PRIVATE draft.
- Publication happens exclusively here, on the Share page, as a second
  deliberate action — and is revocable at any time.
- The public endpoint serves ONLY published items, and only while the flag
  is on. Nothing else about the user is exposed.
"""
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node, Draft, ShareDraft, User
from backend.extensions import db
from backend.utils.share import save_share_draft_from_node
from backend.utils.tool_meta import update_tool_meta
from backend.utils.timefmt import iso_utc
from backend.utils.privacy import PrivacyLevel, AIUsage
from datetime import datetime

share_bp = Blueprint("share", __name__)


def _share_enabled():
    return bool(current_app.config.get("SHARE_V1", False))


def _serialize(share):
    return {
        "id": share.id,
        "content": share.get_content(),
        "share_type": share.share_type,
        "status": share.status,
        "source_node_id": share.source_node_id,
        "public_node_id": share.public_node_id,
        "created_at": iso_utc(share.created_at),
        "updated_at": iso_utc(share.updated_at),
        "published_at": iso_utc(share.published_at),
        "revoked_at": iso_utc(share.revoked_at),
    }


def _get_own_share_or_404(share_id):
    share = ShareDraft.query.get(share_id)
    if not share or share.user_id != current_user.id:
        return None
    return share


@share_bp.route("", methods=["GET"])
@login_required
def list_shares():
    """List the current user's share drafts + published items."""
    if not _share_enabled():
        return jsonify({"error": "Not found"}), 404
    shares = ShareDraft.query.filter_by(user_id=current_user.id).order_by(
        ShareDraft.created_at.desc()).all()
    return jsonify({"shares": [_serialize(s) for s in shares]}), 200


@share_bp.route("", methods=["POST"])
@login_required
def create_share():
    """Manually create a share draft from the Share page."""
    if not _share_enabled():
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400
    share_type = data.get("share_type") or "other"
    if share_type not in ShareDraft.SHARE_TYPES:
        share_type = "other"
    share = ShareDraft(user_id=current_user.id, share_type=share_type,
                       status="draft")
    share.set_content(content)
    db.session.add(share)
    db.session.commit()
    return jsonify(_serialize(share)), 201


@share_bp.route("/<int:share_id>", methods=["PATCH"])
@login_required
def update_share(share_id):
    """Edit a draft's content/type. Published items must be revoked first —
    what is public is always exactly what the user last approved."""
    if not _share_enabled():
        return jsonify({"error": "Not found"}), 404
    share = _get_own_share_or_404(share_id)
    if not share:
        return jsonify({"error": "Not found"}), 404
    if share.status == "published":
        return jsonify({
            "error": "Revoke before editing a published share"}), 409
    data = request.get_json() or {}
    content = data.get("content")
    if content is not None:
        content = content.strip()
        if not content:
            return jsonify({"error": "content cannot be empty"}), 400
        share.set_content(content)
    share_type = data.get("share_type")
    if share_type is not None:
        if share_type not in ShareDraft.SHARE_TYPES:
            return jsonify({"error": "invalid share_type"}), 400
        share.share_type = share_type
    db.session.commit()
    return jsonify(_serialize(share)), 200


@share_bp.route("/<int:share_id>", methods=["DELETE"])
@login_required
def delete_share(share_id):
    """Delete a share draft (any status — deleting a published item also
    removes it from the public page)."""
    if not _share_enabled():
        return jsonify({"error": "Not found"}), 404
    share = _get_own_share_or_404(share_id)
    if not share:
        return jsonify({"error": "Not found"}), 404
    if share.public_node_id:
        from backend.utils.node_deletion import soft_delete_node
        soft_delete_node(share.public_node_id, current_user.id,
                         with_descendants=False)
    db.session.delete(share)
    db.session.commit()
    return jsonify({"status": "deleted"}), 200


@share_bp.route("/<int:share_id>/publish", methods=["POST"])
@login_required
def publish_share(share_id):
    """The one action that makes a share visible to others."""
    if not _share_enabled():
        return jsonify({"error": "Not found"}), 404
    share = _get_own_share_or_404(share_id)
    if not share:
        return jsonify({"error": "Not found"}), 404
    if share.status == "published":
        return jsonify(_serialize(share)), 200

    content = share.get_content()

    # Identity follows content: republishing UNCHANGED content restores the
    # same node (deleted_at cleared) so an existing discussion reattaches —
    # it still refers to exactly what people replied to. Edited content (or
    # a purged/wiped node) gets a NEW node and the old discussion stays
    # correctly severed.
    public_node = None
    if share.public_node_id:
        prior = Node.query.get(share.public_node_id)
        if (prior is not None and prior.deleted_at is not None
                and (prior.get_content() or "") == content):
            prior.deleted_at = None
            public_node = prior

    if public_node is None:
        # Publishing = extraction into the public forum (#228): a standalone
        # public ROOT node (no system node — LLM calls on the thread assemble
        # context purely from the visible chain). ai_usage follows the user's
        # default so the author's AI preference travels with the content;
        # 'none' means other users can read but not include it in completions.
        ai_usage = current_user.default_ai_usage
        if ai_usage not in (AIUsage.CHAT.value, AIUsage.TRAIN.value,
                            AIUsage.NONE.value):
            ai_usage = AIUsage.CHAT.value
        from backend.utils.tokens import approximate_token_count
        public_node = Node(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=None,
            node_type="user",
            privacy_level=PrivacyLevel.PUBLIC.value,
            ai_usage=ai_usage,
            token_count=approximate_token_count(content),
        )
        public_node.set_content(content)
        db.session.add(public_node)
        db.session.flush()

    share.public_node_id = public_node.id
    # Back-link from the private proposal node to its public artifact.
    if share.source_node_id:
        origin = Node.query.get(share.source_node_id)
        if origin is not None and origin.linked_node_id is None:
            origin.linked_node_id = public_node.id

    share.status = "published"
    share.published_at = datetime.utcnow()
    share.revoked_at = None
    db.session.commit()
    return jsonify(_serialize(share)), 200


@share_bp.route("/<int:share_id>/revoke", methods=["POST"])
@login_required
def revoke_share(share_id):
    """Take a published share back off the public page."""
    if not _share_enabled():
        return jsonify({"error": "Not found"}), 404
    share = _get_own_share_or_404(share_id)
    if not share:
        return jsonify({"error": "Not found"}), 404
    if share.status != "published":
        return jsonify({"error": "Only published shares can be revoked"}), 409
    if share.public_node_id:
        from backend.utils.node_deletion import soft_delete_node
        # Your content comes down; public replies by others keep their own
        # nodes (they'll render under a tombstone, like any deleted node).
        # The pointer is KEPT: republishing unchanged content undeletes
        # this same node so the discussion reattaches (identity follows
        # content).
        soft_delete_node(share.public_node_id, current_user.id,
                         with_descendants=False)
    share.status = "revoked"
    share.revoked_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_serialize(share)), 200


@share_bp.route("/save-proposal", methods=["POST"])
@login_required
def save_proposal():
    """Save the share the AI proposed on a pending node as a private draft.

    Mirrors /feedback/submit: the share text lives in the visible LLM node
    content (under ### Share); this confirms + persists it only when the
    user clicks Save (or the equivalent apply_share tool). The result is a
    PRIVATE draft — publication is a separate action on the Share page."""
    if not _share_enabled():
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    llm_node_id = data.get("llm_node_id")

    if not llm_node_id:
        return jsonify({"error": "llm_node_id is required"}), 400

    llm_node = Node.query.get(llm_node_id)
    if not llm_node:
        return jsonify({"error": "Node not found"}), 404

    # Find the pending draft by walking the ancestor chain.
    draft = None
    current_node = llm_node
    visited = set()
    while current_node and current_node.id not in visited:
        visited.add(current_node.id)
        draft = Draft.query.filter_by(
            user_id=current_user.id,
            parent_id=current_node.id,
            label='share_pending',
        ).first()
        if draft:
            break
        current_node = current_node.parent

    if not draft:
        return jsonify({"error": "No pending share found"}), 404

    origin_node = Node.query.get(draft.parent_id)
    if not origin_node:
        return jsonify({"error": "Origin node not found"}), 404

    share, err = save_share_draft_from_node(origin_node, current_user.id)
    if err:
        return jsonify({"error": err}), 400

    db.session.delete(draft)
    update_tool_meta(origin_node, "propose_share", {
        "apply_status": "completed",
        "share_id": share.id,
    })
    db.session.commit()

    return jsonify({
        "status": "completed",
        "share": _serialize(share),
    }), 200


@share_bp.route("/public/<string:username>", methods=["GET"])
def public_shares(username):
    """Public page data: the user's PUBLISHED shares, nothing else.

    Deliberately unauthenticated — this is the one outward-facing surface of
    Upload v1. Serves only status == "published" (revoke/delete removes an
    item immediately) and 404s entirely while the flag is off."""
    if not _share_enabled():
        return jsonify({"error": "Not found"}), 404
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({"error": "Not found"}), 404
    shares = ShareDraft.query.filter_by(
        user_id=user.id, status="published").order_by(
        ShareDraft.published_at.desc()).all()
    return jsonify({
        "username": user.username,
        "shares": [{
            "id": s.id,
            "content": s.get_content(),
            "share_type": s.share_type,
            "public_node_id": s.public_node_id,
            "published_at": iso_utc(s.published_at),
        } for s in shares],
    }), 200
