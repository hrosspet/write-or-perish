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
                         On an existing account whose current intentions
                         the user wrote (not an inferred version), the
                         dump's intentions are skipped (#412). An existing
                         account whose owner said no to starting from
                         their tweets is refused (#662).
    --create-approved    when the user has to be created, set approved=True
                         (default: created unapproved — the same gate a
                         fresh signup gets; flip it in the admin page)
    --twitter-id ID      set the account's X/Twitter numeric id so "Sign in
                         with X" claims it by id, not only by username match
                         (the Community Archive's account_id; e.g. 316970336)

A pre-fill bundle (#662): each entry keeps its provenance, so after a no
to "Start from your tweets" the pre-filled tweets are told apart from the
person's own entries, and the account keeps the dump's prefilled_handle
when it is the account's username (case aside; a different one is left
unset and reported, since Glean leaves out the tweets of the account's
pre-filled handle). The account is marked as one whose writing is private
for the choice of profile models (User.profile_private_read), as a loaded
account was before provenance was carried, so no profile build of it
crosses providers on its own (#589's public-only rule).

Ids are remapped in two passes (nodes: parent + continuation links;
profiles: parent_profile_id; recent contexts: profile_id). LLM author
users (e.g. 'claude-opus-5') are created by username if missing. The
schema is checked up front: the columns this file needs must exist here.
Everything is one transaction per batch of 500 nodes; a failure rolls
back the current batch and stops.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

NODE_BATCH = 500


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
    from backend.models import Node, User

    dump, node_iter, node_total = _open_dump(path)

    _check_schema(db)

    username = as_username or dump["user"]["username"]
    user = User.query.filter_by(username=username).first()
    if user is None:
        u = dump["user"]
        # A dump made before #538 may carry the user's "description": the
        # field is gone, so it is not loaded.
        user = User(username=username,
                    preferred_model=u.get("preferred_model"),
                    plan=u.get("plan") or "alpha", approved=bool(create_approved))
        if u.get("timezone") and hasattr(user, "timezone"):
            user.timezone = u["timezone"]
        db.session.add(user)
        db.session.flush()
        print(f"created user '{username}' (id {user.id}, approved={user.approved})",
              file=sys.stderr)
    else:
        if user.prefill_consent == "no":
            # A dump loaded onto an account is a pre-fill (a lab bundle of
            # tweets, profile and intentions): its owner said no (#662).
            sys.exit(f"User '{username}' said no to starting from their "
                     f"tweets (pre-fill consent: no); nothing loaded.")
        existing = Node.query.filter(
            or_(Node.user_id == user.id, Node.human_owner_id == user.id),
            Node.deleted_at.is_(None)).count()
        if existing and not merge:
            sys.exit(f"User '{username}' already has {existing} nodes here — "
                     f"pass --merge to add to them (dedup on source_key).")
        print(f"loading onto existing user '{username}' (id {user.id}, "
              f"{existing} nodes, merge={merge})", file=sys.stderr)

    handle = (dump["user"].get("prefilled_handle") or "").strip() or None
    if handle and not user.prefilled_handle:
        if handle.lower() == username.lower():
            user.prefilled_handle = handle
        else:
            print(f"prefilled_handle @{handle} is not the username '{username}': "
                  f"left unset", file=sys.stderr)
    # The provider rule as before provenance was carried (see above).
    user.profile_private_read = True
    db.session.flush()

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

    # Profile chain (ids remapped so parent_profile_id stays coherent).
    from backend.utils import dump_rows
    from backend.utils.dump_rows import LINK_COLUMNS, remap_profile_links
    profile_map = {}
    for pf in dump.get("profiles", []):
        row = dump_rows.new_profile(pf, user_id)
        db.session.add(row)
        db.session.flush()
        profile_map[pf["id"]] = row.id
    remap_profile_links(dump.get("profiles", []), profile_map)
    db.session.commit()

    for rc in dump.get("recent_contexts", []):
        db.session.add(dump_rows.new_recent_context(
            rc, user_id, profile_map.get(rc.get("profile_id"))))
    for td in dump.get("todos", []):
        db.session.add(dump_rows.new_todo(td, user_id))
    # The user's edit wins (#412): on an account whose current intentions
    # are a version the user wrote, restored or had updated in a
    # conversation, the dump's intentions are left out (a prefill bundle's
    # are inferred, and would otherwise replace them).
    from backend.utils.intentions_versions import KIND as INTENTIONS, protected_version
    kept_intentions = protected_version(user_id)
    skipped_intentions = 0
    for a in dump.get("artifacts", []):
        if a["kind"] == INTENTIONS and kept_intentions is not None:
            skipped_intentions += 1
            continue
        db.session.add(dump_rows.new_artifact(a, user_id))
    db.session.commit()
    if skipped_intentions:
        print(f"kept the account's own intentions: skipped {skipped_intentions} "
              f"intentions version(s) from the dump", file=sys.stderr)

    # Nodes last, so context pins on system nodes resolve to the rows above.
    prompt_cache = {}
    pinned = 0

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
        new = dump_rows.new_node(
            nd, user_id, _author_id(nd.get("author_username"), user_id, cache, db, User))
        db.session.add(new)
        db.session.flush()
        id_map[nd["id"]] = new.id
        created += 1
        if nd.get("prompt"):
            dump_rows.pin_prompt(new, nd["prompt"], user_id, prompt_cache)
            pinned += 1
        if nd.get("parent_id") or nd.get("continuation_node_id") or nd.get("linked_node_id"):
            # Only the link fields: the entry itself (its text) is not
            # kept in memory for the second pass.
            pending_links.append((new.id, {c: nd.get(c) for c in LINK_COLUMNS}))
        if nd.get("embedding"):
            db.session.add(dump_rows.new_embedding(nd["embedding"], new.id, user_id))
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
        for col in LINK_COLUMNS:
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
