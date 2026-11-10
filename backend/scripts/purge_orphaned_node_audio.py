"""Delete the audio files that nothing needs any more, by hand.

The manual entry point to the sweep the daily cleanup runs
(backend/utils/audio_sweep.py, where the rules are). It deletes, under
the audio storage root:

* a node's folders (``user/<uid>/node/<id>/``, ``nodes/<uid>/<id>/``)
  when the node row is gone or the node was deleted more than
  SOFT_DELETE_GRACE_DAYS ago;
* a saved reference's or a profile's speech folder
  (``user/<uid>/item/<id>/``, ``user/<uid>/profile/<id>/``) when its row
  is gone;
* superseded speech (``tts.mp3``, ``tts_chunk_<i>.mp3``) in a live row's
  folder that no row points at any more;
* a streaming session folder (``streaming/<uid>/<session>/``) that no
  node not yet finalized and no draft names, once unchanged for the
  grace period.

Never a file a row points at, a live node's recording, a node within the
grace period, a row a waiting "Delete all my content" hid, a folder with
anything changed in the last 24 hours, a recording in a folder whose node
row is gone until it is unchanged for the grace period, or anything
outside those folders; symlinks are never followed. Reads folder names, file sizes and
times, row ids, deletion times and stored audio URLs; never a file's
content.

Run from the repo root with the app's environment active (the same audio
storage root as the app; the script prints it):

    python backend/scripts/purge_orphaned_node_audio.py             # dry run
    python backend/scripts/purge_orphaned_node_audio.py --verbose   # + row ids
    python backend/scripts/purge_orphaned_node_audio.py --apply

The dry run prints the folders, files and GB per category; ``--verbose``
adds the kind and id of each row whose files it deletes (or would).
Verify after ``--apply``: a dry run reports 0 folders to delete (folders
changed in the last 24 hours are listed as kept; the daily cleanup
deletes them later). Exits 1 when ``--apply`` could not delete a file;
running it again retries.
"""
import argparse
import os
import sys

sys.path.insert(0, os.getcwd())


def run(apply=False, verbose=False, root=None, now=None, out=print):
    """Sweep (backend.utils.audio_sweep.sweep) and print the report.
    Returns the sweep's result."""
    from backend.utils import audio_sweep
    result = audio_sweep.sweep(apply=apply, verbose=verbose, root=root,
                               now=now, out=out)
    if not result["root"].is_dir():
        out(f"audio root not found: {result['root']}")
    audio_sweep.report(result, apply, out)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Delete audio files nothing needs any more: folders "
                    "of nodes that are gone or deleted more than the grace "
                    "period ago, deleted references' speech, superseded "
                    "speech (dry run by default; never reads a file's "
                    "content).")
    parser.add_argument("--apply", action="store_true",
                        help="delete the files (default: dry run)")
    parser.add_argument("--verbose", action="store_true",
                        help="print the kind and id of each row whose "
                             "files are deleted")
    args = parser.parse_args(argv)
    from backend import create_app
    app = create_app()
    with app.app_context():
        result = run(apply=args.apply, verbose=args.verbose)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
