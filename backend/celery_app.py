"""
Celery application initialization for Write or Perish.
Handles background tasks for long-running operations like transcription, LLM calls, and exports.
"""
from celery import Celery
from backend import create_app

# Create Flask app for Celery context
flask_app = create_app()

# Initialize Celery
celery = Celery(
    'write_or_perish',
    broker=flask_app.config.get('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
    backend=flask_app.config.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
)

# Celery configuration
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hour hard limit
    task_soft_time_limit=3300,  # 55 minute soft limit
    worker_prefetch_multiplier=1,  # Process one task at a time
    worker_max_tasks_per_child=50,  # Restart worker after 50 tasks (prevent memory leaks)
    result_expires=86400,  # Keep results for 24 hours
    beat_schedule={
        'check-profile-updates': {
            'task': 'backend.tasks.exports.check_pending_profile_updates',
            'schedule': 3600.0,  # every hour
        },
        'check-recent-context-updates': {
            'task': 'backend.tasks.recent_context.check_pending_recent_context_updates',
            'schedule': 600.0,  # every 10 minutes
        },
        'cleanup-deleted-nodes': {
            'task': 'backend.tasks.node_cleanup.cleanup_deleted_nodes',
            'schedule': 86400.0,  # daily
        },
        # Semantic-search embedding sweep (issue #155).
        'sweep-embeddings': {
            'task': 'backend.tasks.embeddings.sweep_embeddings',
            'schedule': 300.0,  # every 5 min — new content searchable fast
        },
        # Profile batch pipeline (issue #173). No-ops unless a user is
        # selected via PROFILE_USE_BATCH / PROFILE_BATCH_USER_IDS.
        'seed-profile-batches': {
            'task': 'backend.tasks.profile_batch.seed_profile_batches',
            'schedule': 3600.0,  # hourly, alongside the sync check
        },
        'poll-profile-batches': {
            'task': 'backend.tasks.profile_batch.poll_profile_batches',
            'schedule': 60.0,  # ~1 min — batches typically finish in 1-5 min
        },
        # No-op unless ANTHROPIC_SPEND_LIMIT_USD is set (issue #85).
        'check-api-spend': {
            'task': 'backend.tasks.spend_monitor.check_api_spend',
            'schedule': 3600.0,  # hourly
        },
    },
)

# Keep third-party HTTP/SDK loggers off DEBUG so request bodies (which can
# contain user content, e.g. profile prompts) never get logged — even when the
# worker runs at --loglevel=debug (staging). These set their own level, so it
# holds regardless of the root level Celery configures.
import logging  # noqa: E402
for _noisy_logger in ("anthropic", "openai", "httpx", "httpcore"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

# Import tasks to register them with Celery
# This must be done after celery app is created
from backend.tasks import transcription  # noqa: F401
from backend.tasks import llm_completion  # noqa: F401
from backend.tasks import tts  # noqa: F401
from backend.tasks import exports  # noqa: F401
from backend.tasks import streaming_transcription  # noqa: F401
from backend.tasks import voice_todo_merge  # noqa: F401
from backend.tasks import recent_context  # noqa: F401
from backend.tasks import node_cleanup  # noqa: F401
from backend.tasks import profile_batch  # noqa: F401
from backend.tasks import spend_monitor  # noqa: F401
from backend.tasks import embeddings  # noqa: F401
from backend.tasks import poll_draft  # noqa: F401
