"""
Celery tasks for asynchronous data export operations.
"""
from celery import Task
from celery.utils.log import get_task_logger
import os

from backend.celery_app import celery, flask_app
from backend.models import User, UserProfile
from backend.extensions import db
from backend.llm_providers import LLMProvider
from backend.utils.tokens import approximate_token_count, calculate_max_export_tokens
from backend.utils.api_keys import get_api_keys_for_usage

logger = get_task_logger(__name__)


# Import from export_data module
def build_user_export_content(user, max_tokens=None, filter_ai_usage=False):
    """Import the actual implementation from export_data routes."""
    from backend.routes.export_data import build_user_export_content as _build
    return _build(user, max_tokens, filter_ai_usage)


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

            prompt_template_path = os.path.join(
                flask_app.root_path,
                "prompts",
                "profile_generation.txt"
            )

            try:
                with open(prompt_template_path, "r", encoding="utf-8") as f:
                    prompt_template = f.read()
            except FileNotFoundError:
                raise FileNotFoundError(f"Prompt template not found at {prompt_template_path}")

            # Calculate max tokens for export based on model's context window
            prompt_tokens = approximate_token_count(prompt_template)
            MAX_EXPORT_TOKENS = calculate_max_export_tokens(model_id, reserved_tokens=prompt_tokens)

            # Filter by AI usage to only include nodes where ai_usage is 'chat' or 'train'
            user_export = build_user_export_content(user, max_tokens=MAX_EXPORT_TOKENS, filter_ai_usage=True)

            if not user_export:
                raise ValueError("No writing found to analyze")

            logger.info(f"User export built for user {user_id}, length: {len(user_export)} characters")

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

            api_keys = get_api_keys_for_usage(flask_app.config, 'chat')

            response = LLMProvider.get_completion(model_id, messages, api_keys)
            profile_text = response["content"]
            total_tokens = response["total_tokens"]

            logger.info(f"Profile generated for user {user_id}: {len(profile_text)} characters, {total_tokens} tokens")

            # Step 5: Save to database (95% progress)
            self.update_state(state='PROGRESS', meta={'progress': 95, 'status': 'Saving profile'})

            # Default privacy for AI-generated profiles: private + chat
            from backend.utils.privacy import PrivacyLevel, AIUsage
            new_profile = UserProfile(
                user_id=user.id,
                content=profile_text,
                generated_by=model_id,
                tokens_used=total_tokens,
                privacy_level=PrivacyLevel.PRIVATE,
                ai_usage=AIUsage.CHAT
            )
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
