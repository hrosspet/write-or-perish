import unicodedata
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from backend.models import (
    ExternalItem, Node, NodeContextArtifact,
)
from backend.extensions import db
from backend.utils.timefmt import iso_utc

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


def _parse_date_range(date_from, date_to):
    """ISO 8601 'from'/'to' query params -> (datetime|None, datetime|None).

    Raises ValueError with a user-facing message on a malformed value.
    """
    dt_from = dt_to = None
    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from)
        except ValueError:
            raise ValueError("Invalid 'from' date format. Use ISO 8601.")
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to)
        except ValueError:
            raise ValueError("Invalid 'to' date format. Use ISO 8601.")
    return dt_from, dt_to


# A reference's date is when it was posted, or when it was saved if the
# source has no post date (web clips). Used by both search modes so the
# date filter means the same thing in either.
_EXTERNAL_DATE = func.coalesce(ExternalItem.posted_at, ExternalItem.fetched_at)


def _external_result(item, content, score, snippet=None):
    """One saved reference in search-result shape (kind='external')."""
    return {
        "id": item.id,
        "kind": "external",
        "source": item.source,
        "author_handle": item.author_handle,
        "title": item.title,
        "external_url": item.url,
        "preview": content[:200] + ("..." if len(content) > 200 else ""),
        "snippet": snippet,
        "created_at": iso_utc(item.posted_at or item.fetched_at),
        "score": score,
    }


def _external_keyword_search(q, dt_from, dt_to, page, per_page):
    """Keyword search over the user's saved references (scope=external).

    Same shape as the archive keyword search: decrypt, match
    diacritics-insensitively in memory (title counts too — a clip's
    title is often the only place its subject is named), paginate.
    Ordered newest-saved first, as the References page is.
    """
    query = ExternalItem.query.filter(ExternalItem.user_id == current_user.id)
    if dt_from is not None:
        query = query.filter(_EXTERNAL_DATE >= dt_from)
    if dt_to is not None:
        query = query.filter(_EXTERNAL_DATE <= dt_to)
    items = query.order_by(ExternalItem.id.desc()).all()

    start = (page - 1) * per_page
    if not q:
        total = len(items)
        page_items = [(i, i.get_content() or "")
                      for i in items[start:start + per_page]]
    else:
        q_normalized = _strip_diacritics(q).lower()
        matches = []
        for item in items:
            content = item.get_content() or ""
            haystack = f"{item.title or ''}\n{content}"
            if q_normalized in _strip_diacritics(haystack).lower():
                matches.append((item, content))
        total = len(matches)
        page_items = matches[start:start + per_page]

    results = [
        _external_result(item, content, 1.0,
                         snippet=_snippet(content, q) if q else None)
        for item, content in page_items
    ]
    return jsonify({
        "results": results,
        "page": page,
        "per_page": per_page,
        "total": total,
        "has_more": (start + per_page) < total,
        "search_type": "keyword",
        "scope": "external",
    })


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

    try:
        dt_from, dt_to = _parse_date_range(date_from, date_to)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # scope=external searches saved references instead of the archive
    # (the References page's search button); default is the archive.
    if request.args.get("scope") == "external":
        return _external_keyword_search(q, dt_from, dt_to, page, per_page)

    # Base query: user's own nodes + nodes where they are the human owner
    # Exclude system prompt nodes (content resolved from UserPrompt)
    prompt_node_ids = db.session.query(
        NodeContextArtifact.node_id
    ).filter(NodeContextArtifact.artifact_type == "prompt").subquery()

    query = Node.query.filter(
        Node.deleted_at.is_(None),
        or_(
            Node.user_id == current_user.id,
            Node.human_owner_id == current_user.id,
        ),
        ~Node.id.in_(db.session.query(prompt_node_ids)),
        # ...and roots whose per-thread edit detached that link but kept
        # the prompt_key stamp: still prompt text, not the user's writing.
        Node.prompt_key.is_(None),
    )

    # Date filters (SQL-level, fast)
    if dt_from is not None:
        query = query.filter(Node.created_at >= dt_from)
    if dt_to is not None:
        query = query.filter(Node.created_at <= dt_to)

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
                "created_at": iso_utc(node.created_at),
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
            "created_at": iso_utc(node.created_at),
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


@search_bp.route("/search/semantic", methods=["GET"])
@login_required
def semantic_search():
    """Semantic search over the user's own archive (#155).

    Embeds the query and ranks the user's NodeEmbedding rows by cosine
    similarity (brute-force scan — fine at alpha scale). Only AI-readable
    nodes are embedded, so ai_usage='none' content never appears here
    (keyword search still covers it).
    """
    from flask import current_app
    from backend.models import NodeEmbedding
    from backend.utils.api_keys import get_openai_chat_key
    from backend.utils.embeddings import embed_texts, top_k_similar

    q = request.args.get("q", "").strip()
    limit = min(request.args.get("limit", 20, type=int), 50)
    min_score = request.args.get("min_score", 0.2, type=float)

    if not q:
        return jsonify({"error": "Provide a query (q)."}), 400

    try:
        dt_from, dt_to = _parse_date_range(
            request.args.get("from"), request.args.get("to"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # scope=external: saved references only (the References page's
    # search). Default 'archive' ranks the user's nodes and, unless
    # include_external=0, merges references in by score.
    external_only = request.args.get("scope") == "external"

    api_key = get_openai_chat_key(current_app.config)
    if not api_key:
        return jsonify({"error": "Semantic search is not configured."}), 503

    try:
        query_vector = embed_texts(
            [q], api_key, user_id=current_user.id,
            request_type="embedding_query",
        )[0]
        db.session.commit()  # persist the query cost log
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Embedding the query failed."}), 502

    ranked = []
    if not external_only:
        emb_query = db.session.query(
            NodeEmbedding.node_id, NodeEmbedding.vector
        ).filter(NodeEmbedding.user_id == current_user.id)
        if dt_from is not None or dt_to is not None:
            emb_query = emb_query.join(
                Node, Node.id == NodeEmbedding.node_id)
            if dt_from is not None:
                emb_query = emb_query.filter(Node.created_at >= dt_from)
            if dt_to is not None:
                emb_query = emb_query.filter(Node.created_at <= dt_to)
        rows = emb_query.all()
        ranked = top_k_similar(
            query_vector, rows, k=limit, min_score=min_score)

    # External references (#155 component 2) — included unless opted out
    include_external = external_only or request.args.get(
        "include_external", "1") not in ("0", "false")
    external_results = []
    if include_external:
        from backend.models import ExternalItemEmbedding
        ext_query = db.session.query(
            ExternalItemEmbedding.item_id, ExternalItemEmbedding.vector
        ).filter(ExternalItemEmbedding.user_id == current_user.id)
        if dt_from is not None or dt_to is not None:
            ext_query = ext_query.join(
                ExternalItem,
                ExternalItem.id == ExternalItemEmbedding.item_id)
            if dt_from is not None:
                ext_query = ext_query.filter(_EXTERNAL_DATE >= dt_from)
            if dt_to is not None:
                ext_query = ext_query.filter(_EXTERNAL_DATE <= dt_to)
        ext_rows = ext_query.all()
        ext_ranked = top_k_similar(
            query_vector, ext_rows, k=limit, min_score=min_score)
        items_by_id = {
            i.id: i for i in ExternalItem.query.filter(
                ExternalItem.id.in_([iid for iid, _ in ext_ranked]),
                ExternalItem.user_id == current_user.id,
            ).all()
        } if ext_ranked else {}
        for item_id, score in ext_ranked:
            item = items_by_id.get(item_id)
            if item is None:
                continue
            external_results.append(_external_result(
                item, item.get_content() or "", round(score, 4)))
    nodes_by_id = {
        n.id: n for n in Node.query.filter(
            Node.id.in_([node_id for node_id, _ in ranked]),
            Node.deleted_at.is_(None),
        ).all()
    } if ranked else {}

    results = []
    for node_id, score in ranked:
        node = nodes_by_id.get(node_id)
        if node is None:
            continue
        # Defense in depth: embeddings are already scoped by user_id,
        # but re-check ownership on the node itself.
        if (node.user_id != current_user.id
                and node.human_owner_id != current_user.id):
            continue
        content = node.get_content() or ""
        results.append({
            "id": node.id,
            "preview": content[:200] + ("..." if len(content) > 200 else ""),
            "snippet": None,
            "node_type": node.node_type,
            "created_at": iso_utc(node.created_at),
            "username": node.user.username if node.user else "Unknown",
            "child_count": len(node.children),
            "parent_id": node.parent_id,
            "score": round(score, 4),
        })

    # Merge node + external results by score
    merged = sorted(
        results + external_results,
        key=lambda r: r["score"], reverse=True,
    )[:limit]

    return jsonify({
        "results": merged,
        "total": len(merged),
        "mode": "semantic",
        "scope": "external" if external_only else "archive",
    }), 200


@search_bp.route("/search/neighbors", methods=["GET"])
@login_required
def semantic_neighbors():
    """The most semantically similar nodes to a given node — the node's own
    embedding used as the query vector (no re-embed, no LLM guess). This is
    the 'source zero' guess-free neighborhood, surfaced for inspecting what
    semantic retrieval returns from a given point in the archive (#155/#197).

    Scoped to the requesting user's own archive (own + AI replies they own).
    Returns [] if the node isn't embedded yet (e.g. ai_usage='none', or the
    sweep hasn't reached it).
    """
    from backend.models import NodeEmbedding
    from backend.utils.embeddings import top_k_similar, unpack_vector

    node_id = request.args.get("node_id", type=int)
    limit = min(request.args.get("limit", 5, type=int), 20)
    if not node_id:
        return jsonify({"error": "Provide a node_id."}), 400

    node = Node.query.get(node_id)
    if node is None or node.deleted_at is not None:
        return jsonify({"error": "Node not found."}), 404
    if (node.user_id != current_user.id
            and node.human_owner_id != current_user.id):
        return jsonify({"error": "Not authorized."}), 403

    src = NodeEmbedding.query.filter_by(node_id=node_id).first()
    if src is None:
        # Not embedded yet — no neighborhood to show.
        return jsonify({"results": [], "total": 0, "mode": "neighbors"}), 200

    query_vector = unpack_vector(src.vector)
    rows = db.session.query(
        NodeEmbedding.node_id, NodeEmbedding.vector
    ).filter(
        NodeEmbedding.user_id == current_user.id,
        NodeEmbedding.node_id != node_id,
    ).all()

    ranked = top_k_similar(query_vector, rows, k=limit, min_score=0.0)
    nodes_by_id = {
        n.id: n for n in Node.query.filter(
            Node.id.in_([nid for nid, _ in ranked]),
            Node.deleted_at.is_(None),
        ).all()
    } if ranked else {}

    results = []
    for nid, score in ranked:
        n = nodes_by_id.get(nid)
        if n is None:
            continue
        if (n.user_id != current_user.id
                and n.human_owner_id != current_user.id):
            continue
        content = n.get_content() or ""
        results.append({
            "id": n.id,
            "preview": content[:160] + ("..." if len(content) > 160 else ""),
            "node_type": n.node_type,
            "created_at": iso_utc(n.created_at),
            "username": n.user.username if n.user else "Unknown",
            "score": round(score, 4),
        })

    return jsonify({
        "results": results,
        "total": len(results),
        "mode": "neighbors",
    }), 200
