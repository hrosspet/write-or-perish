#!/usr/bin/env python3
"""Export the admin's last N threads (+ embeddings) to a local JSON fixture
for seeding staging — so semantic-search quality can be evaluated on staging
across repeated redeploys WITHOUT re-pulling/re-embedding from prod (#155/#197).

Run on the PROD VM (create_app loads .env.production → prod DB, KMS key, and
OpenAI key for embedding), from the repo root:

    python backend/scripts/export_threads_for_staging.py --n 10

Idempotent on N: if the output already covers the same N, it skips — so it's
free to re-run before each staging re-seed.

PRIVACY: the fixture holds DECRYPTED node content. It defaults to data/ (which
is gitignored) — never commit it, it stays VM-local, delete it after the eval.
"""
import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from backend.extensions import db
from backend.models import (
    Node, User, UserProfile, UserTodo, UserArtifact,
)
from backend.utils.privacy import AI_ALLOWED

DEFAULT_USERNAME = "hrosspet"
EMBED_API_BATCH = 32


def _collect_subtree_ids(root_id):
    """All node ids in the subtree rooted at root_id (BFS over parent_id)."""
    ids = [root_id]
    frontier = [root_id]
    while frontier:
        rows = db.session.query(Node.id).filter(
            Node.parent_id.in_(frontier)).all()
        child_ids = [r[0] for r in rows]
        ids.extend(child_ids)
        frontier = child_ids
    return ids


def run_export(n, out_path):
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                if json.load(f).get("n") == n:
                    print(f"{out_path} already covers n={n}; skipping "
                          f"(delete it to force a refresh).")
                    return
        except Exception:
            pass

    user = User.query.filter_by(username=DEFAULT_USERNAME).first()
    if not user:
        print(f"No user '{DEFAULT_USERNAME}' found.")
        return

    # Last N threads = the N most recently created root nodes owned by the user.
    roots = (
        Node.query
        .filter(Node.parent_id.is_(None), Node.deleted_at.is_(None),
                db.or_(Node.human_owner_id == user.id,
                       Node.user_id == user.id))
        .order_by(Node.created_at.desc())
        .limit(n).all()
    )
    node_ids = []
    for r in roots:
        node_ids.extend(_collect_subtree_ids(r.id))
    node_ids = list(dict.fromkeys(node_ids))  # dedupe, preserve order

    nodes = Node.query.filter(
        Node.id.in_(node_ids), Node.deleted_at.is_(None)).all()
    print(f"{len(roots)} threads → {len(nodes)} nodes")

    # Map author user ids → usernames (so the seed can re-attribute LLM nodes).
    author_ids = {nd.user_id for nd in nodes}
    author_ids |= {nd.human_owner_id for nd in nodes if nd.human_owner_id}
    authors = {
        u.id: u.username
        for u in User.query.filter(User.id.in_(author_ids)).all()
    }

    # Embed the AI-readable nodes (prod OpenAI key); store packed vectors.
    from flask import current_app
    from backend.utils.api_keys import get_openai_chat_key
    from backend.utils.embeddings import (
        embed_texts, pack_vector, content_hash, EMBEDDING_MODEL,
    )
    key = get_openai_chat_key(current_app.config)
    emb = {}  # node_id -> (b64 vector, content_hash)
    if key:
        targets = [nd for nd in nodes
                   if nd.ai_usage in AI_ALLOWED
                   and (nd.get_content() or "").strip()]
        for i in range(0, len(targets), EMBED_API_BATCH):
            chunk = targets[i:i + EMBED_API_BATCH]
            texts = [nd.get_content() for nd in chunk]
            vectors = embed_texts(texts, key, user_id=user.id)
            for nd, vec in zip(chunk, vectors):
                emb[nd.id] = (
                    base64.b64encode(pack_vector(vec)).decode("ascii"),
                    content_hash(nd.get_content()),
                )
        db.session.commit()  # persist the embedding cost logs
        print(f"Embedded {len(emb)} nodes with {EMBEDDING_MODEL}")
    else:
        print("WARNING: no OpenAI key — exporting without embeddings.")

    out_nodes = []
    for nd in nodes:
        vec_b64, chash = emb.get(nd.id, (None, None))
        out_nodes.append({
            "id": nd.id,
            "parent_id": nd.parent_id,
            "continuation_node_id": nd.continuation_node_id,
            "author_username": authors.get(nd.user_id),
            "node_type": nd.node_type,
            "llm_model": nd.llm_model,
            "ai_usage": nd.ai_usage,
            "privacy_level": nd.privacy_level,
            "created_at": nd.created_at.isoformat() if nd.created_at else None,
            "tool_calls_meta": nd.tool_calls_meta,
            "content": nd.get_content(),
            # Carry the real prod token_count: the export builder's budget
            # windowing (e.g. {user_recent_raw}'s 10k cap) sums it, so a
            # zero/missing value makes recent-context pull the WHOLE archive.
            "token_count": nd.token_count,
            "embedding": vec_b64,
            "embedding_model": EMBEDDING_MODEL if vec_b64 else None,
            "content_hash": chash,
        })

    # The user's current profile / todo / AI preferences, so the agentic
    # context on staging is faithful (latest version of each; not full
    # history — that's not needed for the eval).
    def _doc(row, extra=None):
        if row is None:
            return None
        d = {
            "content": row.get_content(),
            "generated_by": getattr(row, "generated_by", "import"),
            "ai_usage": getattr(row, "ai_usage", "chat"),
            "privacy_level": getattr(row, "privacy_level", "private"),
        }
        if extra:
            d.update(extra)
        return d

    profile = (UserProfile.query.filter_by(user_id=user.id)
               .order_by(UserProfile.created_at.desc()).first())
    todo = (UserTodo.query.filter_by(user_id=user.id)
            .order_by(UserTodo.created_at.desc()).first())
    ai_prefs = UserArtifact.latest_for(user.id, "ai_preferences")
    docs = {
        "profile": _doc(profile),
        "todo": _doc(todo),
        "ai_preferences": _doc(ai_prefs, {
            "title": ai_prefs.title, "description": ai_prefs.description,
        }) if ai_prefs else None,
    }
    print("Also exporting: "
          + ", ".join(k for k, v in docs.items() if v) or "(none)")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"n": n, "owner_username": user.username,
                   "nodes": out_nodes, "docs": docs}, f)
    print(f"Wrote {len(out_nodes)} nodes ({len(emb)} embedded) → {out_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=10,
                   help="number of most-recent threads to export")
    p.add_argument("--out", default="data/staging_fixture.json",
                   help="fixture path (data/ is gitignored)")
    args = p.parse_args()
    from backend.app import create_app
    app = create_app()
    with app.app_context():
        run_export(args.n, args.out)


if __name__ == "__main__":
    main()
