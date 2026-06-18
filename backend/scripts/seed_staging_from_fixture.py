#!/usr/bin/env python3
"""Seed the staging DB from a prod-exported fixture (last-N-threads + their
embeddings) so semantic search can be evaluated on staging (#155/#197).

Run INSIDE the staging backend container — it has the staging DB (via the
Docker network) and the staging encryption config — after `flask init-db` and
the admin seed. Copy the fixture in first, e.g.:

    docker compose -p wop-staging cp data/staging_fixture.json \
        backend:/app/staging_fixture.json
    docker compose -p wop-staging exec -T backend \
        python backend/scripts/seed_staging_from_fixture.py \
        --in /app/staging_fixture.json

Re-attributes the fixture owner to staging's 'hrosspet' admin, recreates any
per-model LLM author users by username, remaps node ids (parent +
continuation links resolved in a second pass), RE-ENCRYPTS content with the
staging key, and inserts the NodeEmbedding vectors. Refuses to double-seed
(reset the staging DB first — it's ephemeral anyway).
"""
import argparse
import base64
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.extensions import db
from backend.models import (
    Node, NodeEmbedding, User, UserProfile, UserTodo, UserArtifact,
)
from backend.utils.tokens import approximate_token_count


def _user_id(username, owner_id, cache):
    """Resolve a username to a staging user id, creating a minimal user (e.g.
    an LLM author like 'claude-opus-4.6') if it doesn't exist yet."""
    if not username:
        return owner_id
    if username in cache:
        return cache[username]
    u = User.query.filter_by(username=username).first()
    if u is None:
        u = User(username=username)  # username is the only required field
        db.session.add(u)
        db.session.flush()
    cache[username] = u.id
    return u.id


def run_seed(in_path):
    with open(in_path) as f:
        fixture = json.load(f)

    owner = User.query.filter_by(
        username=fixture["owner_username"]).first()
    if owner is None:
        print(f"Owner '{fixture['owner_username']}' not on staging — seed the "
              f"admin user first.")
        return
    if Node.query.filter_by(human_owner_id=owner.id).count():
        print("Owner already has nodes; refusing to double-seed. Reset the "
              "staging DB and re-run.")
        return

    nodes = fixture["nodes"]
    user_cache = {fixture["owner_username"]: owner.id}

    # Pass 1: insert every node (content re-encrypted with the staging key);
    # parent/continuation links are wired in pass 2 once all ids are mapped.
    id_map = {}
    created = {}
    for nd in nodes:
        content = nd.get("content") or ""
        # token_count MUST be populated: build_user_export_content's budget
        # windowing (the 10k cap behind {user_recent_raw}, etc.) sums it, so
        # zero-everywhere makes recent context pull the entire archive and
        # blow the context window. Prefer the faithful prod value; recompute
        # for older fixtures that predate the export carrying it (chars/4 is
        # deterministic, so this matches what prod stored).
        token_count = nd.get("token_count")
        if token_count is None:
            token_count = approximate_token_count(content)
        new = Node(
            user_id=_user_id(nd.get("author_username"), owner.id, user_cache),
            human_owner_id=owner.id,
            node_type=nd.get("node_type", "user"),
            llm_model=nd.get("llm_model"),
            ai_usage=nd.get("ai_usage", "chat"),
            privacy_level=nd.get("privacy_level", "private"),
            tool_calls_meta=nd.get("tool_calls_meta"),
            token_count=token_count,
        )
        new.set_content(content)
        if nd.get("created_at"):
            try:
                new.created_at = datetime.fromisoformat(nd["created_at"])
            except ValueError:
                pass
        db.session.add(new)
        db.session.flush()
        id_map[nd["id"]] = new.id
        created[nd["id"]] = new

    # Pass 2: remap parent_id + continuation_node_id.
    for nd in nodes:
        new = created[nd["id"]]
        if nd.get("parent_id") in id_map:
            new.parent_id = id_map[nd["parent_id"]]
        if nd.get("continuation_node_id") in id_map:
            new.continuation_node_id = id_map[nd["continuation_node_id"]]
    db.session.commit()

    # Embeddings — re-insert the prod-computed vectors (no OpenAI call here),
    # attributed to the staging owner so semantic search finds them.
    emb_count = 0
    for nd in nodes:
        if not nd.get("embedding"):
            continue
        db.session.add(NodeEmbedding(
            node_id=id_map[nd["id"]],
            user_id=owner.id,
            model=nd.get("embedding_model") or "text-embedding-3-small",
            content_hash=nd.get("content_hash") or "",
            vector=base64.b64decode(nd["embedding"]),
        ))
        emb_count += 1
    db.session.commit()

    # Current profile / todo / AI preferences → the staging owner, so the
    # agentic context is faithful. Re-encrypted with the staging key.
    docs = fixture.get("docs") or {}
    seeded_docs = []
    pf = docs.get("profile")
    if pf:
        row = UserProfile(
            user_id=owner.id, generated_by=pf.get("generated_by", "import"),
            ai_usage=pf.get("ai_usage", "chat"),
            privacy_level=pf.get("privacy_level", "private"))
        row.set_content(pf.get("content") or "")
        db.session.add(row)
        seeded_docs.append("profile")
    td = docs.get("todo")
    if td:
        row = UserTodo(
            user_id=owner.id, generated_by=td.get("generated_by", "import"),
            ai_usage=td.get("ai_usage", "chat"),
            privacy_level=td.get("privacy_level", "private"))
        row.set_content(td.get("content") or "")
        db.session.add(row)
        seeded_docs.append("todo")
    ap = docs.get("ai_preferences")
    if ap:
        row = UserArtifact(
            user_id=owner.id, kind="ai_preferences",
            title=ap.get("title") or "AI Interaction Preferences",
            description=ap.get("description"),
            generated_by=ap.get("generated_by", "import"),
            ai_usage=ap.get("ai_usage", "chat"),
            privacy_level=ap.get("privacy_level", "private"))
        row.set_content(ap.get("content") or "")
        db.session.add(row)
        seeded_docs.append("ai_preferences")
    db.session.commit()

    print(f"Seeded {len(nodes)} nodes + {emb_count} embeddings for "
          f"'{fixture['owner_username']}'"
          + (f"; docs: {', '.join(seeded_docs)}" if seeded_docs else "")
          + ".")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--in", dest="in_path", default="staging_fixture.json",
                   help="fixture path (inside the container)")
    args = p.parse_args()
    from backend.app import create_app
    app = create_app()
    with app.app_context():
        run_seed(args.in_path)


if __name__ == "__main__":
    main()
