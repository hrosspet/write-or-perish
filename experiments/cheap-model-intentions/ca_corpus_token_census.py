"""Token census of the whole Community Archive, for costing an intentions
pre-fill across every account in it.

For each account: render its tweets in Loore's compact export format (the #276
`[YYYY-MM-DD HH:MM] text` form), count tokens with the tokenizer GPT-5.6 Luna
uses (tiktoken o200k_base — validated against OpenAI's reported usage to within
0.04% on @majamediaco), and price a chunked intentions run.

Counts are exact, not extrapolated: 734 accounts / ~9M tweets is small enough to
tokenize locally in a few minutes. No API calls, no rendering to disk.

Output: a TSV sorted by follower count (highest priority first) with running
cumulative totals, so you can read off "the top N accounts cost $X".

    python experiments/cheap-model-intentions/ca_corpus_token_census.py \
        --parquet ~/data/twitter/community-archive-snapshot-2026-09-01 \
        --out ca_token_census.tsv
"""
import argparse
import math
import os
import sys
import time

# --- cost model -----------------------------------------------------------
# gpt-5.6-luna, chunks sized under OpenAI's 272K long-context threshold, so the
# base tier applies ($0.20/$1.20 per MTok); Batch API halves both.
IN_PER_MTOK, OUT_PER_MTOK = 0.20, 1.20
BATCH_DISCOUNT = 0.5
CHUNK_TOKENS = 100_000
# Measured output on luna intentions runs: 1,400 (whole-corpus) and
# 1,569 / 1,226 / 1,741 (chunked). Output tracks the ~9-14 item list, not the
# input size, so it scales with chunk COUNT, not chunk length.
OUT_TOKENS_PER_CHUNK = 1_500
TEMPLATE_FILE = "backend/prompts/intentions_detection.txt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--out", default="ca_token_census.tsv")
    ap.add_argument("--chunk-tokens", type=int, default=CHUNK_TOKENS)
    ap.add_argument("--limit", type=int, default=None,
                    help="only the top N accounts by followers")
    args = ap.parse_args()

    import duckdb
    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")

    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    tmpl = open(os.path.join(repo, TEMPLATE_FILE), encoding="utf-8").read()
    tmpl_tokens = len(enc.encode_ordinary(tmpl))
    print(f"prompt template: {tmpl_tokens} tokens (re-paid per chunk)",
          file=sys.stderr)

    d = os.path.expanduser(args.parquet)
    con = duckdb.connect()
    con.execute("SET memory_limit='6GB'")
    tweets = os.path.join(d, "tweets.parquet")
    profiles = os.path.join(d, "profiles.parquet")

    accounts = con.execute(
        "select account_id, username, num_followers "
        "from read_parquet(?) order by num_followers desc nulls last",
        [profiles]).fetchall()
    if args.limit:
        accounts = accounts[:args.limit]
    print(f"{len(accounts)} accounts", file=sys.stderr)

    rows, t0 = [], time.time()
    for i, (aid, uname, followers) in enumerate(accounts, 1):
        # Same filter the importer applies: compact_row() drops "RT @" and the
        # pre-fill keeps replies (include_replies=True).
        cur = con.execute(
            "select strftime(created_at at time zone 'UTC', "
            "'[%Y-%m-%d %H:%M] ') || full_text "
            "from read_parquet(?) where account_id = ? "
            "and full_text is not null and full_text not like 'RT @%'",
            [tweets, aid])
        ntw, ntok = 0, 0
        while True:
            batch = cur.fetchmany(20_000)
            if not batch:
                break
            texts = [r[0] for r in batch]
            ntw += len(texts)
            # "\n\n" between entries: 1 extra token per entry, close enough to
            # the renderer's blank-line separation.
            ntok += sum(len(x) for x in enc.encode_ordinary_batch(texts))
            ntok += len(texts)
        if ntw == 0:
            continue
        chunks = max(1, math.ceil(ntok / args.chunk_tokens))
        inp = ntok + chunks * tmpl_tokens
        out = chunks * OUT_TOKENS_PER_CHUNK
        sync = inp * IN_PER_MTOK / 1e6 + out * OUT_PER_MTOK / 1e6
        rows.append({
            "username": uname, "account_id": aid,
            "followers": followers or 0, "tweets": ntw,
            "export_tokens": ntok, "chunks": chunks,
            "input_tokens": inp, "output_tokens": out,
            "cost_sync": sync, "cost_batch": sync * BATCH_DISCOUNT,
        })
        if i % 50 == 0:
            print(f"  {i}/{len(accounts)}  ({time.time()-t0:.0f}s)",
                  file=sys.stderr)

    cum_tok = cum_cost = 0
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("rank\tusername\tfollowers\ttweets\texport_tokens\tchunks\t"
                "input_tokens\toutput_tokens\tcost_batch_usd\tcost_sync_usd\t"
                "cum_input_tokens\tcum_cost_batch_usd\n")
        for n, r in enumerate(rows, 1):
            cum_tok += r["input_tokens"]
            cum_cost += r["cost_batch"]
            f.write(f"{n}\t{r['username']}\t{r['followers']}\t{r['tweets']}\t"
                    f"{r['export_tokens']}\t{r['chunks']}\t{r['input_tokens']}\t"
                    f"{r['output_tokens']}\t{r['cost_batch']:.4f}\t"
                    f"{r['cost_sync']:.4f}\t{cum_tok}\t{cum_cost:.2f}\n")

    tt = sum(r["export_tokens"] for r in rows)
    ti = sum(r["input_tokens"] for r in rows)
    to = sum(r["output_tokens"] for r in rows)
    tc = sum(r["chunks"] for r in rows)
    print(f"\n=== Community Archive token census ===", file=sys.stderr)
    print(f"accounts with tweets : {len(rows):,}", file=sys.stderr)
    print(f"tweets (non-RT)      : {sum(r['tweets'] for r in rows):,}",
          file=sys.stderr)
    print(f"export tokens        : {tt:,}", file=sys.stderr)
    print(f"chunks @ {args.chunk_tokens//1000}K        : {tc:,}",
          file=sys.stderr)
    print(f"input tokens (+tmpl) : {ti:,}", file=sys.stderr)
    print(f"output tokens        : {to:,}", file=sys.stderr)
    print(f"cost  BATCH          : ${sum(r['cost_batch'] for r in rows):,.2f}",
          file=sys.stderr)
    print(f"cost  sync           : ${sum(r['cost_sync'] for r in rows):,.2f}",
          file=sys.stderr)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
