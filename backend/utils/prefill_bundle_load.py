"""Loading a pre-fill bundle into an account on this server (#698).

A bundle (backend/utils/prefill_bundles.py) is the lab's prepared material
for one Community Archive member: their public tweets and the profile
(and intentions, where built) drafted from them. Two ways start a load:

- the admin's "Make ready from prepared material" for a handle or an X id
  (``start_admin_load``): it fills the account with the bundle's X id, or
  opens one under the bundle's handle with that X id, so the person's X
  sign-in finds it. It does not let anyone in: the admin approves as for
  any account (and with payments on, #700's gate does);
- the person's Yes on "Start from your tweets" (``maybe_start_self_load``):
  only for a signed-in account whose X id an X sign-in proved. The request
  only records the Yes and queues a check; the background task looks for
  a bundle and loads it, so a Yes never waits on the storage. With no
  bundle nothing changes. When the load is done the person is let in
  (``let_in_when_ready``).

A bundle is only ever loaded into the account whose X id is the bundle's;
a handle or an email never matches one. A "no" to the tweet seed and AI
usage None are always refused. The admin's load also refuses an account
whose owner signed in or that has writing of its own, unless the admin
forces it; the person's own Yes refuses only the second (their writing
wins over a snapshot). A "no" given while a load runs stops it: what was
loaded stays and is linked (#662: a no keeps what was made), and a later
Yes continues where it stopped.

Nothing in a bundle is trusted beyond what a pre-fill makes. Each entry
is checked against an allow-list (``clean_entry``): a public post Loore
fetched, by the bundle's account, with no model fields; it is stored
private, with AI usage at most "chat" (never "train"; a lower value in
the bundle stays: a load never raises anything), and encrypted on write
like any private entry. The profile, recent contexts and intentions are
stored the same way; todos, other artifacts and versions a person saved
are refused.

The load runs as background tasks (backend/tasks/prefill_bundles.py), one
bundle part (MEMBER_LINES entries, one gzip member) per committed chunk,
read by its byte range, checked against its sha256, and never re-read: a
run continues at the next part. Its PrefillBundleLoad row holds the place
(entries done, links done) and the map of dumped id -> new id is in
PrefillBundleNodeMap, so a resumed run needs nothing in memory. A run
stops after RUN_SECONDS and sends the next; a run that dies is resumed by
the beat (``stalled_load_ids``) once its heartbeat is STALE_AFTER old.
Entries already in the account (same tweet key) are not written again,
and two loads into one account run one after the other.

The load changes nothing of the account that only a person's own action
sets (sign-in, terms, X handle, consent, settings, time zone), so an
account the admin made ready that nobody signs in to stays recognisable
as unclaimed and is deleted after 90 days like any pre-filled one (#685):
``prefilled_handle`` is set, and the entries' ``updated_at`` is when this
server stored them.

Content is encrypted with this server's key, one data key per chunk, as
imports do (backend/utils/dump_rows.py). Nothing here decrypts content or
calls a model; the profile pipeline treats the loaded profile as current.
"""
import logging
import time
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError

from backend.extensions import db
from backend.models import (
    Node, PrefillBundleLoad, PrefillBundleNodeMap, User, UserArtifact,
    UserProfile, UserRecentContext,
)
from backend.utils import dump_rows
from backend.utils import prefill_bundles as pb

logger = logging.getLogger(__name__)

# ── Heuristics (#698, flagged in the PR) ─────────────────────────────────
# How long one task run works before it sends the next run and ends, so
# a large bundle is many short tasks the worker can interleave with
# others, not one that holds a worker for an hour.
RUN_SECONDS = 120
# A load whose run has sent no heartbeat for this long is resumed by the
# beat. A chunk takes seconds.
STALE_AFTER = timedelta(minutes=5)
# Runs in a row that ended without committing a chunk (killed, or an
# error) before the load is stopped as failed: two can be a deploy, a
# third is the load.
MAX_STALLED_RUNS = 3
# How long a failed run waits before the next try.
RETRY_COUNTDOWN_SECONDS = 60

ACTIVE = ("queued", "running")
# Outcomes that are a person's or an admin's choice, not a fault: logged
# as information, not as errors.
EXPECTED_STOPS = ("prefill_declined", "ai_opt_out", "excluded", "deleted",
                  "no_account")

# What a person's own action makes (an edit, a revert): a bundle holds
# none of it (PERSON_VERSIONS of the unclaimed-account deletion, #685).
PERSON_MADE = ("user", "revert")
# What a load gives every entry, profile version, recent context and
# artifact, whatever the bundle says: a pre-fill's own values
# (tasks/imports._import_prefill_rows). Never public; AI usage at most
# "chat" (never "train"), and a lower value in the bundle stays lower:
# a load never raises anything.
LOADED_PRIVACY = "private"
LOADED_AI_USAGE = "chat"


def capped_ai_usage(value):
    """A bundle's AI usage, capped at what a pre-fill gives."""
    return "none" if value == "none" else LOADED_AI_USAGE


# Allow-lists: the only fields of a bundle that reach a row. Anything
# else is dropped; MUST_BE_EMPTY refuses the bundle when set.
ENTRY_FIELDS = ("id", "author_username", "owner_is_user", "parent_id",
                "continuation_node_id", "linked_node_id", "node_type",
                "content", "token_count", "source_key", "origin",
                "provenance", "created_at", "embedding")
MUST_BE_EMPTY = ("llm_model", "prompt", "prompt_key", "tool_calls_meta")
EMBEDDING_FIELDS = ("model", "content_hash", "vector", "node_updated_at")
PROFILE_FIELDS = ("id", "content", "generated_by", "tokens_used",
                  "created_at", "source_tokens_used", "source_data_cutoff",
                  "source_origin_stats", "generation_type",
                  "parent_profile_id", "merged_profile_id", "window_set",
                  "covers_start", "source_rendered_at")
RECENT_FIELDS = ("content", "generated_by", "tokens_used", "created_at",
                 "source_data_cutoff", "source_tokens_covered", "profile_id")
ARTIFACT_FIELDS = ("kind", "title", "description", "content",
                   "generated_by", "tokens_used", "created_at")
# Artifact kinds a pre-fill drafts.
ARTIFACT_KINDS = ("intentions",)


class LostClaim(Exception):
    """Another run took the load over (this run's heartbeat went stale)."""


def _now():
    return datetime.utcnow()


def _dispatch(load_id, countdown=0):
    """Send a run of the load (a module hook: tests run it inline)."""
    from backend.tasks.prefill_bundles import load_prefill_bundle
    load_prefill_bundle.apply_async(args=(load_id,), countdown=countdown)


# ── What a bundle may hold ────────────────────────────────────────────────

def _not_prefill(what):
    return pb.BundleError("not_prefill", f"The bundle holds {what} no "
                                         "pre-fill makes.")


def clean_entry(nd, dump_username):
    """The fields of a dumped entry a load writes, from the allow-list,
    with the load's own privacy and AI usage; or BundleError. An entry is
    a public post Loore fetched (origin "twitter", a pre-fill provenance,
    a source key), written by the bundle's account, with no model fields:
    anything else would also make an account nobody claimed look used."""
    from backend.utils.privacy import PREFILL_PROVENANCES
    if any(nd.get(k) for k in MUST_BE_EMPTY):
        raise _not_prefill("a model's or a session's entry")
    if (nd.get("origin") != "twitter" or not nd.get("source_key")
            or nd.get("provenance") not in PREFILL_PROVENANCES
            or nd.get("node_type", "user") != "user"
            or nd.get("owner_is_user", True) is not True
            or not isinstance(nd.get("content", ""), str)):
        raise _not_prefill("an entry")
    if (nd.get("author_username") or dump_username) != dump_username:
        raise pb.BundleError("foreign_author",
                             "An entry in the bundle is by another account.")
    out = {k: nd[k] for k in ENTRY_FIELDS if k in nd}
    out["privacy_level"] = LOADED_PRIVACY
    out["ai_usage"] = capped_ai_usage(nd.get("ai_usage"))
    out["token_count"] = max(0, int(out.get("token_count") or 0))
    emb = out.get("embedding")
    if emb is not None:
        if not isinstance(emb, dict) or not emb.get("model") or not emb.get("vector"):
            raise _not_prefill("an embedding")
        out["embedding"] = {k: emb.get(k) for k in EMBEDDING_FIELDS}
    return out


def clean_header(header):
    """The header's profile versions, recent contexts and artifacts from
    their allow-lists, private and "chat"; or BundleError. Todos, artifact
    kinds no pre-fill drafts and versions a person saved are refused. The
    header's account fields are never read."""
    if header.get("todos"):
        raise _not_prefill("a todo list")

    def pick(row, fields):
        out = {k: row[k] for k in fields if k in row}
        if out.get("generated_by") in PERSON_MADE:
            raise _not_prefill("a version a person saved")
        out["privacy_level"] = LOADED_PRIVACY
        out["ai_usage"] = capped_ai_usage(row.get("ai_usage"))
        return out
    profiles = [pick(p, PROFILE_FIELDS) for p in header.get("profiles") or []]
    recents = [pick(r, RECENT_FIELDS) for r in header.get("recent_contexts") or []]
    artifacts = []
    for a in header.get("artifacts") or []:
        if a.get("kind") not in ARTIFACT_KINDS:
            raise _not_prefill("an artifact")
        artifacts.append(pick(a, ARTIFACT_FIELDS))
    return profiles, recents, artifacts


# ── Who may receive a bundle ──────────────────────────────────────────────

def account_signed_in(user):
    """Someone signed in to the account (or it predates the record, which
    counts the same: it may be an early signup)."""
    from backend.utils.activity import sign_in_status
    return sign_in_status(user) != "never"


def own_writing_reason(user_id):
    """Why the account counts as holding writing of its own, or None:
    "own_writing" for an entry or upload no pre-fill brought in, or a
    profile version the person wrote; "unlabelled_tweets" for tweets
    with no provenance (pre-fills made before 2026-09-21 left it empty).
    Those cannot be told apart from an upload without guessing: PR
    #691's labelling settles them. Metadata only."""
    from backend.utils.privacy import PREFILL_PROVENANCES
    own = db.session.query(Node.id).filter(
        or_(Node.user_id == user_id, Node.human_owner_id == user_id),
        Node.deleted_at.is_(None), Node.node_type != "llm")
    unlabelled = (Node.origin == "twitter") & Node.provenance.is_(None)
    # Spelled out, not ~unlabelled: NOT of a NULL comparison (no origin)
    # would drop the row.
    labelled_or_not_a_tweet = or_(Node.origin.is_(None),
                                  Node.origin != "twitter",
                                  Node.provenance.isnot(None))
    if own.filter(
            func.coalesce(Node.provenance, "").notin_(PREFILL_PROVENANCES),
            labelled_or_not_a_tweet).first() is not None:
        return "own_writing"
    if db.session.query(UserProfile.id).filter(
            UserProfile.user_id == user_id,
            UserProfile.generated_by.in_(PERSON_MADE)).first() is not None:
        return "own_writing"
    if own.filter(unlabelled).first() is not None:
        return "unlabelled_tweets"
    return None


REFUSAL_MESSAGES = {
    "signed_in": "Someone has signed in to this account. Load only with "
                 "the force option.",
    "own_writing": "The account has writing of its own. Load only with the "
                   "force option.",
    "unlabelled_tweets": "The account holds tweets without a provenance "
                         "label (pre-fills before 2026-09-21); label them "
                         "first (#691), or load with the force option.",
}


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
        return "signed_in", REFUSAL_MESSAGES["signed_in"]
    if not allow_own_writing:
        reason = own_writing_reason(user.id)
        if reason:
            return reason, REFUSAL_MESSAGES[reason]
    return None


def let_in_when_ready(user):
    """The one place a bundle load lets a person in: after their own Yes,
    when the load is done (Peter, 2026-11-13: "lets them in when it is
    ready", no approval step). It approves an account that was never let
    in and that no admin blocked (deactivated_at), as the admin's
    Activate does. Returns whether it approved.

    MERGE WITH #700 (payments): with payments on, #700's gate has let the
    person in at their first request, before they could answer, so this
    finds them approved and does nothing. When both are merged, this body
    must call #700's ``approval.let_in(user)`` instead of setting
    ``approved`` itself, so the plan rule (the default "alpha" becomes
    "free") applies here too, and nothing else in this module may set
    ``approved`` or ``plan``."""
    if user.approved or user.deactivated_at is not None:
        return False
    user.approved = True
    user.approved_at = _now()
    return True


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


def _fill_from_manifest(load, manifest):
    obj = manifest.get("object") or {}
    counts = manifest.get("counts") or {}
    load.handle = pb.clean_handle(manifest.get("handle"))
    load.bundle_sha256 = obj.get("sha256") or ""
    load.bundle_format = manifest.get("bundle_format")
    load.dump_format = manifest.get("dump_format")
    load.exported_at = pb.parse_time(manifest.get("exported_at"))
    load.newest_entry_at = pb.parse_time(manifest.get("newest_entry_at"))
    load.nodes_total = int(counts.get("nodes") or 0)
    load.members = obj.get("members")


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


def start_admin_load(query, admin_id, *, force=False, dispatch=None):
    """The admin's "Make ready from prepared material". Returns (load,
    None) or (None, (code, message)). Safe to repeat: a load under way
    is returned as it is, a failed or stopped one continues in a new load
    (entries already there are skipped), and a finished one of the same
    bundle is not loaded again. Storage errors are StorageError."""
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
    active = _latest_load(user.id, x_id, ACTIVE)
    if active is not None:
        return active, None
    sha = (manifest.get("object") or {}).get("sha256")
    done = _latest_load(user.id, x_id, ("done",))
    if done is not None and done.bundle_sha256 == sha:
        return None, ("already_loaded",
                      "This bundle is already loaded into the account.")
    now = _now()
    load = PrefillBundleLoad(
        user_id=user.id, x_id=str(x_id), origin="admin",
        requested_by_id=admin_id, forced=bool(force), approve_when_done=False,
        status="queued", stage="header", heartbeat_at=now, created_at=now)
    _fill_from_manifest(load, manifest)
    db.session.add(load)
    db.session.commit()
    logger.info("Pre-fill bundle load %s queued by admin %s for user %s",
                load.id, admin_id, user.id)
    (dispatch or _dispatch)(load.id)
    return load, None


def maybe_start_self_load(user, dispatch=None):
    """On a Yes to "Start from your tweets": queue a check for the
    person's bundle. No storage call here: the background task looks for
    the bundle, and finding none changes nothing. Returns the load (the
    check), the one already under way or done, or None. A failed,
    stopped or refused earlier load does not block a new Yes: the new
    load skips what is already there. Never raises: the Yes itself is
    saved either way."""
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
        current = _latest_load(user.id, x_id, ACTIVE + ("done",))
        if current is not None:
            return current
        now = _now()
        load = PrefillBundleLoad(
            user_id=user.id, x_id=x_id, origin="self", approve_when_done=True,
            status="queued", stage="check", bundle_sha256="",
            heartbeat_at=now, created_at=now)
        db.session.add(load)
        db.session.commit()
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
    None, or {"status": "checking" | "preparing" | "ready" | "failed",
    "done", "total"}. A refused or stopped load, or no bundle, changes
    nothing for the person."""
    load = PrefillBundleLoad.query.filter(
        PrefillBundleLoad.user_id == user_id,
        PrefillBundleLoad.status.notin_(("refused", "stopped")),
    ).order_by(PrefillBundleLoad.id.desc()).first()
    if load is None:
        return None
    if load.status in ACTIVE:
        status = "checking" if load.stage == "check" else "preparing"
    else:
        status = {"done": "ready", "failed": "failed"}.get(load.status)
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
        "forced": load.forced, "status": load.status, "stage": load.stage,
        "error": load.error,
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
    into, and why it would be refused. Reads no content. Storage errors
    are StorageError."""
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
        "own_writing": own_writing_reason(user.id),
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


def _fail(load_id, token, code, status="failed"):
    db.session.rollback()
    db.session.execute(
        update(PrefillBundleLoad).where(
            PrefillBundleLoad.id == load_id,
            or_(PrefillBundleLoad.run_token == token,
                PrefillBundleLoad.run_token.is_(None)),
        ).values(status=status, error=str(code)[:255], run_token=None,
                 finished_at=_now()))
    db.session.commit()
    # Problems show: the admin sees the load as failed, and a fault
    # reaches the error monitor. A person's or an admin's choice (a no,
    # an exclusion) is no fault. A code only, never content.
    (logger.info if code in EXPECTED_STOPS else logger.error)(
        "Pre-fill bundle load %s %s: %s", load_id, status, code)


def _check_self_load(load, token, storage):
    """A Yes's check: is there a bundle for the person's X id that may be
    loaded? No bundle: the check is removed and nothing changes. A bundle
    that may not be loaded: the check is kept as "refused" (once per
    bundle and reason) for the admin to see. Else the load goes on."""
    user = db.session.get(User, load.user_id)
    manifest = pb.read_manifest(storage, load.x_id)
    if manifest is None:
        db.session.delete(load)
        db.session.commit()
        return "no_bundle"
    problem = pb.manifest_problem(manifest, load.x_id)
    code = problem.code if problem else None
    if code is None and pb.exclusion(storage, load.x_id) is not None:
        code = "excluded"
    if code is None:
        refusal = target_refusal(user, load.x_id, allow_signed_in=True,
                                 allow_own_writing=False)
        code = refusal[0] if refusal else None
    sha = (manifest.get("object") or {}).get("sha256") or ""
    if code is not None:
        seen = PrefillBundleLoad.query.filter(
            PrefillBundleLoad.id != load.id,
            PrefillBundleLoad.user_id == load.user_id,
            PrefillBundleLoad.status == "refused",
            PrefillBundleLoad.bundle_sha256 == sha,
            PrefillBundleLoad.error == code).first()
        if seen is not None:
            db.session.delete(load)
            db.session.commit()
        else:
            _fill_from_manifest(load, manifest)
            load.status, load.error, load.run_token = "refused", code, None
            load.finished_at = _now()
            db.session.commit()
        (logger.error if problem else logger.info)(
            "Pre-fill bundle for user %s not loaded on their Yes: %s",
            load.user_id, code)
        return "refused"
    _fill_from_manifest(load, manifest)
    db.session.flush()
    _progress(load.id, token, stage="header")
    db.session.commit()
    return None


def _load_header(load, user, header, token):
    """The account's fields and everything small, in one commit. Rows the
    account already has from an earlier load of the material (same time,
    model and type) are not written again."""
    from backend.utils.intentions_versions import KIND as INTENTIONS, protected_version
    profiles, recents, artifacts = clean_header(header)
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
    # sign-in, X handle, approval, plan, settings or defaults. Each is a
    # sign that a person used the account, and an account nobody claimed
    # must stay recognisable as unclaimed (#685).

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
            profile_map[pf.get("id")] = have[key]
            continue
        row = dump_rows.new_profile(pf, uid)
        db.session.add(row)
        db.session.flush()
        profile_map[pf.get("id")] = row.id
        made.append(pf)
    dump_rows.remap_profile_links(made, profile_map)

    def seen(model, *extra):
        return {tuple(r) for r in db.session.query(
            model.created_at, model.generated_by, *extra).filter(
            model.user_id == uid)}

    have_rc = seen(UserRecentContext)
    for rc in recents:
        if (dump_rows.dt(rc.get("created_at")), rc.get("generated_by") or "import") in have_rc:
            continue
        db.session.add(dump_rows.new_recent_context(
            rc, uid, profile_map.get(rc.get("profile_id"))))
    # The user's edit wins (#412): intentions the person wrote stay.
    kept_intentions = protected_version(uid)
    have_a = seen(UserArtifact, UserArtifact.kind)
    count = 0
    for a in artifacts:
        if a.get("kind") == INTENTIONS and kept_intentions is not None:
            continue
        key = (dump_rows.dt(a.get("created_at")),
               a.get("generated_by") or "import", a.get("kind"))
        if key in have_a:
            continue
        db.session.add(dump_rows.new_artifact(a, uid))
        count += 1
    _progress(load.id, token, stage="nodes", profiles_count=len(made),
              artifacts_count=count, stalled_runs=0)
    db.session.commit()


def _node_chunk(load, uid, storage, dump_username, token):
    """Pass 1: write the next part's entries. False when none are left.
    Every entry is cleaned (clean_entry) before the chunk writes
    anything."""
    from backend.utils.encryption import new_dek
    members = load.members or []
    index = pb.member_at(members, load.nodes_done)
    if index is None:
        _progress(load.id, token, stage="links", stalled_runs=0)
        db.session.commit()
        return False
    batch = [clean_entry(nd, dump_username)
             for nd in pb.read_member(storage, load.x_id, members[index])]
    # Already in the account (a load run again, or a pre-fill made here
    # before): matched on the tweet's key. Metadata only.
    keys = [nd["source_key"] for nd in batch]
    existing = dict(db.session.query(Node.source_key, Node.id).filter(
        Node.human_owner_id == uid, Node.source_key.in_(keys))) if keys else {}
    # An entry's updated_at is when it was stored here (the deletion of
    # unclaimed accounts, #685, counts from the first one), not when the
    # lab stored it.
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
        if dek is None:
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


def _link_chunk(load, storage, token):
    """Pass 2: point the next part's entries at their parents and
    continuations, up to the entries pass 1 wrote (all of them, or as
    many as were loaded before a no). An entry this load wrote gets its
    links; one that was already there gets only links it lacks (a load
    a no stopped earlier left them unset). False when none are left."""
    members = load.members or []
    index = pb.member_at(members, load.links_done)
    if index is None or load.links_done >= load.nodes_done:
        _progress(load.id, token, stage="finish", stalled_runs=0)
        db.session.commit()
        return False
    batch = pb.read_member(storage, load.x_id, members[index])
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
        kept = [mapped[nd["id"]][0] for nd in linked
                if nd["id"] in mapped and not mapped[nd["id"]][1]]
        current = {row[0]: row[1:] for row in db.session.query(
            Node.id, Node.parent_id, Node.continuation_node_id,
            Node.linked_node_id).filter(Node.id.in_(kept))} if kept else {}
        for nd in linked:
            own = mapped.get(nd["id"])
            if own is None:
                continue
            values = {c: mapped[nd[c]][0] for c in dump_rows.LINK_COLUMNS
                      if nd.get(c) in mapped}
            if not own[1]:
                have = dict(zip(dump_rows.LINK_COLUMNS, current.get(own[0], ())))
                values = {c: v for c, v in values.items() if have.get(c) is None}
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
    """Done, or stopped by a no. Only a done load of the person's own Yes
    lets them in."""
    PrefillBundleNodeMap.query.filter_by(load_id=load.id).delete(
        synchronize_session=False)
    user = db.session.get(User, load.user_id)
    declined = load.error == "prefill_declined" or user.prefill_consent == "no"
    approved = False
    if load.approve_when_done and not declined:
        approved = let_in_when_ready(user)
    _progress(load.id, token, status="stopped" if declined else "done",
              stage="done", error="prefill_declined" if declined else None,
              run_token=None, finished_at=_now())
    db.session.commit()
    logger.info("Pre-fill bundle load %s %s for user %s: %s entries "
                "created, %s already there, %s relinked%s", load.id,
                "stopped by a no" if declined else "done", load.user_id,
                load.created_count, load.skipped_count, load.relinked_count,
                ", account let in" if approved else "")
    return "stopped" if declined else "done"


def _stop_reason(load, user, storage):
    """Why the load must stop now (a code), or None. A no is not a stop
    here: _work links what was loaded first."""
    if load.stalled_runs > MAX_STALLED_RUNS:
        return "stalled"
    refusal = hard_refusal(user, load.x_id)
    if refusal and refusal[0] != "prefill_declined":
        return refusal[0]
    if pb.exclusion(storage, load.x_id) is not None:
        return "excluded"
    return None


def _work(load_id, token, storage, user, deadline, max_chunks):
    """The header, then parts of entries and of links, until the load
    reaches "finish" or the run's time (or part count) is up. Returns the
    stage reached. A no given meanwhile ends pass 1 where it is: the
    entries loaded so far are linked and stay (#662)."""
    load = db.session.get(PrefillBundleLoad, load_id)
    if load.stage == "header":
        if user.prefill_consent == "no":
            raise pb.BundleError("prefill_declined", "The person said no.")
        # Every part checked before anything is written; each is checked
        # again when it is read, in case the object changes meanwhile.
        pb.verify_bundle(storage, load.x_id, load.members, load.bundle_sha256)
    header = pb.read_member(storage, load.x_id, load.members[0])[0]
    dump_username = (header.get("user") or {}).get("username")
    if load.stage == "header":
        _load_header(load, user, header, token)
    steps = 0
    while time.monotonic() < deadline and (max_chunks is None or steps < max_chunks):
        load = db.session.get(PrefillBundleLoad, load_id)
        if load.stage == "nodes":
            consent = db.session.query(User.prefill_consent).filter(
                User.id == load.user_id).scalar()
            if consent == "no":
                _progress(load_id, token, stage="links",
                          error="prefill_declined")
                db.session.commit()
                continue
            _node_chunk(load, load.user_id, storage, dump_username, token)
        elif load.stage == "links":
            _link_chunk(load, storage, token)
        else:
            break
        steps += 1
    return db.session.get(PrefillBundleLoad, load_id).stage


def run_load(load_id, *, budget=RUN_SECONDS, dispatch=None, max_chunks=None):
    """One run of a load (the task's body). Works until the load is done
    or ``budget`` seconds have passed (or ``max_chunks`` parts, a tests
    hook), then sends the next run. Returns "done", "stopped",
    "continuing", "failed", "refused", "no_bundle", "retrying", "busy"
    (another run holds it), "lost" or "disabled"."""
    dispatch = dispatch or _dispatch
    if not pb.bundles_enabled():
        return "disabled"   # the beat resumes it once switched on again
    token = _claim(load_id)
    if token is None:
        return "busy"
    deadline = time.monotonic() + budget
    try:
        storage = pb.configured_storage()
        load = db.session.get(PrefillBundleLoad, load_id)
        if load.stage == "check":
            outcome = _check_self_load(load, token, storage)
            if outcome is not None:
                return outcome
            load = db.session.get(PrefillBundleLoad, load_id)
        user = db.session.get(User, load.user_id)
        reason = _stop_reason(load, user, storage)
        if reason:
            raise pb.BundleError(reason, reason)
        stage = _work(load_id, token, storage, user, deadline, max_chunks)
        if stage == "finish":
            return _finish(db.session.get(PrefillBundleLoad, load_id), token)
        _release(load_id, token)
        dispatch(load_id)
        return "continuing"
    except LostClaim:
        db.session.rollback()
        return "lost"
    except pb.BundleError as e:
        # A no before anything was loaded stops the load: nothing to show
        # the person. Anything else is a failure the admin sees.
        stopped = e.code == "prefill_declined"
        _fail(load_id, token, e.code, status="stopped" if stopped else "failed")
        return "stopped" if stopped else "failed"
    except Exception as e:  # noqa: BLE001 - storage errors included: retried
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
