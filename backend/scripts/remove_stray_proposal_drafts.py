"""Remove proposal drafts that don't belong to their node's owner.

A proposal draft (a label in utils/proposals.PROPOSAL_TOOLS) marks an AI
reply's proposal as pending for the user who asked for that reply. The
server makes one only on that user's own AI reply, never with a recording
session. This removes every proposal draft that isn't that:

- one with a session_id. A recording not yet saved as an entry also
  loses what Discard removes: its transcript chunks and its audio folder.
  One already saved keeps them, as the app's own clean-up of saved
  recordings does;
- one whose parent isn't an AI reply asked for by the draft's user: no
  parent, a missing node, a node that isn't an AI reply, or someone
  else's.

Run once after the deploy that accepts proposals only as the user's own
live proposal:

    cd /path/to/write-or-perish
    python backend/scripts/remove_stray_proposal_drafts.py          # dry run
    python backend/scripts/remove_stray_proposal_drafts.py --apply

Reads ids, labels and owners only; no content is decrypted. Verify after:
the dry run reports 0.
"""
import argparse
import os
import shutil
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from backend.extensions import db  # noqa: E402
from backend.models import Draft, Node, NodeTranscriptChunk  # noqa: E402
from backend.utils.audio_storage import storage_path  # noqa: E402
from backend.utils.proposals import PROPOSAL_TOOLS  # noqa: E402

SESSION = "recording session"
NO_PARENT = "no parent node"
NOT_AI_REPLY = "parent is not an AI reply"
OTHER_OWNER = "parent belongs to another user"


def stray_drafts():
    """[(draft row, reason)] for each proposal draft to remove. Columns
    only: no Draft or Node content is loaded."""
    drafts = db.session.query(
        Draft.id, Draft.user_id, Draft.parent_id, Draft.label,
        Draft.session_id, Draft.llm_node_id, Draft.streaming_warning,
    ).filter(Draft.label.in_(tuple(PROPOSAL_TOOLS))).all()
    parent_ids = {d.parent_id for d in drafts if d.parent_id}
    parents = {}
    if parent_ids:
        parents = {p.id: p for p in db.session.query(
            Node.id, Node.user_id, Node.human_owner_id, Node.node_type,
        ).filter(Node.id.in_(parent_ids)).all()}
    stray = []
    for d in drafts:
        parent = parents.get(d.parent_id)
        if d.session_id:
            reason = SESSION
        elif parent is None:
            reason = NO_PARENT
        elif parent.node_type != "llm":
            reason = NOT_AI_REPLY
        elif (parent.human_owner_id or parent.user_id) != d.user_id:
            reason = OTHER_OWNER
        else:
            continue
        stray.append((d, reason))
    return stray


def remove(stray, audio_root):
    """Delete the drafts, and for a recording not saved as an entry what
    Discard deletes. Returns the number of audio folders removed. The
    caller commits."""
    folders = 0
    for d, _reason in stray:
        # Saved as an entry: the server chain set llm_node_id, or saved
        # it with the reply skipped (streaming_warning).
        saved = d.llm_node_id is not None or d.streaming_warning is not None
        if d.session_id and not saved:
            NodeTranscriptChunk.query.filter_by(
                session_id=d.session_id,
            ).delete(synchronize_session=False)
            try:
                folder = storage_path(audio_root, "drafts", d.user_id,
                                      d.session_id)
            except ValueError:
                folder = None
            if folder is not None and folder.exists():
                shutil.rmtree(folder)
                folders += 1
        Draft.query.filter_by(id=d.id).delete(synchronize_session=False)
    return folders


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Remove proposal drafts that don't belong to their "
                    "node's owner")
    parser.add_argument("--apply", action="store_true",
                        help="delete; without it this only reports")
    args = parser.parse_args(argv)

    from backend import create_app
    app = create_app()
    with app.app_context():
        # The app's own value, read after create_app loaded the env.
        from backend.routes.drafts import AUDIO_STORAGE_ROOT
        stray = stray_drafts()
        by_reason = Counter((d.label, reason) for d, reason in stray)
        print(f"proposal drafts to remove: {len(stray)} "
              f"({len({d.user_id for d, _ in stray})} user(s))")
        for (label, reason), n in sorted(by_reason.items()):
            print(f"  {label}: {reason}: {n}")
        for d, reason in stray:
            print(f"  draft {d.id} user {d.user_id} parent {d.parent_id} "
                  f"{d.label} session={'yes' if d.session_id else 'no'} "
                  f"({reason})")
        if not args.apply:
            print("dry run, nothing deleted; rerun with --apply")
            return 0
        folders = remove(stray, AUDIO_STORAGE_ROOT)
        db.session.commit()
        print(f"deleted {len(stray)} draft(s), {folders} audio folder(s) "
              f"under {AUDIO_STORAGE_ROOT}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
