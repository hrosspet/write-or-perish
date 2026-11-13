#!/usr/bin/env python3
"""Pre-fill bundles in private storage (#698; runbook:
docs/PREFILL-BUNDLES.md).

Export runs on the lab: one bundle per pre-filled account, keyed by the
account's X id, made with dump_user.py (every entry, with its provenance,
the full profile chain, recent contexts, todos, artifacts) and gzipped,
plus a manifest with no content in it. The other commands manage what is
in the storage and run wherever the storage's write access is.

    python backend/scripts/prefill_bundles.py export --dry-run
    python backend/scripts/prefill_bundles.py export [--only HANDLE|X_ID] [--limit N] [--force]
    python backend/scripts/prefill_bundles.py delete --x-id ID --reason deletion_request
    python backend/scripts/prefill_bundles.py delete --handle HANDLE --reason declined
    python backend/scripts/prefill_bundles.py unexclude --x-id ID
    python backend/scripts/prefill_bundles.py status [--x-id ID]

The location is PREFILL_BUNDLE_LOCATION (or --location): gs://<bucket>[/<prefix>]
or a local directory.

export: skips an account that must not leave the lab (the reason is
counted): an admin, one without a pre-filled handle, one someone signed
in to or that has an email login, a deleted one, AI usage None, a "no" to
the tweet seed, a spam flag, no numeric X id, an X id on the exclusion
list, no profile, or material no pre-fill makes (an entry that is not a
fetched tweet, a version a person saved: production refuses those). It
is idempotent and resumable: an account whose
metadata fingerprint matches its manifest is skipped as unchanged, so a
stopped run is simply run again. Memory stays flat: dump_user streams
the entries to a local file, which is gzipped line by line and uploaded
in chunks; one account at a time. It refuses to run where content is
encrypted (any server but the lab): exporting there would decrypt users'
writing.

delete: removes the bundle of an X id and puts the X id on the exclusion
list, so no export writes it again and no load reads it (a deletion
request, a "no", an account gone protected). Works when there is no
bundle yet. unexclude takes an X id off the list (the person opted in
again); the next export writes the bundle.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


def _storage(location):
    from flask import current_app
    from backend.utils.bundle_storage import storage_for
    storage = storage_for(location or current_app.config.get("PREFILL_BUNDLE_LOCATION"))
    if storage is None:
        sys.exit("No storage location: set PREFILL_BUNDLE_LOCATION or pass --location.")
    return storage


# ── Export ────────────────────────────────────────────────────────────────

def skip_reason(user, excluded):
    """Why ``user`` is not exported, or None. Metadata only."""
    from backend.extensions import db
    from backend.models import UserProfile
    from backend.utils import prefill_bundles as pb
    from backend.utils.activity import sign_in_status
    from backend.utils.hidden_rows import account_on_hold
    from backend.utils.privacy import account_allows_ai
    if user.is_admin:
        return "admin"
    if not user.prefilled_handle:
        return "not_prefilled"
    if user.email or sign_in_status(user) != "never":
        return "signed_in"
    if user.deleted_at is not None or account_on_hold(user.id):
        return "deleted"
    if not account_allows_ai(user):
        return "ai_none"
    if user.prefill_consent == "no":
        return "declined"
    if user.spam:
        return "spam"
    if not pb.valid_x_id(user.twitter_id):
        return "no_x_id"
    if str(user.twitter_id) in excluded:
        return "excluded"
    newest = db.session.query(UserProfile.generation_type).filter(
        UserProfile.user_id == user.id).order_by(
        UserProfile.created_at.desc(), UserProfile.id.desc()).first()
    if newest is None:
        return "no_profile"
    from backend.utils.profile_versions import SET_INTERNAL_TYPES
    if newest[0] in SET_INTERNAL_TYPES:
        # A window set without its final merge: production's profile
        # pipeline would finish it, which is a model call (#698: none).
        return "unfinished_profile"
    if holds_other_material(user.id):
        return "other_material"
    return None


def holds_other_material(user_id):
    """Whether the account holds anything a pre-fill does not make: an
    entry that is not a fetched tweet (a model's reply, something
    written), or a version a person saved. A bundle carries a pre-fill's
    material only; production refuses anything else, since it would make
    an account nobody claimed look used. Metadata only."""
    from sqlalchemy import func, or_
    from backend.extensions import db
    from backend.models import Node, UserArtifact, UserProfile, UserTodo
    from backend.utils.prefill_bundle_load import ARTIFACT_KINDS, PERSON_MADE
    from backend.utils.privacy import PREFILL_PROVENANCES
    other = db.session.query(Node.id).filter(
        or_(Node.user_id == user_id, Node.human_owner_id == user_id),
        Node.deleted_at.is_(None),
        or_(func.coalesce(Node.origin, "") != "twitter",
            func.coalesce(Node.provenance, "").notin_(PREFILL_PROVENANCES),
            Node.source_key.is_(None), Node.node_type != "user"),
    ).first()
    if other is not None:
        return True
    for model in (UserProfile, UserTodo, UserArtifact):
        if db.session.query(model.id).filter(
                model.user_id == user_id,
                model.generated_by.in_(PERSON_MADE)).first() is not None:
            return True
    # Todos and artifact kinds no pre-fill drafts (production refuses them).
    if db.session.query(UserTodo.id).filter(
            UserTodo.user_id == user_id).first() is not None:
        return True
    return db.session.query(UserArtifact.id).filter(
        UserArtifact.user_id == user_id, UserArtifact.deleted_at.is_(None),
        UserArtifact.kind.notin_(ARTIFACT_KINDS)).first() is not None


def fingerprint(user):
    """A hash of what the bundle would hold, from metadata: an export
    skips the account when its manifest has the same one."""
    from sqlalchemy import func, or_
    from backend.extensions import db
    from backend.models import (
        Node, NodeEmbedding, UserArtifact, UserProfile, UserRecentContext,
        UserTodo)
    from backend.utils import prefill_bundles as pb
    from backend.scripts import dump_user
    uid = user.id
    nodes = db.session.query(
        func.count(Node.id), func.max(Node.id), func.max(Node.updated_at)).filter(
        or_(Node.user_id == uid, Node.human_owner_id == uid),
        Node.deleted_at.is_(None)).one()

    def agg(model, *conds):
        return db.session.query(func.count(model.id), func.max(model.id)).filter(
            model.user_id == uid, *conds).one()
    parts = [
        pb.BUNDLE_FORMAT, dump_user.FORMAT_VERSION, str(user.twitter_id),
        user.prefilled_handle, list(nodes),
        list(agg(UserProfile)), list(agg(UserRecentContext)), list(agg(UserTodo)),
        list(agg(UserArtifact, UserArtifact.deleted_at.is_(None))),
        db.session.query(func.count(NodeEmbedding.node_id)).filter(
            NodeEmbedding.user_id == uid).scalar(),
    ]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def _node_count(user):
    from sqlalchemy import func, or_
    from backend.extensions import db
    from backend.models import Node
    return db.session.query(func.count(Node.id)).filter(
        or_(Node.user_id == user.id, Node.human_owner_id == user.id),
        Node.deleted_at.is_(None)).scalar() or 0


def compress(raw_path, gz_path):
    """Write the dump as the bundle's gzip members (prefill_bundles.
    write_members) and count what it holds. Returns (header, stats,
    object): the header line, counts and dates, and the manifest's
    ``object`` part. One line, and one member, in memory at a time."""
    from backend.utils import prefill_bundles as pb
    stats = {"nodes": 0, "embeddings": 0, "oldest": None, "newest": None}
    seen = {"header": None}

    def lines():
        with open(raw_path, encoding="utf-8") as src:
            for line in src:
                if not line.strip():
                    continue
                if seen["header"] is None:
                    seen["header"] = json.loads(line)
                else:
                    nd = json.loads(line)
                    stats["nodes"] += 1
                    if nd.get("embedding"):
                        stats["embeddings"] += 1
                    at = nd.get("created_at")
                    if at:
                        stats["oldest"] = at if stats["oldest"] is None else min(stats["oldest"], at)
                        stats["newest"] = at if stats["newest"] is None else max(stats["newest"], at)
                yield line
    obj = pb.write_members(lines(), gz_path)
    return seen["header"], stats, obj


def _iso_z(value):
    from backend.utils import prefill_bundles as pb
    dt = pb.parse_time(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _archive_snapshot():
    """The Community Archive export the lab imported from, if known."""
    try:
        from flask import current_app
        from backend.tasks.imports import snapshot_dir_for
        from backend.utils.community_archive import snapshot_export_id
        return snapshot_export_id(snapshot_dir_for(current_app.config))
    except Exception:  # noqa: BLE001 - optional metadata
        return None


def export_one(user, storage, workdir, fp, snapshot=None):
    """Dump, gzip and upload one account; the manifest last. Returns it,
    or None when the X id was excluded meanwhile (nothing stays)."""
    from backend.scripts import dump_user
    from backend.utils import prefill_bundles as pb
    x_id = str(user.twitter_id)
    raw = os.path.join(workdir, f"{x_id}.jsonl")
    gz = raw + ".gz"
    try:
        with open(os.devnull, "w") as devnull, redirect_stdout(devnull), \
                redirect_stderr(devnull):
            dump_user._run(user.username, raw, include_private=True)
        header, stats, obj = compress(raw, gz)
        os.remove(raw)
        artifacts = header.get("artifacts") or []
        profiles = header.get("profiles") or []
        manifest = {
            "bundle_format": pb.BUNDLE_FORMAT,
            "dump_format": header.get("format"),
            "x_id": x_id,
            "handle": user.prefilled_handle,
            "exported_at": pb.now_iso(),
            "fingerprint": fp,
            "archive_snapshot": snapshot,
            "counts": {
                "nodes": stats["nodes"], "embeddings": stats["embeddings"],
                "profiles": len(profiles),
                "recent_contexts": len(header.get("recent_contexts") or []),
                "todos": len(header.get("todos") or []),
                "artifacts": len(artifacts),
                "intentions": sum(1 for a in artifacts if a.get("kind") == "intentions"),
            },
            "oldest_entry_at": _iso_z(stats["oldest"]),
            "newest_entry_at": _iso_z(stats["newest"]),
            "profile_generated_by": profiles[-1].get("generated_by") if profiles else None,
            "object": obj,
        }
        # Checked again just before writing and once more after: a
        # deletion (delete --x-id) made while this run was going must not
        # be undone by it. delete writes the exclusion first and then
        # removes the objects, so either the check here sees it, or the
        # delete removes what was written before it.
        if pb.exclusion(storage, x_id) is not None:
            return None
        storage.upload(pb.bundle_name(x_id), gz)
        pb.write_manifest(storage, manifest)
        handle = pb.clean_handle(user.prefilled_handle)
        if handle:
            pb.write_handle_index(storage, handle, x_id)
        if pb.exclusion(storage, x_id) is not None:
            pb.remove_objects(storage, x_id, handle)
            return None
        return manifest
    finally:
        for p in (raw, gz):
            if os.path.exists(p):
                os.remove(p)


def _candidates(only):
    from sqlalchemy import func
    from backend.extensions import db
    from backend.models import User
    q = db.session.query(User.id).filter(User.prefilled_handle.isnot(None))
    if only:
        only = only.strip().lstrip("@")
        if only.isdigit():
            q = q.filter(User.twitter_id == only)
        else:
            q = q.filter(func.lower(User.prefilled_handle) == only.lower())
    return [i for (i,) in q.order_by(User.id)]


def run_export(storage, *, dry_run=False, only=None, limit=None, force=False,
               out=print):
    """Export every account that may leave the lab; returns the counts."""
    from collections import Counter
    from backend.extensions import db
    from backend.models import User
    from backend.utils import prefill_bundles as pb
    from backend.utils.encryption import is_encryption_enabled
    if is_encryption_enabled():
        sys.exit("Content is encrypted here: export runs only on the lab, "
                 "where the pre-filled material is stored unencrypted. "
                 "Exporting here would decrypt users' writing.")
    excluded = pb.excluded_x_ids(storage)
    snapshot = _archive_snapshot()
    counts = Counter()
    workdir = tempfile.mkdtemp(prefix="prefill-bundles-")
    try:
        for uid in _candidates(only):
            if limit is not None and counts["exported"] + counts["would_export"] >= limit:
                break
            user = db.session.get(User, uid)
            counts["candidates"] += 1
            reason = skip_reason(user, excluded)
            if reason:
                counts[f"skipped_{reason}"] += 1
                db.session.expunge_all()
                continue
            fp = fingerprint(user)
            existing = pb.read_manifest(storage, user.twitter_id)
            if (existing and not force and existing.get("fingerprint") == fp
                    and pb.manifest_problem(existing, user.twitter_id) is None):
                counts["unchanged"] += 1
                db.session.expunge_all()
                continue
            if dry_run:
                counts["would_export"] += 1
                counts["would_export_nodes"] += _node_count(user)
                out(f"would export x_id={user.twitter_id} @{user.prefilled_handle}")
                db.session.expunge_all()
                continue
            manifest = export_one(user, storage, workdir, fp, snapshot)
            if manifest is None:
                counts["skipped_excluded"] += 1
                db.session.expunge_all()
                continue
            counts["exported"] += 1
            counts["exported_nodes"] += manifest["counts"]["nodes"]
            counts["exported_bytes"] += manifest["object"]["bytes"]
            out(f"exported x_id={manifest['x_id']} @{manifest['handle']}: "
                f"{manifest['counts']['nodes']} entries, "
                f"{manifest['counts']['profiles']} profile versions, "
                f"{manifest['object']['bytes'] // 1024} KB")
            db.session.expunge_all()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    out("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return dict(counts)


# ── Delete, unexclude, status ─────────────────────────────────────────────

def _x_id_arg(storage, args):
    from backend.utils import prefill_bundles as pb
    if args.x_id:
        if not pb.valid_x_id(args.x_id):
            sys.exit("An X id is digits only.")
        return str(args.x_id)
    x_id = pb.lookup_handle(storage, args.handle)
    if not x_id:
        sys.exit(f"No bundle under the handle {args.handle!r}; pass --x-id "
                 "(the account's numeric X id).")
    return x_id


def run_status(storage, x_id=None, out=print):
    from backend.utils import prefill_bundles as pb
    if x_id:
        manifest = pb.read_manifest(storage, x_id)
        excl = pb.exclusion(storage, x_id)
        if manifest:
            shown = {k: manifest.get(k) for k in (
                "x_id", "handle", "exported_at", "newest_entry_at", "counts",
                "bundle_format", "dump_format")}
            shown["bytes"] = (manifest.get("object") or {}).get("bytes")
            out(json.dumps(shown, indent=1, sort_keys=True))
        else:
            out(f"no bundle for x_id={x_id}")
        out(f"excluded: {excl.get('reason') if excl else 'no'}")
        return
    bundles = sum(1 for n in storage.list("bundles")
                  if n.endswith("/" + pb.MANIFEST_OBJECT))
    out(f"bundles: {bundles}, excluded X ids: {len(pb.excluded_x_ids(storage))}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--location", default=None,
                   help="gs://<bucket>[/<prefix>] or a directory (default: "
                        "PREFILL_BUNDLE_LOCATION)")
    sub = p.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="write bundles (lab only)")
    ex.add_argument("--dry-run", action="store_true")
    ex.add_argument("--only", default=None, help="one account: handle or X id")
    ex.add_argument("--limit", type=int, default=None,
                    help="stop after this many accounts exported")
    ex.add_argument("--force", action="store_true",
                    help="export even when the manifest's fingerprint matches")
    de = sub.add_parser("delete", help="delete a bundle and exclude its X id")
    who = de.add_mutually_exclusive_group(required=True)
    who.add_argument("--x-id")
    who.add_argument("--handle")
    de.add_argument("--reason", required=True)
    un = sub.add_parser("unexclude", help="take an X id off the exclusion list")
    un.add_argument("--x-id", required=True)
    st = sub.add_parser("status", help="counts, or one X id's manifest")
    st.add_argument("--x-id", default=None)
    args = p.parse_args(argv)

    from backend import create_app
    app = create_app()
    with app.app_context():
        from backend.utils import prefill_bundles as pb
        storage = _storage(args.location)
        if args.cmd == "export":
            run_export(storage, dry_run=args.dry_run, only=args.only,
                       limit=args.limit, force=args.force)
        elif args.cmd == "delete":
            x_id = _x_id_arg(storage, args)
            if args.reason not in pb.EXCLUSION_REASONS:
                sys.exit(f"--reason must be one of {', '.join(pb.EXCLUSION_REASONS)}")
            deleted = pb.delete_bundle(storage, x_id, args.reason)
            print(f"x_id={x_id} excluded ({args.reason}); deleted "
                  f"{len(deleted)} object(s): {', '.join(deleted) or 'none'}")
        elif args.cmd == "unexclude":
            if not pb.valid_x_id(args.x_id):
                sys.exit("An X id is digits only.")
            removed = pb.remove_exclusion(storage, args.x_id)
            print(f"x_id={args.x_id} {'taken off' if removed else 'was not on'} "
                  "the exclusion list")
        else:
            run_status(storage, args.x_id)


if __name__ == "__main__":
    main()
