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


# ── single-request Anthropic batch round-trip ──────────────────────────────
# Used by generate_llm_response for prompts that carry {ca_tweets}: one
# conversation turn submitted as a one-item batch (half price, no latency
# requirement), polled by the same Celery task via retry/countdown.

def anthropic_batch_submit_one(api_key, custom_id, api_model, messages,
                               max_tokens, output_schema=None):
    """Submit ONE conversation as a one-item Anthropic batch. ``messages``
    are OpenAI-style (system messages collapse into the system param).
    ``output_schema`` (a JSON schema) constrains the reply to that shape
    via structured outputs. Returns the batch id."""
    from anthropic import Anthropic
    system_param, ant_messages = _convert_messages_for_anthropic(messages)
    params = {"model": api_model, "max_tokens": max_tokens,
              "messages": ant_messages}
    if system_param:
        params["system"] = system_param
    if output_schema:
        params["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema}}
    client = Anthropic(api_key=api_key)
    batch = client.messages.batches.create(
        requests=[{"custom_id": custom_id, "params": params}])
    log.info("Anthropic one-item batch submitted: %s (%s)", batch.id,
             custom_id)
    return batch.id


def anthropic_batch_collect_one(api_key, batch_id, custom_id):
    """Poll a one-item batch. Returns (processing_status, resp) where resp
    is None until the batch has ended, and otherwise the same dict shape
    LLMProvider._call_anthropic returns (plus ``batch: True``) so the
    caller's finalize path is unchanged. Raises RuntimeError when the
    item errored / expired / was canceled."""
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    batch = client.messages.batches.retrieve(batch_id)
    log.info("Anthropic batch %s: status=%s counts=%s", batch_id,
             batch.processing_status, batch.request_counts)
    if batch.processing_status != "ended":
        return batch.processing_status, None
    for entry in client.messages.batches.results(batch_id):
        if entry.custom_id != custom_id:
            continue
        if entry.result.type != "succeeded":
            err = getattr(entry.result, "error", None)
            raise RuntimeError(
                f"Batch item {custom_id} {entry.result.type}: {err}")
        msg = entry.result.message
        content = "".join(
            block.text for block in msg.content if hasattr(block, "text"))
        usage = msg.usage
        return "ended", {
            "content": content,
            "total_tokens": usage.input_tokens + usage.output_tokens,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(
                usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(
                usage, "cache_creation_input_tokens", 0) or 0,
            "tool_calls": [],
            "truncated": msg.stop_reason == "max_tokens",
            "batch": True,
            "batch_id": batch_id,
        }
    raise RuntimeError(f"Batch {batch_id} ended without item {custom_id}")


def _responses_input(messages):
    """OpenAI-style messages → Responses API input items (the same
    conversion LLMProvider._call_openai does for the live call)."""
    items = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, list):
            block_type = ("output_text" if msg["role"] == "assistant"
                          else "input_text")
            content = [{"type": block_type,
                        "text": (b["text"] if isinstance(b, dict) and "text" in b
                                 else str(b))} for b in content]
        items.append({"role": msg["role"], "content": content})
    return items


def openai_batch_submit_one(api_key, custom_id, api_model, messages,
                            max_tokens, output_schema=None):
    """Submit ONE conversation as a one-item OpenAI batch against the
    Responses endpoint (the live path uses Responses too, so the input
    shape and model support match). ``output_schema`` becomes a strict
    JSON-schema text format. Returns the batch id."""
    from openai import OpenAI
    body = {
        "model": api_model,
        "input": _responses_input(messages),
        "max_output_tokens": max_tokens,
        "store": False,
    }
    if output_schema:
        body["text"] = {"format": {
            "type": "json_schema", "name": "feed_reply",
            "schema": output_schema, "strict": True}}
    line = {"custom_id": custom_id, "method": "POST",
            "url": "/v1/responses", "body": body}
    client = OpenAI(api_key=api_key)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                     delete=False) as tmp:
        tmp.write(json.dumps(line) + "\n")
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            uploaded = client.files.create(file=f, purpose="batch")
    finally:
        os.unlink(tmp_path)
    batch = client.batches.create(
        input_file_id=uploaded.id, endpoint="/v1/responses",
        completion_window="24h")
    log.info("OpenAI one-item batch submitted: %s (%s)", batch.id, custom_id)
    return batch.id


def openai_batch_collect_one(api_key, batch_id, custom_id):
    """Poll a one-item OpenAI batch. Returns (status, resp) — resp None
    until the batch completed, else the dict shape the live OpenAI call
    returns (plus ``batch: True``). Raises RuntimeError when the batch
    failed / expired / was cancelled or the item errored."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    batch = client.batches.retrieve(batch_id)
    log.info("OpenAI batch %s: status=%s counts=%s", batch_id,
             batch.status, batch.request_counts)
    if batch.status in ("failed", "expired", "cancelled", "cancelling"):
        raise RuntimeError(f"OpenAI batch {batch_id} {batch.status}")
    if batch.status != "completed":
        return batch.status, None
    if not batch.output_file_id:
        raise RuntimeError(f"OpenAI batch {batch_id} completed without output")
    raw = client.files.content(batch.output_file_id).content.decode()
    for line in raw.strip().splitlines():
        entry = json.loads(line)
        if entry.get("custom_id") != custom_id:
            continue
        response = entry.get("response") or {}
        body = response.get("body") or {}
        if response.get("status_code") != 200 or entry.get("error"):
            raise RuntimeError(
                f"Batch item {custom_id} failed: "
                f"{entry.get('error') or body.get('error')}")
        content = ""
        for item in body.get("output") or []:
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if block.get("type") == "output_text":
                    content += block.get("text") or ""
        usage = body.get("usage") or {}
        in_toks = usage.get("input_tokens", 0)
        out_toks = usage.get("output_tokens", 0)
        cached = (usage.get("input_tokens_details") or {}).get(
            "cached_tokens", 0) or 0
        return "completed", {
            "content": content,
            "total_tokens": in_toks + out_toks,
            "input_tokens": in_toks,
            "output_tokens": out_toks,
            "cached_tokens": cached,
            "tool_calls": [],
            "truncated": body.get("status") == "incomplete",
            "batch": True,
            "batch_id": batch_id,
        }
    raise RuntimeError(f"Batch {batch_id} ended without item {custom_id}")
