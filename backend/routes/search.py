import re
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import or_
from backend.models import Node, User
from backend.extensions import db
from backend.utils.privacy import can_user_access_node

search_bp = Blueprint("search_bp", __name__)


def _snippet(text, keyword, context_chars=80):
    """Return a snippet around the first keyword match with <mark> highlighting."""
    lower = text.lower()
    kw_lower = keyword.lower()
    idx = lower.find(kw_lower)
    if idx == -1:
        return text[:200] + ("..." if len(text) > 200 else "")

    start = max(0, idx - context_chars)
    end = min(len(text), idx + len(keyword) + context_chars)
    fragment = text[start:end]

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""

    # Highlight all occurrences in the fragment (case-insensitive)
    highlighted = re.sub(
        re.escape(keyword),
        lambda m: f"<mark>{m.group(0)}</mark>",
        fragment,
        flags=re.IGNORECASE,
    )
    return prefix + highlighted + suffix


@search_bp.route("/search", methods=["GET"])
@login_required
def search():
    q = request.args.get("q", "").strip()
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    node_type = request.args.get("node_type")
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = min(per_page, 100)

    if not q and not date_from and not date_to:
        return jsonify({"error": "Provide at least a keyword (q) or date range (from/to)."}), 400

    # Base query: user's own nodes + LLM nodes they have access to.
    # LLM nodes are owned by bot users but accessible to the human who
    # triggered them (determined by parent-chain traversal up to the
    # first human author).  We over-fetch at SQL level by including all
    # LLM children/grandchildren of the user's nodes, then verify each
    # non-owned node with can_user_access_node() in Python.
    user_node_ids = (
        db.session.query(Node.id)
        .filter(Node.user_id == current_user.id)
    )
    # Level 1: direct LLM children of user's nodes
    lvl1_ids = (
        db.session.query(Node.id)
        .filter(Node.parent_id.in_(user_node_ids))
    )
    # Level 2: LLM grandchildren (Human → LLM → LLM chains)
    lvl2_ids = (
        db.session.query(Node.id)
        .filter(Node.parent_id.in_(lvl1_ids))
    )
    query = Node.query.filter(
        or_(
            Node.user_id == current_user.id,
            Node.id.in_(lvl1_ids),
            Node.id.in_(lvl2_ids),
        )
    )

    # Date filters (SQL-level, fast)
    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from)
            query = query.filter(Node.created_at >= dt_from)
        except ValueError:
            return jsonify({"error": "Invalid 'from' date format. Use ISO 8601."}), 400

    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to)
            query = query.filter(Node.created_at <= dt_to)
        except ValueError:
            return jsonify({"error": "Invalid 'to' date format. Use ISO 8601."}), 400

    if node_type:
        query = query.filter(Node.node_type == node_type)

    query = query.order_by(Node.created_at.desc())

    # Fetch all candidates and verify access.  The SQL over-fetches
    # (includes all children/grandchildren of user's nodes, not just
    # LLM ones), so we confirm each non-owned node with the authoritative
    # can_user_access_node() check.
    all_nodes = query.all()
    accessible = [
        n for n in all_nodes
        if n.user_id == current_user.id
        or can_user_access_node(n, current_user.id)
    ]

    if not q:
        # No keyword — paginate the accessible list, decrypt only the page
        total = len(accessible)
        start = (page - 1) * per_page
        page_nodes = accessible[start:start + per_page]
        results = []
        for node in page_nodes:
            content = node.get_content()
            results.append({
                "id": node.id,
                "preview": content[:200] + ("..." if len(content) > 200 else ""),
                "snippet": None,
                "node_type": node.node_type,
                "created_at": node.created_at.isoformat(),
                "username": node.user.username if node.user else "Unknown",
                "child_count": len(node.children),
                "parent_id": node.parent_id,
                "score": 1.0,
            })
        return jsonify({
            "results": results,
            "page": page,
            "per_page": per_page,
            "total": total,
            "has_more": (start + per_page) < total,
            "search_type": "keyword",
        })

    # Keyword search: decrypt all accessible nodes, filter in-memory
    matches = []
    for node in accessible:
        content = node.get_content()
        if q.lower() in content.lower():
            matches.append((node, content))

    total = len(matches)
    start = (page - 1) * per_page
    page_matches = matches[start:start + per_page]

    results = []
    for node, content in page_matches:
        results.append({
            "id": node.id,
            "preview": content[:200] + ("..." if len(content) > 200 else ""),
            "snippet": _snippet(content, q),
            "node_type": node.node_type,
            "created_at": node.created_at.isoformat(),
            "username": node.user.username if node.user else "Unknown",
            "child_count": len(node.children),
            "parent_id": node.parent_id,
            "score": 1.0,
        })

    return jsonify({
        "results": results,
        "page": page,
        "per_page": per_page,
        "total": total,
        "has_more": (start + per_page) < total,
        "search_type": "keyword",
    })
