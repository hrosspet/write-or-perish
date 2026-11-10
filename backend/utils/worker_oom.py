"""When memory runs out, the kernel kills a pool process, not the Celery
main process (#613).

The worker units have a memory limit (MemoryHigh/MemoryMax, docs/
RUNBOOKS.md → "Celery queue split"). When a job takes the worker to the
limit, the kernel's OOM killer picks the process in the unit with the
highest score: its memory plus its ``oom_score_adj``. The process that
grew is a pool process running the job, but every process has the whole
app loaded, so the scores of an idle pool process and the main process
are close. Each pool process therefore raises its own ``oom_score_adj``
when it starts: a pool process is always picked before the main process,
and among pool processes the largest, the one that ran out of memory,
goes first. The main process survives, logs the lost task
(WorkerLostError; an acks_late task with reject_on_worker_lost goes back
to the queue) and starts a new pool process. The unit's
``OOMPolicy=continue`` keeps systemd from stopping the whole unit after
the kill.

The same holds when the whole server runs out of memory: a Celery pool
process goes before Postgres, Redis or the site. A process may raise its
own score without privileges; lowering it needs them, so this only adds.
On a system without /proc (macOS) nothing happens.
"""
import logging

log = logging.getLogger(__name__)

OOM_SCORE_ADJ_PATH = "/proc/self/oom_score_adj"
# HEURISTIC (#613): how much a pool process adds to its oom_score_adj
# (range -1000..1000). The kernel adds adj/1000 of the memory limit (or of
# the server's memory) to the process's score: 300 puts a pool process
# ahead of the main process unless the main process uses 30 % of the limit
# more than it.
POOL_PROCESS_OOM_SCORE_RAISE = 300
OOM_SCORE_ADJ_MAX = 1000


def raise_oom_score(path=OOM_SCORE_ADJ_PATH,
                    by=POOL_PROCESS_OOM_SCORE_RAISE):
    """Raise this process's oom_score_adj by *by* (capped at 1000).
    Returns the new value, or None when it could not be set."""
    try:
        with open(path) as fh:
            current = int(fh.read().strip() or 0)
        target = min(OOM_SCORE_ADJ_MAX, current + by)
        if target != current:
            with open(path, "w") as fh:
                fh.write(f"{target}\n")
        return target
    except (OSError, ValueError) as e:
        log.debug("oom_score_adj not raised (%s)", type(e).__name__)
        return None


def on_pool_process_start(**_):
    """worker_process_init handler (connected in backend/celery_app.py):
    runs in each prefork pool process after the fork, not in the main
    process."""
    raise_oom_score()
