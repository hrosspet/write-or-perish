import unicodedata
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import or_
from backend.models import Node, User
from backend.extensions import db

search_bp = Blueprint("search_bp", __name__)


def _strip_diacritics(text):
    """Remove diacritics/accents: 'štědrá' -> 'stedra', 'café' -> 'cafe'."""
    nfkd = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in nfkd if unicodedata.category(c) != 'Mn')


def _snippet(text, keyword, context_chars=80):
    """Return a snippet around the first keyword match with <mark> highlighting.

    Matches are found diacritics-insensitively but the original text is
    preserved in the output, with matching spans wrapped in <mark> tags.
    """
    stripped = _strip_diacritics(text).lower()
    kw_stripped = _strip_diacritics(keyword).lower()
    idx = stripped.find(kw_stripped)
    if idx == -1:
        return text[:200] + ("..." if len(text) > 200 else "")

    # idx/len refer to positions in the stripped string, which is
    # char-for-char the same length as the original (NFKD + remove Mn
    # only drops combining marks that don't occupy a position in the
    # original), so we can slice the original text at the same offsets.
    start = max(0, idx - context_chars)
    end = min(len(text), idx + len(keyword) + context_chars)
    fragment = text[start:end]

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""

    # Build a regex from the stripped keyword that matches each character
    # with or without its diacritics in the original text.
    fragment_stripped = _strip_diacritics(fragment).lower()
    highlighted = []
    i = 0
    while i < len(fragment):
        match_start = fragment_stripped.find(kw_stripped, i)
        if match_start == -1:
            highlighted.append(fragment[i:])
            break
        highlighted.append(fragment[i:match_start])
        match_end = match_start + len(kw_stripped)
        highlighted.append(f"<mark>{fragment[match_start:match_end]}</mark>")
        i = match_end

    return prefix + ''.join(highlighted) + suffix


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

    # Base query: user's own nodes + nodes where they are the human owner
    query = Node.query.filter(
        or_(
            Node.user_id == current_user.id,
            Node.human_owner_id == current_user.id,
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

    if not q:
        # No keyword — paginate at SQL level
        all_nodes = query.all()
        total = len(all_nodes)
        start = (page - 1) * per_page
        page_nodes = all_nodes[start:start + per_page]
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

    # Keyword search: decrypt all matching nodes, filter in-memory
    # Diacritics-insensitive: "stedra" matches "štědrá"
    q_normalized = _strip_diacritics(q).lower()
    all_nodes = query.all()
    matches = []
    for node in all_nodes:
        content = node.get_content()
        if q_normalized in _strip_diacritics(content).lower():
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
