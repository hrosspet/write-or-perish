"""Commons routes (the public forum) (#228, dark behind SHARE_V1 with the Share family).

- /feed (members): public root nodes by everyone, newest first — the
  Commons. Log's public sibling.
- /node/<id> (NO auth — the funnel): a public node and its PUBLIC
  descendants, readable logged out so a link shared on the open web shows
  the piece and the discussion around it — including LLM responses, which
  double as the product demo. Interaction requires an account.

Trimmed serializers only: no emails, no privacy metadata beyond what the
surface needs, private/deleted nodes structurally absent.
"""
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required

from backend.models import Node, User
from backend.extensions import db
from backend.utils.privacy import PrivacyLevel
from backend.utils.timefmt import iso_utc

commons_bp = Blueprint("commons", __name__)

MAX_THREAD_NODES = 500


def _enabled():
    return bool(current_app.config.get("SHARE_V1", False))


def _public_alive(query):
    return query.filter(
        Node.privacy_level == PrivacyLevel.PUBLIC.value,
        Node.deleted_at.is_(None),
    )


def _author_name(node):
    """Display author. For LLM nodes the meaningful author is the HUMAN
    who generated the response (human_owner), not the synthetic model
    account — the frontend renders "model · via <human>"."""
    if node.node_type == "llm":
        owner_id = node.human_owner_id or node.user_id
        owner = User.query.get(owner_id) if owner_id else None
        return owner.username if owner else None
    return node.user.username if node.user else None


@commons_bp.route("/feed", methods=["GET"])
@login_required
def feed():
    """Public root nodes, everyone, newest first."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    query = _public_alive(
        Node.query.filter(Node.parent_id.is_(None))
    ).order_by(Node.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page,
                                error_out=False)
    items = []
    for node in pagination.items:
        content = (node.get_content() or "").strip()
        reply_count = _public_alive(
            Node.query.filter(Node.parent_id == node.id)).count()
        items.append({
            "id": node.id,
            "username": _author_name(node),
            "permalink": (
                f"/u/{node.user.username}/{node.public_slug}"
                if node.public_slug and node.user else None),
            "content": content[:600] + ("…" if len(content) > 600 else ""),
            "created_at": iso_utc(node.created_at),
            "reply_count": reply_count,
        })
    return jsonify({
        "items": items,
        "has_more": pagination.has_next,
        "page": page,
    }), 200


def _serialize_public_subtree(node, budget):
    """Serialize *node* and its public, living descendants. *budget* is a
    single-element list acting as a mutable node counter — deep threads
    truncate rather than serialize unboundedly."""
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    children = []
    child_rows = _public_alive(
        Node.query.filter(Node.parent_id == node.id)
    ).order_by(Node.created_at.asc()).all()
    for child in child_rows:
        serialized = _serialize_public_subtree(child, budget)
        if serialized is not None:
            children.append(serialized)
    return {
        "id": node.id,
        "username": _author_name(node),
        "node_type": node.node_type,
        "llm_model": node.llm_model,
        "content": node.get_content(),
        "created_at": iso_utc(node.created_at),
        "children": children,
    }


@commons_bp.route("/permalink/<string:username>/<string:slug>",
                methods=["GET"])
def resolve_permalink(username, slug):
    """/u/<username>/<slug> → node id. Unauthenticated; 404s identically
    for unknown user/slug and anything non-public."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    user = User.query.filter_by(username=username).first()
    if user is None:
        return jsonify({"error": "Not found"}), 404
    node = _public_alive(Node.query.filter(
        Node.human_owner_id == user.id,
        Node.public_slug == slug,
    )).first()
    if node is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"node_id": node.id}), 200


@commons_bp.route("/node/<int:node_id>", methods=["GET"])
def public_thread(node_id):
    """A public node + its public discussion. Deliberately unauthenticated.

    404s identically for missing, private, and deleted nodes — the
    endpoint never confirms the existence of anything non-public."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    node = Node.query.get(node_id)
    if (node is None or node.deleted_at is not None
            or node.privacy_level != PrivacyLevel.PUBLIC.value):
        return jsonify({"error": "Not found"}), 404

    # Walk up to the nearest public root so a deep-linked reply still
    # renders its thread; stop at the first non-public ancestor boundary.
    root = node
    visited = set()
    while (root.parent_id and root.id not in visited):
        visited.add(root.id)
        parent = Node.query.get(root.parent_id)
        if (parent is None or parent.deleted_at is not None
                or parent.privacy_level != PrivacyLevel.PUBLIC.value):
            break
        root = parent

    budget = [MAX_THREAD_NODES]
    thread = _serialize_public_subtree(root, budget)
    return jsonify({
        "thread": thread,
        "focus_id": node.id,
        "truncated": budget[0] <= 0,
    }), 200
