"""Chunked intentions extraction with the public prompt, per imported account.

Chunks are time-ordered slices of ~equal rendered weight sized so each stays
under ~100K tokens (and thus under luna's 272K long-context tier). Each chunk
output is stored with its metadata (period, tweet count, prompt tokens) for
the aggregation step. Sync mode runs a thread pool; submit/collect use the
app's Batch helpers.
"""
import argparse
import math
import pathlib
import re

from pilot_common import (FRAME, HERE, LUNA, OUT, RAW_DIR, ROOT, accounts, api_keys, app, complete,
                          jdump, jload, o200k_len, parse_blocks, pmap,
                          record_spend, user_for)

CHUNK_TOKENS = 100_000
ENTRY_OVERHEAD = 5.63  # per-entry timestamp scaffolding, stored units (see study §8)
PROMPT_FILE = "intentions_detection_public.txt"
PAT = re.compile(r"\{user_export(\?[^}]*)?\}")
MAX_OUT = 8192


def chunk_edges(rows, n):
    def w(r):
        return (r.token_count or 0) + ENTRY_OVERHEAD
    total = sum(w(r) for r in rows)
    target, acc, bounds = total / n, 0, []
    for r in rows:
        acc += w(r)
        if acc >= target * (len(bounds) + 1) and len(bounds) < n - 1:
            bounds.append(r.created_at)
    return [None] + bounds + [None]


def build(user, template, lo, hi):
    from backend.routes.export_data import build_user_export_content
    export = build_user_export_content(
        user, max_tokens=1_000_000, filter_ai_usage=True,
        created_after=lo, created_before=hi,
        chronological_order=False, include_strategy="engaged_threads")
    if not export:
        return None, 0
    return PAT.sub(lambda _m: export, template, count=1), export.count("\n[")


def save(job, r, model, batch=False):
    content = r["content"]
    jdump(job["out"], {
        **{k: v for k, v in job.items() if k != "prompt"},
        "model": model, "content": content,
        "input_tokens": r.get("input_tokens"), "output_tokens": r.get("output_tokens"),
        "truncated": r.get("truncated"), "elapsed": r.get("elapsed"), "batch": batch,
        "n_blocks": len(parse_blocks(content)),
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=16)
    ap.add_argument("--model", default=LUNA)
    ap.add_argument("--mode", choices=["sync", "submit", "collect", "dry"], default="sync")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--chunk-tokens", type=int, default=CHUNK_TOKENS)
    ap.add_argument("--prompt-file", default=PROMPT_FILE)
    args = ap.parse_args()

    flask_app = app()
    with flask_app.app_context():
        from backend.models import Node
        keys = api_keys(flask_app)
        cfg = flask_app.config["SUPPORTED_MODELS"][args.model]

        if args.mode == "collect":
            from backend.utils.llm_batch import batch_check_and_collect
            state = jload(OUT / f"extract_batch_{FRAME}.json")
            results, pending, dur = batch_check_and_collect(dict(state["batch_ids"]), keys)
            n = 0
            for cid, r in results.items():
                job = state["meta"].get(cid)
                if not job or pathlib.Path(job["out"]).exists():
                    continue
                save(job, r, args.model, batch=True)
                record_spend(args.model, r.get("input_tokens", 0), r.get("output_tokens", 0),
                             f"extract-batch {cid}", batch=True)
                n += 1
            print(f"collected {n} new; pending: {pending}; durations: {dur}")
            return

        pf = HERE / "prompts" / args.prompt_file
        if not pf.exists():
            pf = ROOT / "backend/prompts" / args.prompt_file
        template = pf.read_text(encoding="utf-8")
        print(f"frame={FRAME} prompt={pf}")
        jobs = []
        for r in accounts()[args.start:args.start + args.count]:
            h = r["username"]
            user = user_for(h)
            if not user:
                print(f"  {h}: not imported")
                continue
            rows = (Node.query.filter_by(user_id=user.id, origin="twitter")
                    .filter(Node.deleted_at.is_(None)).order_by(Node.created_at).all())
            n = max(1, math.ceil(r["export_tokens"] / args.chunk_tokens))
            edges = chunk_edges(rows, n)
            for i in range(n):
                out = RAW_DIR / f"{h}_c{i + 1}.json"
                if out.exists():
                    continue
                lo, hi = edges[i], edges[i + 1]
                prompt, ntweets = build(user, template, lo, hi)
                if not prompt:
                    continue
                inside = [x for x in rows if (lo is None or x.created_at > lo)
                          and (hi is None or x.created_at <= hi)]
                period = ((min(x.created_at for x in inside).date().isoformat(),
                           max(x.created_at for x in inside).date().isoformat())
                          if inside else (None, None))
                jobs.append({"handle": h, "chunk": i + 1, "n_chunks": n, "period": period,
                             "tweets": ntweets, "nodes": len(inside),
                             "prompt_tokens": o200k_len(prompt), "prompt": prompt,
                             "out": str(out)})
        total = sum(j["prompt_tokens"] for j in jobs)
        print(f"{len(jobs)} chunk requests, {total:,} prompt tokens "
              f"(~${total * 0.20 / 1e6:.2f} sync input on luna)")
        over = [j for j in jobs if j["prompt_tokens"] > 272_000]
        if over:
            print(f"  !! {len(over)} chunks above the 272K base tier")
        if args.mode == "dry" or not jobs:
            return

        if args.mode == "sync":
            def run(j):
                r = complete(flask_app, keys, args.model, j["prompt"], MAX_OUT,
                             note=f"extract {j['handle']} c{j['chunk']}")
                save(j, r, args.model)
                print(f"  {j['handle']:<18} c{j['chunk']}/{j['n_chunks']} "
                      f"in={r.get('input_tokens') or 0:>7,} out={r.get('output_tokens') or 0:>5,} "
                      f"blocks={len(parse_blocks(r['content']))} trunc={r.get('truncated')} "
                      f"{r.get('elapsed')}s", flush=True)
                return j["out"]
            pmap(run, jobs, args.workers)
            return

        from backend.utils.llm_batch import batch_submit
        reqs = [{"custom_id": f"{j['handle']}_c{j['chunk']}", "model_id": args.model,
                 "api_model": cfg["api_model"], "max_tokens": MAX_OUT,
                 "messages": [{"role": "user",
                               "content": [{"type": "text", "text": j["prompt"]}]}]}
                for j in jobs]
        ids = batch_submit({cfg["provider"]: reqs}, keys, "pilot-extract")
        state = jload(OUT / f"extract_batch_{FRAME}.json", {"batch_ids": {}, "meta": {}})
        state["batch_ids"].update(ids)
        state["meta"].update({f"{j['handle']}_c{j['chunk']}":
                              {k: v for k, v in j.items() if k != "prompt"} for j in jobs})
        jdump(OUT / f"extract_batch_{FRAME}.json", state)
        print("submitted", ids)


if __name__ == "__main__":
    main()
