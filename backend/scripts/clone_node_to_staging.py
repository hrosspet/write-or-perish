#!/usr/bin/env python3
"""Clone a streaming-recording node from production to staging for testing.

Lets you replay a long pre-existing recording on staging without re-recording.
Run on the production VM (where prod DB is reachable). The script also shells
out to `docker exec` and `docker cp` to write into the staging stack.

Re-run after every staging deploy: staging DB is reset on each deploy, but
the audio files persist in the named Docker volume — pass `--skip-files` on
subsequent runs to skip the (already-done) file copy.

Usage on the production VM, with the prod env active (DATABASE_URL set):

    python -m backend.scripts.clone_node_to_staging \\
        --node-ids 5168 5174 \\
        --staging-backend wop-staging-backend-1

Optional:
    --skip-files               Skip docker cp of audio files (already copied)
    --staging-backend NAME     Override the staging backend container name

The script:
  1. Reads User + Node + NodeTranscriptChunk rows from the current DB (prod)
  2. Pipes them as JSON into a Python interpreter inside the staging backend
     container, which writes them to the staging DB
  3. Promotes the cloned user to is_admin=True so voice-mode access works
  4. Mints a magic-link token for that user and prints a one-shot login URL
     (no email send needed)
  5. Uses `docker cp` to copy the audio chunks from prod's data/audio dir
     into the staging backend container's /app/data/audio
"""
import argparse
import json
import os
import pathlib
import secrets
import subprocess
import sys
from datetime import datetime, date, timedelta
from decimal import Decimal

import shutil
import tempfile

project_root = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from backend.app import create_app  # noqa: E402
from backend.models import Node, NodeTranscriptChunk, User  # noqa: E402
from backend.utils.encryption import decrypt_file  # noqa: E402

AUDIO_STORAGE_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve()


def serialize_row(obj):
    out = {}
    for col in obj.__table__.columns:
        v = getattr(obj, col.name)
        if isinstance(v, (datetime, date)):
            v = {"__datetime__": v.isoformat()}
        elif isinstance(v, Decimal):
            v = float(v)
        elif isinstance(v, bytes):
            v = {"__bytes__hex": v.hex()}
        out[col.name] = v
    return out


def export_payload(app, node_ids):
    with app.app_context():
        nodes = Node.query.filter(Node.id.in_(node_ids)).all()
        found = {n.id for n in nodes}
        missing = set(node_ids) - found
        if missing:
            print(f"WARNING: nodes not found in prod DB: {sorted(missing)}",
                  file=sys.stderr)
        if not nodes:
            sys.exit("No nodes to clone — aborting")
        user_ids = sorted({n.user_id for n in nodes})
        users = User.query.filter(User.id.in_(user_ids)).all()
        chunks = NodeTranscriptChunk.query.filter(
            NodeTranscriptChunk.node_id.in_(node_ids)
        ).all()

        # Decrypt encrypted fields on the prod side and ship plaintext.
        # Staging has a different KMS key, so the wire format must be
        # plaintext; the import side re-encrypts via the model setters.
        nodes_data = []
        for n in nodes:
            row = serialize_row(n)
            row["content"] = n.get_content() if n.content else ""
            nodes_data.append(row)
        chunks_data = []
        for c in chunks:
            row = serialize_row(c)
            row["text"] = c.get_text() if c.text else ""
            chunks_data.append(row)

        return {
            "users": [serialize_row(u) for u in users],
            "nodes": nodes_data,
            "chunks": chunks_data,
        }


# This script is written as a string and piped via `docker exec` into a python
# process inside the staging backend container. Avoid f-strings / external
# imports beyond what the staging container already has on path.
IMPORT_SCRIPT = r'''
import json, sys
from datetime import datetime, timedelta
sys.path.insert(0, "/app")
from backend.app import create_app
from backend.models import Node, NodeTranscriptChunk, User
from backend.extensions import db
from backend.utils.magic_link import generate_magic_link_token, hash_token


def deser(v):
    if isinstance(v, dict):
        if "__datetime__" in v:
            return datetime.fromisoformat(v["__datetime__"])
        if "__bytes__hex" in v:
            return bytes.fromhex(v["__bytes__hex"])
    return v


def deser_row(row):
    return {k: deser(v) for k, v in row.items()}


payload = json.loads(sys.stdin.read())
app = create_app()
with app.app_context():
    counts = {"users_inserted": 0, "users_updated": 0,
              "nodes_inserted": 0, "nodes_replaced": 0,
              "chunks_inserted": 0}

    # Users: insert or update so they have admin/voice access
    user_ids = []
    for raw in payload["users"]:
        row = deser_row(raw)
        u = User.query.get(row["id"])
        if u is None:
            u = User(**row)
            db.session.add(u)
            counts["users_inserted"] += 1
        else:
            for k, v in row.items():
                if k != "id":
                    setattr(u, k, v)
            counts["users_updated"] += 1
        # Force voice-mode access on staging
        u.is_admin = True
        u.approved = True
        if not u.accepted_terms_at:
            u.accepted_terms_at = datetime.utcnow()
            u.accepted_terms_version = "v1"
        user_ids.append(u.id)
    db.session.commit()

    # Replace-then-insert for nodes + chunks so re-running the script fixes
    # any prior broken clone. Scoped strictly to the requested node ids —
    # never touches unrelated staging data. We sweep referencing rows in
    # dependency order before deleting the nodes themselves; staging may
    # have created TTSChunk / NodeVersion / Draft rows pointing at these
    # ids during prior speaker-icon clicks, and a plain Node delete fails
    # the FK constraints.
    cloned_node_ids = [deser_row(r)["id"] for r in payload["nodes"]]
    if cloned_node_ids:
        ids_csv = ",".join(str(i) for i in cloned_node_ids)
        # Children with no CASCADE — delete outright.
        for table, col in (
            ("tts_chunk", "node_id"),
            ("node_version", "node_id"),
            ("node_transcript_chunk", "node_id"),
        ):
            db.session.execute(db.text(
                f"DELETE FROM {table} WHERE {col} IN ({ids_csv})"
            ))
        # Tables that should survive — null the FK instead of deleting.
        for table, col in (
            ("draft", "node_id"),
            ("draft", "parent_id"),
            ("draft", "llm_node_id"),
            ("node", "parent_id"),
            ("node", "linked_node_id"),
        ):
            db.session.execute(db.text(
                f"UPDATE \"{table}\" SET {col} = NULL "
                f"WHERE {col} IN ({ids_csv})"
            ))
        # node_context_artifact has ON DELETE CASCADE — auto-cleaned.
        deleted_nodes = Node.query.filter(
            Node.id.in_(cloned_node_ids)
        ).delete(synchronize_session=False)
        db.session.commit()
        counts["nodes_replaced"] = deleted_nodes

    # Nodes: null out FK chain to keep clone shallow. Encrypted columns
    # arrive as plaintext — re-encrypt with staging's key.
    for raw in payload["nodes"]:
        row = deser_row(raw)
        row["parent_id"] = None
        row["linked_node_id"] = None
        plain_content = row.pop("content", None)
        n = Node(**row)
        if plain_content is not None:
            n.set_content(plain_content)
        db.session.add(n)
        counts["nodes_inserted"] += 1
    db.session.commit()

    # Chunks: same plaintext-then-re-encrypt dance for `text`.
    for raw in payload["chunks"]:
        row = deser_row(raw)
        plain_text = row.pop("text", None)
        c = NodeTranscriptChunk(**row)
        if plain_text is not None:
            c.set_text(plain_text)
        db.session.add(c)
        counts["chunks_inserted"] += 1
    db.session.commit()

    # Bump sequences so future inserts on staging don't collide
    for table, col in (("user", "id"), ("node", "id"),
                       ("node_transcript_chunk", "id")):
        db.session.execute(db.text(
            f"SELECT setval(pg_get_serial_sequence('\"{table}\"', '{col}'), "
            f"COALESCE((SELECT MAX({col}) FROM \"{table}\"), 1), true)"
        ))
    db.session.commit()

    # Mint a magic-link login URL for each cloned user
    login_urls = []
    for uid in user_ids:
        u = User.query.get(uid)
        # Use email if set, else fabricate a placeholder so the token verifies
        email = u.email or f"clone-{u.id}@local"
        if not u.email:
            u.email = email
        token = generate_magic_link_token(email, "/")
        u.magic_link_token_hash = hash_token(token)
        u.magic_link_expires_at = datetime.utcnow() + timedelta(days=30)
        db.session.commit()
        login_urls.append((u.id, u.username, token))

    print("IMPORT_RESULT", json.dumps({
        "counts": counts,
        "login_urls": [{"user_id": uid, "username": un, "token": tok}
                       for (uid, un, tok) in login_urls],
    }))
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node-ids", nargs="+", type=int, required=True)
    ap.add_argument("--staging-backend", default="wop-staging-backend-1")
    ap.add_argument("--staging-url", default="https://staging.loore.org")
    ap.add_argument("--skip-files", action="store_true",
                    help="Skip docker cp (audio files persist across deploys)")
    ap.add_argument("--prod-audio-root",
                    default=str(AUDIO_STORAGE_ROOT),
                    help="Root path of prod audio storage. Defaults to "
                         "AUDIO_STORAGE_PATH env var or 'data/audio' "
                         "relative to cwd. Set this when running from a "
                         "git worktree (e.g. /home/<user>/write-or-perish/data/audio)")
    args = ap.parse_args()
    prod_audio_root = pathlib.Path(args.prod_audio_root).resolve()

    print("=== Phase 1: export from prod DB ===")
    app = create_app()
    payload = export_payload(app, args.node_ids)
    user_ids = sorted({n["user_id"] for n in payload["nodes"]})
    print(f"  users: {len(payload['users'])} (ids: {user_ids})")
    print(f"  nodes: {len(payload['nodes'])}")
    print(f"  chunks: {len(payload['chunks'])}")

    print("\n=== Phase 2: import into staging via docker exec ===")
    proc = subprocess.run(
        ["docker", "exec", "-i", args.staging_backend, "python", "-c",
         IMPORT_SCRIPT],
        input=json.dumps(payload).encode(),
        capture_output=True,
    )
    out, err = proc.stdout.decode(), proc.stderr.decode()
    if proc.returncode != 0:
        print("IMPORT FAILED")
        print("STDOUT:", out)
        print("STDERR:", err)
        sys.exit(1)
    # Parse the IMPORT_RESULT line
    result = None
    for line in out.splitlines():
        if line.startswith("IMPORT_RESULT "):
            result = json.loads(line[len("IMPORT_RESULT "):])
    if result is None:
        print("Could not parse import result. Raw output:")
        print(out)
        sys.exit(1)
    print("  counts:", result["counts"])

    if not args.skip_files:
        print(f"\n=== Phase 3: copy audio files into staging container ===")
        print(f"  prod audio root: {prod_audio_root}")
        # Files in prod are encrypted with prod's KMS-wrapped DEK. Staging
        # has its own (or no) KMS key and can't unwrap that DEK, so we
        # decrypt on the prod side and ship plaintext. The /media route
        # serves plaintext files transparently (decrypt_file is a no-op
        # for paths without `.enc`), and the backfill script that runs
        # next can read these plaintext chunks directly.
        for n in payload["nodes"]:
            uid, nid = n["user_id"], n["id"]
            src = prod_audio_root / f"nodes/{uid}/{nid}"
            if not src.exists():
                print(f"  SKIP node {nid}: source dir {src} missing")
                continue
            stage = pathlib.Path(tempfile.mkdtemp(prefix=f'clone_{nid}_'))
            try:
                # Mirror src into stage/<nid>/, decrypting .enc files.
                dst = stage / str(nid)
                dst.mkdir()
                n_dec, n_plain, total_bytes = 0, 0, 0
                for f in src.iterdir():
                    if not f.is_file():
                        continue
                    if f.name.endswith('.enc'):
                        target = dst / f.name[:-4]
                        plaintext = decrypt_file(str(f))
                        target.write_bytes(plaintext)
                        n_dec += 1
                        total_bytes += len(plaintext)
                    else:
                        target = dst / f.name
                        shutil.copy2(str(f), str(target))
                        n_plain += 1
                        total_bytes += target.stat().st_size
                # Wipe any pre-existing staging data for this node so we
                # don't end up with stale prod-encrypted .enc files
                # alongside the new plaintext (the audio-chunks endpoint
                # prefers plain over enc, but the backfill prefers the
                # opposite — leaving both around would break backfill).
                # Run cleanup + mkdir as root because earlier docker cp
                # copied files in as root, and the container's default
                # user can't unlink them (Permission denied).
                dst_dir = f"/app/data/audio/nodes/{uid}/{nid}"
                subprocess.run(
                    ["docker", "exec", "-u", "0", args.staging_backend,
                     "rm", "-rf", dst_dir], check=True,
                )
                dst_parent = f"/app/data/audio/nodes/{uid}"
                subprocess.run(
                    ["docker", "exec", "-u", "0", args.staging_backend,
                     "mkdir", "-p", dst_parent], check=True,
                )
                subprocess.run(
                    ["docker", "cp", str(dst),
                     f"{args.staging_backend}:{dst_parent}/"], check=True,
                )
                # docker cp creates files as root; the container's
                # runtime user can't then write batch_*.webm during
                # backfill. chmod a+rwX so the backfill (running as
                # the container's default user) can write alongside.
                subprocess.run(
                    ["docker", "exec", "-u", "0", args.staging_backend,
                     "chmod", "-R", "a+rwX",
                     f"/app/data/audio/nodes/{uid}/{nid}"], check=True,
                )
                print(f"  node {nid}: {n_dec} decrypted + {n_plain} plain "
                      f"-> {round(total_bytes/1024/1024, 1)} MB")
            finally:
                shutil.rmtree(str(stage), ignore_errors=True)
    else:
        print("\n=== Phase 3: skipped (--skip-files) ===")

    print("\n=== Login URLs (one-time use, expire in 30 days) ===")
    for entry in result["login_urls"]:
        url = f"{args.staging_url}/auth/magic-link/verify?token={entry['token']}"
        print(f"  user {entry['user_id']} ({entry['username']}):")
        print(f"    {url}")
    print("\nDone. Open one of the URLs above to log in to staging,")
    print("then click the speaker icon on the cloned node to test replay.")


if __name__ == "__main__":
    main()
