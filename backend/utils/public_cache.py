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


def _root_of(node):
    """Topmost ancestor by parent chain (privacy-blind — this is cache
    accounting, not access control), cycle-guarded."""
    from backend.models import Node

    root, seen = node, set()
    while root.parent_id and root.id not in seen:
        seen.add(root.id)
        parent = Node.query.get(root.parent_id)
        if parent is None:
            break
        root = parent
    return root


def _paths_for_root(root):
    from backend.models import User

    paths = [f"/node/{root.id}"]
    owner_id = root.human_owner_id or root.user_id
    owner = User.query.get(owner_id) if owner_id else None
    if owner is not None:
        paths.append(f"/@{owner.username}")
        paths.append(f"/@{owner.username}/feed.xml")
        if root.public_slug:
            paths.append(f"/@{owner.username}/{root.public_slug}")
            paths.append(f"/@{owner.username}/{root.public_slug}.md")
    return paths


def invalidate_for_node(node):
    """Drop every cached page *node* can appear on: its own id URL, and
    the pages of the thread root it lives under (a reply edit/delete must
    refresh the cached thread page, which is keyed by the root)."""
    paths = {"/sitemap.xml", f"/node/{node.id}"}
    paths.update(_paths_for_root(_root_of(node)))
    invalidate(*paths)


def invalidate_for_user(user):
    """Drop every cached page the user's content can appear on — used
    when public_sharing_enabled flips, which takes down (or restores)
    their posts AND their replies in other people's threads at once."""
    from backend.models import Node

    paths = {"/sitemap.xml", f"/@{user.username}",
             f"/@{user.username}/feed.xml"}
    rows = Node.query.filter(
        ((Node.human_owner_id == user.id) | (Node.user_id == user.id)),
        Node.privacy_level == "public",
    ).all()
    for node in rows:
        paths.add(f"/node/{node.id}")
        paths.update(_paths_for_root(_root_of(node)))
    invalidate(*paths)
