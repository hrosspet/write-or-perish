#!/usr/bin/env python3
"""Dump one user's Loore data to a portable JSON file, so it can be loaded
into another environment (staging <-> prod) with `load_user.py`.

Why: profile generation over a big imported corpus is slow and costs real
money (a 60k-tweet archive = 15-30 iterative profile versions). Staging's
DB is wiped on every deploy, so anything built there must be dumped to
survive — and the same file can seed prod once the schema matches.

Run INSIDE the environment that holds the data, so create_app() picks up
that environment's DB and KMS key:

    # staging
    docker compose -p wop-staging exec -T backend \\
        python backend/scripts/dump_user.py --username RichDecibels --out /app/data/rich.jsonl
    docker compose -p wop-staging cp backend:/app/data/rich.jsonl data/rich.jsonl
    # prod (systemd env, repo root)
    python backend/scripts/dump_user.py --username RichDecibels --out data/rich.jsonl

What it carries (all of it, not "latest N"):
  - user: username, description, timezone, preferred_model, plan (flags like
    approved/is_admin are NOT carried — the target decides those)
  - nodes: every alive PUBLIC node the user authored or owns (imported
    archives, published writing, LLM replies in those threads) with parent /
    continuation links,
    origin, source_key, privacy, ai_usage, timestamps, token counts,
    tool_calls_meta; LLM authors are referenced by username
  - node embeddings (vectors, so no re-embedding on load)
  - user_profile: the FULL iterative chain with every column, incl.
    parent_profile_id, generation_type, source_data_cutoff, origin stats
  - user_recent_context (linked to its profile), user_todo, user_artifact
  - the prompt pinned on each system node (by value), so agentic threads
    load as prompt references, not as copies of the prompt text

NOT carried: audio files / TTS artifacts, node versions (edit history),
drafts, per-node context-artifact pins, deleted nodes, cost logs.

Public-only by default: non-public nodes are skipped entirely and public
content is copied as stored (plaintext under #257 — no KMS calls; a legacy
encrypted public row is decrypted). Pass --include-private to dump every
node, decrypting as needed. Profiles / recent contexts / todos / artifacts
are always decrypted (they are never stored plaintext).

PRIVACY: the file holds DECRYPTED profile text (and private nodes with
--include-private); each environment has its own KMS key, so ciphertext
cannot move. Keep it under data/ (gitignored) on the VM, never commit it,
delete it when done.
"""
import argparse
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

FORMAT_VERSION = 2  # JSON Lines: header line, then one node per line
NODE_BATCH = 1000


def _iso(dt):
    return dt.isoformat() if dt else None


def _run(username, out_path, include_private=False):
    from sqlalchemy import or_
    from backend.extensions import db
    from backend.models import (
        Node, NodeEmbedding, User, UserProfile, UserRecentContext,
        UserTodo, UserArtifact,
    )
    from backend.utils.encryption import is_encrypted, decrypt_content

    user = User.query.filter_by(username=username).first()
    if not user:
        sys.exit(f"User '{username}' not found")

    authors = {u.id: u.username for u in User.query.all()}

    node_q = Node.query.filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        Node.deleted_at.is_(None),
    )
    if not include_private:
        node_q = node_q.filter(Node.privacy_level == "public")
    node_q = node_q.order_by(Node.id)
    total = node_q.count()
    print(f"@{user.username} (id {user.id}): {total} nodes "
          f"({'all' if include_private else 'public only'})", file=sys.stderr)

    embeddings = {}
    for e in NodeEmbedding.query.filter_by(user_id=user.id).all():
        embeddings[e.node_id] = {
            "model": e.model, "content_hash": e.content_hash,
            "vector": base64.b64encode(e.vector).decode("ascii"),
            "node_updated_at": _iso(e.node_updated_at),
        }

    # Everything small goes in the header line; nodes stream one per line
    # so neither this script nor load_user.py ever holds the corpus in
    # memory (a 61k-node dump json.load()ed into a 512 MB container was
    # OOM-killed silently).
    header = {
        "format": FORMAT_VERSION,
        "source_env": os.environ.get("FLASK_ENV") or os.environ.get("ENV") or "unknown",
        "user": {
            "username": user.username,
            "description": user.description,
            "timezone": getattr(user, "timezone", None),
            "preferred_model": user.preferred_model,
            "plan": user.plan,
        },
        "profiles": _profiles(user, UserProfile),
        "recent_contexts": _recent(user, UserRecentContext),
        "todos": _todos(user, UserTodo),
        "artifacts": _artifacts(user, UserArtifact),
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out = open(out_path, "w", encoding="utf-8")
    out.write(json.dumps(header, ensure_ascii=False) + "\n")

    n_nodes = 0
    n_emb = 0
    last_id = 0
    done = 0
    while True:
        batch = (node_q.filter(Node.id > last_id).limit(NODE_BATCH).all())
        if not batch:
            break
        for n in batch:
            row = {
                "id": n.id,
                "author_username": authors.get(n.user_id),
                "owner_is_user": n.human_owner_id == user.id,
                "parent_id": n.parent_id,
                "continuation_node_id": n.continuation_node_id,
                "linked_node_id": n.linked_node_id,
                "node_type": n.node_type,
                "llm_model": n.llm_model,
                # Raw stored text, NOT get_content(): for a system node that
                # resolves to the pinned prompt, which would flatten the
                # reference into 17k of copied prompt on load. Public rows
                # are plaintext under #257 (no KMS round-trip).
                "content": (n.content if not is_encrypted(n.content)
                            else decrypt_content(n.content)),
                "prompt": _prompt_ref(n),
                "token_count": n.token_count,
                "distributed_tokens": n.distributed_tokens,
                "privacy_level": n.privacy_level,
                "ai_usage": n.ai_usage,
                "source_key": n.source_key,
                "origin": n.origin,
                "tool_calls_meta": n.tool_calls_meta,
                "created_at": _iso(n.created_at),
                "updated_at": _iso(n.updated_at),
                "embedding": embeddings.get(n.id),
            }
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            n_nodes += 1
            n_emb += 1 if row["embedding"] else 0
            last_id = n.id
        done += len(batch)
        db.session.expunge_all()  # keep memory flat
        print(f"  nodes {done}/{total}", file=sys.stderr)
    out.close()
    h = header
    print(f"wrote {out_path}: {n_nodes} nodes, {n_emb} embeddings, "
          f"{len(h['profiles'])} profiles, {len(h['recent_contexts'])} recent contexts, "
          f"{len(h['todos'])} todos, {len(h['artifacts'])} artifacts "
          f"({os.path.getsize(out_path) // 1024} KB)")


def _prompt_ref(node):
    """The pinned prompt (system nodes), carried by value so the loader can
    recreate the row for the target user and re-pin it."""
    prompt = node.get_artifact("prompt") if node.has_artifact("prompt") else None
    if prompt is None:
        return None
    return {
        "prompt_key": prompt.prompt_key, "title": prompt.title,
        "content": prompt.get_content(), "generated_by": prompt.generated_by,
        "created_at": _iso(prompt.created_at),
    }


def _profiles(user, UserProfile):
    return [{
        "id": p.id, "content": p.get_content(), "generated_by": p.generated_by,
        "tokens_used": p.tokens_used, "created_at": _iso(p.created_at),
        "privacy_level": p.privacy_level, "ai_usage": p.ai_usage,
        "source_tokens_used": p.source_tokens_used,
        "source_data_cutoff": _iso(p.source_data_cutoff),
        "source_origin_stats": p.source_origin_stats,
        "generation_type": p.generation_type,
        "parent_profile_id": p.parent_profile_id,
    } for p in UserProfile.query.filter_by(user_id=user.id).order_by(UserProfile.id).all()]


def _recent(user, UserRecentContext):
    return [{
        "content": r.get_content(), "generated_by": r.generated_by,
        "tokens_used": r.tokens_used, "created_at": _iso(r.created_at),
        "source_data_cutoff": _iso(r.source_data_cutoff),
        "source_tokens_covered": r.source_tokens_covered,
        "profile_id": r.profile_id, "ai_usage": r.ai_usage,
    } for r in UserRecentContext.query.filter_by(user_id=user.id).order_by(UserRecentContext.id).all()]


def _todos(user, UserTodo):
    return [{
        "content": t.get_content(), "generated_by": t.generated_by,
        "tokens_used": t.tokens_used, "created_at": _iso(t.created_at),
        "privacy_level": t.privacy_level, "ai_usage": t.ai_usage,
    } for t in UserTodo.query.filter_by(user_id=user.id).order_by(UserTodo.id).all()]


def _artifacts(user, UserArtifact):
    return [{
        "kind": a.kind, "title": a.title, "description": a.description,
        "content": a.get_content(), "generated_by": a.generated_by,
        "tokens_used": a.tokens_used, "created_at": _iso(a.created_at),
        "privacy_level": a.privacy_level, "ai_usage": a.ai_usage,
    } for a in UserArtifact.query.filter_by(user_id=user.id).order_by(UserArtifact.id).all()]


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--username", required=True)
    p.add_argument("--out", required=True, help="output JSON path (keep under data/)")
    p.add_argument("--include-private", action="store_true",
                   help="also dump non-public nodes (decrypted)")
    args = p.parse_args()
    from backend import create_app
    app = create_app()
    with app.app_context():
        _run(args.username, args.out, include_private=args.include_private)


if __name__ == "__main__":
    main()
