"""Celery queue split (#379): which queue each task goes to, and which
queues each worker reads.

Off unless CELERY_SPLIT_QUEUES is 1, true or yes. Off, no routes are set:
every task goes to Celery's default queue ("celery") and the one worker
reads it, exactly as before the split. On, each task goes to the queue
TASK_QUEUES gives it, and two workers read them:

- interactive: the work a user waits for (replies, voice turns,
  transcription, TTS), plus the short spend-cap check after each call;
- background: the work nobody waits for (profile builds, imports and
  pre-fills, digests, batch submissions and their polls, syncs, sweeps,
  purges). It also reads the default queue, where Celery's own tasks,
  messages queued before the switch and retries of tasks first delivered
  there end up (a retry goes back to the queue its task came from).

So a voice turn never waits behind a profile build, in everyday use or
while a deploy restarts the workers (each worker restarts on its own).

Switching it on or off on prod is an ordered procedure, so that no task
is ever routed to a queue nobody reads: docs/RUNBOOKS.md → "Celery queue
split". The systemd units are in deploy/systemd/. Docker Compose (dev,
staging) starts each worker through this file, which picks the worker's
queues from the same setting:

    python backend/celery_queues.py interactive|background [worker options]

Standard library only: Compose runs this file as a script, without
importing the backend package (which loads the whole app).
"""
import os
import sys

SETTING = "CELERY_SPLIT_QUEUES"

DEFAULT_QUEUE = "celery"  # Celery's task_default_queue, unchanged
INTERACTIVE_QUEUE = "interactive"
BACKGROUND_QUEUE = "background"

_I, _B = INTERACTIVE_QUEUE, BACKGROUND_QUEUE

# Every task in backend/tasks/ (a test fails when one is missing).
TASK_QUEUES = {
    # Replies: text, voice and Read/Glean turns. A batched Read polls by
    # re-queueing itself every few minutes; each poll is one short call.
    "backend.tasks.llm_completion.generate_llm_response": _I,
    # Voice mode: the cache warm the reply waits for.
    "backend.tasks.llm_completion.prewarm_anthropic_cache": _I,
    # Beat backstop that only re-dispatches stuck Read batch polls.
    "backend.tasks.llm_completion.resume_stuck_feed_batches": _B,

    # Transcription of recordings and uploads.
    "backend.tasks.transcription.transcribe_audio": _I,
    "backend.tasks.streaming_transcription.transcribe_chunk": _I,
    "backend.tasks.streaming_transcription.finalize_streaming": _I,
    "backend.tasks.streaming_transcription.transcribe_draft_chunk": _I,
    "backend.tasks.streaming_transcription.transcribe_chunk_batch": _I,
    "backend.tasks.streaming_transcription.finalize_draft_streaming": _I,
    "backend.tasks.voice_todo_merge.apply_voice_todo": _I,

    # Text to speech: a reply, a profile or a saved reference the user
    # asked to hear.
    "backend.tasks.tts.generate_tts_audio": _I,
    "backend.tasks.tts.generate_tts_audio_for_profile": _I,
    "backend.tasks.tts.generate_tts_audio_for_item": _I,

    # Per-user spend cap, checked after each logged call: one short query,
    # and a late check lets spending run past the cap.
    "backend.tasks.spend_monitor.enforce_user_cap": _I,
    "backend.tasks.spend_monitor.check_api_spend": _B,

    # Profiles, recent context, intentions, digests, poll drafts.
    "backend.tasks.exports.update_user_profile": _B,
    "backend.tasks.exports.check_pending_profile_updates": _B,
    "backend.tasks.exports.export_user_threads": _B,
    "backend.tasks.profile_batch.seed_profile_batches": _B,
    "backend.tasks.profile_batch.seed_profile_batch_for_user": _B,
    "backend.tasks.profile_batch.poll_profile_batches": _B,
    "backend.tasks.recent_context.check_pending_recent_context_updates": _B,
    "backend.tasks.recent_context.collect_recent_context_batches": _B,
    "backend.tasks.recent_context.generate_recent_context": _B,
    "backend.tasks.intentions.infer_intentions": _B,
    "backend.tasks.external_digest.sweep_external_digests": _B,
    "backend.tasks.external_digest.collect_external_digest_batches": _B,
    "backend.tasks.external_digest.rebuild_external_digest": _B,
    "backend.tasks.poll_draft.submit_poll_draft": _B,
    "backend.tasks.poll_draft.collect_poll_draft_batches": _B,

    # Imports, pre-fills and syncs.
    "backend.tasks.chat_imports.import_conversations": _B,
    "backend.tasks.chat_imports.sweep_import_stashes": _B,
    # Beat (every minute): re-sends an import whose worker died (#546).
    "backend.tasks.chat_imports.resume_stalled_imports": _B,
    "backend.tasks.imports.import_twitter_archive": _B,
    "backend.tasks.imports.prefill_community_archive": _B,
    "backend.tasks.imports.prefill_x_api": _B,
    "backend.tasks.imports.refresh_community_archive_snapshot": _B,
    "backend.tasks.external_sync.fetch_community_archive": _B,
    "backend.tasks.external_sync.sync_twitter_bookmarks": _B,
    "backend.tasks.external_sync.sync_all_twitter_bookmarks": _B,
    "backend.tasks.external_sync.verify_public_source_sweep": _B,

    # Sweeps and deletion.
    "backend.tasks.embeddings.sweep_embeddings": _B,
    "backend.tasks.node_cleanup.cleanup_deleted_nodes": _B,
    "backend.tasks.user_purge.process_user_data_purges": _B,
    "backend.tasks.user_purge.run_user_data_purge": _B,
}

# The queues each worker reads while the split is on.
WORKER_QUEUES = {
    "interactive": (INTERACTIVE_QUEUE,),
    "background": (BACKGROUND_QUEUE, DEFAULT_QUEUE),
}


def split_queues_enabled(value=None):
    """Whether the split is on. *value* defaults to the CELERY_SPLIT_QUEUES
    environment variable; unset and empty (what Compose passes for an
    unset variable) are off."""
    if value is None:
        value = os.environ.get(SETTING)
    return (value or "").strip().lower() in ("1", "true", "yes")


def task_routes(enabled):
    """Celery's task_routes: None (Celery's default, everything on the
    default queue) when off, each task's queue when on."""
    if not enabled:
        return None
    return {name: {"queue": queue} for name, queue in TASK_QUEUES.items()}


def worker_command(role, enabled, options=()):
    """The Celery worker command for *role* ("interactive" or
    "background"). Off, the interactive worker is the one worker and runs
    the command used before the split, and the background worker does not
    run (None)."""
    if role not in WORKER_QUEUES:
        raise ValueError(f"unknown worker role: {role!r}")
    base = ["celery", "-A", "backend.celery_app.celery", "worker"]
    if not enabled:
        return base + list(options) if role == "interactive" else None
    return base + ["-Q", ",".join(WORKER_QUEUES[role]),
                   "-n", f"{role}@%h"] + list(options)


def _wait_for_stop(message):
    """Print *message* and wait until stopped (SIGTERM or SIGINT)."""
    import signal

    def stop(*_):
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(message, flush=True)
    while True:
        signal.pause()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in WORKER_QUEUES:
        sys.exit("usage: python backend/celery_queues.py "
                 "interactive|background [celery worker options]")
    role, options = argv[0], argv[1:]
    command = worker_command(role, split_queues_enabled(), options)
    if command is None:
        # Compose restarts a container that exits, so this one waits.
        _wait_for_stop(f"{SETTING} is off: the {role} worker does not run.")
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
