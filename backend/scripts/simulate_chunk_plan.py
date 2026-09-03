#!/usr/bin/env python3
"""Dry-run the chunk planner over a user's corpus in THIS database.

Uses the real export machinery (the same budget window, scope and compact
rendering the profile pipeline uses) and local tokenizer counts only — no
LLM calls, no profile rows written, nothing committed.

    python backend/scripts/simulate_chunk_plan.py --user exgenesis --model gpt-5.6-sol
    python backend/scripts/simulate_chunk_plan.py --user exgenesis --model gpt-5.6-sol --today

--today replays the current sizing (chunk_budget_for + per-chunk
calibration + the minimum-chunk tail rule) for comparison.

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

CLAUDE_NEW = ("opus-4-7", "opus-4-8", "opus-5", "sonnet-5", "fable", "mythos")
FAMILY_RATIO_TO_O200K = {"o200k": 1.0, "claude_new": 1.31, "claude_old": 1.08}
# Prior real-tokens-per-unit on compact tweet exports (majamediaco), used
# for the cap check before the first chunk has been measured.
FAMILY_PRIOR_PER_UNIT = {"o200k": 1.67, "claude_new": 2.18, "claude_old": 1.80}
MARGIN = 0.05


def family_of(cfg):
    if cfg["provider"] == "openai":
        return "o200k"
    api = cfg["api_model"]
    return "claude_new" if any(t in api for t in CLAUDE_NEW) else "claude_old"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="username in this DB")
    ap.add_argument("--model", default="gpt-5.6-sol")
    ap.add_argument("--target", type=int, default=None,
                    help="target units per chunk (default CHUNK_TARGET_UNITS)")
    ap.add_argument("--profile-tokens", type=int, default=8000,
                    help="assumed real tokens of the current profile (P)")
    ap.add_argument("--today", action="store_true",
                    help="replay the current sizing instead of the planner")
    args = ap.parse_args()

    import tiktoken
    from sqlalchemy import func, or_
    from backend import create_app
    app = create_app()
    with app.app_context():
        from backend.extensions import db
        from backend.models import User, Node
        from backend.utils.privacy import AI_ALLOWED
        from backend.routes.export_data import (
            build_user_export_content, _foreign_thread_node_ids,
            _export_visible_filter)
        from backend.tasks import exports as ex
        from backend.llm_providers import DEFAULT_MAX_OUTPUT_TOKENS

        user = User.query.filter_by(username=args.user).first()
        if not user:
            sys.exit(f"no user {args.user!r}")
        cfg = app.config["SUPPORTED_MODELS"][args.model]
        fam = family_of(cfg)
        ratio_o200k = FAMILY_RATIO_TO_O200K[fam]
        enc = tiktoken.get_encoding("o200k_base")

        def count(text):
            """Local count in the model family's tokens (exact for o200k)."""
            return int(len(enc.encode(text, disallowed_special=())) * ratio_o200k)

        cap = cfg.get("long_context_threshold") or (
            cfg["context_window"] - DEFAULT_MAX_OUTPUT_TOKENS)
        room_cap = int((1 - MARGIN) * cap)
        update_template = ex.build_update_template(user.id)
        gen_template = ex._load_prompt("profile_generation.txt", user_id=user.id)
        theta = count(update_template.replace("{existing_profile}", "")
                      .replace("{new_data}", ""))
        gen_tokens = count(gen_template.replace("{user_export}", ""))
        P = args.profile_tokens
        target = args.target or ex.CHUNK_TARGET_UNITS

        foreign = sorted(_foreign_thread_node_ids(user.id))

        def scope(q, cutoff):
            q = q.filter(or_(Node.user_id == user.id, Node.id.in_(foreign)),
                         _export_visible_filter(Node, user.id),
                         Node.ai_usage.in_(AI_ALLOWED))
            if cutoff is not None:
                q = q.filter(Node.created_at > cutoff)
            return q

        def remaining_units(cutoff):
            return int(scope(db.session.query(
                func.coalesce(func.sum(Node.token_count), 0)), cutoff).scalar() or 0)

        def window_units(ids):
            ids = list(ids)
            tot = 0
            for i in range(0, len(ids), 5000):
                tot += int(db.session.query(func.coalesce(func.sum(Node.token_count), 0))
                           .filter(Node.id.in_(ids[i:i + 5000])).scalar() or 0)
            return tot

        def build(budget, cutoff):
            return build_user_export_content(
                user, max_tokens=int(budget), filter_ai_usage=True,
                created_after=cutoff, chronological_order=True,
                return_metadata=True, include_strategy="engaged_threads")

        total = remaining_units(None)
        nodes_total = scope(db.session.query(func.count(Node.id)), None).scalar()
        est_note = ("exact o200k" if fam == "o200k"
                    else f"ESTIMATE = o200k x {ratio_o200k} ({fam})")
        print(f"user={user.username} id={user.id} model={args.model} family={fam} "
              f"tokens: {est_note}")
        print(f"corpus: {nodes_total:,} nodes, {total:,} units; "
              f"target T={target:,} units; cap={cap:,} real tokens "
              f"(room {room_cap:,} after {MARGIN:.0%} margin); "
              f"template Θ={theta:,}, gen prompt={gen_tokens:,}, assumed profile P={P:,}")
        mode = "TODAY (chunk_budget_for + calibration + tail rule)" if args.today \
            else "PLANNER (plan_chunks over the remainder)"
        print(f"mode: {mode}\n")
        hdr = (f"{'#':>2} {'from':>10} {'to':>10} {'nodes':>6} {'units':>8} "
               f"{'chars':>9} {'tokens':>8} {'tok/unit':>8} {'prompt':>8} {'cap':>5}  note")
        print(hdr)
        print("-" * len(hdr))

        rows = []
        cutoff = None
        rho = FAMILY_PRIOR_PER_UNIT[fam]
        n = 0
        # --today: in-memory calibration only, rolled back at the end.
        user.profile_token_ratio = None
        dropped = 0
        while True:
            R = remaining_units(cutoff)
            if R == 0:
                break
            if args.today:
                budget, min_chunk = ex.chunk_budget_for(user, args.model)
                note = f"budget={budget:,} min={min_chunk:,}"
            else:
                room = room_cap - theta - (P if n else gen_tokens)
                max_units = room / rho if rho > 0 else None
                k, S = ex.plan_chunks(R, target=target, max_units=max_units)
                # The builder keeps a header/footer allowance out of the
                # budget, so a window lands a little short of S. Harmless
                # mid-corpus (the next plan re-measures the remainder) but
                # the final chunk must take everything: over-ask, the
                # window cannot exceed the data anyway.
                budget = S if k > 1 else R + 10_000
                note = f"R={R:,} k={k} S={S:,.0f} max_units={max_units:,.0f}"
            chunk = build(budget, cutoff)
            if not chunk or not chunk.get("content"):
                print(f"   builder returned nothing at cutoff={cutoff}")
                break
            latest = chunk["latest_node_created_at"]
            earliest = chunk.get("earliest_node_created_at")
            units = window_units(chunk["node_ids"])
            text = chunk["content"]
            tokens = count(text)
            n += 1
            if args.today:
                rendered_est = chunk["token_count"]
                more = ex._has_more_source_after(user, latest)
                if rendered_est < min_chunk and not more:
                    dropped = units
                    print(f"{n:>2} {str(earliest)[:10]:>10} {str(latest)[:10]:>10} "
                          f"{len(chunk['node_ids']):>6,} {units:>8,} {len(text):>9,} "
                          f"{tokens:>8,} {tokens / max(units, 1):>8.2f} {'':>8} {'':>5}  "
                          f"TAIL DROPPED: rendered {rendered_est:,} < min {min_chunk:,}, no more data")
                    break
                prompt_tokens = tokens + theta + (P if n > 1 else gen_tokens)
                # calibrate exactly as the loop does: chars/4 of the prompt
                # vs the (here: counted) input tokens
                prompt_chars4 = (len(text) + len(update_template) + P * 4) // 4
                ex.record_token_ratio(user, args.model, prompt_chars4, prompt_tokens)
            else:
                prompt_tokens = tokens + theta + (P if n > 1 else gen_tokens)
                rho = tokens / max(units, 1)
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
              f"({covered / max(total, 1):.1%}); left over {total - covered:,}"
              + (f" (tail dropped: {dropped:,})" if dropped else ""))
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
