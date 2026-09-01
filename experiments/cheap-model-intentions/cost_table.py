"""Cost table for one intentions run over @majamediaco's prefill corpus.

Input-token counts are measured (Anthropic count_tokens for the Claude models,
tiktoken o200k_base for the OpenAI ones).

Prices verified 2026-09-01 against:
  - platform.claude.com/docs/en/about-claude/pricing  (model + batch + long ctx)
  - developers.openai.com/api/docs/pricing            (short/long context tiers)

Long context:
  Anthropic — NONE. "Claude 4.6 and later models include the full 1M token
  context window at standard pricing"; 1M is the default, no beta header, and
  batch/cache discounts apply at standard rates across the full window.
  Pre-4.6 models in play here (Sonnet 4.5, Haiku 4.5) are 200K-only, so there
  is no long-context tier to trip.
  OpenAI — input DOUBLES and output x1.5 above 272K tokens. This workload is
  always in the long tier.
"""
import json

import sys

# JSON written by estimate_prefill_tokens.py --json
TOK = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "tokens.json"))
IN = TOK["real_tokens"]
OPENAI_IN = 508_826          # tiktoken o200k_base on the same prompt
OUT = 3_000                  # intentions artifact ~8.7 KB -> ~2.5-3k tokens
LC_THRESHOLD = 272_000       # OpenAI only

# name, short-ctx in$, short-ctx out$, long-ctx in$ (None = no premium),
# long-ctx out$, ctx window, measured input tokens, in repo SUPPORTED_MODELS?
ROWS = [
    ("claude-opus-4-8  (baseline)", 5.00, 25.00, None, None, 1_000_000, IN["claude-opus-4-8"], True),
    ("claude-opus-5",               5.00, 25.00, None, None, 1_000_000, IN["claude-opus-5"], True),
    ("claude-fable-5",             10.00, 50.00, None, None, 1_000_000, IN["claude-fable-5"], True),
    ("claude-sonnet-5",             2.00, 10.00, None, None, 1_000_000, IN["claude-sonnet-5"], False),
    ("claude-sonnet-4-6",           3.00, 15.00, None, None, 1_000_000, IN["claude-sonnet-4-6"], True),
    ("claude-haiku-4-5",            1.00,  5.00, None, None,   200_000, IN["claude-haiku-4-5"], False),
    ("gpt-5.4",                     2.50, 15.00, 5.00, 22.50, 1_050_000, OPENAI_IN, True),
    ("gpt-5.6-sol",                 4.00, 20.00, 8.00, 30.00, 1_050_000, OPENAI_IN, True),
    ("gpt-5.5",                     5.00, 30.00, 10.00, 45.00, 1_050_000, OPENAI_IN, True),
    ("gpt-5.6-terra",               2.00, 12.00, 4.00, 18.00, 1_050_000, OPENAI_IN, False),
    ("gpt-5.6-luna",                0.20,  1.20, 0.40,  1.80, 1_050_000, OPENAI_IN, False),
]


def price(inp, ip, op, lip, lop):
    """Effective rates for this prompt size."""
    if lip is not None and inp > LC_THRESHOLD:
        return lip, lop, True
    return ip, op, False


base = None
print(f"{'model':<28} {'list$/M':>8} {'eff$/M':>7} {'LC?':>4} {'ctx':>7} {'in tok':>9} "
      f"{'sync$':>7} {'batch$':>7} {'vs4.8':>6}  {'fits':<14} cfg")
print("-" * 118)
for name, ip, op, lip, lop, ctx, inp, in_cfg in ROWS:
    eip, eop, lc = price(inp, ip, op, lip, lop)
    c = inp * eip / 1e6 + OUT * eop / 1e6
    if base is None:
        base = c
    fits = "yes" if inp + OUT <= ctx else f"NO (~{-(-inp // (ctx - 20000))} chunks)"
    print(f"{name:<28} {ip:>8.2f} {eip:>7.2f} {('2x' if lc else '-'):>4} "
          f"{ctx/1e6:>6.2f}M {inp:>9,} {c:>7.3f} {c/2:>7.3f} {c/base:>5.2f}x  "
          f"{fits:<14} {'yes' if in_cfg else 'ADD'}")

print(f"\ncorpus: {TOK['tweets_in_export']:,} tweets, {TOK['chars']:,} chars, "
      f"{TOK['stored_token_units']:,} stored units (chars/4), "
      f"{TOK['prompt_chars']:,}-char prompt")
print(f"output assumed {OUT:,} tok (~2% of cost); batch = 50% off both providers")
