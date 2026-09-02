"""Aggregate chunk-level intentions into one list per account.

The slices go in with their metadata (period, tweet count, source tokens) so
the model can weigh recency and persistence. Output blocks get stable ids
<handle>-<n>. Records the raw→aggregated compression ratio, the single
highest-value unmeasured number in the study."""
import argparse

from pilot_common import (AGG_DIR, FRAME, HERE, LUNA, RAW_DIR, accounts, api_keys, app, complete,
                          jdump, jload, parse_blocks, pmap)

MAX_OUT = 8192


def slices_text(chunks):
    parts = []
    for c in chunks:
        p = c["period"]
        parts.append(f"## Slice {c['chunk']} of {c['n_chunks']} — period {p[0]} → {p[1]} — "
                     f"{c['tweets']:,} tweets — {c['prompt_tokens'] // 1000}K tokens of source\n\n"
                     f"{c['content'].strip()}\n")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=16)
    ap.add_argument("--model", default=LUNA)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    template = (HERE / "prompts/aggregate.txt").read_text(encoding="utf-8")
    print(f"frame={FRAME}")
    flask_app = app()
    keys = api_keys(flask_app)
    with flask_app.app_context():
        jobs = []
        for r in accounts()[args.start:args.start + args.count]:
            h = r["username"]
            out = AGG_DIR / f"{h}.{args.model}.json"
            if out.exists():
                continue
            chunks = sorted([jload(p) for p in RAW_DIR.glob(f"{h}_c*.json")],
                            key=lambda c: c["chunk"])
            if not chunks:
                print(f"  {h}: no raw chunks")
                continue
            if any(c.get("truncated") for c in chunks):
                print(f"  {h}: !! a chunk output was truncated")
            raw_blocks = sum(len(parse_blocks(c["content"])) for c in chunks)
            jobs.append((h, chunks, raw_blocks, out))

        def run(job):
            h, chunks, raw_blocks, out = job
            prompt = template.replace("{slices}", slices_text(chunks))
            r = complete(flask_app, keys, args.model, prompt, MAX_OUT, note=f"aggregate {h}")
            blocks = parse_blocks(r["content"])
            for i, b in enumerate(blocks, 1):
                b["id"] = f"{h}-{i}"
            n_e = sum(1 for b in blocks if b["group"] == "Endorsed")
            jdump(out, {
                "handle": h, "model": args.model, "n_chunks": len(chunks),
                "raw_count": raw_blocks, "agg_count": len(blocks),
                "ratio": round(raw_blocks / max(1, len(blocks)), 2),
                "endorsed": n_e, "inferred": len(blocks) - n_e,
                "blocks": blocks, "content": r["content"],
                "input_tokens": r.get("input_tokens"), "output_tokens": r.get("output_tokens"),
                "truncated": r.get("truncated"), "elapsed": r.get("elapsed"),
            })
            print(f"  {h:<18} slices={len(chunks)} raw={raw_blocks:>3} → agg={len(blocks):>3} "
                  f"(ratio {raw_blocks / max(1, len(blocks)):.1f}) E={n_e} I={len(blocks) - n_e} "
                  f"out={r.get('output_tokens')} trunc={r.get('truncated')} {r.get('elapsed')}s",
                  flush=True)

        pmap(run, jobs, args.workers)


if __name__ == "__main__":
    main()
