"""
Prompt RCT (Randomized Controlled Trial) — Flask CLI commands.

Compare prompt variants across models with blind evaluation and Borda count.

Usage:
    flask rct estimate      # Cost estimate + set shuffle count
    flask rct generate      # Phase 1: generate responses
    flask rct evaluate      # Phase 2: blind evaluation
    flask rct aggregate     # Phase 3: Borda count + summary
    flask rct run-all       # All phases sequentially
    flask rct archive       # Archive results + config, reset for next run

Batch mode (50% cheaper, async within 24h):
    flask rct generate --batch          # Submit generation requests
    flask rct generate --batch-collect  # Check status / collect results
    flask rct evaluate --batch          # Submit evaluation requests
    flask rct evaluate --batch-collect  # Check status / collect results
    Set "use_batch": true in config.json to use batch mode by default.
"""
import json
import logging
import os
import random
import re
import shutil
import string
import tempfile
import time
from datetime import datetime

import click
from flask import current_app
from flask.cli import AppGroup, with_appcontext

from backend.llm_providers import LLMProvider, PromptTooLongError
from backend.models import Node, User
from backend.tasks.llm_completion import get_user_profile_content
from backend.utils.api_keys import get_api_keys_for_usage
from backend.utils.cost import calculate_llm_cost_microdollars

RCT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(RCT_DIR, "config.json")
RESULTS_DIR = os.path.join(RCT_DIR, "results")
ARCHIVE_DIR = os.path.join(RCT_DIR, "archive")
PROMPTS_DIR = os.path.join(RCT_DIR, "prompts")

rct_cli = AppGroup("rct", help="Prompt RCT experiment commands.")

# Module-level logger, configured per-command via setup_logging()
log = logging.getLogger("rct")


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(phase_name):
    """Configure logger to write to both console and a log file in results/."""
    ensure_dir(RESULTS_DIR)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(RESULTS_DIR, f"{phase_name}_{timestamp}.log")

    # Reset handlers (avoid duplicates on repeated calls)
    log.handlers.clear()
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    # File handler
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    log.info(f"Logging to {log_file}")
    return log_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def parse_node_id(raw):
    """Accept bare int or URL like https://loore.org/node/123."""
    s = str(raw).strip().rstrip("/")
    # Try extracting from URL
    m = re.search(r'/node/(\d+)', s)
    if m:
        return int(m.group(1))
    return int(s)


def load_prompt_variant(filename):
    path = os.path.join(RCT_DIR, "prompts", filename)
    with open(path) as f:
        return f.read().strip()


def load_eval_prompt():
    path = os.path.join(RCT_DIR, "eval_prompts", "compare.txt")
    with open(path) as f:
        return f.read().strip()


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def result_path(phase, node_id, filename):
    d = os.path.join(RESULTS_DIR, phase, f"node_{node_id}")
    ensure_dir(d)
    return os.path.join(d, filename)


def model_slug(model_id):
    """Short filesystem-safe slug for a model id."""
    return model_id.replace(".", "").replace("-", "_")


def variant_slug(variant_file):
    """e.g. variant_1.txt -> v1"""
    m = re.search(r'(\d+)', variant_file)
    return f"v{m.group(1)}" if m else variant_file.replace(".txt", "")


def get_api_keys(cfg):
    """Get API keys using key type from config (default: chat)."""
    key_type = cfg.get("api_key_type", "chat")
    return get_api_keys_for_usage(current_app.config, key_type)


def get_batch_api_keys(cfg):
    """Get API keys for batch mode, using batch-specific keys if available."""
    keys = get_api_keys(cfg)
    # Override with batch-specific OpenAI key if configured
    batch_oai_key = current_app.config.get("OPENAI_API_KEY_BATCH")
    if batch_oai_key:
        keys["openai"] = batch_oai_key
    return keys


def resolve_user_profile(owner_username):
    """Fetch the owner's latest user profile content. Returns (content, user_id) or (None, None)."""
    user = User.query.filter_by(username=owner_username).first()
    if not user:
        return None, None
    content = get_user_profile_content(user.id)
    return content, user.id


def apply_prompt_placeholders(prompt_text, user_profile):
    """Substitute {user_profile} placeholder in prompt text."""
    if "{user_profile}" in prompt_text:
        prompt_text = prompt_text.replace("{user_profile}", user_profile or "")
    return prompt_text


def validate_node_ownership(node_ids, owner_username):
    """Check all nodes belong to the given user. Returns (valid_ids, errors)."""
    user = User.query.filter_by(username=owner_username).first()
    if not user:
        return [], [f"User '{owner_username}' not found"]
    errors = []
    valid = []
    for nid in node_ids:
        node = Node.query.get(nid)
        if not node:
            errors.append(f"Node {nid} not found")
        elif node.user_id != user.id:
            errors.append(f"Node {nid} does not belong to '{owner_username}'")
        else:
            valid.append(nid)
    return valid, errors


def estimate_tokens(text):
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def fmt_cost(microdollars):
    return f"${microdollars / 1_000_000:.4f}"


# ---------------------------------------------------------------------------
# Phase 0: Estimate
# ---------------------------------------------------------------------------

@rct_cli.command("estimate")
@with_appcontext
def estimate_cmd():
    """Estimate cost and interactively set shuffle count."""
    setup_logging("estimate")
    cfg = load_config()
    node_ids = [parse_node_id(n) for n in cfg["node_ids"]]
    gen_models = cfg["generation_models"]
    eval_models = cfg["evaluation_models"]
    variants = cfg["prompt_variants"]

    if not node_ids:
        log.error("No node_ids in config.json")
        return

    # Validate node ownership before any content access
    owner = cfg.get("owner")
    if not owner:
        log.error("'owner' not set in config.json")
        return
    valid_ids, errors = validate_node_ownership(node_ids, owner)
    if errors:
        for e in errors:
            log.error(e)
        if not valid_ids:
            return
    node_ids = valid_ids

    # Fetch node content to estimate input tokens
    log.info(f"Fetching {len(node_ids)} nodes...")
    node_texts = {}
    for nid in node_ids:
        node = Node.query.get(nid)
        if not node:
            log.warning(f"Node {nid} not found, skipping")
            continue
        node_texts[nid] = node.get_content()

    if not node_texts:
        log.error("No valid nodes found")
        return

    avg_node_tokens = sum(estimate_tokens(t) for t in node_texts.values()) // len(node_texts)
    est_output_tokens = 1000  # default output estimate

    # Resolve user profile for token estimation
    user_profile, _ = resolve_user_profile(owner)
    profile_tokens = estimate_tokens(user_profile) if user_profile else 0

    # Load actual prompt variants, substitute {user_profile}, then estimate
    prompt_tokens = {}
    for vfile in variants:
        try:
            raw = load_prompt_variant(vfile)
            resolved = apply_prompt_placeholders(raw, user_profile)
            prompt_tokens[vfile] = estimate_tokens(resolved)
        except FileNotFoundError:
            log.warning(f"Prompt file {vfile} not found")
            prompt_tokens[vfile] = 500  # fallback
    avg_prompt_tokens = sum(prompt_tokens.values()) // max(len(prompt_tokens), 1)

    # Load eval prompt for token estimate
    try:
        eval_prompt_tokens = estimate_tokens(load_eval_prompt())
    except FileNotFoundError:
        log.warning("Eval prompt not found")
        eval_prompt_tokens = 500

    n_nodes = len(node_texts)
    n_variants = len(variants)
    n_gen_models = len(gen_models)
    n_eval_models = len(eval_models)

    log.info(f"Avg node: ~{avg_node_tokens} tokens")
    log.info(f"User profile: ~{profile_tokens} tokens")
    log.info(f"Avg prompt variant (with profile): ~{avg_prompt_tokens} tokens")
    log.info(f"Est. output: ~{est_output_tokens} tokens")

    # Generation cost
    n_gen_calls = n_nodes * n_variants * n_gen_models
    gen_input = avg_node_tokens + avg_prompt_tokens
    log.info("=== Generation ===")
    log.info(f"  {n_nodes} nodes x {n_variants} variants x {n_gen_models} models = {n_gen_calls} calls")
    log.info(f"  ~{gen_input} in + ~{est_output_tokens} out tokens/call")
    gen_cost_total = 0
    for mid in gen_models:
        cost = calculate_llm_cost_microdollars(mid, gen_input, est_output_tokens)
        model_cost = cost * n_nodes * n_variants
        gen_cost_total += model_cost
        log.info(f"  {mid}: ~{fmt_cost(cost)}/call, ~{fmt_cost(model_cost)} total")
    log.info(f"  Generation total: ~{fmt_cost(gen_cost_total)}")

    # Evaluation cost (per shuffle)
    n_responses = n_variants * n_gen_models
    eval_input = avg_node_tokens + est_output_tokens * n_responses + eval_prompt_tokens
    n_eval_calls_per_shuffle = n_nodes * n_eval_models
    log.info("=== Evaluation (per shuffle) ===")
    log.info(f"  {n_nodes} nodes x {n_eval_models} eval models = {n_eval_calls_per_shuffle} calls/shuffle")
    log.info(f"  ~{eval_input} in + ~{est_output_tokens} out tokens/call")
    eval_cost_per_shuffle = 0
    for mid in eval_models:
        cost = calculate_llm_cost_microdollars(mid, eval_input, est_output_tokens)
        model_cost = cost * n_nodes
        eval_cost_per_shuffle += model_cost
        log.info(f"  {mid}: ~{fmt_cost(cost)}/call, ~{fmt_cost(model_cost)}/shuffle")
    log.info(f"  Per shuffle total: ~{fmt_cost(eval_cost_per_shuffle)}")

    # Interactive: ask for shuffle count
    default_shuffles = cfg.get("shuffles", 1)
    shuffles = click.prompt(
        "How many evaluation shuffles?",
        type=int,
        default=default_shuffles,
    )

    total_eval_cost = eval_cost_per_shuffle * shuffles
    total_cost = gen_cost_total + total_eval_cost

    batch_total = total_cost // 2

    log.info(f"=== Total Estimate ({shuffles} shuffle(s)) ===")
    log.info(f"  Generation:  {fmt_cost(gen_cost_total)}")
    log.info(f"  Evaluation:  {fmt_cost(total_eval_cost)}")
    log.info(f"  TOTAL (sync):   {fmt_cost(total_cost)}")
    log.info(f"  TOTAL (batch):  {fmt_cost(batch_total)}  (50% off)")

    # Save shuffle count
    cfg["shuffles"] = shuffles
    save_config(cfg)

    # Save metadata
    ensure_dir(RESULTS_DIR)
    metadata = {
        "node_ids": node_ids,
        "generation_models": gen_models,
        "evaluation_models": eval_models,
        "prompt_variants": variants,
        "shuffles": shuffles,
        "estimated_cost_microdollars": total_cost,
    }
    with open(os.path.join(RESULTS_DIR, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")

    log.info(f"Saved shuffles={shuffles} to config.json and metadata.json")


# ---------------------------------------------------------------------------
# Batch API helpers
# ---------------------------------------------------------------------------

BATCH_STATE_PATH = os.path.join(RESULTS_DIR, "batch_state.json")


def load_batch_state():
    if os.path.exists(BATCH_STATE_PATH):
        with open(BATCH_STATE_PATH) as f:
            return json.load(f)
    return {}


def save_batch_state(state):
    ensure_dir(RESULTS_DIR)
    with open(BATCH_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def get_model_provider(model_id):
    """Return (provider, api_model) for a model_id."""
    config = current_app.config["SUPPORTED_MODELS"].get(model_id)
    if not config:
        raise ValueError(f"Unsupported model: {model_id}")
    return config["provider"], config["api_model"]


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


def batch_submit(requests_by_provider, api_keys, phase):
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

    Returns (results_by_custom_id, still_pending) where:
        results_by_custom_id: dict mapping custom_id -> result dict with
            "content", "input_tokens", "output_tokens"
        still_pending: dict of batch_ids still processing
    """
    from anthropic import Anthropic
    from openai import OpenAI

    results = {}
    still_pending = {}

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

    return results, still_pending


# ---------------------------------------------------------------------------
# Phase 1: Generate
# ---------------------------------------------------------------------------

@rct_cli.command("generate")
@click.option("--batch", "batch_mode", is_flag=True,
              help="Submit requests via Batch API (50% cheaper, async).")
@click.option("--batch-collect", "batch_collect", is_flag=True,
              help="Check status / collect results from a previous --batch.")
@with_appcontext
def generate_cmd(batch_mode, batch_collect):
    """Generate responses for all node x variant x model combinations."""
    setup_logging("generate")
    cfg = load_config()

    # --batch flag or config-level use_batch
    use_batch = batch_mode or cfg.get("use_batch", False)

    if batch_collect:
        _generate_batch_collect(cfg)
        return

    node_ids = [parse_node_id(n) for n in cfg["node_ids"]]
    gen_models = cfg["generation_models"]
    variants = cfg["prompt_variants"]
    key_type = cfg.get("api_key_type", "chat")

    owner = cfg.get("owner")
    if not owner:
        log.error("'owner' not set in config.json")
        return
    node_ids, errors = validate_node_ownership(node_ids, owner)
    if errors:
        for e in errors:
            log.error(e)
        if not node_ids:
            return

    # Resolve user profile for {user_profile} placeholder
    user_profile, _ = resolve_user_profile(owner)
    if user_profile:
        log.info(f"User profile: {len(user_profile)} chars")
    else:
        log.warning("User profile: not available (placeholders will be empty)")

    total = len(node_ids) * len(variants) * len(gen_models)
    mode_label = "BATCH" if use_batch else "sync"
    log.info(f"API key type: {key_type} | {total} calls across "
             f"{len(gen_models)} models [{mode_label}]")
    if not click.confirm("Proceed with generation?", default=True):
        return

    api_keys = get_batch_api_keys(cfg) if use_batch else get_api_keys(cfg)

    if use_batch:
        _generate_batch_submit(cfg, node_ids, gen_models, variants,
                               user_profile, api_keys)
        return

    # --- Synchronous mode (original) ---
    done = 0
    skipped = 0
    err_count = 0

    for nid in node_ids:
        node = Node.query.get(nid)
        if not node:
            log.warning(f"Node {nid} not found, skipping")
            done += len(variants) * len(gen_models)
            continue
        node_text = node.get_content()

        for vfile in variants:
            raw_prompt = load_prompt_variant(vfile)
            prompt_text = apply_prompt_placeholders(raw_prompt, user_profile)
            vs = variant_slug(vfile)

            for mid in gen_models:
                done += 1
                ms = model_slug(mid)
                out_file = result_path("generation", nid, f"{vs}_{ms}.json")

                if os.path.exists(out_file):
                    skipped += 1
                    log.debug(f"[{done}/{total}] node {nid}, {vfile}, {mid} ... skipped (exists)")
                    continue

                messages = [
                    {"role": "system", "content": [{"type": "text", "text": prompt_text}]},
                    {"role": "user", "content": [{"type": "text", "text": node_text}]},
                ]

                t0 = time.time()
                try:
                    result = LLMProvider.get_completion(mid, messages, api_keys)
                except PromptTooLongError as e:
                    log.error(f"[{done}/{total}] node {nid}, {vfile}, {mid} ... {e}")
                    err_count += 1
                    continue
                except Exception as e:
                    log.error(f"[{done}/{total}] node {nid}, {vfile}, {mid} ... {e}")
                    err_count += 1
                    continue

                elapsed = time.time() - t0
                cost = calculate_llm_cost_microdollars(
                    mid, result["input_tokens"], result["output_tokens"]
                )

                output = {
                    "node_id": nid,
                    "variant": vfile,
                    "model": mid,
                    "prompt_template": raw_prompt,
                    "prompt_used": prompt_text,
                    "user_profile": user_profile,
                    "node_text": node_text,
                    "response": result["content"],
                    "input_tokens": result["input_tokens"],
                    "output_tokens": result["output_tokens"],
                    "actual_cost_microdollars": cost,
                    "elapsed_seconds": round(elapsed, 2),
                }
                with open(out_file, "w") as f:
                    json.dump(output, f, indent=2)
                    f.write("\n")

                log.info(
                    f"[{done}/{total}] node {nid}, {vfile}, {mid} "
                    f"... done ({elapsed:.1f}s, {fmt_cost(cost)})"
                )

    log.info(f"Generation complete. {done} total, {skipped} skipped, {err_count} errors.")


def _generate_batch_submit(cfg, node_ids, gen_models, variants,
                           user_profile, api_keys):
    """Build and submit generation requests as provider batches."""
    state = load_batch_state()
    gen_state = state.get("generation", {})
    existing_batch_ids = gen_state.get("batch_ids", {})
    existing_meta = gen_state.get("request_meta", {})

    # Determine which providers already have pending batches
    pending_providers = set()
    if "anthropic" in existing_batch_ids:
        pending_providers.add("anthropic")
    for key in existing_batch_ids:
        if key.startswith("openai:"):
            pending_providers.add("openai")

    if pending_providers:
        log.info(f"Existing pending batches for: "
                 f"{', '.join(sorted(pending_providers))} (skipping)")

    requests_by_provider = {}
    request_meta = {}
    skipped = 0

    for nid in node_ids:
        node = Node.query.get(nid)
        if not node:
            log.warning(f"Node {nid} not found, skipping")
            continue
        node_text = node.get_content()

        for vfile in variants:
            raw_prompt = load_prompt_variant(vfile)
            prompt_text = apply_prompt_placeholders(raw_prompt, user_profile)
            vs = variant_slug(vfile)

            for mid in gen_models:
                ms = model_slug(mid)
                out_file = result_path("generation", nid,
                                       f"{vs}_{ms}.json")
                if os.path.exists(out_file):
                    skipped += 1
                    continue

                provider, api_model = get_model_provider(mid)
                cid = f"gen_node{nid}_{vs}_{ms}"

                messages = [
                    {"role": "system",
                     "content": [{"type": "text", "text": prompt_text}]},
                    {"role": "user",
                     "content": [{"type": "text", "text": node_text}]},
                ]

                # Always build metadata (needed for --batch-collect)
                request_meta[cid] = {
                    "node_id": nid,
                    "variant": vfile,
                    "model": mid,
                    "prompt_template": raw_prompt,
                    "prompt_used": prompt_text,
                    "user_profile": user_profile,
                    "node_text": node_text,
                    "out_file": out_file,
                }

                # Only queue for submission if not already pending
                if provider in pending_providers:
                    continue

                requests_by_provider.setdefault(provider, []).append({
                    "custom_id": cid,
                    "model_id": mid,
                    "api_model": api_model,
                    "messages": messages,
                    "max_tokens": 10000,
                })

    total_queued = sum(len(v) for v in requests_by_provider.values())
    if total_queued == 0:
        log.info(f"Nothing to submit ({skipped} already exist).")
        return

    log.info(f"Submitting {total_queued} requests as batch "
             f"({skipped} skipped existing)...")
    batch_ids = batch_submit(requests_by_provider, api_keys, "generation")

    # Merge with existing state
    existing_batch_ids.update(batch_ids)
    existing_meta.update(request_meta)
    state["generation"] = {
        "batch_ids": existing_batch_ids,
        "request_meta": existing_meta,
        "submitted_at": datetime.now().isoformat(),
    }
    save_batch_state(state)

    log.info("Batch submitted. Run `flask rct generate --batch-collect` "
             "to check/collect results.")


def _generate_batch_collect(cfg):
    """Check and collect generation batch results."""
    state = load_batch_state()
    gen_state = state.get("generation")
    if not gen_state:
        log.error("No generation batch state found. "
                  "Run `flask rct generate --batch` first.")
        return

    api_keys = get_batch_api_keys(cfg)
    batch_ids = gen_state["batch_ids"]
    request_meta = gen_state["request_meta"]

    results, still_pending = batch_check_and_collect(batch_ids, api_keys)

    if still_pending:
        log.info(f"Still processing: {list(still_pending.keys())}. "
                 "Run --batch-collect again later.")
        # Update state with only pending batches
        gen_state["batch_ids"] = still_pending
        save_batch_state(state)

    # Write collected results to individual files
    written = 0
    errors = 0
    for cid, result in results.items():
        meta = request_meta.get(cid)
        if not meta:
            log.warning(f"No metadata for custom_id {cid}")
            errors += 1
            continue

        out_file = meta["out_file"]
        if os.path.exists(out_file):
            continue

        cost = calculate_llm_cost_microdollars(
            meta["model"], result["input_tokens"], result["output_tokens"]
        )
        # Apply 50% batch discount
        cost = cost // 2

        output = {
            "node_id": meta["node_id"],
            "variant": meta["variant"],
            "model": meta["model"],
            "prompt_template": meta["prompt_template"],
            "prompt_used": meta["prompt_used"],
            "user_profile": meta["user_profile"],
            "node_text": meta["node_text"],
            "response": result["content"],
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "actual_cost_microdollars": cost,
            "batch_mode": True,
        }
        ensure_dir(os.path.dirname(out_file))
        with open(out_file, "w") as f:
            json.dump(output, f, indent=2)
            f.write("\n")
        written += 1

    log.info(f"Collected {written} results, {errors} errors.")

    if not still_pending:
        # Clean up generation batch state
        del state["generation"]
        save_batch_state(state)
        log.info("All generation batches complete.")


# ---------------------------------------------------------------------------
# Phase 2: Evaluate
# ---------------------------------------------------------------------------

@rct_cli.command("evaluate")
@click.option("--batch", "batch_mode", is_flag=True,
              help="Submit requests via Batch API (50% cheaper, async).")
@click.option("--batch-collect", "batch_collect", is_flag=True,
              help="Check status / collect results from a previous --batch.")
@with_appcontext
def evaluate_cmd(batch_mode, batch_collect):
    """Run blind evaluations with shuffled response labels."""
    setup_logging("evaluate")
    cfg = load_config()

    use_batch = batch_mode or cfg.get("use_batch", False)

    if batch_collect:
        _evaluate_batch_collect(cfg)
        return

    node_ids = [parse_node_id(n) for n in cfg["node_ids"]]
    eval_models = cfg["evaluation_models"]
    gen_models = cfg["generation_models"]
    variants = cfg["prompt_variants"]
    shuffles = cfg.get("shuffles", 1)
    key_type = cfg.get("api_key_type", "chat")

    owner = cfg.get("owner")
    if not owner:
        log.error("'owner' not set in config.json")
        return
    node_ids, errors = validate_node_ownership(node_ids, owner)
    if errors:
        for e in errors:
            log.error(e)
        if not node_ids:
            return

    total = len(node_ids) * len(eval_models) * shuffles
    mode_label = "BATCH" if use_batch else "sync"
    log.info(f"API key type: {key_type} | {total} calls across "
             f"{len(eval_models)} eval models, "
             f"{shuffles} shuffle(s) [{mode_label}]")
    if not click.confirm("Proceed with evaluation?", default=True):
        return

    api_keys = get_batch_api_keys(cfg) if use_batch else get_api_keys(cfg)
    eval_prompt_template = load_eval_prompt()

    if use_batch:
        _evaluate_batch_submit(cfg, node_ids, eval_models, gen_models,
                               variants, shuffles, eval_prompt_template,
                               api_keys)
        return

    # --- Synchronous mode (original) ---
    done = 0
    skipped = 0
    err_count = 0

    for nid in node_ids:
        # Load all generation results for this node
        gen_results = []
        for vfile in variants:
            vs = variant_slug(vfile)
            for mid in gen_models:
                ms = model_slug(mid)
                gen_file = result_path("generation", nid, f"{vs}_{ms}.json")
                if not os.path.exists(gen_file):
                    log.warning(f"Missing generation {gen_file}")
                    continue
                with open(gen_file) as f:
                    gen_results.append(json.load(f))

        if not gen_results:
            log.warning(f"No generation results for node {nid}, skipping")
            done += len(eval_models) * shuffles
            continue

        node_text = gen_results[0]["node_text"]

        for shuffle_idx in range(shuffles):
            for eval_mid in eval_models:
                done += 1
                ems = model_slug(eval_mid)
                out_file = result_path(
                    "evaluation", nid, f"eval_{ems}_shuffle{shuffle_idx}.json"
                )

                if os.path.exists(out_file):
                    skipped += 1
                    log.debug(f"[{done}/{total}] node {nid}, {eval_mid}, shuffle {shuffle_idx} ... skipped")
                    continue

                # Shuffle and assign labels (deterministic, stable seed)
                model_hash = int.from_bytes(eval_mid.encode(), "big") % 1000
                shuffle_seed = nid * 1000000 + shuffle_idx * 1000 + model_hash
                rng = random.Random(shuffle_seed)
                shuffled = list(gen_results)
                rng.shuffle(shuffled)
                labels = list(string.ascii_uppercase[:len(shuffled)])

                label_map = {}
                responses_text = []
                for label, gr in zip(labels, shuffled):
                    label_map[label] = {
                        "variant": gr["variant"],
                        "model": gr["model"],
                    }
                    responses_text.append(f"### Response {label}\n\n{gr['response']}")

                eval_prompt = eval_prompt_template.format(
                    node_text=node_text,
                    responses="\n\n---\n\n".join(responses_text),
                )

                messages = [
                    {"role": "user", "content": [{"type": "text", "text": eval_prompt}]},
                ]

                t0 = time.time()
                eval_max_tokens = cfg.get("eval_max_tokens", 1000)
                try:
                    result = LLMProvider.get_completion(
                        eval_mid, messages, api_keys,
                        max_tokens=eval_max_tokens)
                except Exception as e:
                    log.error(f"[{done}/{total}] node {nid}, {eval_mid}, shuffle {shuffle_idx} ... {e}")
                    err_count += 1
                    continue

                elapsed = time.time() - t0
                cost = calculate_llm_cost_microdollars(
                    eval_mid, result["input_tokens"], result["output_tokens"]
                )

                output = {
                    "node_id": nid,
                    "evaluator_model": eval_mid,
                    "shuffle_index": shuffle_idx,
                    "shuffle_seed": shuffle_seed,
                    "eval_prompt": eval_prompt,
                    "label_map": label_map,
                    "evaluation": result["content"],
                    "input_tokens": result["input_tokens"],
                    "output_tokens": result["output_tokens"],
                    "actual_cost_microdollars": cost,
                    "elapsed_seconds": round(elapsed, 2),
                }
                with open(out_file, "w") as f:
                    json.dump(output, f, indent=2)
                    f.write("\n")

                log.info(
                    f"[{done}/{total}] node {nid}, {eval_mid}, shuffle {shuffle_idx} "
                    f"... done ({elapsed:.1f}s, {fmt_cost(cost)})"
                )

    log.info(f"Evaluation complete. {done} total, {skipped} skipped, {err_count} errors.")


def _evaluate_batch_submit(cfg, node_ids, eval_models, gen_models,
                           variants, shuffles, eval_prompt_template,
                           api_keys):
    """Build and submit evaluation requests as provider batches."""
    state = load_batch_state()
    eval_state = state.get("evaluation", {})
    existing_batch_ids = eval_state.get("batch_ids", {})
    existing_meta = eval_state.get("request_meta", {})

    # Determine which providers already have pending batches
    pending_providers = set()
    if "anthropic" in existing_batch_ids:
        pending_providers.add("anthropic")
    for key in existing_batch_ids:
        if key.startswith("openai:"):
            pending_providers.add("openai")

    if pending_providers:
        log.info(f"Existing pending batches for: "
                 f"{', '.join(sorted(pending_providers))} (skipping)")

    requests_by_provider = {}
    request_meta = {}
    skipped = 0
    eval_max_tokens = cfg.get("eval_max_tokens", 1000)

    for nid in node_ids:
        # Load all generation results for this node
        gen_results = []
        for vfile in variants:
            vs = variant_slug(vfile)
            for mid in gen_models:
                ms = model_slug(mid)
                gen_file = result_path("generation", nid,
                                       f"{vs}_{ms}.json")
                if not os.path.exists(gen_file):
                    log.warning(f"Missing generation {gen_file}")
                    continue
                with open(gen_file) as f:
                    gen_results.append(json.load(f))

        if not gen_results:
            log.warning(f"No generation results for node {nid}, skipping")
            continue

        node_text = gen_results[0]["node_text"]

        for shuffle_idx in range(shuffles):
            for eval_mid in eval_models:
                ems = model_slug(eval_mid)
                out_file = result_path(
                    "evaluation", nid,
                    f"eval_{ems}_shuffle{shuffle_idx}.json")
                if os.path.exists(out_file):
                    skipped += 1
                    continue

                provider, api_model = get_model_provider(eval_mid)

                # Shuffle and assign labels (same seed logic as sync)
                model_hash = (int.from_bytes(eval_mid.encode(), "big")
                              % 1000)
                shuffle_seed = (nid * 1000000 + shuffle_idx * 1000
                                + model_hash)
                rng = random.Random(shuffle_seed)
                shuffled = list(gen_results)
                rng.shuffle(shuffled)
                labels = list(string.ascii_uppercase[:len(shuffled)])

                label_map = {}
                responses_text = []
                for label, gr in zip(labels, shuffled):
                    label_map[label] = {
                        "variant": gr["variant"],
                        "model": gr["model"],
                    }
                    responses_text.append(
                        f"### Response {label}\n\n{gr['response']}")

                eval_prompt = eval_prompt_template.format(
                    node_text=node_text,
                    responses="\n\n---\n\n".join(responses_text),
                )

                cid = f"eval_node{nid}_{ems}_shuffle{shuffle_idx}"

                messages = [
                    {"role": "user",
                     "content": [{"type": "text",
                                  "text": eval_prompt}]},
                ]

                # Always build metadata (needed for --batch-collect)
                request_meta[cid] = {
                    "node_id": nid,
                    "evaluator_model": eval_mid,
                    "shuffle_index": shuffle_idx,
                    "shuffle_seed": shuffle_seed,
                    "eval_prompt": eval_prompt,
                    "label_map": label_map,
                    "out_file": out_file,
                }

                # Only queue for submission if not already pending
                if provider in pending_providers:
                    continue

                requests_by_provider.setdefault(provider, []).append({
                    "custom_id": cid,
                    "model_id": eval_mid,
                    "api_model": api_model,
                    "messages": messages,
                    "max_tokens": eval_max_tokens,
                })

    total_queued = sum(len(v) for v in requests_by_provider.values())
    if total_queued == 0:
        log.info(f"Nothing to submit ({skipped} already exist).")
        return

    log.info(f"Submitting {total_queued} evaluation requests as batch "
             f"({skipped} skipped existing)...")
    batch_ids = batch_submit(requests_by_provider, api_keys, "evaluation")

    # Merge with existing state
    existing_batch_ids.update(batch_ids)
    existing_meta.update(request_meta)
    state["evaluation"] = {
        "batch_ids": existing_batch_ids,
        "request_meta": existing_meta,
        "submitted_at": datetime.now().isoformat(),
    }
    save_batch_state(state)

    log.info("Batch submitted. Run `flask rct evaluate --batch-collect` "
             "to check/collect results.")


def _evaluate_batch_collect(cfg):
    """Check and collect evaluation batch results."""
    state = load_batch_state()
    eval_state = state.get("evaluation")
    if not eval_state:
        log.error("No evaluation batch state found. "
                  "Run `flask rct evaluate --batch` first.")
        return

    api_keys = get_batch_api_keys(cfg)
    batch_ids = eval_state["batch_ids"]
    request_meta = eval_state["request_meta"]

    results, still_pending = batch_check_and_collect(batch_ids, api_keys)

    if still_pending:
        log.info(f"Still processing: {list(still_pending.keys())}. "
                 "Run --batch-collect again later.")
        eval_state["batch_ids"] = still_pending
        save_batch_state(state)

    written = 0
    errors = 0
    for cid, result in results.items():
        meta = request_meta.get(cid)
        if not meta:
            log.warning(f"No metadata for custom_id {cid}")
            errors += 1
            continue

        out_file = meta["out_file"]
        if os.path.exists(out_file):
            continue

        cost = calculate_llm_cost_microdollars(
            meta["evaluator_model"],
            result["input_tokens"], result["output_tokens"]
        )
        # Apply 50% batch discount
        cost = cost // 2

        output = {
            "node_id": meta["node_id"],
            "evaluator_model": meta["evaluator_model"],
            "shuffle_index": meta["shuffle_index"],
            "shuffle_seed": meta["shuffle_seed"],
            "eval_prompt": meta["eval_prompt"],
            "label_map": meta["label_map"],
            "evaluation": result["content"],
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "actual_cost_microdollars": cost,
            "batch_mode": True,
        }
        ensure_dir(os.path.dirname(out_file))
        with open(out_file, "w") as f:
            json.dump(output, f, indent=2)
            f.write("\n")
        written += 1

    log.info(f"Collected {written} results, {errors} errors.")

    if not still_pending:
        del state["evaluation"]
        save_batch_state(state)
        log.info("All evaluation batches complete.")


# ---------------------------------------------------------------------------
# Phase 3: Aggregate
# ---------------------------------------------------------------------------

def parse_ranking(text):
    """Extract ranking from evaluation text. Returns list of labels or None."""
    m = re.search(r'RANKING:\s*([A-Z](?:\s*>\s*[A-Z])*)', text)
    if not m:
        return None
    raw = m.group(1)
    return [c.strip() for c in raw.split(">")]


@rct_cli.command("aggregate")
@with_appcontext
def aggregate_cmd():
    """Aggregate evaluations into Borda count rankings."""
    setup_logging("aggregate")
    cfg = load_config()
    node_ids = [parse_node_id(n) for n in cfg["node_ids"]]

    # Collect all evaluation results
    eval_dir = os.path.join(RESULTS_DIR, "evaluation")
    parsed_rankings = []
    parse_failures = 0

    for nid in node_ids:
        node_eval_dir = os.path.join(eval_dir, f"node_{nid}")
        if not os.path.isdir(node_eval_dir):
            continue
        for fname in sorted(os.listdir(node_eval_dir)):
            if not fname.endswith(".json"):
                continue
            with open(os.path.join(node_eval_dir, fname)) as f:
                ev = json.load(f)

            ranking = parse_ranking(ev["evaluation"])
            if ranking is None:
                rel_path = os.path.join(f"node_{nid}", fname)
                log.warning(f"Could not parse ranking from {rel_path}")
                parse_failures += 1
                parsed_rankings.append({
                    "node_id": ev["node_id"],
                    "evaluator_model": ev["evaluator_model"],
                    "shuffle_index": ev["shuffle_index"],
                    "raw_ranking": None,
                    "resolved_ranking": None,
                    "parse_success": False,
                })
                continue

            # Resolve labels to variant+model
            label_map = ev["label_map"]
            resolved = []
            for label in ranking:
                if label in label_map:
                    resolved.append(label_map[label])
                else:
                    log.warning(f"Label {label} not in label_map for {fname}")

            parsed_rankings.append({
                "node_id": ev["node_id"],
                "evaluator_model": ev["evaluator_model"],
                "shuffle_index": ev["shuffle_index"],
                "raw_ranking": ranking,
                "resolved_ranking": resolved,
                "parse_success": True,
            })

    if not parsed_rankings:
        log.warning("No evaluation results found.")
        return

    # Borda count
    # Scores keyed by variant, model, and variant+model
    variant_scores = {}
    model_scores = {}
    combo_scores = {}

    successful = [r for r in parsed_rankings if r["parse_success"]]
    for pr in successful:
        n = len(pr["resolved_ranking"])
        for rank, entry in enumerate(pr["resolved_ranking"]):
            points = n - rank  # 1st gets max points
            v = entry["variant"]
            m = entry["model"]
            combo = f"{v} + {m}"

            variant_scores[v] = variant_scores.get(v, 0) + points
            model_scores[m] = model_scores.get(m, 0) + points
            combo_scores[combo] = combo_scores.get(combo, 0) + points

    # Sort by score descending
    variant_ranking = sorted(variant_scores.items(), key=lambda x: -x[1])
    model_ranking = sorted(model_scores.items(), key=lambda x: -x[1])
    combo_ranking = sorted(combo_scores.items(), key=lambda x: -x[1])

    # Sum actual costs and elapsed time from all generation + evaluation files
    total_cost = 0
    total_elapsed = 0
    for phase in ["generation", "evaluation"]:
        phase_dir = os.path.join(RESULTS_DIR, phase)
        if not os.path.isdir(phase_dir):
            continue
        for root, _, files in os.walk(phase_dir):
            for fname in files:
                if not fname.endswith(".json"):
                    continue
                with open(os.path.join(root, fname)) as f:
                    data = json.load(f)
                total_cost += data.get("actual_cost_microdollars", 0)
                total_elapsed += data.get("elapsed_seconds", 0)

    # Save parsed rankings
    agg_dir = os.path.join(RESULTS_DIR, "aggregation")
    ensure_dir(agg_dir)

    with open(os.path.join(agg_dir, "parsed_rankings.json"), "w") as f:
        json.dump(parsed_rankings, f, indent=2)
        f.write("\n")

    # Save final rankings
    rankings_data = {
        "by_variant": variant_ranking,
        "by_model": model_ranking,
        "by_variant_model": combo_ranking,
        "total_evaluations": len(parsed_rankings),
        "successful_parses": len(successful),
        "parse_failures": parse_failures,
        "total_actual_cost_microdollars": total_cost,
        "total_elapsed_seconds": round(total_elapsed, 2),
    }
    with open(os.path.join(agg_dir, "rankings.json"), "w") as f:
        json.dump(rankings_data, f, indent=2)
        f.write("\n")

    # Build summary
    lines = []
    lines.append("=" * 60)
    lines.append("PROMPT RCT — RESULTS SUMMARY")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Evaluations: {len(successful)} successful, {parse_failures} parse failures")
    lines.append(f"Total actual cost: {fmt_cost(total_cost)}")
    lines.append(f"Total elapsed time: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
    lines.append("")

    lines.append("--- By Prompt Variant (Borda scores) ---")
    for name, score in variant_ranking:
        lines.append(f"  {name:30s}  {score}")
    lines.append("")

    lines.append("--- By Model (Borda scores) ---")
    for name, score in model_ranking:
        lines.append(f"  {name:30s}  {score}")
    lines.append("")

    lines.append("--- By Variant + Model (Borda scores) ---")
    for name, score in combo_ranking:
        lines.append(f"  {name:40s}  {score}")
    lines.append("")

    summary = "\n".join(lines)
    with open(os.path.join(agg_dir, "summary.txt"), "w") as f:
        f.write(summary)
        f.write("\n")

    log.info(summary)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    "node_ids": [],
    "prompt_variants": [],
    "eval_max_tokens": 1000,
    "shuffles": 1,
    "use_batch": False,
}


@rct_cli.command("archive")
@with_appcontext
def archive_cmd():
    """Archive results + config + prompts, then reset for next run."""
    setup_logging("archive")
    cfg = load_config()

    if not os.path.isdir(RESULTS_DIR):
        log.info("No results directory found. Nothing to archive.")
        return

    file_count = sum(len(files) for _, _, files in os.walk(RESULTS_DIR))
    log.info(f"Results: {file_count} files")

    # Suggest archive folder name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_name = f"rct_{timestamp}"
    archive_name = click.prompt("Archive folder name", default=default_name)
    archive_path = os.path.join(ARCHIVE_DIR, archive_name)

    if os.path.exists(archive_path):
        log.error(f"Archive already exists: {archive_path}")
        return

    if not click.confirm(
        f"Archive to archive/{archive_name}/ and delete results?",
        default=True,
    ):
        log.info("Aborted.")
        return

    # Create archive and copy results
    ensure_dir(archive_path)
    shutil.copytree(RESULTS_DIR, os.path.join(archive_path, "results"))
    log.info("Copied results/")

    # Copy config snapshot
    shutil.copy2(CONFIG_PATH, os.path.join(archive_path, "config.json"))
    log.info("Copied config.json")

    # Copy prompts
    if os.path.isdir(PROMPTS_DIR):
        prompt_files = [f for f in os.listdir(PROMPTS_DIR)
                        if not f.startswith(".")]
        if prompt_files:
            shutil.copytree(PROMPTS_DIR,
                            os.path.join(archive_path, "prompts"))
            log.info(f"Copied prompts/ ({len(prompt_files)} files)")

    # Copy eval prompts
    eval_prompts_dir = os.path.join(RCT_DIR, "eval_prompts")
    if os.path.isdir(eval_prompts_dir):
        shutil.copytree(eval_prompts_dir,
                        os.path.join(archive_path, "eval_prompts"))
        log.info("Copied eval_prompts/")

    # Delete results
    shutil.rmtree(RESULTS_DIR)
    log.info("Deleted results/")

    # Ask about config reset
    resettable = {
        k: v for k, v in CONFIG_DEFAULTS.items()
        if cfg.get(k) != v
    }
    if resettable:
        log.info("Current non-default settings:")
        for k, default in resettable.items():
            log.info(f"  {k}: {cfg[k]} (default: {default})")

        if click.confirm("Reset these to defaults?", default=True):
            for k, v in resettable.items():
                cfg[k] = v
            # Clear prompt files when prompt_variants is being reset
            if "prompt_variants" in resettable:
                prompt_files = [f for f in os.listdir(PROMPTS_DIR)
                                if not f.startswith(".")]
                for f in prompt_files:
                    os.remove(os.path.join(PROMPTS_DIR, f))
                if prompt_files:
                    log.info("Cleared prompts/ directory")
            save_config(cfg)
            log.info("Config reset to defaults.")
        else:
            log.info("Config kept as-is.")
    else:
        log.info("Config already at defaults.")

    log.info(f"Archived to archive/{archive_name}/. "
             "Ready for a new experiment run.")


# ---------------------------------------------------------------------------
# Run All
# ---------------------------------------------------------------------------

@rct_cli.command("run-all")
@click.pass_context
@with_appcontext
def run_all_cmd(ctx):
    """Run all phases: estimate -> generate -> evaluate -> aggregate."""
    setup_logging("run_all")
    log.info("=== Phase 0: Estimate ===")
    ctx.invoke(estimate_cmd)

    if not click.confirm("\nProceed with generation?", default=True):
        return

    log.info("=== Phase 1: Generate ===")
    ctx.invoke(generate_cmd)

    log.info("=== Phase 2: Evaluate ===")
    ctx.invoke(evaluate_cmd)

    log.info("=== Phase 3: Aggregate ===")
    ctx.invoke(aggregate_cmd)
