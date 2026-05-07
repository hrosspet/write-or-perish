#!/usr/bin/env python3
"""Backfill legacy streaming-recording chunks into modern batch files.

Phase-2 voice recordings (Jan-Mar 2026) were stored as one or more
chunk_NNNN.webm[.enc] files where each chunk is a standalone Matroska
"chained" segment with continuous cluster timestamps from the original
recording. After the AudioContext.js WebM-offset fix (PR #119), these no
longer replay correctly: the player treats each chunk URL as an
independent file starting at timestamp 0, but the chunks actually
share absolute timestamps across files.

This script remuxes each legacy chunk individually into a modern
batch_NNNN-NNNN.webm[.enc] file with timestamps rebased to 0 — one
batch per input chunk, preserving the original ~5-min granularity.
Originals are renamed to chunk_NNNN.webm[.enc].legacy_backup. The
audio-chunks endpoint glob-matches chunk_*.webm[.enc] and
batch_*.webm[.enc] but not the .legacy_backup suffix, so after this
script the endpoint serves the new batch_*.webm[.enc].

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

    # Partial-state recovery: prior run created batches but didn't
    # finish renaming the chunks. Verify the batches have a non-zero
    # cumulative duration, then complete the rename. (We expect one
    # batch per chunk after this script's per-chunk remux semantics.)
    if chunks and batches:
        cumulative_dur = 0.0
        for batch in batches:
            decrypted = None
            try:
                if batch.name.endswith('.enc'):
                    fd, decrypted = tempfile.mkstemp(suffix='.webm')
                    os.close(fd)
                    with open(decrypted, 'wb') as f:
                        f.write(decrypt_file(str(batch)))
                    probe_target = decrypted
                else:
                    probe_target = str(batch)
                d_dur = ffprobe_duration(probe_target) or 0
                if d_dur <= 0:
                    return ("partial_bad_batch", None,
                            f"{batch.name} has no duration")
                cumulative_dur += d_dur
            finally:
                if decrypted and os.path.exists(decrypted):
                    os.unlink(decrypted)

        if dry_run:
            return ("partial_would_rename", cumulative_dur,
                    f"{len(batches)} batches, {len(chunks)} chunks to rename")
        for c in chunks:
            c.rename(c.with_name(c.name + '.legacy_backup'))
        return ("partial_completed", cumulative_dur, len(chunks))

    # Standard case: chunks present, no batch yet. Per-chunk remux —
    # each legacy chunk becomes one modern batch with timestamps
    # rebased to 0, preserving the original ~5-min granularity (vs.
    # collapsing the whole recording into one file, which would lose
    # the chunk model the post-fix player expects).
    encrypted = chunks[0].name.endswith('.enc')
    n_chunks = len(chunks)
    workdir = tempfile.mkdtemp(prefix=f'backfill_{node.id}_')
    remuxed_paths = []  # parallel to chunks: workdir paths of remuxed output
    try:
        durations_total = 0.0
        size_total = 0
        for i, c in enumerate(chunks):
            # Decrypt or copy into workdir
            base = c.name[:-4] if encrypted else c.name
            in_path = os.path.join(workdir, f"in_{i:04d}_{base}")
            if encrypted:
                with open(in_path, 'wb') as f:
                    f.write(decrypt_file(str(c)))
            else:
                shutil.copy2(str(c), in_path)

            # Remux this chunk on its own. concat_fragmented_media with
            # a single input is functionally `ffmpeg -c copy +genpts`,
            # which the surrounding codebase already trusts to produce
            # an output WebM whose first cluster lands at timestamp 0
            # regardless of the input's absolute timestamps.
            out_path = concat_fragmented_media([in_path])
            durations_total += ffprobe_duration(out_path) or 0.0
            size_total += os.path.getsize(out_path)
            remuxed_paths.append(out_path)

        if durations_total <= 0:
            return ("fail_no_duration", durations_total, size_total)

        if verify_decode:
            for j, p in enumerate(remuxed_paths):
                ok, err_tail = ffmpeg_full_decode_ok(p)
                if not ok:
                    return ("fail_decode",
                            durations_total,
                            f"chunk {j}: {err_tail}")

        if dry_run:
            return ("ok_dry_run", durations_total,
                    f"{n_chunks} batches, {size_total} bytes total")

        # Write each remuxed output as batch_NNNN-NNNN.webm[.enc] in
        # the node's audio dir. Order: write all batches + encrypt,
        # verify, THEN rename originals — so a crash anywhere keeps
        # the node's existing chunks intact (idempotent rerun
        # completes the job).
        batch_paths = []
        for i, src in enumerate(remuxed_paths):
            batch_plain = d / f"batch_{i:04d}-{i:04d}.webm"
            shutil.move(src, str(batch_plain))
            if encrypted and is_encryption_enabled():
                final = encrypt_file(str(batch_plain))
            else:
                final = str(batch_plain)
            batch_paths.append(final)
        remuxed_paths = []  # consumed; clear so finally{} doesn't re-unlink

        # Sanity check each batch
        for fp in batch_paths:
            if fp.endswith('.enc'):
                fd, tmp = tempfile.mkstemp(suffix='.webm')
                os.close(fd)
                try:
                    with open(tmp, 'wb') as f:
                        f.write(decrypt_file(fp))
                    final_dur = ffprobe_duration(tmp)
                finally:
                    os.unlink(tmp)
            else:
                final_dur = ffprobe_duration(fp)
            if not final_dur or final_dur <= 0:
                return ("fail_post_write_verify", final_dur,
                        f"wrote {fp} but ffprobe failed")

        for c in chunks:
            c.rename(c.with_name(c.name + '.legacy_backup'))

        return ("ok", durations_total,
                f"{n_chunks} batches, {size_total} bytes total")
    except Exception as e:
        return ("fail_exception", None,
                f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        for p in remuxed_paths:
            try:
                if os.path.exists(p):
                    os.unlink(p)
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
