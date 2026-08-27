#!/usr/bin/env python3
"""Load a `dump_user.py` file into the current environment (staging or
prod): recreates the user's nodes, embeddings, full profile chain, recent
contexts, todos and artifacts, re-encrypting content with THIS
environment's key under the current invariant (public nodes plaintext,
everything else KMS-envelope — #257).

Run INSIDE the target environment:

    # staging
    docker compose -p wop-staging cp data/rich.jsonl backend:/app/data/rich.jsonll
    docker compose -p wop-staging exec -T backend \\
        python backend/scripts/load_user.py --in /app/data/rich.jsonll
    # prod (systemd env, repo root)
    python backend/scripts/load_user.py --in data/rich.jsonl

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
    --twitter-id ID      set the account's X/Twitter numeric id so "Sign in
                         with X" claims it by id, not only by username match
                         (the Community Archive's account_id; e.g. 316970336)

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


def _open_dump(path):
    """Return (header, node_iterator, node_total). Format 2 (JSON Lines:
    header line + one node per line) streams; format 1 (single JSON
    document) is loaded whole for compatibility — fine on a workstation,
    NOT inside a memory-capped container (convert it first:
    convert_user_dump.py)."""
    with open(path, encoding="utf-8") as f:
        first = f.readline()
    if first.lstrip().startswith("{") and '"format": 2' in first[:200]:
        header = json.loads(first)
        with open(path, encoding="utf-8") as f:
            total = sum(1 for _ in f) - 1

        def _iter():
            with open(path, encoding="utf-8") as f:
                f.readline()
                for line in f:
                    line = line.strip()
                    if line:
                        yield json.loads(line)
        return header, _iter(), total
    with open(path, encoding="utf-8") as f:
        dump = json.load(f)
    if dump.get("format") != 1:
        sys.exit(f"Unsupported dump format {dump.get('format')!r}")
    print("WARNING: format-1 dump loaded whole into memory; use "
          "convert_user_dump.py for large archives", file=sys.stderr)
    return dump, iter(dump["nodes"]), len(dump["nodes"])


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


def _run(path, as_username, merge, create_approved, twitter_id=None):
    from sqlalchemy import or_
    from backend.extensions import db
    from backend.models import (
        Node, NodeEmbedding, User, UserProfile, UserRecentContext,
        UserTodo, UserArtifact,
    )

    dump, node_iter, node_total = _open_dump(path)

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

    if twitter_id:
        other = User.query.filter(User.twitter_id == str(twitter_id), User.id != user.id).first()
        if other:
            sys.exit(f"twitter_id {twitter_id} already belongs to '{other.username}'")
        user.twitter_id = str(twitter_id)
        db.session.flush()
        print(f"twitter_id set to {twitter_id}", file=sys.stderr)

    user_id = user.id  # scalar: `user` is detached after each batch expunge
    existing_keys = {}
    if merge:
        for key, nid in db.session.query(Node.source_key, Node.id).filter(
                Node.human_owner_id == user_id, Node.source_key.isnot(None)):
            existing_keys[key] = nid

    cache = {dump["user"]["username"]: user_id, username: user_id}

    # Header content (profiles/contexts/todos/artifacts) has no source_key,
    # so a --merge resume must dedup it explicitly or a rerun after a crash
    # doubles the profile chain and artifacts (happened once: a KMS 502
    # crashed a load mid-nodes, the rerun re-inserted all 18 profiles). Key
    # on created_at, which the dump preserves and which is distinct per row.
    existing_profiles, existing_rc, existing_todos, existing_arts = {}, set(), set(), set()
    if merge:
        for r in UserProfile.query.filter_by(user_id=user_id).all():
            existing_profiles[(r.created_at, r.generation_type)] = r.id
        existing_rc = {r.created_at for r in
                       UserRecentContext.query.filter_by(user_id=user_id).all()}
        existing_todos = {r.created_at for r in
                          UserTodo.query.filter_by(user_id=user_id).all()}
        existing_arts = {(r.kind, r.created_at) for r in
                         UserArtifact.query.filter_by(user_id=user_id).all()}

    # Profile chain (ids remapped so parent_profile_id stays coherent).
    profile_map = {}
    for pf in dump.get("profiles", []):
        pkey = (_dt(pf.get("created_at")), pf.get("generation_type") or "initial")
        if pkey[0] is not None and pkey in existing_profiles:
            profile_map[pf["id"]] = existing_profiles[pkey]  # map to the row already there
            continue
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
        if _dt(rc.get("created_at")) in existing_rc:
            continue
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
        if _dt(td.get("created_at")) in existing_todos:
            continue
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
        if (a["kind"], _dt(a.get("created_at"))) in existing_arts:
            continue
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

    # Nodes last, so context pins on system nodes resolve to the rows above.
    from backend.models import NodeContextArtifact, UserPrompt
    from backend.utils.context_artifacts import sync_context_artifacts
    prompt_cache = {}
    pinned = 0

    def _prompt_row(ref):
        key = (ref["prompt_key"], ref.get("content") or "")
        if key in prompt_cache:
            return prompt_cache[key]
        row = None
        for cand in UserPrompt.query.filter_by(user_id=user_id, prompt_key=ref["prompt_key"]) \
                                    .order_by(UserPrompt.created_at.desc()).all():
            if (cand.get_content() or "") == key[1]:
                row = cand
                break
        if row is None:
            row = UserPrompt(user_id=user_id, prompt_key=ref["prompt_key"],
                             title=ref.get("title") or ref["prompt_key"],
                             generated_by=ref.get("generated_by") or "import")
            row.set_content(key[1])
            if _dt(ref.get("created_at")):
                row.created_at = _dt(ref["created_at"])
            db.session.add(row)
            db.session.flush()
        prompt_cache[key] = row.id
        return row.id

    nodes = node_iter
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
        if nd.get("prompt"):
            db.session.add(NodeContextArtifact(
                node_id=new.id, artifact_type="prompt",
                artifact_id=_prompt_row(nd["prompt"])))
            sync_context_artifacts(new.id, user_id, nd["prompt"].get("content") or "")
            pinned += 1
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
            print(f"  nodes {i}/{node_total} (created {created}, skipped {skipped})",
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

    print(f"loaded '{username}' (id {user_id}): {created} nodes created, "
          f"{skipped} deduped, {relinked} relinked, {pinned} prompt-pinned, "
          f"{len(profile_map)} profiles, {len(dump.get('recent_contexts', []))} recent "
          f"contexts, {len(dump.get('todos', []))} todos, {len(dump.get('artifacts', []))} artifacts")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--in", dest="path", required=True)
    p.add_argument("--as-username", default=None)
    p.add_argument("--merge", action="store_true")
    p.add_argument("--create-approved", action="store_true")
    p.add_argument("--twitter-id", default=None)
    args = p.parse_args()
    from backend import create_app
    app = create_app()
    with app.app_context():
        _run(args.path, args.as_username, args.merge, args.create_approved,
             twitter_id=args.twitter_id)


if __name__ == "__main__":
    main()
