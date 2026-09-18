"""Community Archive reading (PoC, 2026-09-13): one request that reads a
day of the archive against the user and answers whether anything in it
is worth their time (see backend/utils/ca_feed.py for the reply shape).

Three entry points, all admin-only while the placeholder is:

  POST /api/read/start            a fresh thread rooted on the 'read'
                                  prompt (profile + intentions + archive);
                                  honours auto_generate like /textmode/start
  POST /api/read/from-node/<id>   the 'read_thread' prompt attached under
                                  an existing node, so the archive is read
                                  against the conversation above it
  POST /api/read/<id>/rerun       cancel the reply's pending batch (if
                                  any) and run it again, through the batch
                                  or, with {"live": true}, the live API

The first two honour `auto_generate` (default true) like /textmode/start:
off means only the prompt node is created, and the user picks a model
and asks for the reply on the thread page.

The prompt is never copied into a node. Like Voice / Text mode, the
prompt node's content stays empty and resolves through the linked
UserPrompt (attach_context_artifacts), which also pins the profile and
intentions snapshots the prompt's placeholders name. The LLM placeholder
under it carries the batch round-trip; the caller lands on that node,
which the thread page polls as "Processing…" until the batch returns.

Every node a read creates has AI usage 'chat' (FEED_AI_USAGE): the reply
quotes other people's public tweets, which Loore has no licence to train
on, so the user's 'train' default is lowered here and the node editor
refuses to raise it (routes/nodes.py). 'none' is refused: a read is an
AI call by definition.
"""
import json
import uuid
from datetime import datetime

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
from backend.utils.ca_feed import FEED_AI_USAGE, READ_PROMPT_KEYS

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


def _attach_prompt_node(prompt_key, parent_id, privacy_level):
    prompt_record = get_user_prompt_record(current_user.id, prompt_key)
    prompt_node = Node(
        user_id=current_user.id,
        human_owner_id=current_user.id,
        parent_id=parent_id,
        node_type="user",
        privacy_level=privacy_level,
        ai_usage=FEED_AI_USAGE,
    )
    db.session.add(prompt_node)
    db.session.flush()
    attach_context_artifacts(
        prompt_node.id, current_user.id, prompt_record=prompt_record,
    )
    return prompt_node


def _start(prompt_key, parent, privacy_level, model_id, auto_generate=True):
    """Create the prompt node and its LLM placeholder; commit; respond.
    A refused placeholder (the pre-flight in create_llm_placeholder) rolls
    the prompt node back too: there is nothing to keep without the reply.
    With auto_generate off only the prompt node is created (the user
    picks a model and asks for the reply on the thread page), as
    /textmode/start does."""
    prompt_node = _attach_prompt_node(
        prompt_key, parent.id if parent else None, privacy_level)
    if not auto_generate:
        db.session.commit()
        return jsonify({"prompt_node_id": prompt_node.id}), 202
    try:
        llm_node, task_id = create_llm_placeholder(
            prompt_node.id, model_id, current_user.id,
            privacy_level=privacy_level, ai_usage=FEED_AI_USAGE,
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
    if current_user.default_ai_usage == 'none':
        return jsonify({
            "error": "Reading the archive needs AI usage of 'chat' or 'train'.",
        }), 400
    privacy_level = (
        getattr(current_user, "default_privacy_level", None) or "private"
    )
    data = request.get_json(silent=True) or {}
    auto_generate = bool(data.get("auto_generate", True))
    return _start(ROOT_PROMPT_KEY, None, privacy_level, model_id,
                  auto_generate=auto_generate)


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
    if (node.ai_usage or current_user.default_ai_usage) == 'none':
        return jsonify({
            "error": "AI usage is off for this thread.",
        }), 400
    model_id, err = _resolve_model(node)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    auto_generate = bool(data.get("auto_generate", True))
    return _start(THREAD_PROMPT_KEY, node, node.privacy_level or "private",
                  model_id, auto_generate=auto_generate)


def _batch_entries(node):
    try:
        meta = json.loads(node.tool_calls_meta or "[]") or []
    except (json.JSONDecodeError, TypeError):
        meta = []
    return meta, [m for m in meta if isinstance(m, dict)
                  and m.get("name") == "_batch"]


def _cancel_submitted_batch(entry):
    """Ask the provider to cancel the batch of a submitted entry. Best
    effort: an error (already ended, gone, network) is reported on the
    entry and the rerun proceeds regardless, since the old task is
    revoked and its poll would find no submitted entry anyway."""
    from backend.utils import llm_batch
    from backend.utils.api_keys import get_api_keys_for_usage
    provider = entry.get("provider") or "anthropic"
    # The key the batch was submitted under (reads are chat-only; the
    # fallback covers entries from before the key type was stored).
    keys = llm_batch.apply_batch_key_override(
        get_api_keys_for_usage(current_app.config,
                               entry.get("key_type") or "chat"),
        current_app.config)
    cancel = {
        "anthropic": llm_batch.anthropic_batch_cancel_one,
        "openai": llm_batch.openai_batch_cancel_one,
    }.get(provider)
    if cancel is None:
        entry["cancel_error"] = f"unknown provider {provider}"
        return
    try:
        cancel(keys[provider], entry["batch_id"])
    except Exception as e:  # noqa: BLE001 - reported, not fatal
        current_app.logger.warning(
            "Batch %s cancel failed: %s", entry.get("batch_id"), e)
        entry["cancel_error"] = str(e)


@read_bp.route("/<int:node_id>/rerun", methods=["POST"])
@login_required
def rerun_read(node_id):
    """Cancel the reply's pending batch, if one is submitted, and run the
    reply again on the same node: through the batch again, or with
    {"live": true} through the live API (minutes instead of up to a day;
    the admin's testing loop). Works on a reply that is still processing
    or has failed; a completed reply is left alone (it has picks)."""
    if not ca_tweets_allowed(current_user):
        return jsonify({"error": ca_tweets_denied_message()}), 403
    node = Node.query.get(node_id)
    if node is None or node.deleted_at is not None:
        return jsonify({"error": "Node not found"}), 404
    if (node.human_owner_id or node.user_id) != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    parent = Node.query.get(node.parent_id) if node.parent_id else None
    is_llm = node.node_type == "llm" or node.llm_model is not None
    meta, batches = _batch_entries(node)
    is_read_reply = bool(batches) or (
        parent is not None and parent.get_prompt_key() in READ_PROMPT_KEYS)
    if not is_llm or parent is None or not is_read_reply:
        return jsonify({"error": "Not a read reply."}), 400
    if node.llm_task_status not in ("pending", "processing", "failed"):
        return jsonify({
            "error": "This reply is finished; start a new read instead.",
        }), 409
    if not node.llm_model:
        return jsonify({"error": "The reply has no model."}), 400

    data = request.get_json(silent=True) or {}
    live = bool(data.get("live", False))
    now = datetime.utcnow().isoformat(timespec="seconds")
    cancelled = []
    for entry in batches:
        if entry.get("status") not in ("submitted", "cancelling"):
            continue
        _cancel_submitted_batch(entry)
        entry["status"] = "cancelled"
        entry["cancelled_at"] = now
        cancelled.append(entry.get("batch_id"))

    # The old poll (a Celery retry chain under the old task id) is
    # revoked; the task also bails on its own when the node's task id
    # is no longer its own, in case the revoke never reaches a worker.
    from backend.tasks.llm_completion import generate_llm_response
    if node.llm_task_id:
        try:
            generate_llm_response.app.control.revoke(node.llm_task_id)
        except Exception as e:  # noqa: BLE001 - best effort
            current_app.logger.warning(
                "Revoke of task %s failed: %s", node.llm_task_id, e)
    # The new task id is chosen here and committed BEFORE the dispatch:
    # the task bails when the node's task id is not its own (the guard
    # against a lost revoke), so the id must be on the node before the
    # worker can pick the task up.
    task_id = str(uuid.uuid4())
    node.tool_calls_meta = json.dumps(meta) if meta else None
    node.llm_task_id = task_id
    node.llm_task_status = "processing"
    node.llm_task_progress = 0
    node.llm_task_error = None
    db.session.commit()
    generate_llm_response.apply_async(
        args=(parent.id, node.id, node.llm_model,
              node.human_owner_id or node.user_id),
        kwargs={"source_mode": None, "ca_live": live},
        task_id=task_id,
    )
    return jsonify({
        "llm_node_id": node.id,
        "task_id": task_id,
        "live": live,
        "cancelled_batches": cancelled,
    }), 202
