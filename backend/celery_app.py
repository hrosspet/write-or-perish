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
)

# Auto-discover tasks
celery.autodiscover_tasks(['backend.tasks'])
