#!/usr/bin/env python3
"""Load a `dump_user.py` file into the current environment (staging or
prod): recreates the user's nodes, embeddings, full profile chain, recent
contexts, todos and artifacts, re-encrypting content with THIS
environment's key under the current invariant (public nodes plaintext,
everything else KMS-envelope — #257).

Run INSIDE the target environment:

    # staging
    docker compose -p wop-staging cp data/rich.json backend:/app/data/rich.json
    docker compose -p wop-staging exec -T backend \\
        python backend/scripts/load_user.py --in /app/data/rich.json
    # prod (systemd env, repo root)
    python backend/scripts/load_user.py --in data/rich.json

Options:
    --as-username NAME   load onto this username instead of the dumped one
    --merge              allow loading onto a user that already has nodes;
                         nodes are deduplicated on source_key (a re-import
                         of the same archive skips existing tweets), the
                         profile chain / contexts / todos / artifacts are
                         appended. Without --merge a non-empty user aborts.
    --create-approved    when the user has to be created, set approved=True
                         (default: created unapproved — the same gate a
                         fresh signup gets; flip it in the admin page)

Ids are remapped in two passes (nodes: parent + continuation links;
profiles: parent_profile_id; recent contexts: profile_id). LLM author
users (e.g. 'claude-opus-5') are created by username if missing. The
schema is checked up front: the columns this file needs must exist here.
Everything is one transaction per batch of 500 nodes; a failure rolls
back the current batch and stops.
"""
import argparse
import base64
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

NODE_BATCH = 500


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _check_schema(db):
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    need = {
        "node": {"origin", "source_key", "continuation_node_id", "tool_calls_meta"},
        "user_profile": {"parent_profile_id", "generation_type", "source_data_cutoff",
                         "source_origin_stats", "source_tokens_used"},
        "user_recent_context": {"profile_id", "source_data_cutoff"},
    }
    for table, cols in need.items():
        have = {c["name"] for c in insp.get_columns(table)}
        missing = cols - have
        if missing:
            sys.exit(f"Schema mismatch: table '{table}' lacks {sorted(missing)} — "
                     f"deploy the matching code first.")


def _author_id(username, owner_id, cache, db, User):
    if not username:
        return owner_id
    if username in cache:
        return cache[username]
    u = User.query.filter_by(username=username).first()
    if not u:
        u = User(username=username)
        db.session.add(u)
        db.session.flush()
        print(f"  created author user '{username}' (id {u.id})", file=sys.stderr)
    cache[username] = u.id
    return u.id


def _run(path, as_username, merge, create_approved):
    from sqlalchemy import or_
    from backend.extensions import db
    from backend.models import (
        Node, NodeEmbedding, User, UserProfile, UserRecentContext,
        UserTodo, UserArtifact,
    )

    with open(path, encoding="utf-8") as f:
        dump = json.load(f)
    if dump.get("format") != 1:
        sys.exit(f"Unsupported dump format {dump.get('format')!r}")

    _check_schema(db)

    username = as_username or dump["user"]["username"]
    user = User.query.filter_by(username=username).first()
    if user is None:
        u = dump["user"]
        user = User(username=username, description=u.get("description") or "",
                    preferred_model=u.get("preferred_model"),
                    plan=u.get("plan") or "alpha", approved=bool(create_approved))
        if u.get("timezone") and hasattr(user, "timezone"):
            user.timezone = u["timezone"]
        db.session.add(user)
        db.session.flush()
        print(f"created user '{username}' (id {user.id}, approved={user.approved})",
              file=sys.stderr)
    else:
        existing = Node.query.filter(
            or_(Node.user_id == user.id, Node.human_owner_id == user.id),
            Node.deleted_at.is_(None)).count()
        if existing and not merge:
            sys.exit(f"User '{username}' already has {existing} nodes here — "
                     f"pass --merge to add to them (dedup on source_key).")
        print(f"loading onto existing user '{username}' (id {user.id}, "
              f"{existing} nodes, merge={merge})", file=sys.stderr)

    user_id = user.id  # scalar: `user` is detached after each batch expunge
    existing_keys = {}
    if merge:
        for key, nid in db.session.query(Node.source_key, Node.id).filter(
                Node.human_owner_id == user_id, Node.source_key.isnot(None)):
            existing_keys[key] = nid

    cache = {dump["user"]["username"]: user_id, username: user_id}
    nodes = dump["nodes"]
    id_map = {}
    created = 0
    skipped = 0
    pending_links = []

    # Pass 1: rows, in batches. Content re-encrypted by set_content()
    # according to the node's privacy level (public stays plaintext).
    for i, nd in enumerate(nodes, 1):
        key = nd.get("source_key")
        if key and key in existing_keys:
            id_map[nd["id"]] = existing_keys[key]
            skipped += 1
            continue
        new = Node(
            user_id=_author_id(nd.get("author_username"), user_id, cache, db, User),
            human_owner_id=user_id if nd.get("owner_is_user", True) else None,
            node_type=nd.get("node_type", "user"),
            llm_model=nd.get("llm_model"),
            token_count=nd.get("token_count") or 0,
            distributed_tokens=nd.get("distributed_tokens") or 0,
            privacy_level=nd.get("privacy_level", "private"),
            ai_usage=nd.get("ai_usage", "none"),
            source_key=key,
            origin=nd.get("origin"),
            tool_calls_meta=nd.get("tool_calls_meta"),
        )
        new.set_content(nd.get("content") or "")
        if _dt(nd.get("created_at")):
            new.created_at = _dt(nd["created_at"])
        if _dt(nd.get("updated_at")):
            new.updated_at = _dt(nd["updated_at"])
        db.session.add(new)
        db.session.flush()
        id_map[nd["id"]] = new.id
        created += 1
        if nd.get("parent_id") or nd.get("continuation_node_id") or nd.get("linked_node_id"):
            pending_links.append((new.id, nd))
        if nd.get("embedding"):
            e = nd["embedding"]
            db.session.add(NodeEmbedding(
                node_id=new.id, user_id=user_id, model=e["model"],
                content_hash=e.get("content_hash") or "",
                vector=base64.b64decode(e["vector"]),
                node_updated_at=_dt(e.get("node_updated_at")),
            ))
        if created % NODE_BATCH == 0:
            db.session.commit()
            db.session.expunge_all()
            print(f"  nodes {i}/{len(nodes)} (created {created}, skipped {skipped})",
                  file=sys.stderr)
    db.session.commit()

    # Pass 2: links, via bulk UPDATEs keyed by the new ids.
    relinked = 0
    for new_id, nd in pending_links:
        values = {}
        for col in ("parent_id", "continuation_node_id", "linked_node_id"):
            old = nd.get(col)
            if old in id_map:
                values[col] = id_map[old]
        if values:
            db.session.query(Node).filter(Node.id == new_id).update(
                values, synchronize_session=False)
            relinked += 1
    db.session.commit()

    # Profile chain (ids remapped so parent_profile_id stays coherent).
    profile_map = {}
    for pf in dump.get("profiles", []):
        row = UserProfile(
            user_id=user_id, generated_by=pf.get("generated_by") or "import",
            tokens_used=pf.get("tokens_used") or 0,
            privacy_level=pf.get("privacy_level") or "private",
            ai_usage=pf.get("ai_usage") or "chat",
            source_tokens_used=pf.get("source_tokens_used") or 0,
            source_data_cutoff=_dt(pf.get("source_data_cutoff")),
            source_origin_stats=pf.get("source_origin_stats"),
            generation_type=pf.get("generation_type") or "initial",
        )
        row.set_content(pf.get("content") or "")
        if _dt(pf.get("created_at")):
            row.created_at = _dt(pf["created_at"])
        db.session.add(row)
        db.session.flush()
        profile_map[pf["id"]] = row.id
    for pf in dump.get("profiles", []):
        parent = pf.get("parent_profile_id")
        if parent in profile_map:
            db.session.query(UserProfile).filter(
                UserProfile.id == profile_map[pf["id"]]).update(
                {"parent_profile_id": profile_map[parent]}, synchronize_session=False)
    db.session.commit()

    for rc in dump.get("recent_contexts", []):
        row = UserRecentContext(
            user_id=user_id, generated_by=rc.get("generated_by") or "import",
            tokens_used=rc.get("tokens_used") or 0,
            source_data_cutoff=_dt(rc.get("source_data_cutoff")),
            source_tokens_covered=rc.get("source_tokens_covered") or 0,
            profile_id=profile_map.get(rc.get("profile_id")),
            ai_usage=rc.get("ai_usage") or "chat",
        )
        row.set_content(rc.get("content") or "")
        if _dt(rc.get("created_at")):
            row.created_at = _dt(rc["created_at"])
        db.session.add(row)
    for td in dump.get("todos", []):
        row = UserTodo(
            user_id=user_id, generated_by=td.get("generated_by") or "import",
            tokens_used=td.get("tokens_used") or 0,
            privacy_level=td.get("privacy_level") or "private",
            ai_usage=td.get("ai_usage") or "chat",
        )
        row.set_content(td.get("content") or "")
        if _dt(td.get("created_at")):
            row.created_at = _dt(td["created_at"])
        db.session.add(row)
    for a in dump.get("artifacts", []):
        row = UserArtifact(
            user_id=user_id, kind=a["kind"], title=a.get("title") or a["kind"],
            description=a.get("description"),
            generated_by=a.get("generated_by") or "import",
            tokens_used=a.get("tokens_used") or 0,
            privacy_level=a.get("privacy_level") or "private",
            ai_usage=a.get("ai_usage") or "chat",
        )
        row.set_content(a.get("content") or "")
        if _dt(a.get("created_at")):
            row.created_at = _dt(a["created_at"])
        db.session.add(row)
    db.session.commit()

    print(f"loaded '{username}' (id {user_id}): {created} nodes created, "
          f"{skipped} deduped, {relinked} relinked, "
          f"{len(profile_map)} profiles, {len(dump.get('recent_contexts', []))} recent "
          f"contexts, {len(dump.get('todos', []))} todos, {len(dump.get('artifacts', []))} artifacts")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--in", dest="path", required=True)
    p.add_argument("--as-username", default=None)
    p.add_argument("--merge", action="store_true")
    p.add_argument("--create-approved", action="store_true")
    args = p.parse_args()
    from backend import create_app
    app = create_app()
    with app.app_context():
        _run(args.path, args.as_username, args.merge, args.create_approved)


if __name__ == "__main__":
    main()
