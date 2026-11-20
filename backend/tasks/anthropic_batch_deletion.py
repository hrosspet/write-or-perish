"""Hourly sweep: delete the Anthropic Message Batches Loore no longer
needs (#693). The rules and the bookkeeping are in
backend/utils/anthropic_batch_deletion.py; most batches are deleted
right after their results are saved, and this pass deletes the rest:
after a crash or a failed delete, and batches Loore gave up on."""
from backend.celery_app import celery, flask_app


@celery.task(
    name="backend.tasks.anthropic_batch_deletion.sweep_anthropic_batches")
def sweep_anthropic_batches():
    """One bounded pass (SWEEP_LIMIT rows). Returns the outcome counts."""
    with flask_app.app_context():
        from backend.utils.anthropic_batch_deletion import sweep
        return sweep()
