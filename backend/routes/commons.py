"""Commons routes (the public forum) (#228, dark behind SHARE_V1 with the Share family).

- /feed (members): public thread tops by everyone, newest first — the
  Commons. Log's public sibling.
- /node/<id> (NO auth — the funnel): a public node and its PUBLIC
  descendants, readable logged out so a link shared on the open web shows
  the piece and the discussion around it — including LLM responses, which
  double as the product demo. Interaction requires an account.

Trimmed serializers only: no emails, no privacy metadata beyond what the
surface needs, private/deleted nodes structurally absent.
"""
import logging

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import aliased

from backend.models import Node, User
from backend.extensions import db
from backend.utils.encryption import key_service_unavailable
from backend.utils.error_privacy import describe_exception
from backend.utils.privacy import PrivacyLevel
from backend.utils.public_threads import (
    card_entries, public_children_filter, public_root_of, thread_top_filter,
)
from backend.utils.slugs import permalink_for
from backend.utils.timefmt import iso_utc

logger = logging.getLogger(__name__)

commons_bp = Blueprint("commons", __name__)

MAX_THREAD_NODES = 500

# INTRODUCED CONSTANT: entries per page of a person's public listing (the
# server-rendered /@<user> and GET /api/share/public/<user>), the Commons
# feed's page size. An account with this many public pieces or fewer gets
# one page, as before.
PUBLIC_LISTING_PAGE_SIZE = 20

# INTRODUCED CONSTANT: the highest page number a listing reads; a larger
# one reads as this. Far past any real listing (20 million entries); keeps
# the OFFSET inside the database's integer range.
MAX_LISTING_PAGE = 1_000_000


def listing_page():
    """The ?page= of a public listing: 1 when absent, malformed or below
    1, at most MAX_LISTING_PAGE."""
    page = request.args.get("page", 1, type=int)
    return min(max(page or 1, 1), MAX_LISTING_PAGE)


def public_root_order():
    """ORDER BY for a person's public roots: pinned first (newest pin
    first), then newest; the id settles ties, so pages never overlap."""
    return (Node.pinned_at.is_(None), Node.pinned_at.desc(),
            Node.created_at.desc().nullslast(), Node.id.desc())


def _enabled():
    return bool(current_app.config.get("SHARE_V1", False))


def _public_alive(query):
    """Filter to nodes the open web may see: public, living, AND with an
    author who currently has public sharing switched on and has not
    deleted the account (#269: hidden for the grace period). Toggling
    public_sharing_enabled off takes every piece (and reply) down
    immediately — respecting a takedown beats URL stability."""
    owner = aliased(User)
    return query.join(
        owner,
        owner.id == func.coalesce(Node.human_owner_id, Node.user_id),
    ).filter(
        Node.privacy_level == PrivacyLevel.PUBLIC.value,
        Node.deleted_at.is_(None),
        owner.public_sharing_enabled.is_(True),
        owner.deleted_at.is_(None),
    )


def _publicly_visible(node):
    """The same predicate as _public_alive, for a single in-hand node."""
    if (node is None or node.deleted_at is not None
            or node.privacy_level != PrivacyLevel.PUBLIC.value):
        return False
    owner_id = node.human_owner_id or node.user_id
    owner = User.query.get(owner_id) if owner_id else None
    return bool(owner and owner.public_sharing_enabled
                and owner.deleted_at is None)


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
    """Public thread tops, everyone, newest first: public roots, and the
    public first entry of a thread whose prompt root is private
    (utils/public_threads). A public prompt root's card is the first
    public entry of its owner under it: its id, text, reply count and
    permalink (else the root's). Members-only surface — needs the user's
    own opt-in (#228 dark ship)."""
    if not _enabled() or not current_user.public_sharing_enabled:
        return jsonify({"error": "Not found"}), 404
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)
    query = _public_alive(
        Node.query.filter(thread_top_filter())
    ).order_by(Node.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page,
                                error_out=False)
    cards = card_entries(pagination.items)
    items = []
    for top in pagination.items:
        node = cards.get(top.id, top)
        try:
            content = (node.get_content() or "").strip()
        except Exception as e:  # noqa: BLE001 — one unreadable entry
            # One entry whose stored value does not decrypt leaves the
            # page, not the whole feed: logged by id only. A key-service
            # outage fails the request as before, instead of every entry
            # waiting out its own retries.
            if key_service_unavailable(e):
                raise
            logger.warning("Commons feed: node %s could not be read (%s); "
                           "left out", node.id, describe_exception(e))
            continue
        reply_count = _public_alive(
            Node.query.filter(public_children_filter(node.id))).count()
        items.append({
            "id": node.id,
            "username": _author_name(node),
            "permalink": permalink_for(node) or permalink_for(top),
            "content": content[:600] + ("…" if len(content) > 600 else ""),
            "created_at": iso_utc(top.created_at),
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
    # Its children, and the public nodes under a private prompt entry of
    # their owner's below it, shown in that entry's place.
    child_rows = _public_alive(
        Node.query.filter(public_children_filter(node.id))
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
    """/@<username>/<slug> → node id. Unauthenticated; 404s identically
    for unknown user/slug and anything non-public."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    from backend.utils.username_history import resolve_public_handle
    user, moved = resolve_public_handle(username)
    if user is None:
        return jsonify({"error": "Not found"}), 404
    node = _public_alive(Node.query.filter(
        Node.human_owner_id == user.id,
        Node.public_slug == slug,
    )).first()
    if node is None:
        return jsonify({"error": "Not found"}), 404
    body = {"node_id": node.id}
    if moved:
        # A former handle, or the current one in another case (#253): the
        # SPA swaps the address bar to the canonical URL; the server-
        # rendered pages 301 instead. Only ever sent with a node that
        # resolved, so it never names the new handle of anything that
        # isn't public under it.
        body["canonical"] = f"/@{user.username}/{slug}"
    return jsonify(body), 200


@commons_bp.route("/node/<int:node_id>", methods=["GET"])
def public_thread(node_id):
    """A public node + its public discussion. Deliberately unauthenticated.

    404s identically for missing, private, and deleted nodes — the
    endpoint never confirms the existence of anything non-public."""
    if not _enabled():
        return jsonify({"error": "Not found"}), 404
    node = Node.query.get(node_id)
    if not _publicly_visible(node):
        return jsonify({"error": "Not found"}), 404

    # Walk up to the nearest public root so a deep-linked reply still
    # renders its thread; stop at the first non-public ancestor boundary
    # (a private prompt entry of the same person is passed through).
    root = public_root_of(node, _publicly_visible)

    budget = [MAX_THREAD_NODES]
    thread = _serialize_public_subtree(root, budget)
    return jsonify({
        "thread": thread,
        "focus_id": node.id,
        "truncated": budget[0] <= 0,
    }), 200
