"""Provider-agnostic Batch API helpers (Anthropic + OpenAI).

Submit / poll / collect for the providers' Batch API (~50% cheaper, async,
≤24h SLA). Extracted from the prompt-RCT harness so the production
profile-batch pipeline and the RCT share one implementation.

These functions are deliberately dependency-light (no Flask / DB): they take
an ``api_keys`` dict and plain request dicts, so they unit-test cleanly with
mocked SDK clients. Persistence and scheduling live with the caller.

Request shape (per item):
    {"custom_id": str, "model_id": str, "api_model": str,
     "messages": list, "max_tokens": int}

Result shape (per custom_id), from batch_check_and_collect:
    {"content": str, "input_tokens": int, "output_tokens": int}
"""
import json
import logging
import os
import tempfile

log = logging.getLogger(__name__)


def apply_batch_key_override(api_keys, config):
    """Overlay the batch-specific OpenAI key (``OPENAI_API_KEY_BATCH``) if set.

    Batch jobs may run on a separate key/quota from interactive calls.
    Returns a new dict; the input is not mutated.
    """
    keys = dict(api_keys)
    batch_oai_key = config.get("OPENAI_API_KEY_BATCH")
    if batch_oai_key:
        keys["openai"] = batch_oai_key
    return keys


def _convert_messages_for_anthropic(messages):
    """Convert OpenAI-style messages to Anthropic batch params format."""
    system_messages = [m for m in messages if m.get("role") == "system"]
    system_text = "\n\n".join([
        m["content"][0]["text"] if isinstance(m.get("content"), list)
        else m["content"]
        for m in system_messages if m.get("content")
    ])
    system_param = ([{"type": "text", "text": system_text}]
                    if system_text else [])

    anthropic_messages = []
    for msg in messages:
        if msg["role"] in ["user", "assistant"]:
            content = msg["content"]
            if isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and "text" in content[0]:
                    content = content[0]["text"]
            anthropic_messages.append({"role": msg["role"],
                                       "content": content})
    return system_param, anthropic_messages


def batch_submit(requests_by_provider, api_keys, phase=None):
    """Submit batch requests grouped by provider.

    requests_by_provider: dict mapping provider -> list of
        {"custom_id": str, "model_id": str, "api_model": str,
         "messages": list, "max_tokens": int}

    Returns dict of batch IDs keyed by provider (+ model for OpenAI).
    """
    from anthropic import Anthropic
    from openai import OpenAI

    batch_ids = {}

    # --- Anthropic: single batch with all requests ---
    anthropic_reqs = requests_by_provider.get("anthropic", [])
    if anthropic_reqs:
        try:
            client = Anthropic(api_key=api_keys["anthropic"])
            batch_requests = []
            for req in anthropic_reqs:
                system_param, ant_messages = (
                    _convert_messages_for_anthropic(req["messages"]))
                params = {
                    "model": req["api_model"],
                    "max_tokens": req.get("max_tokens", 10000),
                    "messages": ant_messages,
                }
                if system_param:
                    params["system"] = system_param
                batch_requests.append({
                    "custom_id": req["custom_id"],
                    "params": params,
                })
            batch = client.messages.batches.create(
                requests=batch_requests)
            batch_ids["anthropic"] = batch.id
            log.info(f"Anthropic batch submitted: {batch.id} "
                     f"({len(batch_requests)} requests)")
        except Exception as e:
            log.error(f"Anthropic batch submission failed: {e}")

    # --- OpenAI: one batch per model (all requests must share a model) ---
    openai_reqs = requests_by_provider.get("openai", [])
    if openai_reqs:
        client = OpenAI(api_key=api_keys["openai"])
        # Group by api_model
        by_model = {}
        for req in openai_reqs:
            by_model.setdefault(req["api_model"], []).append(req)

        for oai_model, reqs in by_model.items():
            try:
                # Write JSONL to a temp file
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".jsonl", delete=False
                ) as tmp:
                    for req in reqs:
                        line = {
                            "custom_id": req["custom_id"],
                            "method": "POST",
                            "url": "/v1/chat/completions",
                            "body": {
                                "model": oai_model,
                                "messages": req["messages"],
                                "max_completion_tokens": req.get(
                                    "max_tokens", 10000),
                                "temperature": 1,
                            },
                        }
                        tmp.write(json.dumps(line) + "\n")
                    tmp_path = tmp.name

                with open(tmp_path, "rb") as f:
                    uploaded = client.files.create(file=f, purpose="batch")
                os.unlink(tmp_path)

                batch = client.batches.create(
                    input_file_id=uploaded.id,
                    endpoint="/v1/chat/completions",
                    completion_window="24h",
                )
                key = f"openai:{oai_model}"
                batch_ids[key] = batch.id
                log.info(f"OpenAI batch submitted for {oai_model}: "
                         f"{batch.id} ({len(reqs)} requests)")
            except Exception as e:
                log.error(f"OpenAI batch submission failed for "
                          f"{oai_model}: {e}")
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

    return batch_ids


def batch_check_and_collect(batch_ids, api_keys):
    """Check batch statuses and collect completed results.

    Returns (results_by_custom_id, still_pending, batch_durations) where:
        results_by_custom_id: dict mapping custom_id -> result dict with
            "content", "input_tokens", "output_tokens"
        still_pending: dict of batch_ids still processing
        batch_durations: dict mapping provider key -> duration in seconds
    """
    from anthropic import Anthropic
    from openai import OpenAI

    results = {}
    still_pending = {}
    batch_durations = {}

    for key, batch_id in batch_ids.items():
        if key == "anthropic":
            client = Anthropic(api_key=api_keys["anthropic"])
            batch = client.messages.batches.retrieve(batch_id)
            log.info(f"Anthropic batch {batch_id}: "
                     f"status={batch.processing_status}, "
                     f"counts={batch.request_counts}")
            if batch.processing_status != "ended":
                still_pending[key] = batch_id
                continue
            # Compute batch duration
            if batch.created_at and batch.ended_at:
                duration = (batch.ended_at - batch.created_at
                            ).total_seconds()
                batch_durations[key] = round(duration, 1)
                log.info(f"Anthropic batch duration: {duration:.0f}s "
                         f"({duration/60:.1f}min)")
            # Collect results
            for entry in client.messages.batches.results(batch_id):
                cid = entry.custom_id
                if entry.result.type == "succeeded":
                    msg = entry.result.message
                    content = ""
                    for block in msg.content:
                        if hasattr(block, "text"):
                            content += block.text
                    results[cid] = {
                        "content": content,
                        "input_tokens": msg.usage.input_tokens,
                        "output_tokens": msg.usage.output_tokens,
                    }
                else:
                    log.warning(f"Anthropic batch item {cid}: "
                                f"type={entry.result.type}")

        elif key.startswith("openai:"):
            client = OpenAI(api_key=api_keys["openai"])
            batch = client.batches.retrieve(batch_id)
            log.info(f"OpenAI batch {batch_id}: status={batch.status}, "
                     f"counts={batch.request_counts}")
            if batch.status not in ("completed", "failed", "expired",
                                    "cancelled"):
                still_pending[key] = batch_id
                continue
            if batch.status != "completed":
                log.error(f"OpenAI batch {batch_id} ended with "
                          f"status={batch.status}")
                continue
            # Compute batch duration
            if batch.created_at and batch.completed_at:
                duration = batch.completed_at - batch.created_at
                batch_durations[key] = round(duration, 1)
                log.info(f"OpenAI batch {key} duration: {duration:.0f}s "
                         f"({duration/60:.1f}min)")
            # Download results
            content_bytes = client.files.content(
                batch.output_file_id).content
            for line in content_bytes.decode().strip().split("\n"):
                entry = json.loads(line)
                cid = entry["custom_id"]
                resp = entry.get("response", entry.get("result", {}))
                body = resp.get("body", {})
                if resp.get("status_code") == 200 and body.get("choices"):
                    results[cid] = {
                        "content": body["choices"][0]["message"]["content"],
                        "input_tokens": body["usage"]["prompt_tokens"],
                        "output_tokens": body["usage"][
                            "completion_tokens"],
                    }
                else:
                    log.warning(f"OpenAI batch item {cid}: "
                                f"status={resp.get('status_code')}")

    return results, still_pending, batch_durations
