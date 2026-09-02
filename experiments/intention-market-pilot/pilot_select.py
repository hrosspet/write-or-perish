"""Pick the pilot accounts: middle tier by corpus size AND by reach.

Runs the offline token census (~4 min, no API calls) if it is not there yet,
then filters to [min, max] export tokens, drops the most-followed accounts,
and shuffles with a fixed seed so "the first N" is a stable random sample.
"""
import argparse
import os
import random
import subprocess
import sys

from pilot_common import OUT, ROOT, jdump


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", default=os.path.expanduser("~/data/twitter/ca-snapshot"))
    ap.add_argument("--min-tokens", type=int, default=100_000)
    ap.add_argument("--max-tokens", type=int, default=400_000)
    ap.add_argument("--exclude-top", type=int, default=20,
                    help="skip the N most-followed accounts")
    ap.add_argument("--exclude", default="majamediaco",
                    help="comma-separated handles to skip (already studied)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    census = OUT / "census.tsv"
    if not census.exists():
        OUT.mkdir(parents=True, exist_ok=True)
        script = ROOT / "experiments/cheap-model-intentions/ca_corpus_token_census.py"
        subprocess.run([sys.executable, str(script), "--parquet", args.parquet,
                        "--out", str(census)], check=True, cwd=str(ROOT))

    rows = []
    with open(census, encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        for line in f:
            d = dict(zip(header, line.rstrip("\n").split("\t")))
            rows.append({
                "rank": int(d["rank"]), "username": d["username"],
                "followers": int(d["followers"]), "tweets": int(d["tweets"]),
                "export_tokens": int(d["export_tokens"]), "chunks": int(d["chunks"]),
            })
    excl = {h.strip().lower() for h in args.exclude.split(",") if h.strip()}
    tier = [r for r in rows
            if args.min_tokens <= r["export_tokens"] <= args.max_tokens
            and r["rank"] > args.exclude_top
            and r["username"].lower() not in excl]
    random.Random(args.seed).shuffle(tier)
    jdump(OUT / "accounts.json", tier)
    print(f"{len(rows)} accounts in census; {len(tier)} in tier "
          f"[{args.min_tokens:,}–{args.max_tokens:,} tokens, follower rank > "
          f"{args.exclude_top}]; shuffled with seed {args.seed}")
    for i, r in enumerate(tier[:32], 1):
        print(f"  {i:3d} {r['username']:<18} followers={r['followers']:>7,} "
              f"tweets={r['tweets']:>6,} tokens={r['export_tokens']:>8,} chunks={r['chunks']}")


if __name__ == "__main__":
    main()
