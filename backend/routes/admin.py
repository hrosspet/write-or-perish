import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify, abort, current_app
from flask_login import login_required, current_user
from backend.models import User, APICostLog
from backend.extensions import db
from backend.utils.timefmt import iso_utc
from sqlalchemy import func
from backend.utils.magic_link import generate_magic_link_token, hash_token
from backend.utils.email import send_welcome_email
from backend.utils.reserved_usernames import validate_username

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin_bp", __name__)

# Decorator to check that the current user is an admin. Keyed on the
# is_admin column (matching nodes.py/sse.py), NOT the username — usernames
# are renamable, and #91 made 'hrosspet' reserved, so a username-keyed check
# turns an admin rename into a permanent lockout.
def admin_required(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            abort(403)  # Forbidden if not admin
        return func(*args, **kwargs)
    return decorated_function

def _profile_status_map():
    """{user_id: {versions, last_generation_type, last_created_at, state}}
    in two grouped queries. state: "complete" when nothing is in flight and
    the chain is at rest — the latest version is an integration (the batch
    rebuild's final step) or a root chunk (parent None: a single-chunk
    build, or a from-scratch rebuild that superseded older versions) —
    "generating" while a batch job is in flight / a rebuild is requested /
    a multi-version chain hasn't integrated yet."""
    from backend.models import UserProfile
    counts = dict(db.session.query(
        UserProfile.user_id, func.count(UserProfile.id)
    ).group_by(UserProfile.user_id).all())
    latest_ids = db.session.query(func.max(UserProfile.id)).group_by(
        UserProfile.user_id).subquery()
    latest = {p.user_id: p for p in UserProfile.query.filter(
        UserProfile.id.in_(latest_ids)).all()}
    from backend.tasks.exports import should_continue_chain, profile_is_provisional
    users_by_id = {u.id: u for u in User.query.all()}
    out = {}
    for user_id, n in counts.items():
        last = latest.get(user_id)
        u = users_by_id.get(user_id)
        in_flight = bool(u and (u.profile_batch_pending or u.profile_needs_full_regen))
        at_rest = last is not None and (
            last.generation_type == "integration" or last.parent_profile_id is None)
        # A chain whose latest version is a root chunk is at rest only
        # if nothing OLDER than that version remains beyond its cutoff:
        # such data is an unfinished chain (the continue rule the seeder
        # applies), and with nothing in flight it is a stuck rebuild
        # (e.g. its batch failed) that ✓ would otherwise hide.
        stuck = False
        if at_rest and last.generation_type != "integration" and u:
            stuck = should_continue_chain(u, last)
        # Nothing in flight and the account can't be seeded: the hourly
        # seeder only walks approved users (budget guard), so a pre-filled
        # Inactive account with a rebuild requested (regen flag) or a
        # chunk still to go (stuck) waits until activation — a different
        # thing from provider latency, and it must not look like it.
        waiting = bool(u and not u.approved and not u.profile_batch_pending
                       and (u.profile_needs_full_regen or stuck))
        out[user_id] = {
            "versions": n,
            "last_generation_type": last.generation_type if last else None,
            "last_created_at": iso_utc(last.created_at) if last else None,
            "state": "generating" if (in_flight or not at_rest or stuck) else "complete",
            "incomplete": stuck and not waiting,
            # A first build from under a full chunk of data: rebuilt from
            # scratch, not updated, once the organic gate next trips.
            "provisional": profile_is_provisional(last),
            "waiting": "inactive" if waiting else None,
            "batch_attempts": (u.profile_batch_attempts or 0) if u else 0,
        }
    return out


def _intentions_status_map():
    """{user_id: {state, versions, last_created_at}} for the Users-tab
    Profile column: "generating" while a kind="intentions" batch item is
    pending (persisted ProfileBatchJob — survives restarts); "failed" when
    the latest run gave up (overflow twice / batch error) and no artifact
    version postdates it; else "complete" when at least one intentions
    artifact version exists."""
    from backend.models import ProfileBatchJob, UserArtifact
    pending_ids, gave_up_at = set(), {}
    for job in ProfileBatchJob.query.order_by(ProfileBatchJob.submitted_at).all():
        for item in job.items:
            if item.get("kind") != "intentions":
                continue
            if job.status == "pending":
                pending_ids.add(item["user_id"])
            elif item.get("gave_up"):
                gave_up_at[item["user_id"]] = job.submitted_at
    rows = db.session.query(
        UserArtifact.user_id, func.count(UserArtifact.id),
        func.max(UserArtifact.created_at),
    ).filter(UserArtifact.kind == "intentions").group_by(
        UserArtifact.user_id).all()
    last_artifact = {uid: last for uid, _, last in rows}
    out = {uid: {"state": "complete", "versions": n,
                 "last_created_at": iso_utc(last)}
           for uid, n, last in rows}
    for uid, when in gave_up_at.items():
        newer = last_artifact.get(uid)
        if newer is None or newer < when:
            entry = out.setdefault(uid, {"versions": 0, "last_created_at": None})
            entry["state"] = "failed"
    for uid in pending_ids:
        entry = out.setdefault(uid, {"versions": 0, "last_created_at": None})
        entry["state"] = "generating"
    return out


@admin_bp.route("/users", methods=["GET"])
@login_required
@admin_required
def list_users():
    from backend.utils.spend import (
        get_user_spend_limit_usd, month_start, user_is_capped,
    )

    config = current_app.config
    now = datetime.utcnow()
    users = User.query.order_by(User.created_at.desc()).all()
    profile_status = _profile_status_map()
    intentions_status = _intentions_status_map()

    # Aggregate total (all-time) spending per user in a single query
    spending_rows = db.session.query(
        APICostLog.user_id,
        func.sum(APICostLog.cost_microdollars).label("total_microdollars")
    ).group_by(APICostLog.user_id).all()
    spending_map = {row.user_id: row.total_microdollars for row in spending_rows}

    # Month-to-date spending per user (drives the cap UI), one extra query
    month_rows = db.session.query(
        APICostLog.user_id,
        func.sum(APICostLog.cost_microdollars).label("month_microdollars")
    ).filter(APICostLog.created_at >= month_start(now)).group_by(
        APICostLog.user_id).all()
    month_map = {row.user_id: row.month_microdollars for row in month_rows}

    # Prompt-cache hit-rate per user, over conversation turns (#187/#189).
    # Unified across providers: cache_read_tokens holds the input SERVED from
    # cache — Anthropic cache reads AND OpenAI cached_tokens — and input_tokens
    # is the full prompt size, so hit-rate = served / total prompt input works
    # for both (OpenAI has no separate "write" concept). Scoped to
    # request_type='conversation' so embeddings/transcription/profile/warm
    # don't dilute the denominator. NULLs are ignored by SUM.
    cache_rows = db.session.query(
        APICostLog.user_id,
        func.sum(APICostLog.cache_read_tokens).label("served"),
        func.sum(APICostLog.input_tokens).label("prompt_input"),
    ).filter(
        APICostLog.request_type == "conversation"
    ).group_by(APICostLog.user_id).all()
    cache_map = {
        row.user_id: (row.served or 0, row.prompt_input or 0)
        for row in cache_rows
    }

    user_list = []
    for user in users:
        total_microdollars = spending_map.get(user.id, 0) or 0
        month_microdollars = month_map.get(user.id, 0) or 0
        cache_served, prompt_input = cache_map.get(user.id, (0, 0))
        cache_hit_rate = (
            cache_served / prompt_input if prompt_input > 0 else None
        )
        user_list.append({
            "id": user.id,
            "twitter_id": user.twitter_id,
            "username": user.username,
            "description": user.description,
            "created_at": iso_utc(user.created_at),
            "accepted_terms_at": iso_utc(user.accepted_terms_at),
            "approved": user.approved,
            "email": user.email,
            "plan": user.plan,
            "deactivated_at": iso_utc(user.deactivated_at),
            "total_spending_usd": total_microdollars / 1_000_000,
            "current_month_spending_usd": month_microdollars / 1_000_000,
            # Prompt-cache hit-rate over conversation turns (all-time): null
            # when the user has no conversation prompt input yet. Raw sums
            # included so the UI can show the breakdown on hover.
            "cache_hit_rate": cache_hit_rate,
            "cache_served_tokens": cache_served,
            "cache_input_tokens": prompt_input,
            # Effective cap (per-user override if set, else the global default),
            # pre-fills the editable input. `spend_limit_is_override` lets the UI
            # distinguish a custom value from the inherited default.
            "spend_limit_usd": get_user_spend_limit_usd(user, config),
            "spend_limit_is_override": user.monthly_spend_limit_usd is not None,
            "spend_blocked": user_is_capped(user, now),
            "profile_batch_pending": bool(user.profile_batch_pending),
            "profile_force_batch": bool(user.profile_force_batch),
            "prefilled_handle": user.prefilled_handle,
            "prefill_consent": user.prefill_consent,
            "prefill_consent_at": iso_utc(user.prefill_consent_at),
            "spam": bool(user.spam),
            "intentions": intentions_status.get(user.id),
            "profile": profile_status.get(user.id) or {
                "versions": 0,
                "last_generation_type": None,
                "last_created_at": None,
                "state": ("generating" if user.profile_batch_pending
                          or user.profile_needs_full_regen else "none"),
                # Rebuild requested, nothing in flight, account inactive →
                # the seeder won't touch it until activation (see
                # _profile_status_map).
                "waiting": ("inactive" if (user.profile_needs_full_regen
                                           and not user.profile_batch_pending
                                           and not user.approved) else None),
            },
        })
    return jsonify({
        "users": user_list,
        "allowed_plans": sorted(User.ALLOWED_PLANS),
        "per_user_limit_default_usd": config.get(
            "PER_USER_MONTHLY_LIMIT_USD") or 0,
    }), 200

@admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_user_status(user_id):
    user = User.query.get_or_404(user_id)
    user.approved = not user.approved  # Toggle the approved flag
    if not user.approved:
        # Record deactivation time (terms acceptance fields are preserved for audit)
        user.deactivated_at = datetime.utcnow()
    else:
        user.approved_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"message": "User status updated", "approved": user.approved}), 200

@admin_bp.route("/activity", methods=["GET"])
@login_required
@admin_required
def activity():
    """Per-user engagement since activation for the Activity tab —
    writes / asks / voice per day, last seen, retention flags — measured
    directly, not via spend. ?days= (default 14, max 60)."""
    from backend.utils.activity import activity_report
    days = min(max(request.args.get("days", 14, type=int), 7), 60)
    return jsonify(activity_report(days=days)), 200


@admin_bp.route("/users/<int:user_id>/build_profile", methods=["POST"])
@login_required
@admin_required
def build_profile(user_id):
    """Force a from-scratch BATCH profile build regardless of the token
    gate (a few-thousand-token corpus the admin wants profiled anyway).
    Sets the full-regen flag (which _should_seed honours unconditionally
    and which makes the first chunk build at any size), pins the user to
    the batch path, and seeds immediately. Idempotent while a job is in
    flight."""
    user = User.query.get_or_404(user_id)
    if user.profile_batch_pending:
        return jsonify({"message": "A batch step is already in flight.", "queued": False}), 200
    user.profile_needs_full_regen = True
    user.profile_force_batch = True
    db.session.commit()
    from backend.tasks.profile_batch import seed_profile_batch_for_user
    seed_profile_batch_for_user.delay(user.id)
    return jsonify({"message": "Batch profile build queued.", "queued": True}), 202


@admin_bp.route("/users/<int:user_id>/infer_intentions", methods=["POST"])
@login_required
@admin_required
def infer_intentions_route(user_id):
    """Generate the intentions artifact from a pre-filled account's public
    tweets (public fork of the tested intentions_detection prompt; whole
    corpus, newest-kept shrink loop). Returns {"task_id"} — poll
    /admin/prefill/status/<task_id>."""
    from backend.utils.privacy import AI_ALLOWED
    user = User.query.get_or_404(user_id)
    if user.default_ai_usage not in AI_ALLOWED:
        return jsonify({"error": "User has opted out of AI usage."}), 400
    from backend.tasks.intentions import infer_intentions
    task = infer_intentions.delay(user.id)
    return jsonify({"task_id": task.id}), 202


@admin_bp.route("/users/<int:user_id>/toggle_spam", methods=["POST"])
@login_required
@admin_required
def toggle_user_spam(user_id):
    """Flip the admin spam flag. Marking spam only hides the row behind
    the Users-table eye toggle; it does not deactivate the account."""
    user = User.query.get_or_404(user_id)
    user.spam = not bool(user.spam)
    db.session.commit()
    return jsonify({"message": "Spam flag updated", "spam": user.spam}), 200


@admin_bp.route("/users/<int:user_id>/update_email", methods=["PUT"])
@login_required
@admin_required
def update_user_email(user_id):
    data = request.get_json()
    email = data.get("email")
    if email is None:
        return jsonify({"error": "Email is required."}), 400
    user = User.query.get_or_404(user_id)
    user.email = email
    db.session.commit()
    return jsonify({"message": "Email updated", "email": user.email}), 200

@admin_bp.route("/users/<int:user_id>/update_plan", methods=["PUT"])
@login_required
@admin_required
def update_user_plan(user_id):
    data = request.get_json()
    plan = data.get("plan")
    if plan not in User.ALLOWED_PLANS:
        return jsonify({"error": f"Invalid plan. Allowed: {sorted(User.ALLOWED_PLANS)}"}), 400
    user = User.query.get_or_404(user_id)
    user.plan = plan
    db.session.commit()
    return jsonify({"message": "Plan updated", "plan": user.plan}), 200

@admin_bp.route("/users/<int:user_id>/update_spend_limit", methods=["PUT"])
@login_required
@admin_required
def update_user_spend_limit(user_id):
    """Set a user's per-user monthly spend cap (USD) and immediately reconcile
    their block flag against current month-to-date spend: raising the limit
    above their spend unblocks them; lowering it at/below their spend blocks
    them. 0 = uncapped for this user."""
    from backend.utils.spend import reconcile_user_spend_block

    data = request.get_json() or {}
    raw = data.get("limit_usd")
    try:
        limit = float(raw)
    except (TypeError, ValueError):
        return jsonify({"error": "limit_usd must be a number."}), 400
    if limit < 0:
        return jsonify({"error": "limit_usd must be >= 0."}), 400

    user = User.query.get_or_404(user_id)
    user.monthly_spend_limit_usd = limit
    state = reconcile_user_spend_block(user, current_app.config)
    db.session.commit()
    return jsonify({
        "message": "Spend limit updated",
        "limit_usd": user.monthly_spend_limit_usd,
        "spend_blocked": state["blocked"],
        "current_month_spending_usd": state["spend_usd"],
    }), 200

# New endpoint: Whitelist a user by handle.
@admin_bp.route("/whitelist", methods=["POST"])
@login_required
@admin_required
def whitelist_user():
    data = request.get_json() or {}
    handle = data.get("handle", "").strip()
    if not handle:
        return jsonify({"error": "Handle is required."}), 400

    # Full username validation: format, length, reserved/protected names
    # (brand/founder/system), and case-insensitive uniqueness.
    error = validate_username(handle)
    if error:
        return jsonify({"error": error}), 400

    # Create a new user with the handle
    user = User(twitter_id=None, username=handle, approved=True)
    db.session.add(user)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "DB error", "details": str(e)}), 500
    return jsonify({
        "message": "User whitelisted successfully.",
        "user": {
            "id": user.id,
            "username": user.username,
            "twitter_id": user.twitter_id,
            "approved": user.approved,
            "accepted_terms_at": user.accepted_terms_at,  # Will be null.
            "email": user.email
        }
    }), 201


@admin_bp.route("/users/<int:user_id>/activate_and_welcome", methods=["POST"])
@login_required
@admin_required
def activate_and_welcome(user_id):
    user = User.query.get_or_404(user_id)

    if not user.email:
        return jsonify({"error": "User has no email address. Add one first."}), 400

    # Approve the user
    user.approved = True
    user.approved_at = datetime.utcnow()
    db.session.commit()

    # Generate magic link pointing to /welcome (30-day expiry)
    welcome_max_age = 30 * 24 * 3600  # 30 days in seconds
    try:
        token = generate_magic_link_token(
            user.email, next_url="/welcome", max_age=welcome_max_age
        )
        token_h = hash_token(token)

        user.magic_link_token_hash = token_h
        user.magic_link_expires_at = (
            datetime.utcnow() + timedelta(seconds=welcome_max_age)
        )
        db.session.commit()

        backend_url = request.host_url.rstrip("/")
        magic_link_url = f"{backend_url}/auth/magic-link/verify?token={token}"

        send_welcome_email(user.email, magic_link_url)
    except Exception:
        logger.exception(
            f"Failed to send welcome email to user {user_id} ({user.email})"
        )
        return jsonify({
            "message": "User approved but welcome email failed to send.",
            "approved": True,
            "email_sent": False,
        }), 200

    return jsonify({
        "message": "User approved and welcome email sent.",
        "approved": True,
        "email_sent": True,
    }), 200


@admin_bp.route("/users/<int:user_id>/prefill", methods=["POST"])
@login_required
@admin_required
def prefill_from_community_archive(user_id):
    """Bootstrap a (typically whitelisted, not-yet-signed-in) account with its
    public tweets from the Community Archive, then queue a BATCH profile
    build. Body: {"handle": "...", "include_replies": bool}; handle
    defaults to the user's username. Returns {"task_id"} — poll
    /admin/prefill/status/<task_id>."""
    user = User.query.get_or_404(user_id)
    data = request.get_json() or {}
    handle = (data.get("handle") or user.username or "").strip().lstrip("@")
    if not handle:
        return jsonify({"error": "Handle is required."}), 400
    from backend.tasks.imports import prefill_community_archive
    task = prefill_community_archive.delay(user.id, handle, {
        "include_replies": bool(data.get("include_replies", True)),
        "force_parquet": bool(data.get("force_parquet", False)),
    })
    return jsonify({"task_id": task.id, "handle": handle}), 202


@admin_bp.route("/prefill/check", methods=["GET"])
@login_required
@admin_required
def prefill_check():
    """Archive coverage for ?handle= before paying for a pre-fill:
    exact archived count, retweets / replies / originals, est. tokens,
    ingestion path, which source the import would read, and how many of
    the tweets ?user_id= already has (re-runs dedup on twitter:<id>)."""
    from backend.models import Node
    from backend.utils import community_archive as ca
    from backend.tasks.imports import snapshot_dir_for
    handle = (request.args.get("handle") or "").strip().lstrip("@")
    if not handle:
        return jsonify({"error": "Handle is required."}), 400
    config = current_app.config
    try:
        summary = ca.coverage_summary(handle, snapshot_dir=snapshot_dir_for(config))
    except Exception as e:  # network / provider errors → readable, not a 500
        current_app.logger.warning(f"Community Archive check failed for @{handle}: {e}")
        return jsonify({"error": f"Community Archive lookup failed: {e}"}), 502
    if summary is None:
        return jsonify({"error": f"@{handle} is not in the Community Archive."}), 404
    min_parquet = config.get("COMMUNITY_ARCHIVE_PARQUET_MIN_TWEETS", 5000)
    live = summary.get("archived_live", summary.get("archived")) or 0
    big = live >= min_parquet
    # The import falls back to REST when the snapshot lags the live archive
    # (detail_source == "parquet" means the snapshot has the account).
    snapshot_complete = (summary.get("detail_source") == "parquet"
                         and "archived_live" not in summary)
    summary["import_source"] = "parquet" if (big and snapshot_complete) else (
        "parquet (if snapshot is current)" if big and summary.get("detail_source") != "parquet"
        else "rest")
    summary["profile_threshold_tokens"] = 10000
    user_id = request.args.get("user_id", type=int)
    if user_id:
        summary["already_imported"] = Node.query.filter(
            Node.human_owner_id == user_id, Node.origin == "twitter",
            Node.deleted_at.is_(None)).count()
    return jsonify(summary), 200


@admin_bp.route("/prefill/x/check", methods=["GET"])
@login_required
@admin_required
def prefill_x_check():
    """What a paid X-API pull for ?handle= would get: the account's
    lifetime tweet_count, whether it's protected, how many posts the
    timeline endpoint can actually serve (min(3200, tweet_count), further
    bounded by ?max_tweets=), and the pay-per-use cost of that pull.
    Costs one user read (~$0.01) itself."""
    from backend.models import Node
    from backend.utils import x_api
    handle = (request.args.get("handle") or "").strip().lstrip("@")
    if not handle:
        return jsonify({"error": "Handle is required."}), 400
    config = current_app.config
    creds = (config.get("TWITTER_API_KEY"), config.get("TWITTER_API_SECRET"))
    try:
        account = x_api.lookup_user(handle, creds)
    except x_api.XApiError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        current_app.logger.warning(f"X API check failed for @{handle}: {e}")
        return jsonify({"error": f"X API lookup failed: {e}"}), 502
    # The lookup itself is billable: log it against the target user when
    # given, else the admin doing the check.
    user_id = request.args.get("user_id", type=int)
    from backend.models import APICostLog
    db.session.add(APICostLog(
        user_id=user_id or current_user.id, model_id="x-api/user-lookup",
        request_type="x_prefill_check", request_ref=f"@{handle}"[:64],
        input_tokens=0, output_tokens=0,
        cost_microdollars=x_api.cost_microdollars(0, user_reads=1)))
    db.session.commit()
    if account is None:
        return jsonify({"error": f"@{handle} is not on X (suspended, renamed, or never existed)."}), 404
    requested = request.args.get("max_tweets", type=int)
    fetchable = 0 if account["protected"] else x_api.fetchable(account["tweet_count"], requested)
    summary = {
        **account,
        "timeline_cap": x_api.TIMELINE_CAP,
        "fetchable": fetchable,
        "est_cost_usd": x_api.estimate_cost(fetchable),
        "profile_threshold_tokens": 10000,
    }
    if user_id:
        summary["already_imported"] = Node.query.filter(
            Node.human_owner_id == user_id, Node.origin == "twitter",
            Node.deleted_at.is_(None)).count()
    return jsonify(summary), 200


@admin_bp.route("/users/<int:user_id>/prefill-x", methods=["POST"])
@login_required
@admin_required
def prefill_from_x_api(user_id):
    """Paid pre-fill straight from the X API. Body: {"handle", "max_tweets",
    "include_replies"}; max_tweets is clamped to the timeline cap here and
    to the account's tweet_count in the task. Returns {"task_id"} — poll
    /admin/prefill/status/<task_id> (same shape as the CA pre-fill)."""
    from backend.utils import x_api
    user = User.query.get_or_404(user_id)
    data = request.get_json() or {}
    handle = (data.get("handle") or user.username or "").strip().lstrip("@")
    if not handle:
        return jsonify({"error": "Handle is required."}), 400
    try:
        raw = data.get("max_tweets")
        max_tweets = x_api.TIMELINE_CAP if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        return jsonify({"error": "max_tweets must be a number."}), 400
    max_tweets = x_api.fetchable(x_api.TIMELINE_CAP, max_tweets)
    if max_tweets <= 0:
        return jsonify({"error": "max_tweets must be positive."}), 400
    from backend.tasks.imports import prefill_x_api
    task = prefill_x_api.delay(user.id, handle, {
        "max_tweets": max_tweets,
        "include_replies": bool(data.get("include_replies", True)),
    })
    return jsonify({"task_id": task.id, "handle": handle, "max_tweets": max_tweets}), 202


@admin_bp.route("/prefill/status/<task_id>", methods=["GET"])
@login_required
@admin_required
def prefill_status(task_id):
    """Same shape as /import/status/<task_id> plus a ``stage``
    ("fetching" | "importing"); no owner check — the admin isn't the
    target user."""
    from backend.celery_app import celery
    task = celery.AsyncResult(task_id)
    info = task.info if isinstance(task.info, dict) else {}
    base = {"task_id": task_id, "done": 0, "total": None,
            "stage": info.get("stage"), "result": None, "error": None}
    if task.state == "PROGRESS":
        return jsonify({**base, "status": "running",
                        "done": info.get("done", 0),
                        "total": info.get("total")}), 200
    if task.state == "SUCCESS":
        return jsonify({**base, "status": "completed",
                        "done": info.get("total"), "total": info.get("total"),
                        "result": {k: v for k, v in info.items()
                                   if k != "user_id"}}), 200
    if task.state == "FAILURE":
        return jsonify({**base, "status": "failed",
                        "error": str(task.info) if task.info
                        else "Pre-fill failed"}), 200
    return jsonify({**base, "status": "queued"}), 200


@admin_bp.route("/spend", methods=["GET"])
@login_required
@admin_required
def spend_status():
    """Month-to-date API spend + limit status (issue #85)."""
    from backend.models import SpendAlert
    from backend.utils.spend import (
        get_month_spend_microdollars, parse_thresholds,
    )

    config = current_app.config
    now = datetime.utcnow()
    month = now.strftime("%Y-%m")

    total = get_month_spend_microdollars(config, now=now)
    anthropic = get_month_spend_microdollars(config, provider="anthropic", now=now)
    openai = get_month_spend_microdollars(config, provider="openai", now=now)

    limit_usd = config.get("ANTHROPIC_SPEND_LIMIT_USD") or 0
    alerts = SpendAlert.query.filter_by(month=month).order_by(
        SpendAlert.threshold).all()

    return jsonify({
        "month": month,
        "total_usd": total / 1_000_000,
        "anthropic_usd": anthropic / 1_000_000,
        "openai_usd": openai / 1_000_000,
        "limit_usd": limit_usd,
        "limit_fraction_used": (
            (anthropic / 1_000_000) / limit_usd if limit_usd > 0 else None
        ),
        "per_user_limit_usd": config.get("PER_USER_MONTHLY_LIMIT_USD") or 0,
        "users_blocked_this_month": User.query.filter_by(
            spend_blocked_month=month).count(),
        "thresholds": parse_thresholds(config.get("SPEND_ALERT_THRESHOLDS")),
        "alerts_fired": [
            {
                "threshold": a.threshold,
                "spend_usd": a.spend_usd,
                "at": iso_utc(a.created_at),
            }
            for a in alerts
        ],
    }), 200


@admin_bp.route("/feedback", methods=["GET"])
@login_required
@admin_required
def list_feedback():
    """List user feedback submitted via the LLM tool (issue #158)."""
    from backend.models import UserFeedback

    status = request.args.get("status")
    query = UserFeedback.query
    if status:
        query = query.filter_by(status=status)
    items = query.order_by(UserFeedback.created_at.desc()).limit(500).all()

    return jsonify({"feedback": [
        {
            "id": f.id,
            "user_id": f.user_id,
            "username": f.user.username if f.user else None,
            "content": f.get_content(),
            "category": f.category,
            "source": f.source,
            "status": f.status,
            "created_at": iso_utc(f.created_at),
        }
        for f in items
    ]}), 200


@admin_bp.route("/feedback/<int:feedback_id>", methods=["PUT"])
@login_required
@admin_required
def update_feedback_status(feedback_id):
    from backend.models import UserFeedback

    feedback = UserFeedback.query.get_or_404(feedback_id)
    status = (request.get_json() or {}).get("status")
    if status not in ("new", "reviewed", "done"):
        return jsonify({"error": "Invalid status"}), 400
    feedback.status = status
    db.session.commit()
    return jsonify({"id": feedback.id, "status": feedback.status}), 200


# ---------------------------------------------------------------------------
# Polls — the admin side of the dev-update channel (#207). The admin sees
# ONLY responses the user explicitly sent (opt-in 2); drafts, failures and
# declines stay private to the user (declined/pending appear as counts only).
# ---------------------------------------------------------------------------

@admin_bp.route("/polls", methods=["POST"])
@login_required
@admin_required
def create_poll():
    from backend.models import Poll

    data = request.get_json() or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Question is required."}), 400

    # Which model drafts answers, and what data it may read (#207). Both
    # are frozen at creation and shown to users before opt-in 1.
    model_id = data.get("model_id") or current_app.config.get(
        "DEFAULT_LLM_MODEL")
    supported = current_app.config.get("SUPPORTED_MODELS", {})
    if model_id not in supported:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400
    data_source = data.get("data_source") or "derived"
    if data_source not in Poll.DATA_SOURCES:
        return jsonify({"error": f"Invalid data_source. Allowed: "
                                 f"{list(Poll.DATA_SOURCES)}"}), 400

    poll = Poll(question=question, created_by=current_user.id,
                model_id=model_id, data_source=data_source)
    db.session.add(poll)
    db.session.commit()
    return jsonify({"id": poll.id, "question": poll.question,
                    "model_id": poll.model_id,
                    "data_source": poll.data_source,
                    "created_at": iso_utc(poll.created_at)}), 201


@admin_bp.route("/polls", methods=["GET"])
@login_required
@admin_required
def list_polls():
    from backend.models import Poll, PollResponse

    counts = {}
    rows = db.session.query(
        PollResponse.poll_id, PollResponse.status,
        func.count(PollResponse.id)
    ).group_by(PollResponse.poll_id, PollResponse.status).all()
    for poll_id, status, n in rows:
        counts.setdefault(poll_id, {})[status] = n

    return jsonify({
        # The app-wide default, so the create-poll selector can preselect
        # a CONCRETE model instead of an ambiguous "default" option.
        "default_model_id": current_app.config.get("DEFAULT_LLM_MODEL"),
        "polls": [
        {
            "id": p.id,
            "question": p.question,
            "model_id": p.model_id,
            "data_source": p.data_source,
            "created_at": iso_utc(p.created_at),
            "closed_at": iso_utc(p.closed_at),
            "sent_count": counts.get(p.id, {}).get("sent", 0),
            "declined_count": counts.get(p.id, {}).get("declined", 0),
        }
        for p in Poll.query.order_by(Poll.created_at.desc()).all()
    ]}), 200


@admin_bp.route("/polls/<int:poll_id>/responses", methods=["GET"])
@login_required
@admin_required
def list_poll_responses(poll_id):
    from backend.models import Poll, PollResponse

    poll = Poll.query.get_or_404(poll_id)
    responses = PollResponse.query.filter_by(
        poll_id=poll.id, status="sent").order_by(
        PollResponse.sent_at.asc()).all()
    return jsonify({
        "poll": {"id": poll.id, "question": poll.question},
        "responses": [
            {
                "id": r.id,
                "username": r.user.username if r.user else None,
                "content": r.get_content(),
                "llm_drafted": r.generated_by is not None,
                "sent_at": iso_utc(r.sent_at),
            }
            for r in responses
        ],
    }), 200


@admin_bp.route("/polls/<int:poll_id>/close", methods=["POST"])
@login_required
@admin_required
def close_poll(poll_id):
    from backend.models import Poll

    poll = Poll.query.get_or_404(poll_id)
    if poll.closed_at is None:
        poll.closed_at = datetime.utcnow()
        db.session.commit()
    return jsonify({"id": poll.id, "closed_at": iso_utc(poll.closed_at)}), 200
