"""
Celery tasks for generating recent context summaries.

Recent context sits between the long-term user profile (~monthly) and raw data,
providing ~500-800 token summaries regenerated every ~10k new tokens of user writing.
"""
from datetime import datetime, timedelta
from celery.utils.log import get_task_logger
from sqlalchemy import func

from backend.celery_app import celery, flask_app
from backend.models import User, UserProfile, UserRecentContext, Node, APICostLog
from backend.extensions import db
from backend.llm_providers import LLMProvider, PromptTooLongError
from backend.utils.tokens import reduce_export_tokens
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars

logger = get_task_logger(__name__)

RECENT_CONTEXT_TOKEN_THRESHOLD = 10000
PROFILE_UPDATE_TOKEN_THRESHOLD = 100000
MIN_INACTIVITY = timedelta(minutes=5)
MIN_GENERATION_INTERVAL = timedelta(minutes=5)


def _get_latest_chat_profile(user_id):
    """Return the latest profile with ai_usage in ('chat', 'train'), or None."""
    return (
        UserProfile.query
        .filter_by(user_id=user_id)
        .filter(UserProfile.ai_usage.in_(["chat", "train"]))
        .order_by(UserProfile.created_at.desc())
        .first()
    )


def _get_latest_recent_context(user_id, profile_id=None):
    """Return the latest UserRecentContext for this user+profile combo."""
    q = UserRecentContext.query.filter_by(user_id=user_id)
    if profile_id is not None:
        q = q.filter_by(profile_id=profile_id)
    else:
        q = q.filter(UserRecentContext.profile_id.is_(None))
    return q.order_by(UserRecentContext.created_at.desc()).first()


def _count_new_tokens(user_id, since):
    """Count tokens of user nodes created at/after *since*."""
    return db.session.query(
        func.coalesce(func.sum(Node.token_count), 0)
    ).filter(
        Node.user_id == user_id,
        Node.created_at >= since,
        Node.ai_usage.in_(['chat', 'train']),
        Node.token_count.isnot(None)
    ).scalar()


def _count_total_eligible_tokens(user_id):
    """Count all eligible tokens for a user (no cutoff)."""
    return db.session.query(
        func.coalesce(func.sum(Node.token_count), 0)
    ).filter(
        Node.user_id == user_id,
        Node.ai_usage.in_(['chat', 'train']),
        Node.token_count.isnot(None)
    ).scalar()


def _load_prompt(name, user_id=None):
    """Load a prompt template, checking user overrides first."""
    import os
    if user_id:
        from backend.utils.prompts import get_user_prompt
        prompt_key = name.rsplit('.', 1)[0] if '.' in name else name
        content = get_user_prompt(user_id, prompt_key)
        if content:
            return content
    path = os.path.join(flask_app.root_path, "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _should_generate_recent_context(user):
    """Check if recent context generation should be triggered for this user.

    Returns (should_generate, profile, cutoff_for_data) or (False, None, None).
    """
    user_id = user.id

    # Only for Voice-Mode plans
    if (user.plan or "free") not in User.VOICE_MODE_PLANS:
        return False, None, None

    # User must be inactive for 5+ min
    last_node = (
        Node.query.filter_by(user_id=user_id)
        .order_by(Node.created_at.desc())
        .first()
    )
    if last_node and (datetime.utcnow() - last_node.created_at) < MIN_INACTIVITY:
        return False, None, None

    # Find current profile (if any)
    profile = _get_latest_chat_profile(user_id)
    profile_id = profile.id if profile else None
    profile_cutoff = profile.source_data_cutoff if profile else None

    # Check: profile update NOT imminent
    # Total new tokens since profile cutoff must be < 100k
    if profile_cutoff:
        total_since_profile = _count_new_tokens(user_id, profile_cutoff)
    else:
        total_since_profile = _count_total_eligible_tokens(user_id)

    if total_since_profile >= PROFILE_UPDATE_TOKEN_THRESHOLD:
        logger.debug(
            f"User {user_id}: skipping recent context — profile update "
            f"imminent ({total_since_profile} tokens)"
        )
        return False, None, None

    # Find latest recent context for this profile
    latest_rc = _get_latest_recent_context(user_id, profile_id)

    # No recent context created in last 5 min (concurrency guard)
    if latest_rc and (datetime.utcnow() - latest_rc.created_at) < MIN_GENERATION_INTERVAL:
        return False, None, None

    # Determine cutoff: data since the profile's cutoff (or all data if no profile)
    data_cutoff = profile_cutoff  # may be None

    # Count new tokens since the latest recent context's cutoff
    # (or since profile cutoff if no recent context yet)
    if latest_rc and latest_rc.source_data_cutoff:
        tokens_since_last_rc = _count_new_tokens(
            user_id, latest_rc.source_data_cutoff
        )
    elif data_cutoff:
        tokens_since_last_rc = _count_new_tokens(user_id, data_cutoff)
    else:
        tokens_since_last_rc = _count_total_eligible_tokens(user_id)

    if tokens_since_last_rc < RECENT_CONTEXT_TOKEN_THRESHOLD:
        return False, None, None

    return True, profile, data_cutoff


@celery.task
def check_pending_recent_context_updates():
    """Periodic task: check all eligible users for pending recent context updates."""
    with flask_app.app_context():
        users = User.query.filter(
            User.plan.in_(list(User.VOICE_MODE_PLANS))
        ).all()
        for user in users:
            try:
                should, profile, data_cutoff = _should_generate_recent_context(user)
                if should:
                    generate_recent_context.delay(
                        user.id,
                        profile_id=profile.id if profile else None,
                        data_cutoff_iso=data_cutoff.isoformat() if data_cutoff else None,
                    )
            except Exception as e:
                logger.warning(
                    f"Recent context check failed for user {user.id}: {e}"
                )


@celery.task
def generate_recent_context(user_id, profile_id=None, data_cutoff_iso=None):
    """Generate a recent context summary for a user.

    Each generation includes ALL data since the last profile update (not just
    the delta since the last recent context). This means the summary gets
    progressively more comprehensive.
    """
    with flask_app.app_context():
        user = User.query.get(user_id)
        if not user:
            logger.warning(f"User {user_id} not found")
            return

        data_cutoff = (
            datetime.fromisoformat(data_cutoff_iso)
            if data_cutoff_iso else None
        )

        # Re-check concurrency: no recent context created in last 5 min
        profile = UserProfile.query.get(profile_id) if profile_id else None
        pid = profile.id if profile else None
        latest_rc = _get_latest_recent_context(user_id, pid)
        if latest_rc and (datetime.utcnow() - latest_rc.created_at) < MIN_GENERATION_INTERVAL:
            logger.info(
                f"User {user_id}: recent context was just generated, skipping"
            )
            return

        # Determine the model to use (same as profile generation)
        model_id = user.preferred_model or "claude-opus-4-6"
        if model_id not in flask_app.config.get("SUPPORTED_MODELS", {}):
            model_id = "claude-opus-4-6"

        # Build data: ALL nodes since profile cutoff
        from backend.routes.export_data import (
            build_user_export_content as _build_export
        )
        export_result = _build_export(
            user,
            max_tokens=None,
            filter_ai_usage=True,
            created_after=data_cutoff,
            chronological_order=True,
            return_metadata=True,
        )
        if not export_result:
            logger.info(f"User {user_id}: no data for recent context")
            return

        recent_data = export_result["content"]
        source_tokens = export_result["token_count"]
        latest_ts = export_result["latest_node_created_at"]

        # Load prompt template
        prompt_template = _load_prompt("recent_context.txt", user_id=user_id)

        # Inject profile content (if available)
        profile_content = ""
        if profile:
            profile_content = profile.get_content()
        prompt_text = prompt_template.replace("{user_profile}", profile_content)
        prompt_text = prompt_text.replace("{recent_data}", recent_data)

        # Build messages
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt_text}]
            }
        ]

        api_keys = get_api_keys_for_usage(flask_app.config, 'chat')

        MAX_RETRIES = 2
        max_data_tokens = None
        for attempt in range(MAX_RETRIES + 1):
            if max_data_tokens is not None:
                # Retry with truncated data
                export_result = _build_export(
                    user,
                    max_tokens=max_data_tokens,
                    filter_ai_usage=True,
                    created_after=data_cutoff,
                    chronological_order=True,
                    return_metadata=True,
                )
                if not export_result:
                    logger.warning(f"User {user_id}: no data after truncation")
                    return
                recent_data = export_result["content"]
                source_tokens = export_result["token_count"]
                latest_ts = export_result["latest_node_created_at"]
                prompt_text = prompt_template.replace(
                    "{user_profile}", profile_content
                )
                prompt_text = prompt_text.replace("{recent_data}", recent_data)
                messages = [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt_text}]
                    }
                ]

            try:
                response = LLMProvider.get_completion(
                    model_id, messages, api_keys
                )
                break
            except PromptTooLongError as e:
                if attempt == MAX_RETRIES:
                    raise
                max_data_tokens = reduce_export_tokens(
                    max_data_tokens, e.actual_tokens, e.max_tokens,
                    export_content=recent_data,
                )
                logger.warning(
                    f"Prompt too long for recent context "
                    f"({e.actual_tokens} > {e.max_tokens}), "
                    f"retrying with max_data_tokens={max_data_tokens}"
                )

        summary_text = response["content"]
        input_tokens = response.get("input_tokens", 0)
        output_tokens = response.get("output_tokens", 0)
        total_tokens = response["total_tokens"]

        # Log API cost
        cost = calculate_llm_cost_microdollars(
            model_id, input_tokens, output_tokens
        )
        cost_log = APICostLog(
            user_id=user_id,
            model_id=model_id,
            request_type="recent_context",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microdollars=cost,
        )
        db.session.add(cost_log)

        # Save the recent context
        rc = UserRecentContext(
            user_id=user_id,
            generated_by=model_id,
            tokens_used=total_tokens,
            source_data_cutoff=latest_ts,
            source_tokens_covered=source_tokens,
            profile_id=pid,
            ai_usage="chat",
        )
        rc.set_content(summary_text)
        db.session.add(rc)
        db.session.commit()

        logger.info(
            f"Generated recent context for user {user_id}: "
            f"{len(summary_text)} chars, {total_tokens} tokens used, "
            f"covers {source_tokens} source tokens"
        )
