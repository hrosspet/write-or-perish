"""Community Archive reading (PoC, 2026-09-13), called Glean in the app
since #435: one request that reads a day of the archive against the user
and answers whether anything in it is worth their time (see
backend/utils/ca_feed.py for the reply shape, utils/glean.py for who
gleans).

Three entry points:

  POST /api/read/start            admin experiments: a fresh thread rooted
                                  on the 'read' prompt (profile +
                                  intentions + archive, no reflection),
                                  through the Batch API; honours
                                  auto_generate like /textmode/start
  POST /api/read/from-node/<id>   a glean, for every user who gleans (the
                                  rollout gate and their own switch): the
                                  'read_thread' prompt attached under an
                                  existing node, so the archive is read
                                  against the conversation above it; inside
                                  a thread that already has a read prompt
                                  it reads FURTHER instead: no second
                                  prompt, a read turn under <id> (marked
                                  "_read" in its tool_calls_meta) with the
                                  whole thread so far in view — the earlier
                                  picks, the user's marks and whatever was
                                  written since. Always a live call, and
                                  the reply is always created: the click
                                  is the request.
  POST /api/read/<id>/rerun       admin: cancel the reply's pending batch
                                  (if any) and run it again, through the
                                  batch or, with {"live": true}, the live
                                  API

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

That stamp is a record, not the guard. determine_api_key_type reads user
nodes only, so the placeholder's usage never reaches key selection and
the prompt node's is only one vote among the chain's. What actually
keeps the tweets off the training key is llm_completion forcing
key_type='chat' on every turn of a read thread — the render on a read,
the earlier picks' resolved quotes on a chat turn.
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
    AIUsageRefused, ai_usage_refused_response, create_llm_placeholder,
    is_read_model, reply_refusal, resolve_read_model,
)
from backend.utils.placeholders import (
    UserExportValidationError, ca_tweets_allowed, ca_tweets_denied_message,
)
from backend.utils.context_artifacts import attach_context_artifacts
from backend.utils.ca_feed import (
    FEED_AI_USAGE, READ_PROMPT_KEYS, READ_FURTHER_MARKER, in_read_thread,
)

read_bp = Blueprint("read", __name__)

ROOT_PROMPT_KEY = 'read'
THREAD_PROMPT_KEY = 'read_thread'


def _resolve_model(anchor_node):
    """A read runs only on a read model (#355) of the provider the user's
    chat model is from (#435: a user's provider is never switched): the
    thread's last read's while it is on that provider, else the
    provider's glean model (GLEAN_MODEL_*). Never the chat default: a
    conversation on Opus does not carry into a read. The server chooses:
    only an admin may name a model (any read model, for their own
    evaluations); a model a non-admin's request names is ignored."""
    from backend.utils.glean import GleanModelUnavailable
    data = request.get_json(silent=True) or {}
    # The role of this request's user (Flask-Login loads it per request);
    # anything but an explicit admin gets the server's choice.
    model_id = data.get("model") if getattr(
        current_user, "is_admin", False) is True else None
    if not model_id:
        try:
            return resolve_read_model(anchor_node, user=current_user)[0], None
        except GleanModelUnavailable as e:
            return None, (jsonify({"error": str(e)}), 400)
    if not is_read_model(model_id):
        names = [cfg.get("display_name", key)
                 for key, cfg in current_app.config["SUPPORTED_MODELS"].items()
                 if is_read_model(key)]
        return None, (jsonify({
            "error": f"Reads run on {', '.join(names)}; not on {model_id}.",
        }), 400)
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


def _start(prompt_key, parent, privacy_level, model_id, auto_generate=True,
           read_live=True):
    """Create the prompt node and its LLM placeholder; commit; respond.
    A refused placeholder (the pre-flight in create_llm_placeholder) rolls
    the prompt node back too: there is nothing to keep without the reply.
    With auto_generate off only the prompt node is created (the user
    picks a model and asks for the reply on the thread page), as
    /textmode/start does. *read_live* False sends the read through the
    Batch API (the admin's /read/start); a glean is always live."""
    from backend.utils.glean import GleanModelUnavailable
    prompt_node = _attach_prompt_node(
        prompt_key, parent.id if parent else None, privacy_level)
    if not auto_generate:
        db.session.commit()
        return jsonify({"prompt_node_id": prompt_node.id}), 202
    try:
        llm_node, task_id = create_llm_placeholder(
            prompt_node.id, model_id, current_user.id,
            privacy_level=privacy_level, ai_usage=FEED_AI_USAGE,
            read_live=read_live,
        )
    except (UserExportValidationError, GleanModelUnavailable) as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except AIUsageRefused as e:
        db.session.rollback()
        return ai_usage_refused_response(e)
    db.session.commit()
    return jsonify({
        "prompt_node_id": prompt_node.id,
        "llm_node_id": llm_node.id,
        "task_id": task_id,
    }), 202


@read_bp.route("/start", methods=["POST"])
@login_required
def start_read():
    """A fresh thread: the 'read' prompt as root, the reply under it.
    Admin experiments only (#435): a glean always answers a reflection."""
    if not getattr(current_user, "is_admin", False):
        return jsonify({"error": ca_tweets_denied_message()}), 403
    model_id, err = _resolve_model(None)
    if err:
        return err
    if current_user.default_ai_usage == 'none':
        return ai_usage_refused_response(AIUsageRefused(scope="account"))
    privacy_level = (
        getattr(current_user, "default_privacy_level", None) or "private"
    )
    data = request.get_json(silent=True) or {}
    auto_generate = bool(data.get("auto_generate", True))
    return _start(ROOT_PROMPT_KEY, None, privacy_level, model_id,
                  auto_generate=auto_generate, read_live=False)


@read_bp.route("/from-node/<int:node_id>", methods=["POST"])
@login_required
def start_read_from_node(node_id):
    """A glean (#435): the 'read_thread' prompt under *node_id*, the reply
    under that, so the archive is read against the whole conversation
    above. For users who glean (the rollout gate and their own switch).
    The click is the request: the reply is always created, as a live
    call (create_llm_placeholder marks it)."""
    from backend.utils.glean import glean_enabled
    if not ca_tweets_allowed(current_user):
        return jsonify({"error": ca_tweets_denied_message()}), 403
    if not glean_enabled(current_user):
        return jsonify({
            "error": "Glean is off for your account. You can turn it on "
                     "in Account settings.",
        }), 403
    node = Node.query.get(node_id)
    if node is None or node.deleted_at is not None:
        return jsonify({"error": "Node not found"}), 404
    if node.human_owner_id != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    # The read sends the whole thread above the node: refused before the
    # prompt is attached when any of it keeps AI out.
    refused = reply_refusal(node, current_user.id)
    if refused is not None:
        return ai_usage_refused_response(refused)
    model_id, err = _resolve_model(node)
    if err:
        return err
    privacy_level = node.privacy_level or "private"
    if in_read_thread(node):
        return _glean_again(node, model_id, privacy_level)
    return _start(THREAD_PROMPT_KEY, node, privacy_level, model_id)


def _glean_again(node, model_id, privacy_level):
    """Read further: the thread already has its read prompt — a read turn
    under *node*, the day rendered again against everything above. The
    marker is what tells the task this is a read and not a chat about
    the picks (_ca_turn); it is written in the commit that creates the
    node."""
    from backend.utils.glean import GleanModelUnavailable
    try:
        llm_node, task_id = create_llm_placeholder(
            node.id, model_id, current_user.id,
            privacy_level=privacy_level, ai_usage=FEED_AI_USAGE,
            meta=[{"name": READ_FURTHER_MARKER}],
        )
    except (UserExportValidationError, GleanModelUnavailable) as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    except AIUsageRefused as e:
        db.session.rollback()
        return ai_usage_refused_response(e)
    db.session.commit()
    return jsonify({"llm_node_id": llm_node.id, "task_id": task_id}), 202


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
    or has failed; a completed reply is left alone (it has picks).
    Admin-only (#435): a user retries a glean with Glean again. A batch
    rerun of a glean drops its live marker, so it really is a batch."""
    from backend.utils.glean import READ_LIVE_MARKER
    if not getattr(current_user, "is_admin", False):
        return jsonify({"error": "Only an admin can rerun a glean."}), 403
    node = Node.query.get(node_id)
    if node is None or node.deleted_at is not None:
        return jsonify({"error": "Node not found"}), 404
    if (node.human_owner_id or node.user_id) != current_user.id:
        return jsonify({"error": "Unauthorized"}), 403
    parent = Node.query.get(node.parent_id) if node.parent_id else None
    is_llm = node.node_type == "llm" or node.llm_model is not None
    meta, batches = _batch_entries(node)
    marked = any(isinstance(m, dict) and m.get("name") in (
        READ_FURTHER_MARKER, READ_LIVE_MARKER) for m in meta)
    is_read_reply = bool(batches) or marked or (
        parent is not None and parent.get_prompt_key() in READ_PROMPT_KEYS)
    if not is_llm or parent is None or not is_read_reply:
        return jsonify({"error": "Not a read reply."}), 400
    if node.llm_task_status not in ("pending", "processing", "failed"):
        return jsonify({
            "error": "This reply is finished; start a new read instead.",
        }), 409
    if not node.llm_model:
        return jsonify({"error": "The reply has no model."}), 400
    # A run sends the thread again: not once it keeps AI out.
    refused = reply_refusal(parent, node.human_owner_id or node.user_id,
                            node.ai_usage)
    if refused is not None:
        return ai_usage_refused_response(refused)

    data = request.get_json(silent=True) or {}
    live = bool(data.get("live", False))
    if not live:
        meta = [m for m in meta if not (
            isinstance(m, dict) and m.get("name") == READ_LIVE_MARKER)]
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
