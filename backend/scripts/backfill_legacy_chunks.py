#!/usr/bin/env python3
"""Backfill legacy streaming-recording chunks into modern batch files.

Phase-2 voice recordings (Jan-Mar 2026) were stored as one or more
chunk_NNNN.webm[.enc] files where each chunk is a standalone Matroska
"chained" segment with continuous cluster timestamps from the original
recording. After the AudioContext.js WebM-offset fix (PR #119), these no
longer replay correctly: the player treats each chunk URL as an
independent file starting at timestamp 0, but the chunks actually
share absolute timestamps across files.

This script remuxes each affected node's chunks into a single
batch_0000-NNNN.webm[.enc] file (the modern format the post-fix
player expects) and renames the originals to
chunk_NNNN.webm[.enc].legacy_backup. The endpoint at
backend/routes/nodes.py glob-matches chunk_*.webm[.enc] and
batch_*.webm[.enc] but not the .legacy_backup suffix, so after this
script the endpoint falls through to the new batch_*.webm[.enc].

Idempotent. Safe to rerun.

Usage (from repo root, on production VM):

    # Dry-run (default): inspect what would change, no writes
    python -m backend.scripts.backfill_legacy_chunks

    # Process a single node first to validate end-to-end
    python -m backend.scripts.backfill_legacy_chunks --commit --node 5168

    # Full backfill, with stronger per-file decode verification
    python -m backend.scripts.backfill_legacy_chunks --commit --verify-decode

Rollback (if needed, per node, in flask shell):

    from pathlib import Path
    d = Path('data/audio/nodes/<user_id>/<node_id>')
    for f in d.glob('batch_*'):
        f.unlink()
    for f in d.glob('chunk_*.legacy_backup'):
        f.rename(f.with_suffix(''))   # strips .legacy_backup
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

project_root = pathlib.Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from backend.app import create_app  # noqa: E402
from backend.models import Node  # noqa: E402
from backend.utils.encryption import (  # noqa: E402
    decrypt_file, encrypt_file, is_encryption_enabled,
)
from backend.utils.webm_utils import concat_fragmented_media  # noqa: E402

AUDIO_STORAGE_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve()


def ffprobe_duration(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_format', '-of', 'json', path],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return None
    d = json.loads(r.stdout).get('format', {}).get('duration')
    return float(d) if d else None


def ffmpeg_full_decode_ok(path):
    """Decode the entire file to /dev/null. Catches mid-file corruption
    that ffprobe (which only reads container metadata) would miss."""
    r = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', path, '-f', 'null', '-'],
        capture_output=True, text=True, timeout=600,
    )
    return r.returncode == 0 and not r.stderr.strip(), r.stderr.strip()[-300:]


def list_chunks(d):
    enc = sorted(d.glob("chunk_*.webm.enc"))
    plain = sorted(d.glob("chunk_*.webm"))
    return enc if enc else plain


def list_batches(d):
    return list(d.glob("batch_*.webm")) + list(d.glob("batch_*.webm.enc"))


def process_node(node, dry_run, verify_decode):
    """Returns (status, dur_seconds, info)."""
    d = AUDIO_STORAGE_ROOT / f"nodes/{node.user_id}/{node.id}"
    if not d.exists():
        return ("skip_no_dir", None, None)

    chunks = list_chunks(d)
    batches = list_batches(d)

    if not chunks and not batches:
        return ("skip_empty", None, None)
    if not chunks and batches:
        return ("skip_already_modern", None, None)

    # Partial-state recovery: prior run created the batch but didn't
    # finish renaming the chunks. Verify the batch then complete the
    # rename.
    if chunks and batches:
        batch = batches[0]
        batch_decrypted = None
        try:
            if batch.name.endswith('.enc'):
                fd, batch_decrypted = tempfile.mkstemp(suffix='.webm')
                os.close(fd)
                with open(batch_decrypted, 'wb') as f:
                    f.write(decrypt_file(str(batch)))
                batch_for_probe = batch_decrypted
            else:
                batch_for_probe = str(batch)
            dur = ffprobe_duration(batch_for_probe)
            if not dur or dur <= 0:
                return ("partial_bad_batch", None, "existing batch has no duration")
        finally:
            if batch_decrypted and os.path.exists(batch_decrypted):
                os.unlink(batch_decrypted)

        if dry_run:
            return ("partial_would_rename", dur, len(chunks))
        for c in chunks:
            c.rename(c.with_name(c.name + '.legacy_backup'))
        return ("partial_completed", dur, len(chunks))

    # Standard case: chunks present, no batch yet
    encrypted = chunks[0].name.endswith('.enc')
    n_chunks = len(chunks)
    workdir = tempfile.mkdtemp(prefix=f'backfill_{node.id}_')
    merged = None
    try:
        plain_paths = []
        chunks_total_size = 0
        for c in chunks:
            base = c.name[:-4] if encrypted else c.name
            p = os.path.join(workdir, base)
            if encrypted:
                with open(p, 'wb') as f:
                    f.write(decrypt_file(str(c)))
            else:
                shutil.copy2(str(c), p)
            chunks_total_size += os.path.getsize(p)
            plain_paths.append(p)

        merged = concat_fragmented_media(plain_paths)
        merged_size = os.path.getsize(merged)
        merged_dur = ffprobe_duration(merged)

        if not merged_dur or merged_dur <= 0:
            return ("fail_no_duration", merged_dur, merged_size)
        if merged_size < chunks_total_size * 0.5:
            return ("fail_size_anomaly",
                    merged_dur,
                    f"merged={merged_size} chunks_total={chunks_total_size}")

        if verify_decode:
            ok, err_tail = ffmpeg_full_decode_ok(merged)
            if not ok:
                return ("fail_decode", merged_dur, err_tail)

        if dry_run:
            return ("ok_dry_run", merged_dur, merged_size)

        # Write the merged file as batch_0000-NNNN.webm[.enc] in the
        # node's audio dir. Order: write batch first, verify, THEN
        # rename originals — so a crash anywhere keeps the node's
        # existing chunks intact (idempotent rerun completes the job).
        batch_basename = f"batch_0000-{n_chunks - 1:04d}.webm"
        batch_plain_path = d / batch_basename
        shutil.move(merged, str(batch_plain_path))
        merged = None  # consumed

        if encrypted and is_encryption_enabled():
            final_path = encrypt_file(str(batch_plain_path))
        else:
            final_path = str(batch_plain_path)

        # Sanity check: final file is on disk and ffprobe agrees.
        if final_path.endswith('.enc'):
            fd, tmp = tempfile.mkstemp(suffix='.webm')
            os.close(fd)
            try:
                with open(tmp, 'wb') as f:
                    f.write(decrypt_file(final_path))
                final_dur = ffprobe_duration(tmp)
            finally:
                os.unlink(tmp)
        else:
            final_dur = ffprobe_duration(final_path)
        if not final_dur or final_dur <= 0:
            return ("fail_post_write_verify", final_dur,
                    f"wrote {final_path} but ffprobe failed")

        for c in chunks:
            c.rename(c.with_name(c.name + '.legacy_backup'))

        return ("ok", merged_dur, merged_size)
    except Exception as e:
        return ("fail_exception", None,
                f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        if merged and os.path.exists(merged):
            try:
                os.unlink(merged)
            except OSError:
                pass
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(
        description="Backfill legacy chunk_*.webm into modern batch_*.webm",
    )
    ap.add_argument("--commit", action="store_true",
                    help="Actually perform the backfill (default: dry-run)")
    ap.add_argument("--node", type=int, default=None,
                    help="Process only this node id")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most this many nodes (after sorting by id)")
    ap.add_argument("--verify-decode", action="store_true",
                    help="Run ffmpeg full-decode to verify each merged batch "
                         "(slow; otherwise only ffprobe duration is checked)")
    args = ap.parse_args()
    dry_run = not args.commit

    app = create_app()
    with app.app_context():
        q = Node.query.filter_by(streaming_transcription=True)
        if args.node:
            q = q.filter_by(id=args.node)
        nodes = q.order_by(Node.id.asc()).all()
        if args.limit:
            nodes = nodes[:args.limit]

        print(f"Mode:           {'DRY-RUN' if dry_run else 'COMMIT'}")
        print(f"Verify decode:  {args.verify_decode}")
        print(f"Storage root:   {AUDIO_STORAGE_ROOT}")
        print(f"Nodes to scan:  {len(nodes)}")
        print()

        outcome = Counter()
        for n in nodes:
            status, dur, info = process_node(n, dry_run, args.verify_decode)
            outcome[status] += 1
            if status.startswith("ok") or status.startswith("partial"):
                dur_s = f"{dur:.1f}s" if dur else "?"
                print(f"  Node {n.id:>6d} (user {n.user_id:>4d}): "
                      f"{status:<22s} dur={dur_s:>8s}  info={info}")
            elif status.startswith("fail"):
                print(f"  Node {n.id:>6d} (user {n.user_id:>4d}): "
                      f"{status:<22s} info={info}")
            # skip_* outcomes: too verbose; just count

        print("\nSummary:")
        for k, v in sorted(outcome.most_common()):
            print(f"  {k:<22s} {v}")


if __name__ == "__main__":
    main()
