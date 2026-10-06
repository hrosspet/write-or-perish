from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from backend.models import Draft, Node, NodeTranscriptChunk
from backend.extensions import db
from backend.utils.privacy import can_user_edit_node
import uuid
import pathlib
import os
import shutil
from datetime import datetime
from backend.utils.audio_storage import (
    is_storage_id, move_session_audio_to_node, storage_path,
)
from backend.utils.encryption import encrypt_file_atomically
from backend.utils.llm_nodes import (
    AIUsageRefused, ai_usage_refused_response, pick_model_for_generation,
    voice_turn_refusal,
)
from backend.utils.spend import require_spend_headroom
from backend.utils.webm_utils import (
    chunk_is_init_bearing, persist_init_segment,
)
from backend.utils.streaming_session import (
    not_live_clause, release_session, session_is_live, stamp_session_alive,
)

drafts_bp = Blueprint("drafts_bp", __name__)


@drafts_bp.before_request
def _refuse_malformed_session_id():
    """A <session_id> in the URL names a folder on disk: one that is not a
    plain folder name (audio_storage.is_storage_id) gets the same 404 as
    a session that does not exist."""
    session_id = (request.view_args or {}).get("session_id")
    if session_id is not None and not is_storage_id(session_id):
        return jsonify({
            "error": "Streaming session not found",
            "code": "session_not_found",
        }), 404
    return None


def _session_dir(user_id, session_id):
    """drafts/<user_id>/<session_id> under AUDIO_STORAGE_ROOT."""
    return storage_path(AUDIO_STORAGE_ROOT, "drafts", user_id, session_id)


# Proposal-pending drafts (created by the agentic loop's _auto_create_drafts,
# consumed by apply_* / the proposal REST routes) live in the same Draft table
# but are NOT composing/input drafts. They are keyed on the proposal node's id
# via parent_id — which collides with the input draft a user composes when
# replying under that same node. The input-draft endpoints (get/save/delete)
# must therefore exclude these labels, or composing a text reply under a
# proposal node would hijack and then delete the pending proposal draft
# (breaking "yes, send it" / "apply those changes" text confirmation). #158.
_PROPOSAL_DRAFT_LABELS = (
    "todo_pending", "github_issue_pending", "feedback_pending",
    "share_pending",
)


def _exclude_proposal_drafts(query):
    """Restrict an input-draft query so it never matches proposal-pending
    drafts that happen to share the same (parent_id, node_id)."""
    return query.filter(
        db.or_(
            Draft.label.is_(None),
            Draft.label.notin_(_PROPOSAL_DRAFT_LABELS),
        )
    )


def _input_drafts(node_id, parent_id):
    """The current user's input drafts for one writing context (an edit of
    *node_id*, or a new entry under *parent_id*, top level when None),
    newest first. GET, POST and DELETE all start here, so autosave writes
    the row GET restores and a send deletes that same row.

    Never a proposal-pending draft (#158), nor a recording the server
    chain already saved as a node (llm_node_id / streaming_warning). Such
    a row waits for its SSE all_complete, or, when the client polls
    /status instead (the iPhone app always does), for the next
    streaming/init's _cleanup_stale_drafts. Picked as "the" draft with no
    ordering, it took the typed text out of GET's sight, and a send
    deleted it in place of the typed draft, which then came back with the
    entry just sent.
    """
    query = Draft.query.filter_by(user_id=current_user.id)
    query = _exclude_proposal_drafts(query)
    query = query.filter(Draft.llm_node_id.is_(None),
                         Draft.streaming_warning.is_(None))
    if node_id:
        query = query.filter_by(node_id=node_id)
    else:
        query = query.filter_by(node_id=None)
        if parent_id:
            query = query.filter_by(parent_id=parent_id)
        else:
            query = query.filter_by(parent_id=None)
    return query.order_by(Draft.updated_at.desc(), Draft.id.desc())


def _no_session_in_progress_clause():
    """Every Draft except a session still in 'recording', live or left
    behind (#320), or in 'finalizing' (its finalize task is turning it
    into a node). Those end only through the task, save-as-node or
    /streaming/<id>/discard: typed text never goes into their row (it
    would overwrite the transcript), and deleting the input draft never
    takes them (it would lose the turn and orphan its audio)."""
    return db.or_(
        Draft.streaming_status.is_(None),
        Draft.streaming_status.notin_(('recording', 'finalizing')),
    )


def _dead_session_clause():
    """A recording session nothing can continue or recover: still in
    'recording', no sign of life within the liveness window (#320), and
    not one chunk stored. A recording left before its first chunk was
    uploaded ends this way (the iPhone app, leaving the Voice screen,
    told the server nothing; the web releases it on leaving). The voice
    banner skips it (/interrupted lists sessions with chunks only), so
    shown in the writing form and spared by DELETE it came back after
    every send and discard, holding whatever autosave had put in it."""
    has_chunks = db.exists().where(
        NodeTranscriptChunk.session_id == Draft.session_id)
    return db.and_(
        Draft.session_id.isnot(None),
        Draft.streaming_status.isnot(None),
        Draft.streaming_status == 'recording',
        not_live_clause(),
        ~has_chunks,
    )


def _delete_dead_session(draft):
    """Delete a dead session's row and its (chunkless) folder on disk."""
    try:
        audio_dir = _session_dir(draft.user_id, draft.session_id)
    except ValueError:
        audio_dir = None
    if audio_dir is not None and audio_dir.exists():
        shutil.rmtree(audio_dir, ignore_errors=True)
    db.session.delete(draft)


def _parent_error(parent_id):
    """An error response when *parent_id* names a node the current user
    may not build on (missing, or not visible to them: 404), else None.
    No parent is fine. A parent the user could see before it was deleted
    passes: callers keep their own handling of deleted parents."""
    if not parent_id:
        return None
    try:
        pid = int(parent_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parent_id"}), 400
    from backend.utils.node_deletion import parent_visibility_error
    return parent_visibility_error(Node.query.get(pid), current_user.id)


def _editable_node(node_id):
    """(node, None) when the current user may edit the node *node_id*
    (can_user_edit_node), else (None, a 404 response): a node the user
    cannot edit gets the same 404 as one that does not exist. A deleted
    node the user could edit is returned; callers handle deletion."""
    node = Node.query.get(node_id)
    if node is None or not can_user_edit_node(node):
        return None, (jsonify({"error": "Node not found"}), 404)
    return node, None


# Audio storage root - same as in nodes.py
AUDIO_STORAGE_ROOT = pathlib.Path(
    os.environ.get("AUDIO_STORAGE_PATH", "data/audio")
).resolve()


def _mime_family(m: str) -> str:
    """Return the MIME family prefix (substring before `;`) lowercased.

    Browsers disagree on whether `mediaRecorder.mimeType` retains codec
    params after construction (Safari may report 'audio/mp4', Chrome
    'audio/mp4;codecs=mp4a.40.2'). We persist and compare on the family
    only so chunk-N mime checks can't be defeated by codec-string drift.
    """
    return (m or '').split(';', 1)[0].strip().lower()


@drafts_bp.route("/", methods=["GET"])
@login_required
def get_draft():
    """
    Get a draft for the current user.
    Query params:
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    Returns the draft if found, or 404 if no draft exists.
    Drafts are private - only the owner can access them.
    """
    node_id = request.args.get("node_id", type=int)
    parent_id = request.args.get("parent_id", type=int)

    # Validate node_id if provided - user must own the node OR be LLM
    # requester (parent node owner); any other node answers 404.
    if node_id:
        node, err = _editable_node(node_id)
        if err is not None:
            return err
        # Soft-deleted target — treat as gone (per plan §17). The
        # underlying Draft row is left alone so a future "rescue
        # interrupted drafts" UI could surface it; at the GET-by-target
        # entry point, behave as if no draft exists.
        if node.deleted_at is not None:
            return jsonify({"error": "Node not found"}), 404

    # Plan §17 parent_id branch: if the parent has been soft-deleted,
    # we still want to surface the user's in-progress writing — but
    # rebound to a top-level draft (parent_id=None) and flag a UI
    # warning. We resolve this by reading parent.deleted_at first;
    # the draft-fetch logic below decides the actual lookup key.
    parent_deleted = False
    if parent_id:
        parent = Node.query.get(parent_id)
        if parent is not None and parent.deleted_at is not None:
            parent_deleted = True

    # The most recent input draft (stale empty drafts must not hide newer
    # ones with actual content or stored audio chunks). Even under a
    # soft-deleted parent, the lookup uses the original parent_id so the
    # user's saved content comes back (with a warning); the response
    # below null-rebinds the parent_id field per plan §17. A draft saved
    # as a node with a streaming_warning (spend cap, #341, or a refused
    # placeholder) is not restored: it would save the transcript twice.
    query = _input_drafts(node_id, parent_id)

    # A session another tab is recording right now is not a draft to
    # load here: this view would auto-recover it, which completes it
    # under the recording tab (#320). Nor a dead one: it holds no audio,
    # and nothing the form could do would end it.
    query = query.filter(not_live_clause(), db.not_(_dead_session_clause()))

    draft = query.first()

    if not draft:
        return jsonify({"error": "No draft found"}), 404

    response_data = {
        "id": draft.id,
        "content": draft.get_content(),
        "node_id": draft.node_id,
        "parent_id": None if parent_deleted else draft.parent_id,
        "created_at": draft.created_at.isoformat() + "Z",
        "updated_at": draft.updated_at.isoformat() + "Z"
    }
    if parent_deleted:
        response_data["parent_deleted"] = True
        response_data["warning"] = (
            "Original parent was deleted; this draft is now top-level."
        )

    # Include streaming session info so frontend can trigger recovery
    if draft.session_id:
        response_data["session_id"] = draft.session_id
        stored_count = NodeTranscriptChunk.query.filter_by(
            session_id=draft.session_id,
            status='stored'
        ).count()
        response_data["has_stored_chunks"] = stored_count > 0

    return jsonify(response_data), 200


@drafts_bp.route("/interrupted", methods=["GET"])
@login_required
def get_interrupted_drafts():
    """
    Find streaming drafts that were interrupted (e.g. page refresh mid-recording).

    Returns drafts where:
    - streaming_status is 'recording' (never finalized)
    - session_id is set
    - llm_node_id is NULL (not already processed)
    - Has at least one stored or completed chunk
    - is not live: no tab has shown a sign of life within the liveness
      window (#320) — a recording still running elsewhere is not
      interrupted, and recovering or resuming it here would end it there

    Returns the most recent interrupted draft regardless of parent context,
    so recovery works from any entry point (Reflect, Orient, Log resume).
    """
    query = Draft.query.filter(
        Draft.user_id == current_user.id,
        Draft.session_id.isnot(None),
        Draft.streaming_status == 'recording',
        Draft.llm_node_id.is_(None),
        not_live_clause(),
    )

    drafts = query.order_by(Draft.updated_at.desc()).all()

    # Resolve all referenced parent_ids in one query so we can apply the
    # plan §17 rules without N+1 lookups.
    parent_ids = {d.parent_id for d in drafts if d.parent_id is not None}
    deleted_parent_ids = set()
    if parent_ids:
        deleted_parent_ids = {
            row.id for row in
            Node.query.filter(
                Node.id.in_(parent_ids),
                Node.deleted_at.isnot(None),
            ).with_entities(Node.id).all()
        }

    # Same lookup for node_id (editing target). Drafts whose edit target
    # is gone get omitted entirely.
    node_ids = {d.node_id for d in drafts if d.node_id is not None}
    deleted_node_ids = set()
    if node_ids:
        deleted_node_ids = {
            row.id for row in
            Node.query.filter(
                Node.id.in_(node_ids),
                Node.deleted_at.isnot(None),
            ).with_entities(Node.id).all()
        }

    results = []
    for draft in drafts:
        # Plan §17: omit drafts whose edit target is soft-deleted.
        if draft.node_id and draft.node_id in deleted_node_ids:
            continue

        chunk_count = NodeTranscriptChunk.query.filter_by(
            session_id=draft.session_id,
        ).count()
        if chunk_count == 0:
            continue

        stored_count = NodeTranscriptChunk.query.filter_by(
            session_id=draft.session_id,
            status='stored',
        ).count()

        # Plan §17: drafts with a soft-deleted parent_id surface with
        # parent_id null + a warning, so the user's in-progress writing
        # isn't lost.
        parent_deleted = (
            draft.parent_id is not None
            and draft.parent_id in deleted_parent_ids
        )
        entry = {
            "id": draft.id,
            "session_id": draft.session_id,
            "parent_id": None if parent_deleted else draft.parent_id,
            "label": draft.label,
            "content": draft.get_content(),
            "chunk_count": chunk_count,
            "has_stored_chunks": stored_count > 0,
            "streaming_mime_type": draft.streaming_mime_type,
            "created_at": draft.created_at.isoformat() + "Z",
            "updated_at": draft.updated_at.isoformat() + "Z",
        }
        if parent_deleted:
            entry["parent_deleted"] = True
            entry["warning"] = (
                "Original parent was deleted; this draft is now top-level."
            )
        results.append(entry)

    return jsonify(results), 200


@drafts_bp.route("/", methods=["POST"])
@login_required
def save_draft():
    """
    Create or update a draft for the current user.
    Body:
      - content: The draft content (required)
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    If a draft already exists for this context, it will be updated.
    Drafts are private - only the owner can access them.
    """
    data = request.get_json() or {}
    content = data.get("content", "")
    node_id = data.get("node_id")
    parent_id = data.get("parent_id")

    # Validate node_id if provided - user must own the node OR be LLM
    # requester (parent node owner); any other node answers 404.
    if node_id:
        node, err = _editable_node(node_id)
        if err is not None:
            return err
        # Soft-deleted edit target — match the create endpoint's 410
        # so the frontend can treat parent/edit-target deletions
        # uniformly (clear local state, surface a warning).
        if node.deleted_at is not None:
            return jsonify({"error": "Node has been deleted"}), 410

    # Validate parent_id if provided - the user must be able to see the
    # parent (404 like a missing one otherwise), as when creating a node.
    if parent_id:
        err = _parent_error(parent_id)
        if err is not None:
            return err
        parent = Node.query.get(int(parent_id))
        if parent.deleted_at is not None:
            return jsonify({"error": "Parent node has been deleted"}), 410

    # The newest input draft, as GET restores it, but never a session in
    # progress (#320): a second tab's autosave would overwrite its
    # transcript. Typing next to one goes to a plain draft of its own,
    # which is then the newest.
    draft = _input_drafts(node_id, parent_id).filter(
        _no_session_in_progress_clause()).first()

    if draft:
        # Update existing draft
        draft.set_content(content)
    else:
        # Create new draft
        draft = Draft(
            user_id=current_user.id,
            node_id=node_id,
            parent_id=parent_id
        )
        draft.set_content(content)
        db.session.add(draft)

    db.session.commit()

    # Refresh to get the updated timestamp from database
    db.session.refresh(draft)

    return jsonify({
        "id": draft.id,
        "content": draft.get_content(),
        "node_id": draft.node_id,
        "parent_id": draft.parent_id,
        "created_at": draft.created_at.isoformat() + "Z",
        "updated_at": draft.updated_at.isoformat() + "Z"
    }), 200


@drafts_bp.route("/", methods=["DELETE"])
@login_required
def delete_draft():
    """
    Delete a draft for the current user.
    Query params:
      - node_id: If editing an existing node (optional)
      - parent_id: If creating a new node under a parent (optional)

    Called when user saves their work or explicitly discards the draft.
    Drafts are private - only the owner can delete them.
    """
    node_id = request.args.get("node_id", type=int)
    parent_id = request.args.get("parent_id", type=int)

    # Validate node_id if provided - user must own the node OR be LLM
    # requester (parent node owner); any other node answers 404.
    if node_id:
        _node, err = _editable_node(node_id)
        if err is not None:
            return err

    # Every input draft of the context, not just the newest: after a send
    # or a discard none may come back. Never a proposal draft (deleting the
    # composing draft under a proposal node must not take the pending
    # proposal with it) nor a session in progress (#320), unless it is
    # dead: then nothing else would ever end it.
    drafts = _input_drafts(node_id, parent_id).filter(db.or_(
        _no_session_in_progress_clause(), _dead_session_clause())).all()

    if not drafts:
        return jsonify({"error": "No draft found"}), 404

    for draft in drafts:
        if draft.streaming_status == 'recording':
            _delete_dead_session(draft)
        else:
            db.session.delete(draft)
    db.session.commit()

    return jsonify({"message": "Draft deleted"}), 200


@drafts_bp.route("/streaming/<session_id>/discard", methods=["DELETE"])
@login_required
def discard_streaming_draft(session_id):
    """
    Discard an interrupted streaming draft and its audio chunks.

    Deletes the draft, its NodeTranscriptChunk records, and audio files.
    """
    import shutil

    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id,
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    # Delete chunk records
    NodeTranscriptChunk.query.filter_by(session_id=session_id).delete()

    # Delete audio files
    audio_dir = _session_dir(draft.user_id, draft.session_id)
    if audio_dir.exists():
        shutil.rmtree(audio_dir)

    db.session.delete(draft)
    db.session.commit()

    current_app.logger.info(
        f"Discarded interrupted draft {draft.id} (session {session_id})"
    )

    return jsonify({"message": "Draft discarded"}), 200


# =============================================================================
# Streaming Transcription Endpoints (Draft-based)
# =============================================================================

def _cleanup_stale_drafts(user_id):
    """Delete streaming drafts already processed by the server-side LLM chain.

    When Reflect/Orient workflows complete, the transcript is saved as a node
    and llm_node_id is set on the draft. The draft only lingers for the SSE
    all_complete event — after that it's safe to delete.
    """
    stale_drafts = Draft.query.filter(
        Draft.user_id == user_id,
        Draft.session_id.isnot(None),
        # streaming_warning: saved as a node with the reply skipped (#341)
        db.or_(Draft.llm_node_id.isnot(None),
               Draft.streaming_warning.isnot(None)),
    ).all()

    deleted = 0
    for draft in stale_drafts:
        try:
            audio_dir = _session_dir(user_id, draft.session_id)
        except ValueError:
            continue
        if audio_dir.exists():
            current_app.logger.warning(
                f"Draft {draft.id} (session {draft.session_id}) was "
                f"saved as a node (llm_node_id={draft.llm_node_id}) but "
                f"audio files were not moved: {list(audio_dir.iterdir())}"
            )
            continue
        db.session.delete(draft)
        deleted += 1

    # Dead sessions too (any context): no audio, nothing to recover.
    for draft in Draft.query.filter(
            Draft.user_id == user_id, _dead_session_clause()).all():
        _delete_dead_session(draft)
        deleted += 1

    if deleted:
        db.session.commit()
        current_app.logger.info(
            f"Cleaned up {deleted} stale drafts for user {user_id}"
        )


@drafts_bp.route("/streaming/init", methods=["POST"])
@login_required
@require_spend_headroom
def init_streaming():
    """
    Initialize a streaming transcription session.

    Creates a Draft record to store the streaming session and transcript.
    NO node is created until the user explicitly saves.

    A capped user gets 402 here, before the frontend opens the mic (#341).
    Resuming an interrupted session never calls init, and its chunks
    (audio-chunk, transcribe-remaining, finalize) are not cap-checked.

    A Voice recording (label "Voice") where AI may not read gets 403
    ``{"error", "code": "ai_usage_none", "scope"}`` and no draft, so the
    mic never opens: a fresh thread when the account's Default AI usage
    or the ai_usage sent is 'none', a continued one (parent_id) when the
    thread is not AI-readable (voice_turn_refusal). Voice mode exists to
    get a reply, and that reply would send the recording to a model.

    Request body:
    {
        "parent_id": 123,  // optional - parent node for the eventual node
        "privacy_level": "private",  // optional
        "ai_usage": "none",  // optional
        "label": "Voice",  // optional
    }

    Returns: { "session_id": "uuid", "draft_id": 456 }
    """
    _cleanup_stale_drafts(current_user.id)

    data = request.get_json() or {}

    parent_id = data.get("parent_id")
    err = _parent_error(parent_id)
    if err is not None:
        return err
    privacy_level = data.get("privacy_level", "private")
    ai_usage = data.get("ai_usage", "none")
    label = data.get("label")  # 'Reflect', 'Orient', etc.
    if label == "Voice":
        parent = Node.query.get(int(parent_id)) if parent_id else None
        refused = voice_turn_refusal(
            current_user, parent, None if parent else ai_usage)
        if refused is not None:
            return ai_usage_refused_response(refused)

    # Generate session ID
    session_id = str(uuid.uuid4())

    # Check if a draft already exists for this parent_id context
    # If so, we'll use a new session but keep the draft context separate
    # For streaming, we always create a new draft with session_id
    draft = Draft(
        user_id=current_user.id,
        parent_id=parent_id,
        session_id=session_id,
        streaming_status="recording",
        streaming_total_chunks=None,
        streaming_completed_chunks=0,
        label=label,
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        streaming_heartbeat_at=datetime.utcnow(),
    )
    draft.set_content("")  # Will be populated as chunks are transcribed
    db.session.add(draft)
    db.session.commit()

    # Create directory for chunk storage
    chunk_dir = _session_dir(current_user.id, session_id)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    current_app.logger.info(
        f"Initialized streaming session {session_id} for draft {draft.id} "
        f"(user: {current_user.id})"
    )

    return jsonify({
        "session_id": session_id,
        "draft_id": draft.id,
        "sse_url": f"/api/sse/drafts/{session_id}/transcription-stream"
    }), 201


def _chunk_already_uploaded(chunk_index, chunk):
    return jsonify({
        "message": "Chunk already uploaded",
        "chunk_index": chunk_index,
        "status": chunk.status
    }), 200


@drafts_bp.route("/streaming/<session_id>/audio-chunk", methods=["POST"])
@login_required
def upload_streaming_chunk(session_id):
    """
    Upload an audio chunk for streaming transcription.

    Receives an audio chunk, stores it, and queues it for transcription.
    When transcription completes, the text is appended to the draft content.

    Expects multipart/form-data with:
    - chunk: audio file blob
    - chunk_index: integer (0-based)
    - mime_type: optional, family-only or codec-qualified MIME of the
      chunk (e.g. 'audio/webm', 'audio/mp4;codecs=mp4a.40.2'). Defaults
      to 'audio/webm' for backward compatibility with pre-MP4 frontends.

    Returns: { "chunk_index": 0, "task_id": "celery-task-id" }
    """
    # Find the draft by session_id
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        # The codes tell the recorder the session is gone for good (e.g.
        # ended or discarded from another tab), so it stops recording
        # instead of retrying into it (#320).
        return jsonify({
            "error": "Streaming session not found",
            "code": "session_not_found",
        }), 404

    if draft.streaming_status not in ["recording", "finalizing"]:
        return jsonify({
            "error": "Streaming session is not active",
            "code": "session_not_active",
        }), 400

    # A chunk is the recording tab's sign of life (#320) — unless the tab
    # already released the session: an upload still in flight when the
    # tab left must not make it live again.
    stamp_session_alive(session_id, only_if_unreleased=True)
    db.session.commit()

    # Get form data
    if "chunk" not in request.files:
        return jsonify({"error": "Missing chunk file"}), 400

    chunk_file = request.files["chunk"]
    chunk_index = request.form.get("chunk_index")

    if chunk_index is None:
        return jsonify({"error": "Missing chunk_index"}), 400

    try:
        chunk_index = int(chunk_index)
    except ValueError:
        return jsonify({"error": "Invalid chunk_index"}), 400

    # Normalize the chunk's mime to a family prefix; older frontends
    # without the field send WebM by definition.
    form_mime_family = _mime_family(
        request.form.get("mime_type", "audio/webm")
    )
    if form_mime_family not in ("audio/webm", "audio/mp4"):
        return jsonify({
            "error": f"Unsupported audio mime: {form_mime_family}",
        }), 400

    # On chunks past 0, the session mime is locked in by chunk 0. Reject
    # any chunk that disagrees — closes the resume-on-different-device
    # class of bug where a user could try to append fMP4 fragments to a
    # WebM session (or vice versa).
    if (chunk_index > 0
            and draft.streaming_mime_type
            and form_mime_family != draft.streaming_mime_type):
        return jsonify({
            "error": (
                f"Chunk mime {form_mime_family!r} does not match "
                f"session mime {draft.streaming_mime_type!r}"
            ),
            "code": "mime_mismatch",
        }), 400

    ext = ".mp4" if form_mime_family == "audio/mp4" else ".webm"

    # Save chunk to disk
    chunk_dir = _session_dir(current_user.id, draft.session_id)
    if not chunk_dir.exists():
        return jsonify({"error": "Streaming session directory not found"}), 404

    # A copy of a chunk that is already stored is a duplicate: a hidden
    # page sends every chunk by sendBeacon and by the normal upload (#88).
    # Answer it before touching the files (#371).
    existing_chunk = NodeTranscriptChunk.query.filter_by(
        session_id=session_id, chunk_index=chunk_index).first()
    if existing_chunk and existing_chunk.status != 'failed':
        return _chunk_already_uploaded(chunk_index, existing_chunk)

    chunk_filename = f"chunk_{chunk_index:04d}{ext}"
    chunk_path = chunk_dir / chunk_filename
    # Two copies can still arrive at once: each writes a file of its own,
    # which is encrypted and then moved into place, so neither reads or
    # deletes the other's half-written file. (Not "chunk_*": playback
    # lists those.)
    upload_path = chunk_dir / f"upload-{uuid.uuid4().hex}{ext}"
    chunk_file.save(upload_path)

    # Chunk 0 carries the format's init segment — the bytes that every
    # later batch needs as a prefix to remain a valid file (WebM:
    # EBML/Segment/Tracks; fMP4: ftyp+moov). Extract and persist it now,
    # before the upload file is encrypted and moved into place.
    #
    # Reject the upload if extraction fails: batch 1 (chunks 0..19) would
    # still succeed because chunk 0 carries its own header, but any batch
    # starting at chunk 20+ would fail silently for want of an init segment.
    # Better to surface the problem on the first chunk than silently lose
    # later audio.
    if chunk_index == 0:
        # Narrow to the failure modes that actually signal "bad browser
        # output": ValueError from the EBML/ISOBMFF walker and OSError
        # from the file write. Anything else (e.g. KMS outage in
        # encrypt_file) is not the client's fault and should propagate as
        # a 500 by the usual Flask error path rather than getting
        # misattributed to a parse failure the user can't do anything
        # about.
        try:
            persist_init_segment(upload_path, chunk_dir)
        except (ValueError, OSError) as exc:
            current_app.logger.error(
                f"Failed to extract init segment from chunk 0 of "
                f"session {session_id}: {exc}"
            )
            try:
                upload_path.unlink()
            except OSError:
                pass
            # 400 Bad Request: the client sent bytes the server can't
            # parse. Not a server failure, so it shouldn't count in the
            # 5xx alert dashboards.
            return jsonify({
                "error": "Could not parse init segment from first chunk",
                "detail": str(exc),
                "code": "init_parse_failed",
            }), 400
        except Exception:
            upload_path.unlink(missing_ok=True)
            raise
        # Only persist the family mime AFTER successful init extraction —
        # if persist_init_segment raised, we don't want a half-committed
        # mime that'd survive a future early-commit refactor.
        draft.streaming_mime_type = form_mime_family
    elif chunk_is_init_bearing(upload_path):
        # A chunk N>0 that carries its own stream header came from a
        # fresh MediaRecorder — the user resumed a recovered recording
        # (#124). Persist its init under an index-suffixed name so
        # transcription can split the batch into subsessions instead of
        # binary-concatenating incompatible streams (which silently
        # drops the pre-resume audio at remux).
        #
        # Unlike chunk 0 we do NOT reject the upload on extraction
        # failure: the chunk's audio is real and already streamed once —
        # rejecting would lose it outright, while keeping it preserves
        # at worst today's behavior for this subsession.
        try:
            persist_init_segment(upload_path, chunk_dir, index=chunk_index)
            current_app.logger.info(
                f"Session {session_id}: chunk {chunk_index} opens a new "
                f"subsession (resumed recording); init persisted"
            )
        except (ValueError, OSError) as exc:
            current_app.logger.error(
                f"Failed to extract subsession init from chunk "
                f"{chunk_index} of session {session_id}: {exc}"
            )
        except Exception:
            upload_path.unlink(missing_ok=True)
            raise

    # Encrypt the audio chunk at rest
    encrypted_path = encrypt_file_atomically(upload_path, chunk_path)

    # Create transcript chunk record (linked to session, not node)
    # Chunks are stored on disk first; transcription is batched every 20 chunks
    # (20 × 15s = 5min) for better Whisper quality.
    existing_chunk = NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
        chunk_index=chunk_index
    ).first()

    if existing_chunk:
        # Update existing chunk if it failed before
        if existing_chunk.status == 'failed':
            existing_chunk.status = 'stored'
            existing_chunk.error = None
            db.session.commit()
            transcript_chunk = existing_chunk
        else:
            return _chunk_already_uploaded(chunk_index, existing_chunk)
    else:
        transcript_chunk = NodeTranscriptChunk(
            session_id=session_id,
            node_id=None,  # No node yet - this is draft-based
            chunk_index=chunk_index,
            status='stored'
        )
        db.session.add(transcript_chunk)
        try:
            db.session.commit()
        except IntegrityError:
            # The same chunk arrived twice at once: a hidden page sends
            # each chunk by sendBeacon and by the normal upload (#88).
            # The copy that lost the race is a duplicate, not a failure
            # (a 500 made the client retry after 2 s, delaying the voice
            # reply when the recording was stopped from the lock screen).
            db.session.rollback()
            existing_chunk = NodeTranscriptChunk.query.filter_by(
                session_id=session_id, chunk_index=chunk_index).first()
            if existing_chunk is None:
                raise
            return _chunk_already_uploaded(chunk_index, existing_chunk)

    # Check if we have enough stored chunks for a batch (20 × 15s = 5min)
    BATCH_SIZE = 20
    stored_chunks = NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
        status='stored'
    ).order_by(NodeTranscriptChunk.chunk_index).all()

    task_id = None
    if len(stored_chunks) >= BATCH_SIZE:
        # Take the first BATCH_SIZE stored chunks and queue transcription
        batch = stored_chunks[:BATCH_SIZE]
        chunk_indices = [c.chunk_index for c in batch]

        from backend.tasks.streaming_transcription import transcribe_chunk_batch

        task = transcribe_chunk_batch.delay(
            session_id=session_id,
            chunk_indices=chunk_indices
        )
        task_id = task.id

        # Mark batch chunks as processing
        for c in batch:
            c.status = 'processing'
            c.task_id = task.id
        db.session.commit()

        current_app.logger.info(
            f"Queued batch transcription for session {session_id}, "
            f"chunks {chunk_indices}, task {task.id}"
        )

    # Count total chunks in DB for this session for debugging
    total_db_chunks = NodeTranscriptChunk.query.filter_by(
        session_id=session_id
    ).count()
    current_app.logger.info(
        f"Received audio chunk {chunk_index} for session {session_id}, "
        f"encrypted_path={encrypted_path}, "
        f"total_db_chunks={total_db_chunks}, "
        f"status=stored"
    )

    response = {
        "chunk_index": chunk_index,
        "status": "stored"
    }
    if task_id:
        response["task_id"] = task_id
        response["batch_queued"] = True
    return jsonify(response), 202


@drafts_bp.route("/streaming/<session_id>/finalize", methods=["POST"])
@login_required
def finalize_streaming(session_id):
    """
    Finalize streaming transcription.

    Called when the user stops recording. Marks the session as finalizing
    and waits for all chunks to complete transcription.

    Request body:
    {
        "total_chunks": 5  // Total number of chunks sent
    }

    Returns: { "message": "Finalization started", "draft_id": 123 }
    """
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    if draft.streaming_status != "recording":
        return jsonify({"error": "Streaming session is not in recording state"}), 400

    data = request.get_json() or {}
    total_chunks = data.get("total_chunks")
    label = data.get("label")  # e.g. "Reflect", "Orient"
    parent_id = data.get("parent_id")  # thread parent for LLM chain
    err = _parent_error(parent_id)
    if err is not None:
        return err
    model = data.get("model")  # LLM model for server-side generation
    if not model and label in ("Reflect", "Orient", "Voice"):
        # Resolve a model when the client didn't send one (e.g. user has no
        # preferred_model). Without this, should_chain is False for Voice and
        # node creation falls back to the frontend POST /voice path, which
        # re-posts the draft's titled content. Walks ancestry from parent_id
        # (if any) → user.preferred_model → DEFAULT.
        parent_node = Node.query.get(parent_id) if parent_id else None
        model = pick_model_for_generation(parent_node, current_user)

    if total_chunks is None:
        return jsonify({"error": "Missing total_chunks"}), 400

    # Update draft with total chunks and status
    draft.streaming_total_chunks = total_chunks
    draft.streaming_status = "finalizing"
    db.session.commit()

    # Queue finalization task
    from backend.tasks.streaming_transcription import finalize_draft_streaming

    task = finalize_draft_streaming.delay(
        session_id=session_id,
        total_chunks=total_chunks,
        label=label,
        user_id=current_user.id,
        parent_id=parent_id,
        model=model,
    )

    # Log chunk status at time of finalize request
    existing_chunks = NodeTranscriptChunk.query.filter_by(session_id=session_id).all()
    chunk_summary = [(c.chunk_index, c.status) for c in existing_chunks]
    current_app.logger.info(
        f"Finalizing streaming session {session_id}, "
        f"total_chunks: {total_chunks}, task: {task.id}, "
        f"existing_chunks_in_db: {chunk_summary}"
    )

    return jsonify({
        "message": "Finalization started",
        "task_id": task.id,
        "draft_id": draft.id,
        "total_chunks": total_chunks
    }), 202


@drafts_bp.route("/streaming/<session_id>/transcribe-remaining", methods=["POST"])
@login_required
def transcribe_remaining(session_id):
    """
    Trigger transcription of all remaining stored (untranscribed) chunks.

    Used for:
    - On-access recovery: user opens a draft that has stored but untranscribed chunks
    - Finalize flow: transcribe whatever remains regardless of batch size

    Returns: { "task_id": "...", "chunk_count": N }
    """
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    # A live session belongs to the tab recording it: its own batching
    # and finalize transcribe these chunks (#320).
    if session_is_live(draft):
        return jsonify({
            "error": "This recording is still in progress in another tab",
            "code": "session_live",
        }), 409

    # Find all stored (untranscribed) chunks
    stored_chunks = NodeTranscriptChunk.query.filter_by(
        session_id=session_id,
        status='stored'
    ).order_by(NodeTranscriptChunk.chunk_index).all()

    if not stored_chunks:
        return jsonify({
            "message": "No stored chunks to transcribe",
            "chunk_count": 0
        }), 200

    chunk_indices = [c.chunk_index for c in stored_chunks]

    from backend.tasks.streaming_transcription import transcribe_chunk_batch

    task = transcribe_chunk_batch.delay(
        session_id=session_id,
        chunk_indices=chunk_indices
    )

    # Mark as processing
    for c in stored_chunks:
        c.status = 'processing'
        c.task_id = task.id
    db.session.commit()

    current_app.logger.info(
        f"Triggered transcribe-remaining for session {session_id}, "
        f"chunks {chunk_indices}, task {task.id}"
    )

    return jsonify({
        "task_id": task.id,
        "chunk_count": len(chunk_indices),
        "chunk_indices": chunk_indices
    }), 202


@drafts_bp.route("/streaming/<session_id>/status", methods=["GET"])
@login_required
def get_streaming_status(session_id):
    """
    Get the current streaming transcription status.

    Returns status of all chunks and overall transcription progress.
    """
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    # Get all chunks
    chunks = NodeTranscriptChunk.query.filter_by(session_id=session_id).order_by(
        NodeTranscriptChunk.chunk_index
    ).all()

    chunk_statuses = [{
        "chunk_index": c.chunk_index,
        "status": c.status,
        "text": c.get_text() if c.status == 'completed' else None,
        "error": c.error if c.status == 'failed' else None
    } for c in chunks]

    completed_count = sum(1 for c in chunks if c.status == 'completed')
    failed_count = sum(1 for c in chunks if c.status == 'failed')
    pending_count = sum(1 for c in chunks if c.status in ('stored', 'processing', 'pending'))

    # Auto-complete interrupted recordings: if all chunks are done
    # (no pending/stored/processing) and the draft is still in 'recording'
    # state, the user refreshed mid-recording. Mark as completed so the
    # frontend recovery polling can finish. Never a live session: "no
    # chunk pending" is also true for a moment after every batch of a
    # recording that is still going, and completing it there ends the
    # recording in the tab that owns it (#320).
    live = session_is_live(draft)
    if (draft.streaming_status == 'recording'
            and not live
            and chunks and pending_count == 0):
        draft.streaming_status = 'completed'
        draft.streaming_completed_chunks = completed_count
        db.session.commit()

    status_data = {
        "session_id": session_id,
        "draft_id": draft.id,
        "streaming_status": draft.streaming_status,
        "streaming_mime_type": draft.streaming_mime_type,
        "total_chunks": draft.streaming_total_chunks,
        "completed_chunks": completed_count,
        "failed_chunks": failed_count,
        "chunks": chunk_statuses,
        "content": draft.get_content(),
        "live": live,
    }
    if draft.llm_node_id:
        status_data["llm_node_id"] = draft.llm_node_id
    # Same field the SSE all_complete event carries: the frontend's polling
    # fallback (iOS drops SSE when backgrounded) needs it too, or it treats
    # a skipped reply as "no server-side chain" and saves the entry again.
    if draft.streaming_warning:
        status_data["warning"] = draft.streaming_warning
    return jsonify(status_data)


@drafts_bp.route("/streaming/<session_id>/release", methods=["POST"])
@login_required
def release_streaming(session_id):
    """
    The recording tab is leaving (pagehide beacon, or its recorder
    unmounted mid-recording): the session stops being live now, so a
    reload offers recovery at once instead of after the liveness window
    (#320). A no-op unless the session is still 'recording'.
    """
    released = release_session(session_id, current_user.id)
    db.session.commit()
    return jsonify({"released": bool(released)}), 200


@drafts_bp.route("/streaming/<session_id>/save-as-node", methods=["POST"])
@login_required
def save_streaming_as_node(session_id):
    """
    Save the streaming draft as a node.

    Creates a new node from the draft content and moves audio files
    from the drafts folder to the nodes folder.

    Request body:
    {
        "content": "optional edited content",  // If not provided, uses draft.content
        "agentic": false,        // parent the entry under a system node
                                 // carrying the textmode prompt: a new
                                 // thread's root (like POST /textmode/start)
                                 // or, under a parent with no agentic prompt
                                 // above it, one attached under the parent
                                 // (like POST /textmode/from-node); a no-op
                                 // inside an agentic thread
        "auto_generate": false,  // create + enqueue an LLM reply after the
                                 // entry (spend cap honored, like /textmode/start)
        "model": "optional model id for auto_generate"
    }

    Returns: The created node data. With the flags above it also carries
    `conversation_id` (the system node) and `llm_node_id` / `task_id` (the
    placeholder), or `spend_capped: true` when the cap skipped the reply.

    The flags exist so a recorded entry behaves like a typed one: Text mode
    is agentic, and before them a recording on the Write page produced a
    bare node with no system prompt and no reply (auto-generate was
    silently ignored), while the same words typed went through
    /textmode/start and got both.
    """
    draft = Draft.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not draft:
        return jsonify({"error": "Streaming session not found"}), 404

    if draft.streaming_status not in ["completed", "finalizing"]:
        return jsonify({"error": "Streaming session is not complete"}), 400

    err = _parent_error(draft.parent_id)
    if err is not None:
        return err

    data = request.get_json() or {}
    content = data.get("content", draft.get_content())
    agentic = bool(data.get("agentic", False))
    auto_generate = bool(data.get("auto_generate", False))
    model_id = data.get("model")

    privacy_level = draft.privacy_level or "private"
    ai_usage = draft.ai_usage or "none"

    if (agentic or auto_generate) and ai_usage == "none":
        # Same contract as /textmode/start: an AI reply / agentic prompt
        # contradicts ai_usage 'none'. The frontend gates on this too.
        return ai_usage_refused_response()
    if auto_generate:
        if not model_id:
            # Walks ancestry from the parent (if any) → user.preferred_model
            # → DEFAULT, same as /textmode/start and /nodes/<id>/llm.
            parent_node = Node.query.get(draft.parent_id) if draft.parent_id else None
            model_id = pick_model_for_generation(parent_node, current_user)
        if model_id not in current_app.config["SUPPORTED_MODELS"]:
            return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    # Agentic new thread: system node with the textmode prompt pinned,
    # mirroring textmode.start_conversation. The user's entry then hangs
    # under it so the thread is an agentic session (tools, profile, todo
    # context) from its first turn.
    user_parent_id = draft.parent_id
    system_node = None
    if agentic and draft.parent_id is not None:
        # A recorded reply inside an existing thread (the reply box under
        # a read reply, #323): the textmode prompt goes under the parent
        # when no agentic prompt sits above it, as POST /textmode/from-node
        # does for a typed reply; inside an agentic thread nothing is added.
        from backend.utils.session_helpers import attach_agentic_prompt_under
        parent_node = Node.query.get(draft.parent_id)
        if parent_node is None:
            return jsonify({"error": "Parent node not found"}), 404
        system_node = attach_agentic_prompt_under(
            parent_node, current_user.id, "textmode", privacy_level, ai_usage)
        if system_node is not None:
            user_parent_id = system_node.id
    elif agentic:
        from backend.utils.prompts import get_user_prompt_record
        from backend.utils.context_artifacts import attach_context_artifacts
        prompt_record = get_user_prompt_record(current_user.id, "textmode")
        system_node = Node(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=None,
            node_type="user",
            privacy_level=privacy_level,
            ai_usage=ai_usage,
        )
        db.session.add(system_node)
        db.session.flush()
        attach_context_artifacts(
            system_node.id, current_user.id, prompt_record=prompt_record,
        )
        user_parent_id = system_node.id

    # Create the node
    node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=user_parent_id,
        node_type="user",
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        transcription_status="completed",
        streaming_transcription=True  # Mark as having chunked audio
    )
    from backend.utils.tokens import approximate_token_count
    node.set_content(content)
    node.token_count = approximate_token_count(content)
    db.session.add(node)
    db.session.flush()
    # Per-node cap: split very long transcripts into a serial chain
    # (audio stays on this head node). An LLM reply chains after the
    # tip so its context walk sees the whole transcript (as the Voice
    # path's _start_server_side_llm_chain does).
    from backend.utils.node_split import split_node_into_chain
    _split_parts = split_node_into_chain(node)
    tip_node = _split_parts[-1] if _split_parts else node
    db.session.commit()

    # Move the session's audio files and transcript chunk rows to the node
    move_session_audio_to_node(
        draft, node, current_app.logger, root=AUDIO_STORAGE_ROOT)

    # Delete the draft
    db.session.delete(draft)
    db.session.commit()

    current_app.logger.info(
        f"Saved streaming session {session_id} as node {node.id}"
    )

    response = {
        "id": node.id,
        "user_node_id": node.id,
        "tip_id": tip_node.id,
        "content": node.get_content(),
        "parent_id": node.parent_id,
        "privacy_level": node.privacy_level,
        "ai_usage": node.ai_usage,
        "created_at": node.created_at.isoformat() + "Z"
    }
    if system_node is not None:
        response["conversation_id"] = system_node.id

    if auto_generate:
        # The entry (and its audio) is already saved above: the user's
        # writing is never blocked by the cap or by a placeholder error,
        # only the reply is skipped — same stance as /textmode/start.
        from backend.utils.spend import user_is_capped
        if user_is_capped(current_user):
            response["spend_capped"] = True
        else:
            from backend.utils.llm_nodes import create_llm_placeholder
            from backend.utils.placeholders import UserExportValidationError
            from backend.utils.node_deletion import ParentDeletedError
            try:
                llm_node, task_id = create_llm_placeholder(
                    tip_node.id, model_id, current_user.id,
                    privacy_level=privacy_level,
                    ai_usage=ai_usage,
                    source_mode="textmode",
                )
                db.session.commit()
                response["llm_node_id"] = llm_node.id
                response["task_id"] = task_id
            except (UserExportValidationError, ParentDeletedError,
                    AIUsageRefused) as e:
                # A thread above that keeps AI out (AIUsageRefused) skips
                # the reply the same way: the entry is saved.
                db.session.rollback()
                current_app.logger.warning(
                    f"save-as-node: LLM reply skipped for node {node.id}: {e}"
                )
                response["llm_error"] = str(e)
                if isinstance(e, AIUsageRefused):
                    response["llm_error_code"] = e.code

    return jsonify(response), 201
