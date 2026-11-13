"""Background runs of pre-fill bundle loads (#698).

``load_prefill_bundle`` runs one load for up to RUN_SECONDS and sends the
next run itself (backend/utils/prefill_bundle_load.run_load); the load's
row holds its place, so any run can continue it. ``acks_late`` with
``reject_on_worker_lost`` hands a run killed with its worker to another
worker; the run claims the load only once the dead run's heartbeat is
stale, and ``resume_stalled_bundle_loads`` (beat) sends a load whose run
was lost altogether. Both are no-ops while PREFILL_BUNDLES_ENABLED is off.
"""
from celery.utils.log import get_task_logger

from backend.celery_app import celery, flask_app

logger = get_task_logger(__name__)


@celery.task(name="backend.tasks.prefill_bundles.load_prefill_bundle",
             acks_late=True, reject_on_worker_lost=True)
def load_prefill_bundle(load_id):
    with flask_app.app_context():
        from backend.utils.prefill_bundle_load import run_load
        return run_load(load_id)


@celery.task(name="backend.tasks.prefill_bundles.resume_stalled_bundle_loads")
def resume_stalled_bundle_loads():
    """Beat: send again the loads whose run stopped without a trace (a
    killed worker, a lost message). One indexed query when none did."""
    with flask_app.app_context():
        from backend.utils.prefill_bundle_load import stalled_load_ids
        ids = stalled_load_ids()
        for load_id in ids:
            load_prefill_bundle.apply_async(args=(load_id,))
            logger.warning("Pre-fill bundle load %s had stopped; sent again",
                           load_id)
        return {"resumed": len(ids)}
