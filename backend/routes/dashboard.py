import logging
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from backend.models import Node, User, UserProfile
from backend.extensions import db
from backend.utils.email import (
    is_valid_email, send_email_change_email, send_email_changed_notice,
    send_email_in_use_notice,
)
from backend.utils.magic_link import (
    email_change_expiry_seconds, generate_email_change_token,
    verify_email_change_token, hash_token,
)
from backend.utils.timefmt import iso_utc, is_valid_timezone
from backend.utils.privacy import (
    accessible_nodes_filter, VALID_PRIVACY_LEVELS, VALID_AI_USAGE,
)
from backend.routes.terms import CURRENT_TERMS_VERSION
from backend.utils.reserved_usernames import validate_username
from backend.utils.spend import user_is_capped

logger = logging.getLogger(__name__)
dashboard_bp = Blueprint("dashboard_bp", __name__)


def _terms_up_to_date(user):
    if user.accepted_terms_version != CURRENT_TERMS_VERSION:
        return False
    if user.deactivated_at and (
        not user.accepted_terms_at or user.deactivated_at > user.accepted_terms_at
    ):
        return False
    return True

def get_latest_profile(user):
    """Get the most recent profile for a user, or None if no profile exists."""
    profile = UserProfile.query.filter_by(user_id=user.id).order_by(UserProfile.created_at.desc()).first()
    if profile:
        return {
            "id": profile.id,
            "content": profile.get_content(),
            "generated_by": profile.generated_by,
            "tokens_used": profile.tokens_used,
            "created_at": iso_utc(profile.created_at),
            "source_tokens_used": profile.source_tokens_used,
            "source_origin_stats": profile.source_origin_stats,
            "source_data_cutoff": (
                iso_utc(profile.source_data_cutoff)
            ),
            "generation_type": profile.generation_type,
            # Whether this profile has generated TTS audio — drives the
            # "regenerate audio?" edit prompt (#66).
            "has_tts": bool(profile.audio_tts_url),
        }
    return None


def _serialize_node_for_list(node):
    """Serialize a node for dashboard list views (Log has its own)."""
    # If this is a system prompt root, skip to the first child
    display_node = node
    prompt_key = None
    if node.is_system_prompt:
        prompt_key = node.get_prompt_key()
        first_child = Node.query.filter_by(parent_id=node.id).order_by(Node.created_at.asc()).first()
        if first_child:
            display_node = first_child

    content = display_node.get_content()
    preview = content[:200] + ("..." if len(content) > 200 else "")

    # Determine human owner username for LLM nodes
    human_owner_username = None
    if display_node.node_type == "llm" and display_node.human_owner_id:
        human_owner = User.query.get(display_node.human_owner_id)
        if human_owner:
            human_owner_username = human_owner.username

    return {
        "id": display_node.id,
        "preview": preview,
        "node_type": display_node.node_type,
        "child_count": len(node.children),
        "created_at": iso_utc(display_node.created_at),
        "pinned_at": iso_utc(node.pinned_at),
        "username": node.user.username if node.user else "Unknown",
        "human_owner_username": human_owner_username,
        "llm_model": display_node.llm_model,
        "origin": display_node.origin,
        "has_original_audio": bool(display_node.audio_original_url or display_node.streaming_transcription),
        "prompt_key": prompt_key,
    }


# Dashboard endpoint: only return top-level nodes (nodes with no parent)
@dashboard_bp.route("/", methods=["GET"])
@login_required
def get_dashboard():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = min(per_page, 100)

    # Pinned nodes for this user (separate from pagination)
    pinned_nodes = Node.query.filter(
        Node.pinned_by == current_user.id,
        Node.pinned_at.isnot(None)
    ).order_by(Node.pinned_at.desc()).all()
    pinned_list = [_serialize_node_for_list(n) for n in pinned_nodes]

    query = Node.query.filter_by(user_id=current_user.id, parent_id=None).order_by(Node.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    nodes_list = [_serialize_node_for_list(node) for node in pagination.items]
    # Determine if Voice Mode is enabled for this user (admin or paid plan)
    voice_mode_enabled = current_user.has_voice_mode
    dashboard = {
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "description": current_user.description,
            "accepted_terms_at": iso_utc(current_user.accepted_terms_at),
            "terms_up_to_date": _terms_up_to_date(current_user),
            "approved": current_user.approved,
            "email": current_user.email,
            "is_admin": current_user.is_admin,
            "plan": current_user.plan,
            "voice_mode_enabled": voice_mode_enabled,
            "craft_mode": current_user.craft_mode,
            "preferred_model": current_user.preferred_model,
            "profile_generation_task_id": current_user.profile_generation_task_id,
            # Batch-pipeline builds set no task id (#258); the watcher starts
            # polling /export/profile-progress on either flag.
            "profile_batch_pending": bool(current_user.profile_batch_pending),
            "default_privacy_level": current_user.default_privacy_level,
            "default_ai_usage": current_user.default_ai_usage,
            "twitter_login": bool(current_user.twitter_id),
            "twitter_handle": current_user.twitter_handle,
            "pending_email": current_user.pending_email,
            "pending_email_expired": _pending_email_expired(current_user),
            "prefill_consent": current_user.prefill_consent,
            "prefilled_handle": current_user.prefilled_handle,
            "timezone": current_user.timezone or "UTC",
            # Lets the client block cost actions (e.g. starting a long voice
            # recording) up front instead of after the fact (issue #85).
            "spend_blocked": user_is_capped(current_user),
            # Public side (#228): enabled = deployed (env) AND the user's
            # own opt-in — every frontend surface keys off this. available
            # = deployed only; it decides whether Account shows the toggle.
            "share_v1_enabled": bool(
                current_app.config.get("SHARE_V1", False)
                and current_user.public_sharing_enabled),
            "share_v1_available": bool(
                current_app.config.get("SHARE_V1", False)),
            "public_sharing_enabled": bool(
                current_user.public_sharing_enabled),
            # Saved external references (#208/#329): available = the env
            # killswitch is on (decides whether Account shows the toggle);
            # enabled = the user's own opt-in. Own-archive search is on for
            # everyone under the same killswitch and has no toggle.
            "external_content_available": bool(
                current_app.config.get("SEMANTIC_SEARCH_AGENTIC", True)),
            "external_content_enabled": bool(
                current_user.external_content_enabled),
        },
        "pinned_nodes": pinned_list,
        "nodes": nodes_list,
        "has_more": pagination.has_next,
        "page": page,
        "total_nodes": pagination.total,
        "latest_profile": get_latest_profile(current_user)
    }
    return jsonify(dashboard), 200


# Public view of any user's dashboard; no private stats provided.
@dashboard_bp.route("/<string:username>", methods=["GET"])
@login_required
def get_public_dashboard(username):
    # Lookup the user by their (unique) handle (username).
    user = User.query.filter_by(username=username).first_or_404()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    per_page = min(per_page, 100)

    # Pinned nodes for this user (filtered by accessibility)
    pinned_nodes = Node.query.filter(
        Node.pinned_by == user.id,
        Node.pinned_at.isnot(None),
        accessible_nodes_filter(Node, current_user.id)
    ).order_by(Node.pinned_at.desc()).all()
    pinned_list = [_serialize_node_for_list(n) for n in pinned_nodes]

    query = Node.query.filter(
        Node.user_id == user.id,
        Node.parent_id.is_(None),
        accessible_nodes_filter(Node, current_user.id)
    ).order_by(Node.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    nodes_list = [_serialize_node_for_list(node) for node in pagination.items]

    dashboard = {
        "user": {
            "id": user.id,
            "username": user.username,
            "description": user.description
        },
        "pinned_nodes": pinned_list,
        "nodes": nodes_list,
        "has_more": pagination.has_next,
        "page": page,
        "total_nodes": pagination.total,
        "latest_profile": get_latest_profile(user)
    }
    return jsonify(dashboard), 200


def _json_object():
    """The request's JSON body when it is an object, else {} — a body of
    "hello" or [1, 2] is valid JSON and has no .get()."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _pending_email_expired(user):
    """True when a pending change's link can no longer be confirmed, so the
    UI offers a new link instead of "open the one we sent"."""
    return bool(user.pending_email) and (
        user.email_change_expires_at is None
        or user.email_change_expires_at <= datetime.utcnow())


def _clear_pending_email(user):
    user.pending_email = None
    user.email_change_token_hash = None
    user.email_change_expires_at = None


def _email_state(user):
    return {
        "email": user.email,
        "pending_email": user.pending_email,
        "pending_email_expired": _pending_email_expired(user),
    }


@dashboard_bp.route("/email", methods=["POST"])
@login_required
def request_email_change():
    """Start an email change / add (#260): send a confirmation link to the
    NEW address. Nothing binds until that link is confirmed from inside
    this account (confirm_email_change below); until then the address sits
    in `pending_email` so the UI can say "check your inbox".

    An address that already signs in to ANOTHER account gets the same
    response and the same pending state, but no usable link: the mail that
    goes out tells the inbox's owner the address is in use. Answering "in
    use" here would let any logged-in user test which addresses have
    accounts, which /auth/magic-link/send takes care not to reveal."""
    raw = _json_object().get("email")
    new_email = raw.strip().lower() if isinstance(raw, str) else ""
    if not is_valid_email(new_email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    if current_user.email and current_user.email.lower() == new_email:
        return jsonify({"error": "That is already your email address."}), 400
    taken = User.query.filter(db.func.lower(User.email) == new_email,
                              User.id != current_user.id).first() is not None

    # Persist first, send second: a delivered link always has its hash on
    # the row. A failed send puts back whatever was pending before, so it
    # leaves no "pending" address the user never got a link for and keeps
    # an earlier link working.
    previous = (current_user.pending_email,
                current_user.email_change_token_hash,
                current_user.email_change_expires_at)
    lifetime = email_change_expiry_seconds()
    token = generate_email_change_token(current_user.id, new_email)
    current_user.pending_email = new_email
    current_user.email_change_token_hash = None if taken else hash_token(token)
    current_user.email_change_expires_at = (
        datetime.utcnow() + timedelta(seconds=lifetime))
    db.session.commit()
    try:
        if taken:
            send_email_in_use_notice(new_email)
        else:
            frontend_url = current_app.config.get("FRONTEND_URL", "").rstrip("/")
            send_email_change_email(
                new_email, f"{frontend_url}/confirm-email?token={token}",
                lifetime)
    except Exception:
        # Unless another request (a second tab) replaced ours while the
        # send ran: its link is out, and restoring over it would void it.
        ours = (new_email, None if taken else hash_token(token))
        if (current_user.pending_email,
                current_user.email_change_token_hash) == ours:
            (current_user.pending_email,
             current_user.email_change_token_hash,
             current_user.email_change_expires_at) = previous
            db.session.commit()
        return jsonify({"error": "Could not send the confirmation email. "
                                 "Please try again."}), 502
    return jsonify({
        "message": f"Confirmation link sent to {new_email}. The address "
                   "becomes yours once you confirm it.",
        **_email_state(current_user),
    }), 200


_LINK_INVALID = ("That confirmation link is invalid or has expired. "
                 "Request a new one.")


@dashboard_bp.route("/email/confirm", methods=["POST"])
@login_required
def confirm_email_change():
    """Bind the pending address (#260). Body: {"token"} from the link mailed
    to the new address.

    The token only counts inside a session of the account that asked for
    the change, and opening it never signs anyone in. The link alone must
    not be enough: a mistyped address would hand the account to whoever
    received the mail, and a request for someone else's address would walk
    that person into the requester's account. A POST from the signed-in app
    rather than a GET on the link, so mail scanners and link previews that
    fetch the URL change nothing."""
    token = _json_object().get("token")
    payload = (verify_email_change_token(token)
               if isinstance(token, str) and token else None)
    if payload is None:
        return jsonify({"error": _LINK_INVALID,
                        "reason": "invalid_or_expired"}), 400
    if payload["user_id"] != current_user.id:
        return jsonify({
            "error": "This confirmation link was requested from a different "
                     "Loore account. Sign in to that account to use it.",
            "reason": "other_account"}), 403
    new_email = payload["email"]
    if (current_user.email or "").lower() == new_email:
        # A second click or a double submit: already done, say so again
        # rather than "invalid".
        return jsonify({"message": "Email confirmed.",
                        **_email_state(current_user)}), 200
    if (current_user.email_change_token_hash != hash_token(token)
            or (current_user.pending_email or "").lower() != new_email
            or _pending_email_expired(current_user)):
        # Superseded by a newer request, cancelled, or replaced by an admin.
        return jsonify({"error": _LINK_INVALID,
                        "reason": "invalid_or_expired"}), 400

    taken_error = jsonify({
        "error": "That email was claimed by another account before you "
                 "confirmed it.",
        "reason": "taken"})
    if User.query.filter(db.func.lower(User.email) == new_email,
                         User.id != current_user.id).first():
        _clear_pending_email(current_user)
        db.session.commit()
        return taken_error, 409
    user_id = current_user.id
    old_email = current_user.email
    current_user.email = new_email
    _clear_pending_email(current_user)
    try:
        db.session.commit()
    except IntegrityError:
        # Another account took the address between the check and the
        # commit (two pending changes confirmed at once, or a magic-link
        # signup for it): the unique constraint decides.
        db.session.rollback()
        user = User.query.get(user_id)
        _clear_pending_email(user)
        db.session.commit()
        return taken_error, 409
    logger.info("User %s bound a confirmed email (had one before: %s)",
                user_id, bool(old_email))
    if old_email and old_email.lower() != new_email:
        send_email_changed_notice(old_email, new_email)
    return jsonify({"message": "Email confirmed.",
                    **_email_state(current_user)}), 200


@dashboard_bp.route("/email/pending", methods=["DELETE"])
@login_required
def cancel_email_change():
    """Cancel a pending change (#260). Its own route, and it never touches
    the bound email: with one DELETE deciding by server state, "Cancel" in
    a tab that still showed a change already confirmed elsewhere removed
    the account's real address."""
    _clear_pending_email(current_user)
    db.session.commit()
    return jsonify({"message": "Pending email change cancelled.",
                    **_email_state(current_user)}), 200


@dashboard_bp.route("/email", methods=["DELETE"])
@login_required
def remove_email():
    """Drop the account's email (#260). Only allowed when the account keeps
    another way in (Sign in with X) — otherwise it would lock the user
    out."""
    only_way_in = jsonify({"error": "This email is your only way to sign in. "
                                    "Add another address first."}), 400
    if not current_user.twitter_id:
        return only_way_in
    # Conditional on the X login still being there: a Disconnect X running
    # at the same time (another tab) must not leave neither.
    written = (User.query
               .filter(User.id == current_user.id, User.twitter_id.isnot(None))
               .update({"email": None, "pending_email": None,
                        "email_change_token_hash": None,
                        "email_change_expires_at": None},
                       synchronize_session=False))
    db.session.commit()
    db.session.refresh(current_user)
    if not written:
        return only_way_in
    return jsonify({"message": "Email removed.",
                    **_email_state(current_user)}), 200


@dashboard_bp.route("/x", methods=["DELETE"])
@login_required
def disconnect_x():
    """Drop the account's X login (#311), the counterpart of Connect X
    (/auth/x/connect). Only allowed when the account keeps another way in
    (its email), as remove_email above is the other way round. Nothing
    imported from X is touched; the X account can then be connected here
    again, or signed in with on its own, which makes a new account."""
    only_way_in = jsonify({"error": "X is your only way to sign in. "
                                    "Add an email first."}), 400
    if not current_user.email:
        return only_way_in
    # Conditional on the email still being there (see remove_email).
    written = (User.query
               .filter(User.id == current_user.id, User.email.isnot(None))
               .update({"twitter_id": None, "twitter_handle": None,
                        "x_connected_at": None},
                       synchronize_session=False))
    db.session.commit()
    db.session.refresh(current_user)
    if not written:
        return only_way_in
    # The session's X token belongs to the account just disconnected.
    from backend.routes.auth import _drop_x_token
    _drop_x_token()
    return jsonify({"message": "X disconnected.", "twitter_login": False,
                    "twitter_handle": None}), 200


# New endpoint to update the user’s display handle and description.
@dashboard_bp.route("/user", methods=["PUT"])
@login_required
def update_user():
    data = request.get_json()
    new_username = data.get("username")
    new_description = data.get("description")
    # `email` is deliberately NOT accepted here any more (#260): binding an
    # address to a logged-in session without proving control of it would
    # let a hijacked session re-home the account. POST /dashboard/email
    # sends a verification link; the address binds when it is opened.
    if "email" in data:
        return jsonify({
            "error": "Email changes go through verification: use "
                     "POST /api/dashboard/email."}), 400

    if new_description and len(new_description) > 128:
        return jsonify({"error": "Description exceeds maximum length of 128 characters."}), 400

    renamed_from = None
    if new_username:
        new_username = new_username.strip()
        # Validates non-empty, length, allowed chars, reserved names,
        # case-insensitive uniqueness (excluding the current user's own row)
        # and other accounts' former handles (#253).
        error = validate_username(new_username, exclude_user_id=current_user.id)
        if error:
            return jsonify({"error": error}), 400
        if new_username != current_user.username:
            renamed_from = current_user.username
        current_user.username = new_username

    if new_description is not None:
        current_user.description = new_description

    if "craft_mode" in data:
        current_user.craft_mode = bool(data["craft_mode"])

    sharing_flipped = False
    if "public_sharing_enabled" in data:
        new_sharing = bool(data["public_sharing_enabled"])
        sharing_flipped = (
            current_user.public_sharing_enabled != new_sharing)
        current_user.public_sharing_enabled = new_sharing

    if "external_content_enabled" in data:
        current_user.external_content_enabled = bool(
            data["external_content_enabled"])

    if "preferred_model" in data:
        current_user.preferred_model = data["preferred_model"]

    if "default_privacy_level" in data:
        val = data["default_privacy_level"]
        if val not in VALID_PRIVACY_LEVELS:
            return jsonify({"error": f"Invalid privacy level: {val}"}), 400
        current_user.default_privacy_level = val

    if "default_ai_usage" in data:
        val = data["default_ai_usage"]
        if val not in VALID_AI_USAGE:
            return jsonify({"error": f"Invalid AI usage value: {val}"}), 400
        current_user.default_ai_usage = val

    if "prefill_consent" in data:
        val = data["prefill_consent"]
        if val not in ("yes", "no"):
            return jsonify({"error": f"Invalid prefill_consent value: {val}"}), 400
        current_user.prefill_consent = val
        current_user.prefill_consent_at = datetime.now(timezone.utc)

    if renamed_from:
        # #253: the old handle keeps resolving (a redirect to the new one)
        # and stays reserved, while the account has public writing to reach
        # that way. After every field check above, so a 400 further down
        # never leaves history behind.
        from backend.utils.username_history import record_rename
        record_rename(current_user, renamed_from, new_username)

    try:
        db.session.commit()
        # The opt-out must reach the open web immediately: drop every
        # cached public page this user's content appears on. A rename
        # drops the same set — those pages are cached under the old
        # handle and carry it in their bylines — and only once the new
        # name is committed, so a request landing in between can't
        # re-cache the old one.
        if sharing_flipped or renamed_from:
            from backend.utils.public_cache import invalidate_for_user
            invalidate_for_user(current_user, former_handle=renamed_from)
        # Include voice mode feature flag and user plan in the response
        voice_mode_enabled = current_user.has_voice_mode
        return jsonify({
            "message": "Profile updated successfully.",
            "user": {
                "id": current_user.id,
                "username": current_user.username,
                "description": current_user.description,
                "email": current_user.email,
                "approved": current_user.approved,
                "accepted_terms_at": iso_utc(current_user.accepted_terms_at),
                "terms_up_to_date": _terms_up_to_date(current_user),
                "is_admin": current_user.is_admin,
                "plan": current_user.plan,
                "voice_mode_enabled": voice_mode_enabled,
                "craft_mode": current_user.craft_mode,
                "preferred_model": current_user.preferred_model,
                "profile_generation_task_id": current_user.profile_generation_task_id,
                "profile_batch_pending": bool(current_user.profile_batch_pending),
                "default_privacy_level": current_user.default_privacy_level,
                "default_ai_usage": current_user.default_ai_usage,
                "twitter_login": bool(current_user.twitter_id),
                "twitter_handle": current_user.twitter_handle,
                "pending_email": current_user.pending_email,
                "pending_email_expired": _pending_email_expired(current_user),
                "prefill_consent": current_user.prefill_consent,
                "prefilled_handle": current_user.prefilled_handle,
                "spend_blocked": user_is_capped(current_user),
                "share_v1_enabled": bool(
                    current_app.config.get("SHARE_V1", False)
                    and current_user.public_sharing_enabled),
                "share_v1_available": bool(
                    current_app.config.get("SHARE_V1", False)),
                "public_sharing_enabled": bool(
                    current_user.public_sharing_enabled),
                "external_content_available": bool(
                    current_app.config.get(
                        "SEMANTIC_SEARCH_AGENTIC", True)),
                "external_content_enabled": bool(
                    current_user.external_content_enabled),
                "timezone": current_user.timezone or "UTC",
            }
        }), 200
    except IntegrityError:
        # Two submissions of the same rename racing (username_history's
        # unique index) or two accounts racing for one handle (user's):
        # the first won; this one re-reads instead of echoing the SQL.
        db.session.rollback()
        return jsonify({"error": "Your profile changed in another request. "
                                 "Reload and try again."}), 409
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update profile.", "details": str(e)}), 500


# Persist the browser-reported IANA timezone (e.g. "Europe/Prague"), used to
# render absolute local-time stamps in the LLM context (#130). Called by the
# frontend on session start when the detected timezone differs from the stored
# one. Fire-and-forget: invalid values are rejected rather than clobbering the
# stored timezone.
@dashboard_bp.route("/timezone", methods=["PATCH"])
@login_required
def update_timezone():
    data = request.get_json(silent=True) or {}
    tz_name = data.get("timezone")
    if not is_valid_timezone(tz_name):
        return jsonify({"error": "Invalid timezone."}), 400
    current_user.timezone = tz_name
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to update timezone.",
                        "details": str(e)}), 500
    return jsonify({"timezone": current_user.timezone}), 200
