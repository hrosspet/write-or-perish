"""Community Archive reading (PoC, 2026-09-13): one request that reads a
day of the archive against the user and answers whether anything in it
is worth their time (see backend/utils/ca_feed.py for the reply shape).

Two entry points, both admin-only while the placeholder is:

  POST /api/read/start            a fresh thread rooted on the 'read'
                                  prompt (profile + intentions + archive)
  POST /api/read/from-node/<id>   the 'read_thread' prompt attached under
                                  an existing node, so the archive is read
                                  against the conversation above it

The prompt is never copied into a node. Like Voice / Text mode, the
prompt node's content stays empty and resolves through the linked
UserPrompt (attach_context_artifacts), which also pins the profile and
intentions snapshots the prompt's placeholders name. The LLM placeholder
under it carries the batch round-trip; the caller lands on that node,
which the thread page polls as "Processing…" until the batch returns.
"""
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from backend.models import Node
from backend.extensions import db
from backend.utils.prompts import get_user_prompt_record
from backend.utils.llm_nodes import (
    create_llm_placeholder, pick_model_for_generation,
)
from backend.utils.placeholders import (
    UserExportValidationError, ca_tweets_allowed, ca_tweets_denied_message,
)
from backend.utils.context_artifacts import attach_context_artifacts

read_bp = Blueprint("read", __name__)

ROOT_PROMPT_KEY = 'read'
THREAD_PROMPT_KEY = 'read_thread'


def _resolve_model(anchor_node):
    data = request.get_json(silent=True) or {}
    model_id = data.get("model")
    if not model_id:
        model_id = pick_model_for_generation(anchor_node, current_user)
    if model_id not in current_app.config["SUPPORTED_MODELS"]:
        return None, (jsonify({"error": f"Unsupported model: {model_id}"}), 400)
    return model_id, None


def _attach_prompt_node(prompt_key, parent_id, privacy_level, ai_usage):
    prompt_record = get_user_prompt_record(current_user.id, prompt_key)
    prompt_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=parent_id,
        node_type="user",
        privacy_level=privacy_level,
        ai_usage=ai_usage,
    )
    db.session.add(prompt_node)
    db.session.flush()
    attach_context_artifacts(
        prompt_node.id, current_user.id, prompt_record=prompt_record,
    )
    return prompt_node


def _start(prompt_key, parent, privacy_level, ai_usage, model_id):
    """Create the prompt node and its LLM placeholder; commit; respond.
    A refused placeholder (the pre-flight in create_llm_placeholder) rolls
    the prompt node back too: there is nothing to keep without the reply."""
    prompt_node = _attach_prompt_node(
        prompt_key, parent.id if parent else None, privacy_level, ai_usage)
    try:
        llm_node, task_id = create_llm_placeholder(
            prompt_node.id, model_id, current_user.id,
            privacy_level=privacy_level, ai_usage=ai_usage,
        )
    except UserExportValidationError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    db.session.commit()
    return jsonify({
        "prompt_node_id": prompt_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task_id,
    }), 202


@read_bp.route("/start", methods=["POST"])
@login_required
def start_read():
    """A fresh thread: the 'read' prompt as root, the reply under it."""
    if not ca_tweets_allowed(current_user):
        return jsonify({"error": ca_tweets_denied_message()}), 403
    model_id, err = _resolve_model(None)
    if err:
        return err
    ai_usage = current_user.default_ai_usage
    if ai_usage == 'none':
        return jsonify({
            "error": "Reading the archive needs AI usage of 'chat' or 'train'.",
        }), 400
    privacy_level = (
        getattr(current_user, "default_privacy_level", None) or "private"
    )
    return _start(ROOT_PROMPT_KEY, None, privacy_level, ai_usage, model_id)


@read_bp.route("/from-node/<int:node_id>", methods=["POST"])
@login_required
def start_read_from_node(node_id):
    """The 'read_thread' prompt under *node_id*, the reply under that, so
    the archive is read against the whole conversation above."""
    if not ca_tweets_allowed(current_user):
        return jsonify({"error": ca_tweets_denied_message()}), 403
    node = Node.query.get(node_id)
    if node is None or node.deleted_at is not None:
        return jsonify({"error": "Node not found"}), 404
    if node.human_owner_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    ai_usage = node.ai_usage or current_user.default_ai_usage
    if ai_usage == 'none':
        return jsonify({
            "error": "AI usage is off for this thread.",
        }), 400
    model_id, err = _resolve_model(node)
    if err:
        return err
    return _start(THREAD_PROMPT_KEY, node, node.privacy_level or "private",
                  ai_usage, model_id)
