import time

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from backend.utils.prompts import get_user_prompt_record
from backend.utils.llm_nodes import (
    ai_usage_refused_response, create_llm_placeholder,
    pick_model_for_generation, reply_ai_usage, voice_turn_refusal,
)
from backend.utils.placeholders import UserExportValidationError
from backend.utils.audio_storage import is_storage_id
from backend.utils.context_artifacts import attach_context_artifacts
from backend.utils import voice_timing
from backend.utils.session_helpers import (
    ancestors_have_prompt, is_llm_node, create_llm_placeholder_node,
    AGENTIC_PROMPT_KEYS,
)

voice_bp = Blueprint("voice", __name__)

PROMPT_KEY = 'voice'
# AGENTIC_PROMPT_KEYS (voice + textmode share agentic.txt): any of them
# counts as "an agentic prompt is already attached" when walking ancestry,
# so bridging a text thread into voice mode (or vice-versa) doesn't append
# a second prompt node.


@voice_bp.route("/from-node/<int:node_id>", methods=["POST"])
@login_required
def create_voice_from_node(node_id):
    """Start or resume a voice session from an existing node's thread.
    A thread that keeps AI out gets 403 ``code: ai_usage_none`` and
    nothing is created: the clients then open the Voice screen on it,
    which explains instead of recording."""
    node = Node.query.get(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    if node.human_owner_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    refused = voice_turn_refusal(current_user, node)
    if refused is not None:
        return ai_usage_refused_response(refused)

    data = request.get_json() or {}
    model_id = data.get("model")
    if not model_id:
        # Walks ancestry from `node` → user.preferred_model → DEFAULT.
        model_id = pick_model_for_generation(node, current_user)
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    # Inherit ai_usage from the target node, looking through a read (#362)
    ai_usage = reply_ai_usage(node, current_user)

    has_prompt = ancestors_have_prompt(node, current_user.id, AGENTIC_PROMPT_KEYS)
    is_llm = is_llm_node(node)

    if has_prompt and not is_llm:
        llm_node = create_llm_placeholder_node(
            node.id, model_id, current_user.id,
            ai_usage=ai_usage,
            source_mode='voice',
        )
        return jsonify({
            "mode": "processing",
            "llm_node_id": llm_node.id,
            "parent_id": llm_node.id,
            "fresh": True,
        }), 202

    if has_prompt and is_llm:
        return jsonify({
            "mode": "processing",
            "llm_node_id": node.id,
            "parent_id": node.id,
        }), 200

    if not has_prompt and not is_llm:
        prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
        system_node = Node(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=node.id,
            node_type="user",
            privacy_level="private",
            ai_usage=ai_usage,
        )
        db.session.add(system_node)
        db.session.flush()
        attach_context_artifacts(
            system_node.id, current_user.id, prompt_record=prompt_record,
        )

        llm_node = create_llm_placeholder_node(
            system_node.id, model_id, current_user.id,
            ai_usage=ai_usage,
            source_mode='voice',
        )
        return jsonify({
            "mode": "processing",
            "llm_node_id": llm_node.id,
            "parent_id": llm_node.id,
            "fresh": True,
        }), 202

    # not has_prompt and is_llm
    prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
    system_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=node.id,
        node_type="user",
        privacy_level="private",
        ai_usage=ai_usage,
    )
    db.session.add(system_node)
    db.session.flush()
    attach_context_artifacts(
        system_node.id, current_user.id, prompt_record=prompt_record,
    )
    db.session.commit()

    return jsonify({
        "mode": "processing",
        "llm_node_id": node.id,
        "parent_id": system_node.id,
    }), 200


@voice_bp.route("/", methods=["POST"])
@login_required
def create_voice_session():
    """
    Start or continue a voice session.
    Body: { content: string, model?: string, parent_id?: int,
            session_id?: string }
    Without parent_id: creates system node (prompt) -> user node -> LLM node
    With parent_id: continues thread
    """
    data = request.get_json() or {}
    content = data.get("content")
    model_id = data.get("model")
    parent_id = data.get("parent_id")
    session_id = data.get("session_id")
    if not content or not content.strip():
        return jsonify({"error": "Content is required"}), 400
    if session_id and not is_storage_id(session_id):
        return jsonify({"error": "Invalid session_id"}), 400

    parent_node = None
    if parent_id:
        parent_node = Node.query.get(parent_id)
        if not parent_node:
            return jsonify({"error": "Parent node not found"}), 404
        if parent_node.human_owner_id != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403
        # Inherit ai_usage from the thread, looking through a read (#362)
        ai_usage = reply_ai_usage(parent_node, current_user)
        user_parent_id = parent_id

    if not model_id:
        # Walks ancestry from parent_node (or skips ancestry for fresh
        # sessions) → user.preferred_model → DEFAULT.
        model_id = pick_model_for_generation(parent_node, current_user)

    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return jsonify({"error": f"Unsupported model: {model_id}"}), 400

    if not parent_id:
        ai_usage = data.get("ai_usage") or current_user.default_ai_usage

    # Every turn here gets a reply: refused before anything is written
    # (or any audio moved) where AI may not read.
    refused = voice_turn_refusal(
        current_user, parent_node, None if parent_node else ai_usage)
    if refused is not None:
        return ai_usage_refused_response(refused)

    if not parent_id:
        prompt_record = get_user_prompt_record(current_user.id, PROMPT_KEY)
        system_node = Node(
            user_id=current_user.id,
            human_owner_id=current_user.id,
            parent_id=None,
            node_type="user",
            privacy_level="private",
            ai_usage=ai_usage,
        )
        db.session.add(system_node)
        db.session.flush()
        attach_context_artifacts(
            system_node.id, current_user.id, prompt_record=prompt_record,
        )
        user_parent_id = system_node.id

    from backend.utils.tokens import approximate_token_count
    user_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=user_parent_id,
        node_type="user",
        privacy_level="private",
        ai_usage=ai_usage,
        token_count=approximate_token_count(content),
    )
    user_node.set_content(content)
    db.session.add(user_node)
    db.session.flush()

    if session_id:
        # Moves the user's own draft session only (see the helper).
        from backend.utils.audio_storage import (
            attach_streaming_audio_to_node,
        )
        attach_streaming_audio_to_node(
            session_id, user_node, current_user.id
        )

    try:
        llm_node, task_id = create_llm_placeholder(
            user_node.id, model_id, current_user.id,
            ai_usage=ai_usage,
            source_mode='voice',
        )
    except UserExportValidationError as e:
        return jsonify({"error": str(e)}), 400

    current_app.logger.info(
        f"Voice session: parent={user_parent_id}, "
        f"user={user_node.id}, llm={llm_node.id}, task={task_id}"
    )

    return jsonify({
        "parent_id": user_parent_id,
        "user_node_id": user_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task_id,
    }), 202


@voice_bp.route("/availability", methods=["GET"])
@login_required
def voice_availability():
    """Whether the Voice screen may record: ``{"allowed": true}``, or
    ``{"allowed": false, "code": "ai_usage_none", "scope", "error"}``,
    which the screen shows in place of the record button. The rule is
    the one streaming init and finalize apply (voice_turn_refusal).
    ``?parent=<node id>`` names the thread Voice continues; without it,
    the account's Default AI usage decides. A node the user cannot see
    answers 404, like one that does not exist."""
    from backend.utils.privacy import can_user_see_node_or_tombstone
    parent = None
    raw = request.args.get("parent")
    if raw not in (None, ""):
        try:
            parent = Node.query.get(int(raw))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid parent"}), 400
        if parent is None or not can_user_see_node_or_tombstone(
                parent, current_user.id):
            return jsonify({"error": "Node not found"}), 404
    refused = voice_turn_refusal(current_user, parent)
    if refused is None:
        return jsonify({"allowed": True})
    return jsonify({"allowed": False, "code": refused.code,
                    "scope": refused.scope, "error": refused.message})


# ── Where a voice turn's wait goes (#371 step 0) ─────────────────────────

@voice_bp.route("/timing/clock", methods=["GET"])
@login_required
def voice_timing_clock():
    """The server's wall clock (epoch seconds): the browser measures its
    offset from it before it reports its marks."""
    return jsonify({"t": time.time()})


@voice_bp.route("/timing", methods=["POST"])
@login_required
def report_voice_timing():
    """The browser's marks for a voice turn, keyed by the turn's first
    reply node: ``{"node_id", "marks": {stage: ms in the browser's
    clock}, "offset_ms", "rtt_ms"}`` (offset_ms: server clock minus the
    browser's). Returns the turn's record, the backend's marks included."""
    data = request.get_json(silent=True) or {}
    node_id = data.get("node_id")
    node = Node.query.get(node_id) if isinstance(node_id, int) else None
    if node is None or node.human_owner_id != current_user.id:
        return jsonify({"error": "Node not found"}), 404
    try:
        offset = float(data.get("offset_ms") or 0) / 1000
        rtt = float(data.get("rtt_ms") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Bad clock values"}), 400
    marks = data.get("marks")
    if not isinstance(marks, dict):
        return jsonify({"error": "Bad marks"}), 400
    for stage, t_ms in marks.items():
        if (stage in voice_timing.BROWSER_MARKS
                and isinstance(t_ms, (int, float))):
            voice_timing.mark(node.id, stage, t=t_ms / 1000 + offset)
    voice_timing.note(node.id, clock_offset_ms=round(offset * 1000),
                      clock_rtt_ms=round(rtt))
    voice_timing.remember_turn(current_user.id, node.id)
    rec = voice_timing.record(node.id)
    voice_timing.log_summary(rec)
    return jsonify(rec)


@voice_bp.route("/timing", methods=["GET"])
@login_required
def list_voice_timing():
    """The current user's recent voice turns (newest first) with the
    median of each stage. ``?limit=N`` (default 20)."""
    limit = request.args.get("limit", 20, type=int)
    limit = max(1, min(limit, voice_timing.RECENT_TURNS))
    records = [voice_timing.record(node_id) for node_id
               in voice_timing.recent_turns(current_user.id, limit)]
    return jsonify({
        "stages": {name: f"{start} -> {end}"
                   for name, start, end in voice_timing.STAGES},
        "median": voice_timing.medians(records),
        "turns": records,
    })
