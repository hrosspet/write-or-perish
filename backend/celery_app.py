"""
Celery application initialization for Write or Perish.
Handles background tasks for long-running operations like transcription, LLM calls, and exports.
"""
from celery import Celery, schedules
from backend import create_app

# Create Flask app for Celery context
flask_app = create_app()

# Initialize Celery
celery = Celery(
    'write_or_perish',
    broker=flask_app.config.get('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
    backend=flask_app.config.get('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
)

TASK_TIME_LIMIT = 3600  # 1 hour hard limit
TASK_SOFT_TIME_LIMIT = 3300  # 55 minute soft limit
# How long Redis waits for a delivered message's ack before it delivers the
# message again. An acks_late task (the profile window advance, a chat
# import) is acked only when it ends, and a reserved message is unacked
# while it waits for a free worker process; with Redis's default of one
# hour, a task that ran to the hard limit was delivered a second time while
# it was still running or being killed. HEURISTIC: three hard limits: a
# reserved message may wait up to one hard limit for a free process, then
# run for one, and the third covers timers that fire late when the server
# is short of memory (the hard limit has fired many minutes late). The cost:
# a message whose worker died without acking it (the whole worker killed,
# not one process) comes back only after this time. Also above every
# countdown the code schedules (the longest fixed one is 30 minutes; the
# nightly bookmark sync staggers 30 s per account, which passes three
# hours at 360 accounts in one local hour).
BROKER_VISIBILITY_TIMEOUT = 3 * TASK_TIME_LIMIT

# Celery configuration
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=TASK_TIME_LIMIT,
    task_soft_time_limit=TASK_SOFT_TIME_LIMIT,
    broker_transport_options={'visibility_timeout': BROKER_VISIBILITY_TIMEOUT},
    worker_prefetch_multiplier=1,  # Process one task at a time
    worker_max_tasks_per_child=50,  # Restart worker after 50 tasks (prevent memory leaks)
    result_expires=86400,  # Keep results for 24 hours
    beat_schedule={
        'check-profile-updates': {
            'task': 'backend.tasks.exports.check_pending_profile_updates',
            'schedule': 3600.0,  # every hour
        },
        # Submits each due user's recent context as a provider batch
        # (#380, ~50% cheaper); the collector below saves the results.
        'check-recent-context-updates': {
            'task': 'backend.tasks.recent_context.check_pending_recent_context_updates',
            'schedule': 600.0,  # every 10 minutes
        },
        # Until a user's batch is collected, prompts read their previous
        # summary, so collect promptly. No-op when nothing is pending.
        'collect-recent-context-batches': {
            'task': 'backend.tasks.recent_context.collect_recent_context_batches',
            'schedule': 60.0,  # batches typically finish in 1-5 min
        },
        # Emails the owner when the server disk is nearly full. No-op
        # unless DISK_ALERT_ENABLED (backend/utils/disk_alert.py).
        'check-disk-usage': {
            'task': 'backend.tasks.disk_monitor.check_disk_usage',
            'schedule': 3600.0,  # every hour
        },
        # A daily job gets a fixed time, never an interval: deploy.sh
        # deletes beat's schedule file, so an interval's countdown starts
        # over on every deploy and the job only runs after 24 h without
        # one (#614). Beat runs in UTC (timezone/enable_utc above).
        # 03:17 UTC: away from the :00 hourly jobs and the 05:00 sweep.
        'cleanup-deleted-nodes': {
            'task': 'backend.tasks.node_cleanup.cleanup_deleted_nodes',
            'schedule': schedules.crontab(hour=3, minute=17),  # daily, 03:17 UTC
        },
        # Backstop for the {ca_tweets} batch poll: a hard worker kill loses
        # the scheduled retry; this re-dispatches nodes whose poll
        # heartbeat stopped (see llm_completion.resume_stuck_feed_batches).
        'resume-stuck-feed-batches': {
            'task': 'backend.tasks.llm_completion.resume_stuck_feed_batches',
            'schedule': 600.0,  # every 10 minutes
        },
        # Keep the cached Community Archive export current for reads (the
        # archive exports nightly ~07:00 UTC). One manifest GET per run;
        # the ~900 MB download only when the export changed, and only
        # where a snapshot already exists.
        'refresh-community-archive-snapshot': {
            'task': 'backend.tasks.imports.refresh_community_archive_snapshot',
            'schedule': 1800.0,  # every 30 minutes
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
        # Poll drafts ride the Batch API (#207); no-op when none pending.
        'collect-poll-draft-batches': {
            'task': 'backend.tasks.poll_draft.collect_poll_draft_batches',
            'schedule': 60.0,  # batches typically finish in 1-5 min
        },
        # Nightly X bookmark refresh (#208): an HOURLY gate that dispatches
        # each account when its user's local clock hits ~3am (User.timezone)
        # — users sync in their own night, wherever they are. A 20h min-gap
        # guard stops double-syncs when beat restarts shift the phase.
        # No-op until X_CLIENT_ID is configured.
        'sync-twitter-bookmarks-nightly-gate': {
            'task': 'backend.tasks.external_sync.sync_all_twitter_bookmarks',
            'schedule': 3600.0,  # hourly; the task itself gates on local 3am
        },
        # Nightly external-references digest rebuild: an HOURLY gate that
        # dispatches a user at local ~4am (an hour after the bookmark sync
        # above) and only when their saved references changed since the
        # last rebuild. The digest is ONE LLM call over the whole corpus,
        # so it must never ride individual saves.
        'sweep-external-digests': {
            'task': 'backend.tasks.external_digest.sweep_external_digests',
            'schedule': 3600.0,  # hourly; the task itself gates on local 4am
        },
        # The sweep submits digests as a provider batch (~50% cheaper);
        # this collects finished batches. Nobody waits on a digest, so a
        # slow poll is fine. No-op when nothing is pending.
        'collect-external-digest-batches': {
            'task': 'backend.tasks.external_digest.collect_external_digest_batches',
            'schedule': 600.0,  # every 10 min
        },
        # Nightly publicness check for saved tweets nobody has vouched
        # for (#295 step 0): JSON-imported bookmarks, tweets clipped
        # without a visible lock, bookmarks synced before the sync asked
        # X about the author. An HOURLY gate like the two above — a
        # daily interval starts its countdown over on every deploy,
        # which deletes the beat schedule file (deploy.sh) — that runs
        # the sweep at 05:00 UTC, bounded per run (see the task's
        # constants). X's free oEmbed endpoint: no API credits, nothing
        # waits on it.
        'verify-public-source-nightly': {
            'task': 'backend.tasks.external_sync.verify_public_source_sweep',
            'schedule': 3600.0,  # hourly; the task itself gates on 05:00 UTC
        },
        # User data purge (#268): starts a purge when the user's 30-day
        # grace period ends (at once for an admin purge) and resumes one
        # whose runner died. One indexed query on a small table when
        # nothing is due.
        'process-user-data-purges': {
            'task': 'backend.tasks.user_purge.process_user_data_purges',
            'schedule': 60.0,  # every minute
        },
        # Import stashes (an analyzed ChatGPT/Claude export, encrypted, or
        # a Twitter archive's tweets) that nobody confirmed within their
        # 24 h TTL. A directory listing when there is nothing to remove.
        'sweep-import-stashes': {
            'task': 'backend.tasks.chat_imports.sweep_import_stashes',
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
from backend.tasks import profile_windows  # noqa: F401
from backend.tasks import spend_monitor  # noqa: F401
from backend.tasks import disk_monitor  # noqa: F401
from backend.tasks import embeddings  # noqa: F401
from backend.tasks import poll_draft  # noqa: F401
from backend.tasks import external_sync  # noqa: F401
from backend.tasks import external_digest  # noqa: F401
from backend.tasks import imports  # noqa: F401
from backend.tasks import chat_imports  # noqa: F401
from backend.tasks import intentions  # noqa: F401
from backend.tasks import user_purge  # noqa: F401
