"""Shared factory for creating LLM placeholder nodes."""

from flask import current_app

from backend.models import Node, User
from backend.extensions import db
from backend.utils.placeholders import validate_user_export_placeholders


_MAX_ANCESTRY_HOPS = 1000


def pick_model_for_generation(parent_node, user):
    """Pick the LLM model for an auto-generated response when the caller
    has not supplied an explicit model.

    Priority:
      1. Closest ancestor LLM node's ``llm_model`` (active, non-deprecated).
      2. ``user.preferred_model`` from the Account page (active,
         non-deprecated).
      3. ``DEFAULT_LLM_MODEL`` from the Flask config / env.

    If the closest LLM ancestor is recognized but no longer usable
    (deprecated, or the historical ``gpt-4.5-preview`` legacy id), the
    walk stops there and falls through to ``user.preferred_model``.
    Walking past it to find an even older active model would silently
    override the user's current account preference and diverge from the
    ``/suggested-model`` display endpoint. Truly-unknown ancestors keep
    walking — they're typically placeholder rows from data migrations.

    Cycle-safe walk up to ``_MAX_ANCESTRY_HOPS`` parents.
    """
    supported = current_app.config.get("SUPPORTED_MODELS", {})

    def _is_active(model_id):
        cfg = supported.get(model_id)
        return cfg is not None and not cfg.get("deprecated")

    if parent_node is not None:
        current = parent_node
        visited = set()
        for _ in range(_MAX_ANCESTRY_HOPS):
            if current is None or current.id in visited:
                break
            visited.add(current.id)
            # Skip tombstones: a soft-deleted ancestor's `llm_model` is
            # still set (only `content` gets wiped at +30d) but the
            # node is meant to be invisible. Falling through to the
            # user's preference is the right behavior here.
            if current.deleted_at is None and current.node_type == "llm" and current.llm_model:
                if _is_active(current.llm_model):
                    return current.llm_model
                # Recognized-but-unusable: stop and fall through to the
                # user's account preference.
                if (current.llm_model in supported
                        or current.llm_model == "gpt-4.5-preview"):
                    break
            current = (
                Node.query.get(current.parent_id)
                if current.parent_id else None
            )

    if user is not None:
        pref = getattr(user, "preferred_model", None)
        if pref and _is_active(pref):
            return pref

    return current_app.config.get("DEFAULT_LLM_MODEL", "claude-opus-4.6")


def create_llm_placeholder(parent_node_id, model_id, human_owner_id,
                           privacy_level="private", ai_usage="chat",
                           placeholder_text="[LLM response generation pending...]",
                           enqueue=True, source_mode=None):
    """Create an LLM placeholder node, optionally enqueue generation task.

    Returns (llm_node, task_id) -- task_id is None if enqueue=False.

    Raises UserExportValidationError if the parent node's content
    contains a {user_export} placeholder with unrecognized param keys.
    Validation runs BEFORE any DB writes so a misconfigured placeholder
    never produces an orphan LLM node and never incurs LLM API spend.
    """
    # Spend-cap guard: a blocked user must never get an LLM placeholder node
    # (and never incur generation spend). Raised before any DB write so no
    # orphan node is created; HTTP callers surface it as a 402 → banner via
    # the SpendCapExceeded error handler. This is the single chokepoint for
    # every placeholder-creating path (textmode, converse, voice, replies).
    from backend.utils.spend import SpendCapExceeded, user_is_capped
    if user_is_capped(human_owner_id):
        raise SpendCapExceeded()

    # Race A guard: lock the parent row and reject if soft-deleted. The
    # locking is what closes the create-vs-soft-delete race under READ
    # COMMITTED — a plain SELECT-then-INSERT can't see the concurrent
    # deleted_at UPDATE in time. See backend/utils/node_deletion.py.
    from backend.utils.node_deletion import ParentDeletedError
    parent = Node.query.with_for_update().get(parent_node_id)
    if parent is None:
        raise ParentDeletedError("Parent node not found")
    if parent.deleted_at is not None:
        raise ParentDeletedError("Parent node has been deleted")

    # Pre-flight: validate any {user_export} placeholders in the parent's
    # content. Misconfigured placeholders previously fell back silently
    # to "no token cap" and cost real $$$ on a single request.
    validate_user_export_placeholders(
        parent.get_content(), user_id=human_owner_id,
    )

    llm_user = User.query.filter_by(username=model_id).first()
    if not llm_user:
        llm_user = User(twitter_id=f"llm-{model_id}", username=model_id)
        db.session.add(llm_user)
        db.session.flush()

    from backend.utils.tokens import approximate_token_count

    llm_node = Node(
        user_id=llm_user.id,
        parent_id=parent_node_id,
        human_owner_id=human_owner_id,
        node_type="llm",
        llm_model=model_id,
        llm_task_status="pending",
        privacy_level=privacy_level,
        ai_usage=ai_usage,
        token_count=approximate_token_count(placeholder_text),
    )
    llm_node.set_content(placeholder_text)
    db.session.add(llm_node)
    db.session.commit()

    task_id = None
    if enqueue:
        from backend.tasks.llm_completion import generate_llm_response
        task = generate_llm_response.delay(
            parent_node_id, llm_node.id, model_id, human_owner_id,
            source_mode=source_mode,
        )
        llm_node.llm_task_id = task.id
        db.session.commit()
        task_id = task.id

    return llm_node, task_id
