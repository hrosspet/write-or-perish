#!/usr/bin/env python3
"""Dry-run the chunk planner over a user's corpus in THIS database.

Uses the real pipeline pieces — the remainder sum in the export window's
own scope, the planner, the model's input cap, the tokenizer-family prior
and the per-chunk calibration, the export windows and compact rendering —
with local tokenizer counts only: no LLM calls, no profile rows written,
nothing committed.

    python backend/scripts/simulate_chunk_plan.py --user exgenesis --model gpt-5.6-sol

Token counts: tiktoken o200k_base is exact for the OpenAI models. Claude
families have no local tokenizer; they are ESTIMATED from the o200k count
with the ratios measured on the majamediaco tweet export (new tokenizer
1.31x, old tokenizer 1.08x) and labelled as such.
"""
import argparse
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

FAMILY_RATIO_TO_O200K = {"o200k": 1.0, "claude_new": 1.31, "claude_old": 1.08}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="username in this DB")
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--target", type=int, default=None,
                    help="target units per chunk (default CHUNK_TARGET_UNITS)")
    ap.add_argument("--profile-tokens", type=int, default=8000,
                    help="assumed real tokens of the current profile (P)")
    args = ap.parse_args()

    import tiktoken
    from sqlalchemy import func
    from backend import create_app
    app = create_app()
    with app.app_context():
        from backend.extensions import db
        from backend.models import User, Node
        from backend.routes.export_data import (
            build_user_export_content, count_remaining_units)
        from backend.tasks import exports as ex
        from backend.llm_providers import (
            DEFAULT_MAX_OUTPUT_TOKENS, model_input_cap)
        from backend.utils.chunk_plan import CAP_MARGIN

        user = User.query.filter_by(username=args.user).first()
        if not user:
            sys.exit(f"no user {args.user!r}")
        cfg = app.config["SUPPORTED_MODELS"][args.model]
        fam = ex.tokenizer_family(args.model)
        ratio_o200k = FAMILY_RATIO_TO_O200K[fam]
        enc = tiktoken.get_encoding("o200k_base")

        def count(text):
            """Local count in the model family's tokens (exact for o200k)."""
            return int(len(enc.encode(text, disallowed_special=())) * ratio_o200k)

        cap = model_input_cap(cfg, DEFAULT_MAX_OUTPUT_TOKENS)
        room_cap = int((1 - CAP_MARGIN) * cap)
        update_template = ex.build_update_template(user.id)
        gen_template = ex._load_prompt("profile_generation.txt", user_id=user.id)
        theta = count(update_template.replace("{existing_profile}", "")
                      .replace("{new_data}", ""))
        gen_tokens = count(gen_template.replace("{user_export}", ""))
        P = args.profile_tokens
        target = args.target or ex.CHUNK_TARGET_UNITS

        def build(budget, cutoff):
            return build_user_export_content(
                user, max_tokens=int(budget), filter_ai_usage=True,
                created_after=cutoff, chronological_order=True,
                return_metadata=True, include_strategy="engaged_threads")

        total = count_remaining_units(user.id, None)
        nodes_total = db.session.query(func.count(Node.id)).filter(
            Node.user_id == user.id).scalar()
        est_note = ("exact o200k" if fam == "o200k"
                    else f"ESTIMATE = o200k x {ratio_o200k} ({fam})")
        # Calibration starts from the family prior, in memory only —
        # rolled back at the end.
        user.profile_token_ratio, user.profile_token_ratio_family = None, None
        print(f"user={user.username} id={user.id} model={args.model} family={fam} "
              f"content class={ex.content_class(user)} tokens: {est_note}")
        print(f"corpus: {nodes_total:,} own nodes, {total:,} units in the window scope; "
              f"target T={target:,} units; input cap={cap:,} real tokens "
              f"(room {room_cap:,} after {CAP_MARGIN:.0%} margin); "
              f"template Θ={theta:,}, gen prompt={gen_tokens:,}, assumed profile P={P:,}")
        print(f"prior: {ex.tokens_per_unit(user, args.model):.2f} tokens/unit\n")
        hdr = (f"{'#':>2} {'from':>10} {'to':>10} {'nodes':>6} {'units':>8} "
               f"{'chars':>9} {'tokens':>8} {'tok/unit':>8} {'prompt':>8} {'cap':>5}  plan")
        print(hdr)
        print("-" * len(hdr))

        rows = []
        cutoff = None
        n = 0
        while True:
            R = count_remaining_units(user.id, cutoff)
            if R == 0:
                break
            rho = ex.tokens_per_unit(user, args.model)
            max_units = ex.max_units_for_cap(cap, rho)
            k, S, budget = ex.next_window_budget(
                R, max_units=max_units, target=target)
            note = (f"R={R:,} k={k} S={S:,.0f} budget={budget:,} "
                    f"max_units={max_units:,.0f} @ {rho:.2f}/unit")
            chunk = build(budget, cutoff)
            if not chunk or not chunk.get("content"):
                print(f"   builder returned nothing at cutoff={cutoff} (R={R:,})")
                break
            latest = chunk["latest_node_created_at"]
            earliest = chunk.get("earliest_node_created_at")
            units = chunk["unit_count"]
            text = chunk["content"]
            tokens = count(text)
            n += 1
            prompt_tokens = tokens + theta + (P if n > 1 else gen_tokens)
            # Calibrate exactly as the loop does: the whole prompt's tokens
            # over the window's stored units.
            ex.record_token_ratio(user, args.model, units, prompt_tokens)
            over = "OVER" if prompt_tokens > room_cap else "ok"
            rows.append((units, tokens, prompt_tokens))
            print(f"{n:>2} {str(earliest)[:10]:>10} {str(latest)[:10]:>10} "
                  f"{len(chunk['node_ids']):>6,} {units:>8,} {len(text):>9,} "
                  f"{tokens:>8,} {tokens / max(units, 1):>8.2f} {prompt_tokens:>8,} {over:>5}  {note}")
            cutoff = latest

        db.session.rollback()
        covered = sum(r[0] for r in rows)
        print()
        print(f"chunks: {len(rows)}; units covered {covered:,} of {total:,} "
              f"({covered / max(total, 1):.1%}); left over {total - covered:,}")
        if rows:
            us = [r[0] for r in rows]
            ts = [r[1] for r in rows]
            print(f"units per chunk: min {min(us):,} mean {statistics.mean(us):,.0f} "
                  f"max {max(us):,}; spread {(max(us) - min(us)) / statistics.mean(us):.0%} of mean")
            print(f"chunk tokens: min {min(ts):,} mean {statistics.mean(ts):,.0f} max {max(ts):,}; "
                  f"prompts over cap: {sum(1 for r in rows if r[2] > room_cap)}")
            print(f"vs target T={target:,}: chunks within ±20%: "
                  f"{sum(1 for u in us if 0.8 * target <= u <= 1.2 * target)} of {len(us)}")


if __name__ == "__main__":
    main()
