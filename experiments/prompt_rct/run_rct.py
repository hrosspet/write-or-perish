"""
Prompt RCT (Randomized Controlled Trial) — Flask CLI commands.

Compare prompt variants across models with blind evaluation and Borda count.

Usage:
    flask rct estimate      # Cost estimate + set shuffle count
    flask rct generate      # Phase 1: generate responses
    flask rct evaluate      # Phase 2: blind evaluation
    flask rct aggregate     # Phase 3: Borda count + summary
    flask rct run-all       # All phases sequentially
"""
import json
import os
import random
import re
import string
import time

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

rct_cli = AppGroup("rct", help="Prompt RCT experiment commands.")


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
    cfg = load_config()
    node_ids = [parse_node_id(n) for n in cfg["node_ids"]]
    gen_models = cfg["generation_models"]
    eval_models = cfg["evaluation_models"]
    variants = cfg["prompt_variants"]

    if not node_ids:
        click.echo("Error: no node_ids in config.json")
        return

    # Validate node ownership before any content access
    owner = cfg.get("owner")
    if not owner:
        click.echo("Error: 'owner' not set in config.json")
        return
    valid_ids, errors = validate_node_ownership(node_ids, owner)
    if errors:
        for e in errors:
            click.echo(f"  ERROR: {e}")
        if not valid_ids:
            return
        click.echo()
    node_ids = valid_ids

    # Fetch node content to estimate input tokens
    click.echo(f"Fetching {len(node_ids)} nodes...")
    node_texts = {}
    for nid in node_ids:
        node = Node.query.get(nid)
        if not node:
            click.echo(f"  Warning: node {nid} not found, skipping")
            continue
        node_texts[nid] = node.get_content()
    click.echo()

    if not node_texts:
        click.echo("Error: no valid nodes found")
        return

    avg_node_tokens = sum(estimate_tokens(t) for t in node_texts.values()) // len(node_texts)
    est_output_tokens = 1000  # default output estimate

    # Load actual prompt variants to get real token counts
    prompt_tokens = {}
    for vfile in variants:
        try:
            prompt_tokens[vfile] = estimate_tokens(load_prompt_variant(vfile))
        except FileNotFoundError:
            click.echo(f"  Warning: prompt file {vfile} not found")
            prompt_tokens[vfile] = 500  # fallback
    avg_prompt_tokens = sum(prompt_tokens.values()) // max(len(prompt_tokens), 1)

    # Load eval prompt for token estimate
    try:
        eval_prompt_tokens = estimate_tokens(load_eval_prompt())
    except FileNotFoundError:
        click.echo("  Warning: eval prompt not found")
        eval_prompt_tokens = 500

    n_nodes = len(node_texts)
    n_variants = len(variants)
    n_gen_models = len(gen_models)
    n_eval_models = len(eval_models)

    click.echo(f"  Avg node: ~{avg_node_tokens} tokens")
    click.echo(f"  Avg prompt variant: ~{avg_prompt_tokens} tokens")
    click.echo(f"  Est. output: ~{est_output_tokens} tokens")
    click.echo()

    # Generation cost
    n_gen_calls = n_nodes * n_variants * n_gen_models
    gen_input = avg_node_tokens + avg_prompt_tokens
    click.echo("=== Generation ===")
    click.echo(f"  {n_nodes} nodes x {n_variants} variants x {n_gen_models} models = {n_gen_calls} calls")
    click.echo(f"  ~{gen_input} in + ~{est_output_tokens} out tokens/call")
    gen_cost_total = 0
    for mid in gen_models:
        cost = calculate_llm_cost_microdollars(mid, gen_input, est_output_tokens)
        model_cost = cost * n_nodes * n_variants
        gen_cost_total += model_cost
        click.echo(f"  {mid}: ~{fmt_cost(cost)}/call, ~{fmt_cost(model_cost)} total")
    click.echo(f"  Generation total: ~{fmt_cost(gen_cost_total)}")
    click.echo()

    # Evaluation cost (per shuffle)
    n_responses = n_variants * n_gen_models
    eval_input = avg_node_tokens + est_output_tokens * n_responses + eval_prompt_tokens
    n_eval_calls_per_shuffle = n_nodes * n_eval_models
    click.echo("=== Evaluation (per shuffle) ===")
    click.echo(f"  {n_nodes} nodes x {n_eval_models} eval models = {n_eval_calls_per_shuffle} calls/shuffle")
    click.echo(f"  ~{eval_input} in + ~{est_output_tokens} out tokens/call")
    eval_cost_per_shuffle = 0
    for mid in eval_models:
        cost = calculate_llm_cost_microdollars(mid, eval_input, est_output_tokens)
        model_cost = cost * n_nodes
        eval_cost_per_shuffle += model_cost
        click.echo(f"  {mid}: ~{fmt_cost(cost)}/call, ~{fmt_cost(model_cost)}/shuffle")
    click.echo(f"  Per shuffle total: ~{fmt_cost(eval_cost_per_shuffle)}")
    click.echo()

    # Interactive: ask for shuffle count
    default_shuffles = cfg.get("shuffles", 1)
    shuffles = click.prompt(
        "How many evaluation shuffles?",
        type=int,
        default=default_shuffles,
    )

    total_eval_cost = eval_cost_per_shuffle * shuffles
    total_cost = gen_cost_total + total_eval_cost

    click.echo()
    click.echo(f"=== Total Estimate ({shuffles} shuffle(s)) ===")
    click.echo(f"  Generation:  {fmt_cost(gen_cost_total)}")
    click.echo(f"  Evaluation:  {fmt_cost(total_eval_cost)}")
    click.echo(f"  TOTAL:       {fmt_cost(total_cost)}")

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

    click.echo(f"\nSaved shuffles={shuffles} to config.json and metadata.json")


# ---------------------------------------------------------------------------
# Phase 1: Generate
# ---------------------------------------------------------------------------

@rct_cli.command("generate")
@with_appcontext
def generate_cmd():
    """Generate responses for all node x variant x model combinations."""
    cfg = load_config()
    node_ids = [parse_node_id(n) for n in cfg["node_ids"]]
    gen_models = cfg["generation_models"]
    variants = cfg["prompt_variants"]
    key_type = cfg.get("api_key_type", "chat")

    owner = cfg.get("owner")
    if not owner:
        click.echo("Error: 'owner' not set in config.json")
        return
    node_ids, errors = validate_node_ownership(node_ids, owner)
    if errors:
        for e in errors:
            click.echo(f"  ERROR: {e}")
        if not node_ids:
            return

    # Resolve user profile for {user_profile} placeholder
    user_profile, _ = resolve_user_profile(owner)
    if user_profile:
        click.echo(f"User profile: {len(user_profile)} chars")
    else:
        click.echo("User profile: not available (placeholders will be empty)")

    total = len(node_ids) * len(variants) * len(gen_models)
    click.echo(f"API key type: {key_type} | {total} calls across {len(gen_models)} models")
    if not click.confirm("Proceed with generation?", default=True):
        return

    api_keys = get_api_keys(cfg)
    done = 0
    skipped = 0
    errors = 0

    for nid in node_ids:
        node = Node.query.get(nid)
        if not node:
            click.echo(f"Warning: node {nid} not found, skipping")
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
                    click.echo(f"[{done}/{total}] node {nid}, {vfile}, {mid} ... skipped (exists)")
                    continue

                messages = [
                    {"role": "system", "content": [{"type": "text", "text": prompt_text}]},
                    {"role": "user", "content": [{"type": "text", "text": node_text}]},
                ]

                t0 = time.time()
                try:
                    result = LLMProvider.get_completion(mid, messages, api_keys)
                except PromptTooLongError as e:
                    click.echo(f"[{done}/{total}] node {nid}, {vfile}, {mid} ... ERROR: {e}")
                    errors += 1
                    continue
                except Exception as e:
                    click.echo(f"[{done}/{total}] node {nid}, {vfile}, {mid} ... ERROR: {e}")
                    errors += 1
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
                }
                with open(out_file, "w") as f:
                    json.dump(output, f, indent=2)
                    f.write("\n")

                click.echo(
                    f"[{done}/{total}] node {nid}, {vfile}, {mid} "
                    f"... done ({elapsed:.1f}s, {fmt_cost(cost)})"
                )

    click.echo(f"\nGeneration complete. {done} total, {skipped} skipped, {errors} errors.")


# ---------------------------------------------------------------------------
# Phase 2: Evaluate
# ---------------------------------------------------------------------------

@rct_cli.command("evaluate")
@with_appcontext
def evaluate_cmd():
    """Run blind evaluations with shuffled response labels."""
    cfg = load_config()
    node_ids = [parse_node_id(n) for n in cfg["node_ids"]]
    eval_models = cfg["evaluation_models"]
    gen_models = cfg["generation_models"]
    variants = cfg["prompt_variants"]
    shuffles = cfg.get("shuffles", 1)
    key_type = cfg.get("api_key_type", "chat")

    owner = cfg.get("owner")
    if not owner:
        click.echo("Error: 'owner' not set in config.json")
        return
    node_ids, errors = validate_node_ownership(node_ids, owner)
    if errors:
        for e in errors:
            click.echo(f"  ERROR: {e}")
        if not node_ids:
            return

    total = len(node_ids) * len(eval_models) * shuffles
    click.echo(f"API key type: {key_type} | {total} calls across {len(eval_models)} eval models, {shuffles} shuffle(s)")
    if not click.confirm("Proceed with evaluation?", default=True):
        return

    api_keys = get_api_keys(cfg)
    eval_prompt_template = load_eval_prompt()

    done = 0
    skipped = 0
    errors = 0

    for nid in node_ids:
        # Load all generation results for this node
        gen_results = []
        for vfile in variants:
            vs = variant_slug(vfile)
            for mid in gen_models:
                ms = model_slug(mid)
                gen_file = result_path("generation", nid, f"{vs}_{ms}.json")
                if not os.path.exists(gen_file):
                    click.echo(f"Warning: missing generation {gen_file}")
                    continue
                with open(gen_file) as f:
                    gen_results.append(json.load(f))

        if not gen_results:
            click.echo(f"Warning: no generation results for node {nid}, skipping")
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
                    click.echo(f"[{done}/{total}] node {nid}, {eval_mid}, shuffle {shuffle_idx} ... skipped")
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
                    click.echo(f"[{done}/{total}] node {nid}, {eval_mid}, shuffle {shuffle_idx} ... ERROR: {e}")
                    errors += 1
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
                }
                with open(out_file, "w") as f:
                    json.dump(output, f, indent=2)
                    f.write("\n")

                click.echo(
                    f"[{done}/{total}] node {nid}, {eval_mid}, shuffle {shuffle_idx} "
                    f"... done ({elapsed:.1f}s, {fmt_cost(cost)})"
                )

    click.echo(f"\nEvaluation complete. {done} total, {skipped} skipped, {errors} errors.")


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
                click.echo(f"Warning: could not parse ranking from {fname}")
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
                    click.echo(f"Warning: label {label} not in label_map for {fname}")

            parsed_rankings.append({
                "node_id": ev["node_id"],
                "evaluator_model": ev["evaluator_model"],
                "shuffle_index": ev["shuffle_index"],
                "raw_ranking": ranking,
                "resolved_ranking": resolved,
                "parse_success": True,
            })

    if not parsed_rankings:
        click.echo("No evaluation results found.")
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

    # Sum actual costs from all generation + evaluation files
    total_cost = 0
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

    click.echo(summary)


# ---------------------------------------------------------------------------
# Run All
# ---------------------------------------------------------------------------

@rct_cli.command("run-all")
@click.pass_context
@with_appcontext
def run_all_cmd(ctx):
    """Run all phases: estimate -> generate -> evaluate -> aggregate."""
    click.echo("=== Phase 0: Estimate ===\n")
    ctx.invoke(estimate_cmd)

    if not click.confirm("\nProceed with generation?", default=True):
        return

    click.echo("\n=== Phase 1: Generate ===\n")
    ctx.invoke(generate_cmd)

    click.echo("\n=== Phase 2: Evaluate ===\n")
    ctx.invoke(evaluate_cmd)

    click.echo("\n=== Phase 3: Aggregate ===\n")
    ctx.invoke(aggregate_cmd)
