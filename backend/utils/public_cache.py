"""Redis cache for server-rendered public pages.

Public content is KMS envelope-encrypted per node, so a cold render of a
long public thread is up to MAX_THREAD_NODES billable KMS decrypts (the
2026-07-06 bill incident class). Crawler traffic must not be able to
re-trigger that per request, so successful renders are cached here.

- Shared across gunicorn workers (unlike the in-process DEK LRU).
- Short TTL bounds staleness for no-JS clients; JS clients always fetch
  live data through the API after hydrating.
- Publish/revoke/delete/edit paths call invalidate() so takedowns are
  immediate — "nothing leaves without your say" includes un-saying it.
- Fail-open: no Redis (tests, dev) just means live renders.
"""
import json

import redis
from flask import current_app

TTL_SECONDS = 300
_PREFIX = "public_html:"

_client = None


def _redis():
    global _client
    if _client is None:
        url = current_app.config.get("CELERY_BROKER_URL")
        if not url:
            return None
        _client = redis.Redis.from_url(
            url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return _client


def get(path):
    """Cached (status, content_type, body) for *path*, or None."""
    try:
        r = _redis()
        raw = r.get(_PREFIX + path) if r else None
    except redis.RedisError:
        return None
    if not raw:
        return None
    try:
        entry = json.loads(raw)
        return entry["status"], entry["content_type"], entry["body"]
    except (ValueError, KeyError):
        return None


def put(path, status, content_type, body, ttl=TTL_SECONDS):
    try:
        r = _redis()
        if r:
            r.setex(_PREFIX + path, ttl, json.dumps({
                "status": status,
                "content_type": content_type,
                "body": body,
            }))
    except redis.RedisError:
        pass


def invalidate(*paths):
    try:
        r = _redis()
        if r and paths:
            r.delete(*[_PREFIX + p for p in paths])
    except redis.RedisError:
        pass


def _roots_of(nodes):
    """Topmost ancestor of each node by parent chain, with one recursive
    query for the lot (privacy-blind — this is cache accounting, not
    access control). A node that is its own root, or whose root can't be
    found, maps to itself."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.thread_tree import thread_root_of

    roots = {n.id: n for n in nodes if not n.parent_id}
    pending = [n for n in nodes if n.parent_id]
    root_id_of = thread_root_of([n.id for n in pending]) if pending else {}
    missing = set(root_id_of.values()) - set(roots)
    if missing:
        # Only the columns _paths_for_root reads: a root's content can be
        # long, and this is cache accounting.
        roots.update({r.id: r for r in db.session.query(
            Node.id, Node.parent_id, Node.human_owner_id, Node.user_id,
            Node.public_slug).filter(Node.id.in_(missing)).all()})
    return [roots.get(root_id_of.get(n.id, n.id), n) for n in nodes]


def _paths_for_root(root, also=None):
    """The pages a thread root is served on: its id URL and, under its
    owner's handle, the profile, feed and (with a slug) article pages.
    *also* maps a user id to further handles the same pages are cached
    under — the old one, right after a rename."""
    from backend.models import User

    paths = [f"/node/{root.id}"]
    owner_id = root.human_owner_id or root.user_id
    owner = User.query.get(owner_id) if owner_id else None
    if owner is not None:
        for handle in [owner.username, *(also or {}).get(owner.id, [])]:
            paths.append(f"/@{handle}")
            paths.append(f"/@{handle}/feed.xml")
            if root.public_slug:
                paths.append(f"/@{handle}/{root.public_slug}")
                paths.append(f"/@{handle}/{root.public_slug}.md")
    return paths


def invalidate_in_thread(member_id, node_ids):
    """Drop every cached page that nodes of ONE thread can appear on:
    each id's own URL, and the pages of the thread root they live under
    (a reply edit/delete must refresh the cached thread page, which is
    keyed by the root). `member_id` is any node of that thread; the root
    is found with one walk up from it. Walking up from every id costs
    the sum of their depths, which is quadratic in the length of a
    public chain. Takes ids, not rows: a cascade delete passes a whole
    subtree's ids, and only the root's path columns are read."""
    from backend.extensions import db
    from backend.models import Node
    from backend.utils.thread_tree import thread_root_of

    paths = {"/sitemap.xml", *(f"/node/{int(i)}" for i in node_ids)}
    # No root found (a corrupt parent chain): use the member's own row.
    root_id = thread_root_of([member_id]).get(member_id, member_id)
    root = db.session.query(
        Node.id, Node.human_owner_id, Node.user_id, Node.public_slug,
    ).filter(Node.id == root_id).first()
    if root is not None:
        paths.update(_paths_for_root(root))
    invalidate(*paths)


def invalidate_for_node(node):
    """Drop every cached page *node* can appear on: its own id URL and
    the pages of its thread root."""
    invalidate_in_thread(node.id, [node.id])


def invalidate_deleted(target_id, deleted_ids):
    """The cache step of a committed soft-delete of `target_id`.
    `deleted_ids` are the nodes it tombstoned (the target, the
    descendants a cascade took, the session's prompt root when that was
    deleted too), all in the target's thread. The public ones must stop
    being served now, not when their cache entries expire.

    The target is asked about even when this request did not tombstone
    it (a DELETE repeated on a tombstone: a second tab, a retry). Its
    pages can still be cached then — an earlier cache step failed, or a
    render that began before the first delete stored its page after the
    drop — so every DELETE of a public node drops that node's pages.

    Which of these nodes are public is asked of the database on ids: a
    cascade can take tens of thousands of nodes, and loading their rows
    (content included) to read two columns is the whole-subtree load
    that has run staging out of memory. One IN list holds them all:
    psycopg2 binds parameters client-side, so the wire protocol's
    65,535-parameter limit does not apply (300,000 ids measured at under
    a second). A driver that binds server-side would need the list
    chunked.

    Never raises. The delete is already committed when this runs, so the
    caller answers with success whatever happens here: a failure is
    logged, the session is rolled back so later statements on it still
    work, and the TTL bounds how long a stale page outlives its node."""
    from sqlalchemy import or_

    from backend.extensions import db
    from backend.models import Node

    try:
        ids = list({target_id, *deleted_ids})
        public_ids = [nid for (nid,) in db.session.query(Node.id).filter(
            Node.id.in_(ids),
            or_(Node.public_slug.isnot(None), Node.privacy_level == "public"),
        ).all()]
        if public_ids:
            invalidate_in_thread(target_id, public_ids)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            f"Node {target_id} was deleted, but its public pages could not "
            "be dropped from the cache")


def invalidate_for_user(user, former_handle=None):
    """Drop every cached page the user's content can appear on — used
    when public_sharing_enabled flips, which takes down (or restores)
    their posts AND their replies in other people's threads at once, and
    after a rename, when the same pages sit in the cache under the old
    handle and carry it in their bylines: pass it as *former_handle* and
    its URL variants are dropped too. Reads only the columns the paths
    need — a public archive can be large, and its content is not the
    point here."""
    from backend.extensions import db
    from backend.models import Node

    handles = [user.username] + ([former_handle] if former_handle else [])
    paths = {"/sitemap.xml"}
    for handle in handles:
        paths.update({f"/@{handle}", f"/@{handle}/feed.xml"})
    also = {user.id: handles[1:]} if former_handle else None
    rows = db.session.query(
        Node.id, Node.parent_id, Node.human_owner_id, Node.user_id,
        Node.public_slug,
    ).filter(
        ((Node.human_owner_id == user.id) | (Node.user_id == user.id)),
        Node.privacy_level == "public",
    ).all()
    for node, root in zip(rows, _roots_of(rows)):
        paths.add(f"/node/{node.id}")
        paths.update(_paths_for_root(root, also))
    invalidate(*paths)
