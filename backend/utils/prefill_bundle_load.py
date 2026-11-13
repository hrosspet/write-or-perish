"""Loading a pre-fill bundle into an account on this server (#698).

A bundle (backend/utils/prefill_bundles.py) is the lab's prepared material
for one Community Archive member: their public tweets and the profile
(and intentions, where built) drafted from them. Two ways start a load:

- the admin's "Make ready from prepared material" for a handle or an X id
  (``start_admin_load``): it fills the account with the bundle's X id, or
  opens one under the bundle's handle with that X id, so the person's X
  sign-in finds it;
- the person's Yes on "Start from your tweets" (``maybe_start_self_load``):
  only for a signed-in account whose X id an X sign-in proved, and only
  when a bundle exists for that X id. With no bundle nothing changes.

A bundle is only ever loaded into the account whose X id is the bundle's;
a handle or an email never matches one. A "no" to the tweet seed, AI
usage None, a deleted account and an X id on the exclusion list are always
refused. The admin's load also refuses an account whose owner signed in
or that has writing of its own, unless the admin forces it; the person's
own Yes refuses only the second (their writing wins over a snapshot).

The load runs as background tasks (backend/tasks/prefill_bundles.py) in
committed chunks of NODE_CHUNK entries, and records its place in its
PrefillBundleLoad row: the bundle is downloaded and its sha256 checked
against the manifest, the header (profile versions, recent contexts,
todos, artifacts and the account's fields) is written in one commit, the
entries in chunks (each chunk with the map of dumped id -> new id, so a
resumed run needs nothing in memory), then the links between entries in
chunks. A run stops after RUN_SECONDS and sends the next; a run that
dies is resumed by the beat (``stalled_load_ids``) once its heartbeat is
STALE_AFTER old. Entries already in the account (same tweet key) are
not written again, so loading a bundle twice adds nothing twice; two
loads into one account run one after the other.

A bundle holds a pre-fill's material only (fetched tweets; model-drafted
profile, intentions and the like; ``check_prefill_entry``), and the load
changes nothing of the account that only a person's own action sets
(sign-in, terms, X handle, consent, settings, time zone). So an account
the admin made ready that nobody signs in to stays recognisable as
unclaimed, and is deleted after 90 days like any pre-filled one (#685):
``prefilled_handle`` is set, and the entries' ``updated_at`` is when this
server stored them.

Content is encrypted on write with this server's key, one data key per
chunk, as imports do (backend/utils/dump_rows.py). Nothing here
decrypts content or calls a model; the profile pipeline treats the
loaded profile as current (its cutoff and window set come with it).
"""
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta
from itertools import islice
from uuid import uuid4

from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError

from backend.extensions import db
from backend.models import (
    Node, PrefillBundleLoad, PrefillBundleNodeMap, User, UserArtifact,
    UserProfile, UserRecentContext, UserTodo,
)
from backend.utils import dump_rows
from backend.utils import prefill_bundles as pb

logger = logging.getLogger(__name__)

# ── Heuristics (#698, flagged in the PR) ─────────────────────────────────
# Entries per committed chunk: what load_user.py and the imports use. One
# chunk is about half a megabyte of entries in memory and one data key.
NODE_CHUNK = 500
# How long one task run works before it sends the next run and ends, so
# a large bundle is many short tasks the worker can interleave with
# others, not one that holds a worker for an hour.
RUN_SECONDS = 120
# A load whose run has sent no heartbeat for this long is resumed by the
# beat. A chunk takes seconds; the download of a large bundle well under
# a minute.
STALE_AFTER = timedelta(minutes=5)
# Runs in a row that ended without committing a chunk (killed, or an
# error) before the load is stopped as failed: two can be a deploy, a
# third is the load.
MAX_STALLED_RUNS = 3
# How long a failed run waits before the next try.
RETRY_COUNTDOWN_SECONDS = 60

ACTIVE = ("queued", "running")


class LostClaim(Exception):
    """Another run took the load over (this run's heartbeat went stale)."""


def _now():
    return datetime.utcnow()


def _dispatch(load_id, countdown=0):
    """Send a run of the load (a module hook: tests run it inline)."""
    from backend.tasks.prefill_bundles import load_prefill_bundle
    load_prefill_bundle.apply_async(args=(load_id,), countdown=countdown)


# ── Who may receive a bundle ──────────────────────────────────────────────

def account_signed_in(user):
    """Someone signed in to the account (or it predates the record, which
    counts the same: it may be an early signup)."""
    from backend.utils.activity import sign_in_status
    return sign_in_status(user) != "never"


def has_own_writing(user_id):
    """Whether the account holds writing of its own: an entry or upload
    that no pre-fill brought in, or a profile version the person wrote.
    Metadata only."""
    from backend.utils.privacy import PREFILL_PROVENANCES
    own_node = db.session.query(Node.id).filter(
        or_(Node.user_id == user_id, Node.human_owner_id == user_id),
        Node.deleted_at.is_(None),
        Node.node_type != "llm",
        func.coalesce(Node.provenance, "").notin_(PREFILL_PROVENANCES),
    ).first()
    if own_node is not None:
        return True
    return db.session.query(UserProfile.id).filter(
        UserProfile.user_id == user_id,
        UserProfile.generated_by == "user").first() is not None


def hard_refusal(user, x_id):
    """(code, message) when no bundle may be loaded into ``user`` at all,
    forced or not; else None. Checked when a load starts and at every
    run, since these can change while it runs."""
    from backend.utils.hidden_rows import account_on_hold
    from backend.utils.privacy import prefill_refusal
    if user is None:
        return "no_account", "The account no longer exists."
    if user.deleted_at is not None or account_on_hold(user.id):
        return "deleted", "The account is deleted or being deleted."
    if str(user.twitter_id or "") != str(x_id):
        return ("x_id_mismatch",
                "The account's X id is not the bundle's X id.")
    return prefill_refusal(user)


def target_refusal(user, x_id, *, allow_signed_in, allow_own_writing):
    """hard_refusal, then the protections a forced admin load lifts."""
    refusal = hard_refusal(user, x_id)
    if refusal:
        return refusal
    if not allow_signed_in and account_signed_in(user):
        return ("signed_in", "Someone has signed in to this account. "
                             "Load only with the force option.")
    if not allow_own_writing and has_own_writing(user.id):
        return ("own_writing", "The account has writing of its own. "
                               "Load only with the force option.")
    return None


# ── Starting a load ───────────────────────────────────────────────────────

def resolve_x_id(storage, query):
    """An X id from what the admin typed: digits are an X id, anything
    else a handle looked up in the bucket's handle index."""
    query = (query or "").strip().lstrip("@")
    if pb.valid_x_id(query):
        return query
    return pb.lookup_handle(storage, query)


def _latest_load(user_id, x_id=None, statuses=None):
    q = PrefillBundleLoad.query.filter(PrefillBundleLoad.user_id == user_id)
    if x_id is not None:
        q = q.filter(PrefillBundleLoad.x_id == str(x_id))
    if statuses is not None:
        q = q.filter(PrefillBundleLoad.status.in_(statuses))
    return q.order_by(PrefillBundleLoad.id.desc()).first()


def _new_load(user, manifest, *, origin, requested_by_id=None, forced=False,
              approve_when_done=False, status="queued", error=None):
    obj = manifest.get("object") or {}
    counts = manifest.get("counts") or {}
    now = _now()
    load = PrefillBundleLoad(
        user_id=user.id, x_id=str(manifest["x_id"]),
        handle=pb.clean_handle(manifest.get("handle")), origin=origin,
        requested_by_id=requested_by_id, forced=bool(forced),
        approve_when_done=bool(approve_when_done), status=status,
        stage="download", error=error, bundle_sha256=obj.get("sha256") or "",
        bundle_format=manifest.get("bundle_format"),
        dump_format=manifest.get("dump_format"),
        exported_at=pb.parse_time(manifest.get("exported_at")),
        newest_entry_at=pb.parse_time(manifest.get("newest_entry_at")),
        nodes_total=int(counts.get("nodes") or 0),
        heartbeat_at=now, created_at=now,
        finished_at=now if status == "refused" else None)
    db.session.add(load)
    return load


def _resume(load):
    """A failed load of the same bundle continues from where it stopped."""
    load.status = "queued"
    load.error = None
    load.run_token = None
    load.stalled_runs = 0
    load.finished_at = None
    load.heartbeat_at = _now()


def _new_account(x_id, handle):
    """An account for the bundle's X id under its handle (or a free
    variant of it), unapproved, as a whitelist opens one. Its owner's X
    sign-in finds it by the X id."""
    from backend.utils.reserved_usernames import (
        derive_available_username, validate_username)
    base = pb.clean_handle(handle) or f"x{x_id}"
    username = base if validate_username(base) is None else \
        derive_available_username(base)
    user = User(twitter_id=str(x_id), username=username, approved=False)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent request (or the person's own X sign-in) made it.
        db.session.rollback()
        user = User.query.filter_by(twitter_id=str(x_id)).first()
        if user is None:
            raise
    return user


def start_admin_load(query, admin_id, *, force=False, approve=True,
                     dispatch=None):
    """The admin's "Make ready from prepared material". Returns (load,
    None) or (None, (code, message)). Safe to repeat: a load of the
    bundle under way is returned as it is, a failed one continues, and a
    finished one is not loaded again."""
    if not pb.bundles_enabled():
        return None, ("disabled", "Pre-fill bundles are switched off here.")
    storage = pb.configured_storage()
    x_id = resolve_x_id(storage, query)
    if not x_id:
        return None, ("no_bundle", "No prepared material under that handle.")
    manifest = pb.read_manifest(storage, x_id)
    problem = pb.manifest_problem(manifest, x_id)
    if problem:
        return None, (problem.code, problem.message)
    if pb.exclusion(storage, x_id) is not None:
        return None, ("excluded", "This X id is on the exclusion list.")
    user = User.query.filter_by(twitter_id=str(x_id)).first()
    if user is None:
        user = _new_account(x_id, manifest.get("handle"))
    refusal = target_refusal(user, x_id, allow_signed_in=force,
                             allow_own_writing=force)
    if refusal:
        return None, refusal
    sha = (manifest.get("object") or {}).get("sha256")
    active = _latest_load(user.id, x_id, ACTIVE)
    if active is not None:
        return active, None
    previous = _latest_load(user.id, x_id, ("done", "failed"))
    if previous is not None and previous.bundle_sha256 == sha:
        if previous.status == "done":
            return None, ("already_loaded",
                          "This bundle is already loaded into the account.")
        _resume(previous)
        previous.forced = previous.forced or bool(force)
        previous.approve_when_done = bool(approve)
        db.session.commit()
        (dispatch or _dispatch)(previous.id)
        return previous, None
    load = _new_load(user, manifest, origin="admin", requested_by_id=admin_id,
                     forced=force, approve_when_done=approve)
    db.session.commit()
    logger.info("Pre-fill bundle load %s queued by admin %s for user %s",
                load.id, admin_id, user.id)
    (dispatch or _dispatch)(load.id)
    return load, None


def _record_refused(user, manifest, code):
    """A refused automatic load, once per bundle and reason, for the
    admin's list."""
    sha = (manifest.get("object") or {}).get("sha256") or ""
    seen = PrefillBundleLoad.query.filter_by(
        user_id=user.id, x_id=str(user.twitter_id), status="refused",
        bundle_sha256=sha, error=code).first()
    if seen is None:
        _new_load(user, manifest, origin="self", status="refused", error=code)
        db.session.commit()
    logger.info("Pre-fill bundle for user %s not loaded on their Yes: %s",
                user.id, code)


def maybe_start_self_load(user, dispatch=None):
    """On a Yes to "Start from your tweets": start the load of the
    person's bundle, if there is one. Returns the load, or None when
    nothing starts (no bundle: nothing changes from before). Never
    raises: the Yes itself is saved either way."""
    try:
        if not pb.bundles_enabled():
            return None
        if user.prefill_consent != "yes":
            return None
        # The X id an X sign-in proved: twitter_handle is written only by
        # an X sign-in or Connect X (routes/auth.py), never by an admin.
        if not (user.twitter_id and user.twitter_handle) \
                or not pb.valid_x_id(user.twitter_id):
            return None
        x_id = str(user.twitter_id)
        current = _latest_load(user.id, x_id, ACTIVE + ("done", "failed"))
        if current is not None:
            return current
        storage = pb.configured_storage()
        manifest = pb.read_manifest(storage, x_id)
        if manifest is None:
            return None
        problem = pb.manifest_problem(manifest, x_id)
        if problem:
            logger.error("Pre-fill bundle for user %s not loadable: %s",
                         user.id, problem.code)
            _record_refused(user, manifest, problem.code)
            return None
        if pb.exclusion(storage, x_id) is not None:
            _record_refused(user, manifest, "excluded")
            return None
        refusal = target_refusal(user, x_id, allow_signed_in=True,
                                 allow_own_writing=False)
        if refusal:
            _record_refused(user, manifest, refusal[0])
            return None
        load = _new_load(user, manifest, origin="self", approve_when_done=True)
        db.session.commit()
        logger.info("Pre-fill bundle load %s started by user %s's Yes",
                    load.id, user.id)
        (dispatch or _dispatch)(load.id)
        return load
    except Exception as e:  # noqa: BLE001 - the Yes must not fail
        db.session.rollback()
        logger.error("Could not start a pre-fill bundle load for user %s: %s",
                     getattr(user, "id", None), type(e).__name__)
        return None


# ── What the person and the admin see ─────────────────────────────────────

def starting_point(user_id):
    """The state of the account's newest load, for the person's screen:
    None (no load), or {"status": "preparing" | "ready" | "failed",
    "done", "total"}. Refused loads change nothing for the person."""
    load = PrefillBundleLoad.query.filter(
        PrefillBundleLoad.user_id == user_id,
        PrefillBundleLoad.status != "refused",
    ).order_by(PrefillBundleLoad.id.desc()).first()
    if load is None:
        return None
    status = {"queued": "preparing", "running": "preparing",
              "done": "ready", "failed": "failed"}.get(load.status)
    if status is None:
        return None
    return {"status": status, "done": load.nodes_done,
            "total": load.nodes_total}


def serialize_load(load, user=None):
    """The admin's view of a load: metadata and counts only."""
    from backend.utils.timefmt import iso_utc
    if user is None:
        user = db.session.get(User, load.user_id)
    return {
        "id": load.id, "user_id": load.user_id,
        "username": user.username if user else None,
        "approved": bool(user.approved) if user else None,
        "x_id": load.x_id, "handle": load.handle, "origin": load.origin,
        "forced": load.forced, "approve_when_done": load.approve_when_done,
        "status": load.status, "stage": load.stage, "error": load.error,
        "nodes_total": load.nodes_total, "nodes_done": load.nodes_done,
        "links_done": load.links_done, "created": load.created_count,
        "skipped": load.skipped_count, "relinked": load.relinked_count,
        "profiles": load.profiles_count, "artifacts": load.artifacts_count,
        "exported_at": iso_utc(load.exported_at),
        "newest_entry_at": iso_utc(load.newest_entry_at),
        "created_at": iso_utc(load.created_at),
        "started_at": iso_utc(load.started_at),
        "finished_at": iso_utc(load.finished_at),
    }


def check(query):
    """What "Make ready" would do for ``query`` (handle or X id), for the
    admin's Check button: the bundle's metadata, the account it would go
    into, and why it would be refused. Reads no content."""
    from backend.utils.activity import sign_in_status
    if not pb.bundles_enabled():
        return {"error": "Pre-fill bundles are switched off here.",
                "code": "disabled"}
    storage = pb.configured_storage()
    x_id = resolve_x_id(storage, query)
    if not x_id:
        return {"error": "No prepared material under that handle.",
                "code": "no_bundle"}
    manifest = pb.read_manifest(storage, x_id)
    problem = pb.manifest_problem(manifest, x_id)
    out = {"x_id": x_id}
    if problem:
        out.update(error=problem.message, code=problem.code)
        return out
    out["bundle"] = {
        "handle": manifest.get("handle"),
        "exported_at": manifest.get("exported_at"),
        "oldest_entry_at": manifest.get("oldest_entry_at"),
        "newest_entry_at": manifest.get("newest_entry_at"),
        "counts": manifest.get("counts") or {},
        "profile_generated_by": manifest.get("profile_generated_by"),
        "bytes": (manifest.get("object") or {}).get("bytes"),
    }
    excluded = pb.exclusion(storage, x_id)
    if excluded is not None:
        out.update(error="This X id is on the exclusion list.",
                   code="excluded", excluded_reason=excluded.get("reason"))
        return out
    user = User.query.filter_by(twitter_id=str(x_id)).first()
    if user is None:
        handle = pb.clean_handle(manifest.get("handle"))
        holder = (User.query.filter(func.lower(User.username) == handle.lower())
                  .first() if handle else None)
        out["account"] = {"exists": False, "username": handle,
                          "username_taken_by_user_id":
                              holder.id if holder else None}
        return out
    out["account"] = {
        "exists": True, "user_id": user.id, "username": user.username,
        "approved": bool(user.approved), "sign_in": sign_in_status(user),
        "own_writing": has_own_writing(user.id),
        "prefill_consent": user.prefill_consent,
    }
    refusal = target_refusal(user, x_id, allow_signed_in=False,
                             allow_own_writing=False)
    if refusal:
        out["refusal"] = {"code": refusal[0], "message": refusal[1]}
        forced = target_refusal(user, x_id, allow_signed_in=True,
                                allow_own_writing=True)
        out["refusal"]["forceable"] = forced is None
    last = _latest_load(user.id, x_id)
    if last is not None:
        out["last_load"] = serialize_load(last, user)
    return out


# ── Running a load ────────────────────────────────────────────────────────

def _work_dir():
    """Where a worker keeps the bundles it is loading (a tests hook)."""
    return os.path.join(tempfile.gettempdir(), "loore-prefill-bundles")


def _work_path(load_id, sha=""):
    folder = _work_dir()
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"load-{load_id}-{(sha or '')[:16]}.jsonl.gz")


def _remove_quietly(path):
    if not path:
        return
    for p in (path, path + ".part"):
        try:
            os.remove(p)
        except OSError:
            pass


def _claim(load_id):
    """Take the load for this run, or None when another run holds it or
    an earlier load into the same account is still under way (two loads
    started at once, by a double click or the admin and the person's
    Yes, run one after the other; the second then finds the entries
    there and adds nothing twice)."""
    now = _now()
    user_id = db.session.query(PrefillBundleLoad.user_id).filter(
        PrefillBundleLoad.id == load_id).scalar()
    if user_id is None:
        return None
    earlier = db.session.query(PrefillBundleLoad.id).filter(
        PrefillBundleLoad.user_id == user_id,
        PrefillBundleLoad.id < load_id,
        PrefillBundleLoad.status.in_(ACTIVE)).first()
    if earlier is not None:
        return None
    token = uuid4().hex
    taken = db.session.execute(
        update(PrefillBundleLoad).where(
            PrefillBundleLoad.id == load_id,
            PrefillBundleLoad.status.in_(ACTIVE),
            or_(PrefillBundleLoad.run_token.is_(None),
                PrefillBundleLoad.heartbeat_at.is_(None),
                PrefillBundleLoad.heartbeat_at < now - STALE_AFTER),
        ).values(run_token=token, heartbeat_at=now, status="running",
                 stalled_runs=PrefillBundleLoad.stalled_runs + 1,
                 started_at=func.coalesce(PrefillBundleLoad.started_at, now))
    ).rowcount
    db.session.commit()
    return token if taken else None


def _progress(load_id, token, **values):
    """Record progress in the current transaction, only while this run
    still holds the load; the caller commits."""
    values.setdefault("heartbeat_at", _now())
    held = db.session.execute(
        update(PrefillBundleLoad).where(
            PrefillBundleLoad.id == load_id,
            PrefillBundleLoad.run_token == token,
        ).values(**values)).rowcount
    if not held:
        raise LostClaim()


def _release(load_id, token):
    _progress(load_id, token, run_token=None)
    db.session.commit()


def _fail(load_id, token, code):
    db.session.rollback()
    db.session.execute(
        update(PrefillBundleLoad).where(
            PrefillBundleLoad.id == load_id,
            or_(PrefillBundleLoad.run_token == token,
                PrefillBundleLoad.run_token.is_(None)),
        ).values(status="failed", error=str(code)[:255], run_token=None,
                 finished_at=_now()))
    db.session.commit()
    # Problems show: the admin sees the load as failed, and this reaches
    # the error monitor. A code or a class name only, never content.
    logger.error("Pre-fill bundle load %s failed: %s", load_id, code)


def _local_copy(load, storage):
    """The bundle as a local file, downloaded when this worker has none,
    with its sha256 checked against the manifest's on every run. A local
    copy that does not match is downloaded once more; a download that
    does not match stops the load before anything else is written."""
    path = _work_path(load.id, load.bundle_sha256)
    if os.path.exists(path) and pb.sha256_file(path) == load.bundle_sha256:
        return path
    _remove_quietly(path)
    part = path + ".part"
    if not storage.download(pb.bundle_name(load.x_id), part):
        raise pb.BundleError("no_bundle", "The bundle is gone from storage.")
    os.replace(part, path)
    if pb.sha256_file(path) != load.bundle_sha256:
        _remove_quietly(path)
        raise pb.BundleError("checksum", "The bundle's checksum does not "
                                         "match its manifest.")
    return path


# What a person's own action makes (an edit, a revert): a bundle holds
# none of it (PERSON_VERSIONS of the unclaimed-account deletion, #685).
PERSON_MADE = ("user", "revert")


def check_prefill_entry(nd, dump_username):
    """A bundle holds what a pre-fill makes and nothing else: the person's
    public posts Loore fetched (origin "twitter", a pre-fill provenance, a
    source key), by the bundle's account. Anything else (a model's reply,
    an entry someone wrote) would make an account nobody claimed look
    used, and is refused."""
    from backend.utils.privacy import PREFILL_PROVENANCES
    if (nd.get("origin") != "twitter" or not nd.get("source_key")
            or nd.get("provenance") not in PREFILL_PROVENANCES
            or nd.get("node_type", "user") != "user" or nd.get("prompt")):
        raise pb.BundleError("not_prefill",
                             "The bundle holds an entry no pre-fill makes.")
    if (nd.get("author_username") or dump_username) != dump_username:
        raise pb.BundleError("foreign_author",
                             "An entry in the bundle is by another account.")


def check_prefill_header(header):
    """Refuse versions a person saved (an edit, a revert): a pre-fill's
    profile, todos and artifacts are all drafted by a model."""
    for key in ("profiles", "todos", "artifacts"):
        for row in header.get(key) or []:
            if row.get("generated_by") in PERSON_MADE:
                raise pb.BundleError(
                    "not_prefill", "The bundle holds a version a person saved.")


def _load_header(load, user, header, token):
    """The account's fields and everything small, in one commit. Rows the
    account already has from an earlier load of the material (same time,
    model and type) are not written again."""
    from backend.utils.intentions_versions import KIND as INTENTIONS, protected_version
    uid = user.id
    handle = pb.clean_handle(load.handle)
    if handle and not user.prefilled_handle:
        # The handle the tweets came from; the X id proved it is theirs.
        # It is also what makes an account nobody signed in to known to
        # the deletion of unclaimed pre-filled accounts (#685).
        user.prefilled_handle = handle
    # As load_user.py: no profile build of a loaded account crosses
    # providers on its own (#589's public-only rule, #687).
    user.profile_private_read = True
    # As every pre-fill: a bootstrapped corpus is built on the Batch path.
    user.profile_force_batch = True
    # Nothing else of the account changes: not its time zone, consent,
    # sign-in, X handle, settings or defaults. Each of those is a sign
    # that a person used the account, and an account nobody claimed must
    # stay recognisable as unclaimed for its deletion after 90 days
    # (#685). The bundle's own time zone is the lab's, not the person's.
    check_prefill_header(header)

    profiles = header.get("profiles") or []
    have = {(p.created_at, p.generated_by, p.generation_type): p.id
            for p in UserProfile.query.with_entities(
                UserProfile.created_at, UserProfile.generated_by,
                UserProfile.generation_type, UserProfile.id).filter(
                UserProfile.user_id == uid)}
    profile_map, made = {}, []
    for pf in profiles:
        key = (dump_rows.dt(pf.get("created_at")),
               pf.get("generated_by") or "import",
               pf.get("generation_type") or "initial")
        if key[0] is not None and key in have:
            profile_map[pf["id"]] = have[key]
            continue
        row = dump_rows.new_profile(pf, uid)
        db.session.add(row)
        db.session.flush()
        profile_map[pf["id"]] = row.id
        made.append(pf)
    dump_rows.remap_profile_links(made, profile_map)

    def seen(model, *extra):
        return {tuple(r) for r in db.session.query(
            model.created_at, model.generated_by, *extra).filter(
            model.user_id == uid)}

    have_rc = seen(UserRecentContext)
    for rc in header.get("recent_contexts") or []:
        if (dump_rows.dt(rc.get("created_at")), rc.get("generated_by") or "import") in have_rc:
            continue
        db.session.add(dump_rows.new_recent_context(
            rc, uid, profile_map.get(rc.get("profile_id"))))
    have_td = seen(UserTodo)
    for td in header.get("todos") or []:
        if (dump_rows.dt(td.get("created_at")), td.get("generated_by") or "import") in have_td:
            continue
        db.session.add(dump_rows.new_todo(td, uid))
    # The user's edit wins (#412): intentions the person wrote stay.
    kept_intentions = protected_version(uid)
    have_a = seen(UserArtifact, UserArtifact.kind)
    artifacts = 0
    for a in header.get("artifacts") or []:
        if a.get("kind") == INTENTIONS and kept_intentions is not None:
            continue
        key = (dump_rows.dt(a.get("created_at")),
               a.get("generated_by") or "import", a.get("kind"))
        if key in have_a:
            continue
        db.session.add(dump_rows.new_artifact(a, uid))
        artifacts += 1
    _progress(load.id, token, stage="nodes", profiles_count=len(made),
              artifacts_count=artifacts, stalled_runs=0)
    db.session.commit()


def _node_chunk(load, uid, path, token, chunk):
    """Pass 1: write the next chunk of entries. False when none are left.
    Every entry is checked to be a pre-fill's (check_prefill_entry) before
    the chunk writes anything."""
    from backend.utils.encryption import new_dek
    header, entries = pb.open_bundle(path, skip=load.nodes_done)
    try:
        batch = list(islice(entries, chunk))
    finally:
        entries.close()
    if not batch:
        _progress(load.id, token, stage="links", stalled_runs=0)
        db.session.commit()
        return False
    dump_username = (header.get("user") or {}).get("username")
    for nd in batch:
        check_prefill_entry(nd, dump_username)
    # Already in the account (a load run again, or a pre-fill made here
    # before): matched on the tweet's key. Metadata only.
    keys = [nd["source_key"] for nd in batch]
    existing = dict(db.session.query(Node.source_key, Node.id).filter(
        Node.human_owner_id == uid, Node.source_key.in_(keys)))
    # Stored now: an entry's updated_at is when it was stored here (the
    # deletion of unclaimed accounts, #685, counts from the first one),
    # not when the lab stored it.
    stored_at = _now()
    dek = None
    created = skipped = 0
    for nd in batch:
        key = nd["source_key"]
        if key in existing:
            db.session.add(PrefillBundleNodeMap(
                load_id=load.id, old_id=nd["id"], new_id=existing[key],
                created=False, user_id=uid))
            skipped += 1
            continue
        if dek is None and nd.get("privacy_level", "private") != "public":
            dek = new_dek() or False   # False: encryption is off here
        node = dump_rows.new_node(nd, uid, uid, dek=dek or None)
        node.updated_at = stored_at
        db.session.add(node)
        db.session.flush()
        existing[key] = node.id
        db.session.add(PrefillBundleNodeMap(
            load_id=load.id, old_id=nd["id"], new_id=node.id, created=True,
            user_id=uid))
        if nd.get("embedding"):
            emb = dump_rows.new_embedding(nd["embedding"], node.id, uid)
            emb.node_updated_at = stored_at   # still matches the entry
            db.session.add(emb)
        created += 1
    _progress(load.id, token, nodes_done=load.nodes_done + len(batch),
              created_count=load.created_count + created,
              skipped_count=load.skipped_count + skipped, stalled_runs=0)
    db.session.commit()
    db.session.expunge_all()
    return True


def _link_chunk(load, path, token, chunk):
    """Pass 2: point the next chunk's new entries at their parents and
    continuations. False when none are left."""
    _header, entries = pb.open_bundle(path, skip=load.links_done)
    try:
        batch = list(islice(entries, chunk))
    finally:
        entries.close()
    if not batch:
        _progress(load.id, token, stage="finish", stalled_runs=0)
        db.session.commit()
        return False
    linked = [nd for nd in batch
              if any(nd.get(c) for c in dump_rows.LINK_COLUMNS)]
    relinked = 0
    if linked:
        wanted = {nd["id"] for nd in linked}
        wanted |= {nd[c] for nd in linked for c in dump_rows.LINK_COLUMNS
                   if nd.get(c)}
        mapped = {r.old_id: (r.new_id, r.created)
                  for r in PrefillBundleNodeMap.query.filter(
                      PrefillBundleNodeMap.load_id == load.id,
                      PrefillBundleNodeMap.old_id.in_(wanted))}
        for nd in linked:
            own = mapped.get(nd["id"])
            if own is None or not own[1]:
                continue    # an entry that was already here keeps its links
            values = {c: mapped[nd[c]][0] for c in dump_rows.LINK_COLUMNS
                      if nd.get(c) in mapped}
            if values:
                # updated_at kept: the entry was stored, not edited (its
                # embedding stays current; #685 reads it as the store time).
                values["updated_at"] = Node.updated_at
                db.session.query(Node).filter(Node.id == own[0]).update(
                    values, synchronize_session=False)
                relinked += 1
    _progress(load.id, token, links_done=load.links_done + len(batch),
              relinked_count=load.relinked_count + relinked, stalled_runs=0)
    db.session.commit()
    db.session.expunge_all()
    return True


def _finish(load, token):
    PrefillBundleNodeMap.query.filter_by(load_id=load.id).delete(
        synchronize_session=False)
    user = db.session.get(User, load.user_id)
    approved = False
    if load.approve_when_done and not user.approved \
            and user.deactivated_at is None:
        # "Lets them in when it is ready" (Peter, 2026-11-13). An account
        # an admin deactivated stays deactivated.
        user.approved = True
        user.approved_at = _now()
        approved = True
    _progress(load.id, token, status="done", stage="done", error=None,
              run_token=None, finished_at=_now())
    db.session.commit()
    logger.info("Pre-fill bundle load %s done for user %s: %s entries "
                "created, %s already there, %s relinked%s", load.id,
                load.user_id, load.created_count, load.skipped_count,
                load.relinked_count, ", account approved" if approved else "")


def _stop_reason(load, user, storage):
    """Why the load must stop now (a code), or None. A "no" given
    meanwhile stops it (#662): what was loaded stays, as for any
    pre-fill."""
    if load.stalled_runs > MAX_STALLED_RUNS:
        return "stalled"
    refusal = hard_refusal(user, load.x_id)
    if refusal:
        return refusal[0]
    if pb.exclusion(storage, load.x_id) is not None:
        return "excluded"
    return None


def _work(load_id, token, path, user, deadline, chunk, max_chunks):
    """The header, then chunks of entries and of links, until the load
    reaches "finish" or the run's time (or chunk count) is up. Returns
    the stage reached."""
    header, entries = pb.open_bundle(path)
    entries.close()
    load = db.session.get(PrefillBundleLoad, load_id)
    if load.stage == "download":
        _progress(load_id, token, stage="header")
        db.session.commit()
        load = db.session.get(PrefillBundleLoad, load_id)
    if load.stage == "header":
        _load_header(load, user, header, token)
    steps = 0
    while time.monotonic() < deadline and (max_chunks is None or steps < max_chunks):
        load = db.session.get(PrefillBundleLoad, load_id)
        if load.stage == "nodes":
            consent = db.session.query(User.prefill_consent).filter(
                User.id == load.user_id).scalar()
            if consent == "no":
                raise pb.BundleError("prefill_declined", "The person said no.")
            _node_chunk(load, load.user_id, path, token, chunk)
        elif load.stage == "links":
            _link_chunk(load, path, token, chunk)
        else:
            break
        steps += 1
    return db.session.get(PrefillBundleLoad, load_id).stage


def run_load(load_id, *, budget=RUN_SECONDS, chunk=NODE_CHUNK, dispatch=None,
             max_chunks=None):
    """One run of a load (the task's body). Works until the load is done
    or ``budget`` seconds have passed (or ``max_chunks`` chunks, a tests
    hook), then sends the next run. Returns "done", "continuing",
    "failed", "retrying", "busy" (another run holds it), "lost" or
    "disabled"."""
    dispatch = dispatch or _dispatch
    if not pb.bundles_enabled():
        return "disabled"   # the beat resumes it once switched on again
    token = _claim(load_id)
    if token is None:
        return "busy"
    deadline = time.monotonic() + budget
    path = None
    try:
        load = db.session.get(PrefillBundleLoad, load_id)
        path = _work_path(load_id, load.bundle_sha256)
        user = db.session.get(User, load.user_id)
        storage = pb.configured_storage()
        reason = _stop_reason(load, user, storage)
        if reason:
            raise pb.BundleError(reason, reason)
        path = _local_copy(load, storage)
        stage = _work(load_id, token, path, user, deadline, chunk, max_chunks)
        if stage == "finish":
            _finish(db.session.get(PrefillBundleLoad, load_id), token)
            _remove_quietly(path)
            return "done"
        _release(load_id, token)
        dispatch(load_id)
        return "continuing"
    except LostClaim:
        db.session.rollback()
        return "lost"
    except pb.BundleError as e:
        _fail(load_id, token, e.code)
        _remove_quietly(path)
        return "failed"
    except Exception as e:  # noqa: BLE001 - counted, retried, then failed
        db.session.rollback()
        logger.warning("Pre-fill bundle load %s: a run stopped (%s); "
                       "trying again", load_id, type(e).__name__)
        try:
            _release(load_id, token)
        except Exception:  # noqa: BLE001 - the beat resumes it when stale
            db.session.rollback()
            return "retrying"
        dispatch(load_id, countdown=RETRY_COUNTDOWN_SECONDS)
        return "retrying"


def stalled_load_ids():
    """Loads to send again (the beat): queued or running, with no
    heartbeat for STALE_AFTER. Their heartbeat is moved to now, so one
    waiting in the queue is not sent again on every beat."""
    if not pb.bundles_enabled():
        return []
    now = _now()
    ids = [i for (i,) in db.session.query(PrefillBundleLoad.id).filter(
        PrefillBundleLoad.status.in_(ACTIVE),
        or_(PrefillBundleLoad.heartbeat_at.is_(None),
            PrefillBundleLoad.heartbeat_at < now - STALE_AFTER))]
    if ids:
        db.session.query(PrefillBundleLoad).filter(
            PrefillBundleLoad.id.in_(ids)).update(
            {PrefillBundleLoad.heartbeat_at: now,
             PrefillBundleLoad.run_token: None},
            synchronize_session=False)
        db.session.commit()
    return ids
