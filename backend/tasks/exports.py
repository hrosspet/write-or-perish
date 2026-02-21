"""
Celery tasks for asynchronous data export operations.
"""
from celery import Task
from celery.utils.log import get_task_logger
import os

from backend.celery_app import celery, flask_app
from backend.models import User, UserProfile, APICostLog
from backend.extensions import db
from backend.llm_providers import LLMProvider, PromptTooLongError
from backend.utils.tokens import approximate_token_count, reduce_export_tokens
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars

logger = get_task_logger(__name__)


# Import from export_data module
def build_user_export_content(user, max_tokens=None, filter_ai_usage=False,
                              **kwargs):
    """Import the actual implementation from export_data routes."""
    from backend.routes.export_data import build_user_export_content as _build
    return _build(user, max_tokens, filter_ai_usage, **kwargs)


class ProfileGenerationTask(Task):
    """Custom task class with error handling."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        user_id = args[0] if args else None
        if user_id:
            logger.error(f"Profile generation failed for user {user_id}: {exc}")


@celery.task(base=ProfileGenerationTask, bind=True)
def generate_user_profile(self, user_id: int, model_id: str):
    """
    Asynchronously generate a user profile using LLM analysis.

    Args:
        user_id: Database ID of the user
        model_id: Model identifier (e.g., "gpt-5", "claude-sonnet-4.5")
    """
    logger.info(f"Starting profile generation task for user {user_id} with model {model_id}")

    with flask_app.app_context():
        # Get user from database
        user = User.query.get(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        try:
            # Validate model is supported
            if model_id not in flask_app.config["SUPPORTED_MODELS"]:
                raise ValueError(f"Unsupported model: {model_id}")

            # Step 1: Load prompt template and calculate token budget
            self.update_state(state='PROGRESS', meta={'progress': 10, 'status': 'Gathering writing samples'})

            prompt_template = _load_prompt(
                "profile_generation.txt", user_id=user_id
            )

            prompt_tokens = approximate_token_count(prompt_template)
            max_export_tokens = None  # Send entire archive; let retry loop converge

            api_keys = get_api_keys_for_usage(flask_app.config, 'chat')

            MAX_RETRIES = 3
            for attempt in range(MAX_RETRIES + 1):
                # Filter by AI usage to only include nodes where ai_usage is 'chat' or 'train'
                user_export = build_user_export_content(user, max_tokens=max_export_tokens, filter_ai_usage=True)

                if not user_export:
                    raise ValueError("No writing found to analyze")

                export_tokens = approximate_token_count(user_export)
                logger.info(f"User export built for user {user_id}, length: {len(user_export)} characters, ~{export_tokens} tokens (attempt {attempt + 1})")

                # Step 2: Build final prompt (45% progress)
                self.update_state(state='PROGRESS', meta={'progress': 45, 'status': 'Preparing prompt'})

                # Replace placeholder with user export
                final_prompt = prompt_template.replace("{user_export}", user_export)

                # Step 3: Build messages (50% progress)
                self.update_state(state='PROGRESS', meta={'progress': 50, 'status': 'Building context'})

                messages = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": final_prompt
                            }
                        ]
                    }
                ]

                # Step 4: Call LLM API (60% -> 90% progress)
                self.update_state(state='PROGRESS', meta={'progress': 60, 'status': 'Generating profile'})

                try:
                    response = LLMProvider.get_completion(model_id, messages, api_keys)
                    break  # Success
                except PromptTooLongError as e:
                    if attempt == MAX_RETRIES:
                        raise
                    max_export_tokens = reduce_export_tokens(
                        max_export_tokens, e.actual_tokens, e.max_tokens,
                        export_content=user_export
                    )
                    logger.warning(
                        f"Prompt too long ({e.actual_tokens} > {e.max_tokens}), "
                        f"retrying with max_export_tokens={max_export_tokens} "
                        f"(attempt {attempt + 2}/{MAX_RETRIES + 1})"
                    )

            profile_text = response["content"]
            total_tokens = response["total_tokens"]
            input_tokens = response.get("input_tokens", 0)
            output_tokens = response.get("output_tokens", 0)

            logger.info(f"Profile generated for user {user_id}: {len(profile_text)} characters, {total_tokens} tokens")

            # Log API cost
            cost = calculate_llm_cost_microdollars(model_id, input_tokens, output_tokens)
            cost_log = APICostLog(
                user_id=user.id,
                model_id=model_id,
                request_type="profile",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microdollars=cost,
            )
            db.session.add(cost_log)

            # Step 5: Save to database (95% progress)
            self.update_state(state='PROGRESS', meta={'progress': 95, 'status': 'Saving profile'})

            # Default privacy for AI-generated profiles: private + chat
            from backend.utils.privacy import PrivacyLevel, AIUsage
            new_profile = UserProfile(
                user_id=user.id,
                generated_by=model_id,
                tokens_used=total_tokens,
                privacy_level=PrivacyLevel.PRIVATE,
                ai_usage=AIUsage.CHAT
            )
            new_profile.set_content(profile_text)
            db.session.add(new_profile)
            db.session.commit()

            logger.info(f"Profile generation successful for user {user_id}, profile ID: {new_profile.id}")

            return {
                'user_id': user_id,
                'profile_id': new_profile.id,
                'status': 'completed',
                'total_tokens': total_tokens,
                'profile_length': len(profile_text)
            }

        except Exception as e:
            logger.error(f"Profile generation error for user {user_id}: {e}", exc_info=True)
            raise


def _load_prompt(name, user_id=None):
    """Load a prompt template by name, checking user overrides first."""
    if user_id:
        from backend.utils.prompts import get_user_prompt
        # Derive prompt_key from filename (e.g. "profile_generation.txt" -> "profile_generation")
        prompt_key = name.rsplit('.', 1)[0] if '.' in name else name
        content = get_user_prompt(user_id, prompt_key)
        if content:
            return content
    path = os.path.join(flask_app.root_path, "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _call_llm_with_retries(self, model_id, prompt_text, user_id,
                            api_keys, progress_base=50,
                            status_label='Generating profile'):
    """Call LLM with retry logic for prompt-too-long errors.

    Returns (response_dict, profile_text, input_tokens, output_tokens).
    """
    max_export_tokens = None
    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES + 1):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt_text}
        ]}]

        self.update_state(state='PROGRESS', meta={
            'progress': progress_base + 10,
            'status': status_label
        })

        try:
            response = LLMProvider.get_completion(model_id, messages,
                                                  api_keys)
            return response
        except PromptTooLongError as e:
            if attempt == MAX_RETRIES:
                raise
            max_export_tokens = reduce_export_tokens(
                max_export_tokens, e.actual_tokens, e.max_tokens,
                export_content=prompt_text
            )
            logger.warning(
                f"Prompt too long ({e.actual_tokens} > {e.max_tokens}), "
                f"retry {attempt + 2}/{MAX_RETRIES + 1}"
            )
            # Truncate the prompt text proportionally
            ratio = max_export_tokens / approximate_token_count(prompt_text)
            prompt_text = prompt_text[:int(len(prompt_text) * ratio)]


def _save_profile(user, model_id, profile_text, response,
                   source_tokens_used, source_data_cutoff,
                   generation_type, parent_profile_id=None):
    """Save a new UserProfile and log API cost. Returns the profile."""
    from backend.utils.privacy import PrivacyLevel, AIUsage

    input_tokens = response.get("input_tokens", 0)
    output_tokens = response.get("output_tokens", 0)
    total_tokens = response["total_tokens"]

    cost = calculate_llm_cost_microdollars(model_id, input_tokens,
                                           output_tokens)
    cost_log = APICostLog(
        user_id=user.id, model_id=model_id, request_type="profile",
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_microdollars=cost,
    )
    db.session.add(cost_log)

    new_profile = UserProfile(
        user_id=user.id, generated_by=model_id,
        tokens_used=total_tokens,
        privacy_level=PrivacyLevel.PRIVATE,
        ai_usage=AIUsage.CHAT,
        source_tokens_used=source_tokens_used,
        source_data_cutoff=source_data_cutoff,
        generation_type=generation_type,
        parent_profile_id=parent_profile_id,
    )
    new_profile.set_content(profile_text)
    db.session.add(new_profile)
    db.session.commit()
    return new_profile


@celery.task(base=ProfileGenerationTask, bind=True)
def update_user_profile(self, user_id: int, model_id: str,
                        previous_profile_id: int = None):
    """
    Unified profile generation / update task.

    If previous_profile_id is provided, performs an incremental update
    using only new data written after the previous profile's cutoff.
    Otherwise, performs initial generation (possibly iterative if the
    source data exceeds the context window budget).
    """
    logger.info(
        f"Starting profile update for user {user_id}, model {model_id}, "
        f"prev_profile={previous_profile_id}"
    )

    with flask_app.app_context():
        user = User.query.get(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        # Set concurrency guard
        user.profile_generation_task_id = self.request.id
        db.session.commit()

        success = False
        try:
            if model_id not in flask_app.config["SUPPORTED_MODELS"]:
                raise ValueError(f"Unsupported model: {model_id}")

            model_cfg = flask_app.config["SUPPORTED_MODELS"][model_id]
            context_window = model_cfg.get("context_window", 200000)
            max_output_tokens = 4096
            api_keys = get_api_keys_for_usage(flask_app.config, 'chat')

            if previous_profile_id:
                result = _do_incremental_update(
                    self, user, model_id, previous_profile_id,
                    context_window, max_output_tokens, api_keys
                )
            else:
                result = _do_initial_generation(
                    self, user, model_id, context_window,
                    max_output_tokens, api_keys
                )

            success = True
            return result

        except Exception as e:
            logger.error(
                f"Profile update error for user {user_id}: {e}",
                exc_info=True
            )
            raise
        finally:
            # Clear concurrency guard; only clear full-regen flag on success
            user = User.query.get(user_id)
            if user:
                user.profile_generation_task_id = None
                if success:
                    user.profile_needs_full_regen = False
                db.session.commit()


def _do_incremental_update(self, user, model_id, previous_profile_id,
                           context_window, max_output_tokens, api_keys):
    """Incremental update: load previous profile + new data since cutoff."""
    self.update_state(state='PROGRESS', meta={
        'progress': 10, 'status': 'Loading previous profile'
    })

    prev_profile = UserProfile.query.get(previous_profile_id)
    if not prev_profile or prev_profile.user_id != user.id:
        raise ValueError(f"Previous profile {previous_profile_id} not found")

    existing_content = prev_profile.get_content()
    cutoff = prev_profile.source_data_cutoff

    # Calculate budget for new data
    update_template = _load_prompt("profile_update.txt", user_id=user.id)
    gen_template = _load_prompt("profile_generation.txt", user_id=user.id)
    # Strip the OUTPUT section so its "generate now" instruction
    # doesn't confuse the update prompt.
    gen_template_no_output = gen_template.split("## OUTPUT")[0]
    update_template = update_template.replace(
        "{profile_generation_prompt}", gen_template_no_output
    )
    overhead = (approximate_token_count(update_template)
                + approximate_token_count(existing_content)
                + max_output_tokens + 500)
    budget = max(context_window // 2 - overhead, 5000)

    self.update_state(state='PROGRESS', meta={
        'progress': 20, 'status': 'Gathering new writing'
    })

    export_result = build_user_export_content(
        user, max_tokens=budget, filter_ai_usage=True,
        created_after=cutoff, chronological_order=True,
        return_metadata=True
    )

    if not export_result or not export_result.get("content"):
        logger.info(f"No new data for user {user.id} since cutoff {cutoff}")
        return {
            'user_id': user.id,
            'profile_id': prev_profile.id,
            'status': 'completed',
            'total_tokens': 0,
            'message': 'No new data to update',
        }

    new_data = export_result["content"]
    new_data_tokens = export_result["token_count"]
    latest_ts = export_result["latest_node_created_at"]

    prev_source_tokens = prev_profile.source_tokens_used or 0
    total_source = prev_source_tokens + new_data_tokens
    ratio_pct = round(
        new_data_tokens / max(total_source, 1) * 100, 1
    )

    self.update_state(state='PROGRESS', meta={
        'progress': 40, 'status': 'Building update prompt'
    })

    prompt = update_template.replace("{existing_profile}", existing_content)
    prompt = prompt.replace("{new_data}", new_data)
    prompt = prompt.replace("{source_tokens_past}", str(prev_source_tokens))
    prompt = prompt.replace("{source_tokens_new}", str(new_data_tokens))
    prompt = prompt.replace("{ratio_percent}", str(ratio_pct))

    response = _call_llm_with_retries(
        self, model_id, prompt, user.id, api_keys, progress_base=50
    )

    # Use actual input tokens from LLM response for accurate tracking
    actual_input_tokens = response.get("input_tokens", new_data_tokens)
    actual_total_source = prev_source_tokens + actual_input_tokens

    self.update_state(state='PROGRESS', meta={
        'progress': 90, 'status': 'Saving updated profile'
    })

    new_profile = _save_profile(
        user, model_id, response["content"], response,
        source_tokens_used=actual_total_source,
        source_data_cutoff=latest_ts,
        generation_type="update",
        parent_profile_id=prev_profile.id,
    )

    logger.info(
        f"Incremental profile update for user {user.id}: "
        f"profile {new_profile.id}, +{actual_input_tokens} tokens"
    )

    return {
        'user_id': user.id,
        'profile_id': new_profile.id,
        'status': 'completed',
        'total_tokens': response["total_tokens"],
        'profile_length': len(response["content"]),
    }


def _do_initial_generation(self, user, model_id, context_window,
                           max_output_tokens, api_keys):
    """Initial generation, possibly iterative if data exceeds budget."""
    self.update_state(state='PROGRESS', meta={
        'progress': 10, 'status': 'Gathering writing samples'
    })

    gen_template = _load_prompt("profile_generation.txt", user_id=user.id)
    prompt_tokens = approximate_token_count(gen_template)
    budget = max(
        context_window // 2 - prompt_tokens - max_output_tokens - 500,
        5000
    )

    # First pass: get metadata to decide if iterative is needed
    total_export = build_user_export_content(
        user, max_tokens=None, filter_ai_usage=True,
        return_metadata=True
    )

    if not total_export or not total_export.get("content"):
        raise ValueError("No writing found to analyze")

    total_tokens = total_export["token_count"]

    if total_tokens <= budget:
        # Single-pass generation
        return _single_pass_generation(
            self, user, model_id, gen_template, total_export,
            api_keys
        )
    else:
        # Iterative build
        return _iterative_generation(
            self, user, model_id, gen_template, budget,
            context_window, max_output_tokens, api_keys
        )


def _single_pass_generation(self, user, model_id, gen_template,
                            export_result, api_keys):
    """Single-pass profile generation when all data fits in budget."""
    self.update_state(state='PROGRESS', meta={
        'progress': 30, 'status': 'Preparing prompt'
    })

    content = export_result["content"]
    prompt = gen_template.replace("{user_export}", content)

    response = _call_llm_with_retries(
        self, model_id, prompt, user.id, api_keys, progress_base=40
    )

    # Use actual input tokens from LLM response for accurate tracking
    actual_source_tokens = response.get(
        "input_tokens", export_result["token_count"]
    )

    self.update_state(state='PROGRESS', meta={
        'progress': 90, 'status': 'Saving profile'
    })

    new_profile = _save_profile(
        user, model_id, response["content"], response,
        source_tokens_used=actual_source_tokens,
        source_data_cutoff=export_result["latest_node_created_at"],
        generation_type="initial",
    )

    logger.info(
        f"Single-pass profile for user {user.id}: "
        f"profile {new_profile.id}"
    )

    return {
        'user_id': user.id,
        'profile_id': new_profile.id,
        'status': 'completed',
        'total_tokens': response["total_tokens"],
        'profile_length': len(response["content"]),
    }


def _iterative_generation(self, user, model_id, gen_template, budget,
                          context_window, max_output_tokens, api_keys):
    """Iterative profile building: process data in chronological chunks."""
    logger.info(
        f"Starting iterative profile build for user {user.id}, "
        f"budget={budget} tokens per chunk"
    )

    update_template = _load_prompt("profile_update.txt", user_id=user.id)
    # Strip the OUTPUT section so its "generate now" instruction
    # doesn't confuse the update prompt.
    gen_template_no_output = gen_template.split("## OUTPUT")[0]
    update_template = update_template.replace(
        "{profile_generation_prompt}", gen_template_no_output
    )
    current_profile = None
    current_profile_id = None
    cumulative_source_tokens = 0
    chunk_num = 0
    current_cutoff = None

    while True:
        chunk_num += 1
        progress = min(10 + chunk_num * 20, 85)
        self.update_state(state='PROGRESS', meta={
            'progress': progress,
            'status': f'Processing chunk {chunk_num}'
        })

        chunk = build_user_export_content(
            user, max_tokens=budget, filter_ai_usage=True,
            created_after=current_cutoff,
            chronological_order=True, return_metadata=True
        )

        if not chunk or not chunk.get("content"):
            break

        chunk_tokens_est = chunk["token_count"]
        latest_ts = chunk["latest_node_created_at"]

        if current_profile is None:
            # First chunk: use generation template
            prompt = gen_template.replace("{user_export}", chunk["content"])
        else:
            # Subsequent chunks: use update template
            # Use estimates for prompt placeholders (guidance for LLM)
            ratio_pct = round(
                chunk_tokens_est / max(
                    cumulative_source_tokens + chunk_tokens_est, 1
                ) * 100, 1
            )
            prompt = update_template.replace(
                "{existing_profile}", current_profile
            )
            prompt = prompt.replace("{new_data}", chunk["content"])
            prompt = prompt.replace(
                "{source_tokens_past}",
                str(cumulative_source_tokens)
            )
            prompt = prompt.replace(
                "{source_tokens_new}", str(chunk_tokens_est)
            )
            prompt = prompt.replace("{ratio_percent}", str(ratio_pct))

        response = _call_llm_with_retries(
            self, model_id, prompt, user.id, api_keys,
            progress_base=progress,
            status_label=f'Generating profile: Chunk {chunk_num}'
        )

        # Use actual input tokens from LLM response for accurate tracking
        actual_chunk_tokens = response.get(
            "input_tokens", chunk_tokens_est
        )
        cumulative_source_tokens += actual_chunk_tokens

        # Determine generation type for intermediate vs final
        gen_type = "iterative"

        profile = _save_profile(
            user, model_id, response["content"], response,
            source_tokens_used=cumulative_source_tokens,
            source_data_cutoff=latest_ts,
            generation_type=gen_type,
            parent_profile_id=current_profile_id,
        )

        current_profile = response["content"]
        current_profile_id = profile.id
        current_cutoff = latest_ts

        # Check if we've processed all data
        if chunk["node_count"] == 0:
            break

        # Check if there's more data after this cutoff
        from backend.models import Node
        has_more = Node.query.filter(
            Node.user_id == user.id,
            Node.created_at > current_cutoff,
            Node.ai_usage.in_(['chat', 'train'])
        ).first() is not None
        if not has_more:
            break

    # Mark the final profile as "initial" (the iterative process is done)
    if current_profile_id:
        final = UserProfile.query.get(current_profile_id)
        if final:
            final.generation_type = "initial"
            db.session.commit()

    self.update_state(state='PROGRESS', meta={
        'progress': 95, 'status': 'Finalizing'
    })

    logger.info(
        f"Iterative profile build for user {user.id}: "
        f"{chunk_num} chunks, profile {current_profile_id}"
    )

    return {
        'user_id': user.id,
        'profile_id': current_profile_id,
        'status': 'completed',
        'total_tokens': cumulative_source_tokens,
        'chunks_processed': chunk_num,
    }


def maybe_trigger_profile_update(user_id, model_id=None,
                                  force_full_regen=False):
    """
    Check concurrency guard and dispatch update_user_profile if safe.
    Returns the task_id or None if skipped.
    """
    user = User.query.get(user_id)
    if not user:
        return None

    # Check concurrency guard
    if user.profile_generation_task_id:
        from backend.celery_app import celery as _celery
        task = _celery.AsyncResult(user.profile_generation_task_id)
        if task.state in ('PENDING', 'STARTED', 'PROGRESS'):
            logger.info(
                f"Skipping profile update for user {user_id}: "
                f"task {user.profile_generation_task_id} in progress"
            )
            return None
        # Clear stale guard
        user.profile_generation_task_id = None
        db.session.commit()

    if model_id is None:
        model_id = flask_app.config.get(
            "DEFAULT_LLM_MODEL", "claude-opus-4.6"
        )

    # Find latest profile
    latest_profile = UserProfile.query.filter_by(
        user_id=user_id
    ).order_by(UserProfile.created_at.desc()).first()

    prev_id = None if force_full_regen else (
        latest_profile.id if latest_profile else None
    )

    task = update_user_profile.delay(user_id, model_id, prev_id)
    user.profile_generation_task_id = task.id
    db.session.commit()

    logger.info(
        f"Dispatched profile update task {task.id} for user {user_id}"
        f" (force_full_regen={force_full_regen})"
    )
    return task.id


def maybe_trigger_incremental_profile_update(user):
    """
    Check if enough new writing has accumulated to trigger an
    incremental profile update. Called periodically by Celery beat.
    """
    from datetime import datetime, timedelta
    from backend.models import Node

    # Only for paid plans
    if (user.plan or "free") not in User.VOICE_MODE_PLANS:
        return None

    # User must have been inactive for at least 30 minutes
    last_node = Node.query.filter_by(user_id=user.id) \
        .order_by(Node.created_at.desc()).first()
    MIN_INACTIVITY = timedelta(minutes=30)
    if last_node and (datetime.utcnow() - last_node.created_at) < MIN_INACTIVITY:
        return None

    # Find latest profile
    latest_profile = UserProfile.query.filter_by(
        user_id=user.id
    ).order_by(UserProfile.created_at.desc()).first()

    THRESHOLD_TOKENS = 10000
    MIN_INTERVAL = timedelta(hours=1)

    if latest_profile:
        # Check minimum interval
        if (datetime.utcnow() - latest_profile.created_at) < MIN_INTERVAL:
            return None

        cutoff = latest_profile.source_data_cutoff
        if cutoff:
            from sqlalchemy import func
            new_tokens = db.session.query(
                func.coalesce(func.sum(Node.token_count), 0)
            ).filter(
                Node.user_id == user.id,
                Node.updated_at >= cutoff,
                Node.ai_usage.in_(['chat', 'train']),
                Node.token_count.isnot(None)
            ).scalar()
        else:
            new_tokens = THRESHOLD_TOKENS  # No cutoff = trigger
    else:
        # No profile exists: check total eligible tokens
        from sqlalchemy import func
        new_tokens = db.session.query(
            func.coalesce(func.sum(Node.token_count), 0)
        ).filter(
            Node.user_id == user.id,
            Node.ai_usage.in_(['chat', 'train']),
            Node.token_count.isnot(None)
        ).scalar()

    if new_tokens >= THRESHOLD_TOKENS:
        force = user.profile_needs_full_regen
        return maybe_trigger_profile_update(
            user.id, force_full_regen=force
        )

    return None


@celery.task
def check_pending_profile_updates():
    """Periodic task: check all eligible users for pending profile updates."""
    with flask_app.app_context():
        users = User.query.filter(
            User.plan.in_(list(User.VOICE_MODE_PLANS))
        ).all()
        for user in users:
            try:
                maybe_trigger_incremental_profile_update(user)
            except Exception as e:
                logger.warning(
                    f"Profile update check failed for user {user.id}: {e}"
                )


@celery.task(bind=True)
def export_user_threads(self, user_id: int):
    """
    Asynchronously export user's threads to formatted text.

    Args:
        user_id: Database ID of the user

    Returns:
        dict: Export result with formatted text
    """
    logger.info(f"Starting thread export task for user {user_id}")

    with flask_app.app_context():
        user = User.query.get(user_id)
        if not user:
            raise ValueError(f"User {user_id} not found")

        try:
            self.update_state(state='PROGRESS', meta={'progress': 20, 'status': 'Building export'})

            export_content = build_user_export_content(user)

            if not export_content:
                raise ValueError("No threads found to export")

            logger.info(f"Thread export successful for user {user_id}: {len(export_content)} characters")

            return {
                'user_id': user_id,
                'status': 'completed',
                'export_length': len(export_content),
                'content': export_content
            }

        except Exception as e:
            logger.error(f"Thread export error for user {user_id}: {e}", exc_info=True)
            raise
