"""Helpers for recording user-facing warnings during async LLM tasks.

The flow:
  task body -> record_task_warning(llm_node, msg)
  -> persists JSON list on Node.llm_task_warnings
  -> /nodes/<id>/llm-status returns it
  -> frontend useLlmTaskWarnings hook fires toast(s)

Generic by design: any Celery task that produces a Node with an
`llm_task_*` lifecycle (LLM completion, voice transcription follow-up,
etc.) can append warnings without inventing its own surface.
"""

import json


def record_task_warning(node, message):
    """Append `message` to `node.llm_task_warnings` (a JSON list of
    strings).

    Caller is responsible for committing the SQLAlchemy session — the
    LLM task body batches a single commit per phase, and double-commits
    would interfere with that pattern.
    """
    if node.llm_task_warnings:
        try:
            existing = json.loads(node.llm_task_warnings)
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, TypeError):
            existing = []
    else:
        existing = []
    existing.append(message)
    node.llm_task_warnings = json.dumps(existing)


def load_task_warnings(node):
    """Return the list of warnings stored on `node`, or [] if none /
    malformed. Used by the status endpoint."""
    if not node.llm_task_warnings:
        return []
    try:
        parsed = json.loads(node.llm_task_warnings)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
