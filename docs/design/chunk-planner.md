# Design: equal-chunk planning for profile generation

**Status:** groundwork landed (pure planner + tests, boundary-tie fix,
dry-run script). Wiring into the chunk loops, the seeding rule and the
tokenizer-family config is the next PR. Date: 2026-09-03.
**Related:** issue #259 (chunks crossing Sol's 272k pricing tier), the
2026-08-27 tail commits c385a45 / bcac412.

## Symptom

Pre-filled profiles stop around 2025. A prefilled corpus is cut into
fixed-budget windows; whatever remains below the minimum chunk waits
for "more data" that a prefilled account never produces. On @exgenesis
(35,396 tweets, 621k content units) today's sizing leaves a 44k-unit tail
from 2025-09-06 onward unread — exactly "his profile reads as 2025".

## Units

- **Content unit** `u(n)` = `Node.token_count` = chars/4 of the node's own
  text. Stored, model-independent, summable in SQL without decrypting.
  Chunk *balance* is defined in this unit and nothing else.
- **Real tokens** are used only where money or the context window is at
  stake: the cap check and the calibration ratio. They are never written
  to a node — users switch models mid-thread, and a billed count covers
  the whole prompt (profile + template + scaffolding), so it cannot be
  attributed to nodes.
- **Tokenizer families** (three in play): Claude old (Opus ≤ 4.6, Sonnet
  4.6, Haiku 4.5); Claude new, introduced with Opus 4.7 (Opus 4.7/4.8/5,
  Sonnet 5, Fable 5/5.1); OpenAI o200k_base (GPT-5, 5.5, 5.6 Sol/Luna).
  Anthropic states 1.0–1.35× new/old; measured 1.21× on compact tweets.
  tiktoken o200k_base matches Sol billing exactly; Anthropic's count
  endpoint is free (5k–20k RPM, separate limit).
- **Rendering overhead is per node** and varies with node class (compact
  tweet line vs tree node header), tweet length and thread depth. It is
  measured on the rendered chunk, never modelled.

Measured tokens per unit (prompt overhead included):

| Family | Loore threads (hrosspet, prod) | Compact tweets (exgenesis) |
|---|---|---|
| o200k (gpt-5.2, Sol) | 0.85–1.13 | 1.61–2.39 (older tweets denser) |
| Claude old (Opus 4.6) | 1.08–1.53 | — |
| Claude new (Opus 4.8, Fable 5) | 1.51–2.18 | ≈ 2.1–3.1 (estimate) |

## Planner (`backend/utils/chunk_plan.py`)

Run before every chunk, over the remainder from the current cutoff.
Stateless: restarts, imports and cap-forced splits re-plan the same way.

```
R = Σ u(n) for n after the cutoff              # SQL sum, window scope
k = max(1, floor(R / T + 0.5))                 # round the COUNT, not the size
S = R / k                                      # every chunk within T/(2k) of T
room = (1 - μ)·C_m - P - Θ                     # cap minus profile and template
k = max(k, ceil(R·ρ / room))                   # cap only ever raises k
```

- `T` = `CHUNK_TARGET_UNITS` = 90k. Matches the chain's existing cadence:
  hrosspet's updates since March 2026 carried 59k–106k units, mean 82k.
- `C_m` = input cap. Both labs count input + output in one window.
  Anthropic: window − max_tokens, no price tier at 1M. OpenAI: the
  272k-input price tier binds first (whole request ×2 input, ×1.5
  output); hard input ceiling 922k = window − fixed 128k output reserve.
- `ρ_{u,f}` = billed input tokens per unit for the user on the family,
  learned after each billed call (`record_token_ratio`), seeded from a
  family prior per content class.
- The cap branch can leave chunks more than 25 % off T (down to about
  half the largest size that fits) — the price of staying under the tier.
- The final chunk (k = 1) must ask for the whole remainder: the export
  builder keeps a header allowance out of the budget, so a window lands
  slightly short of S.
- Windows include every node sharing the boundary timestamp (fixed in
  `_preselect_node_ids`), or a node on the cutoff's second is never read.

**Continue rule** (replaces the pinned-account special case and the
minimum-chunk deferral): remaining data *older* than the last version is
an unfinished chain → continue regardless of thresholds; remaining data
*newer* than it is organic growth → the 80k gate applies.

**Proportionality** in the update prompt: both terms in units
(`U_new / (U_past + U_new)`). Today past is billed tokens and new is
chars/4, which roughly halves every chunk's stated share.

## Dry run, @exgenesis, Sol, 2026-09-03

`backend/scripts/simulate_chunk_plan.py --user exgenesis --model gpt-5.6-sol`
(real prefill import into local dev, real export windows and rendering,
exact o200k counts, no LLM calls, profile assumed 8k tokens).

| | Today's sizing | Planner, T = 90k |
|---|---|---|
| Chunks | 9 × 44,879–71,528 units | 7 × 88,652–89,051 units |
| Coverage | 92.9 %; 44,336-unit tail from 2025-09 dropped | 100 %, leftover 0 |
| Billed per chunk | 111k–139k | 143k–212k (2013–16 tweets at 2.39 tok/unit) |
| Largest prompt | 147k | 214k, under the 258k room below 272k |

## Changes

Done in this PR:
- `backend/utils/chunk_plan.py` — `plan_chunks`, `CHUNK_TARGET_UNITS`;
  re-exported from `backend/tasks/exports.py`. Tests in
  `backend/tests/test_chunk_planner.py`.
- `backend/routes/export_data.py` — `_preselect_node_ids` includes
  boundary-timestamp ties.
- `backend/scripts/simulate_chunk_plan.py` — dry run, `--today` replays
  the current sizing.

Next PR:
- `_chunked_profile_loop`, `_iterative_generation`,
  `_build_next_profile_request`: size by `plan_chunks` over the remainder
  (same node scope as the window), final chunk takes everything, delete
  the `MIN_CHUNK_TOKENS` deferral; `fit_by_count` limit from the pricing
  tier plus a per-model max-input figure (Sol 922k).
- `_should_seed`: the continue rule.
- `build_chunk_prompt`: both share terms in units; `source_tokens_used`
  becomes cumulative units (changes the profile header's token figure).
- Config/models: `token_multiplier` → tokenizer family + family prior;
  Opus 4.8 joins the new family; the user ratio gains a family tag.
- One module for T, the threshold and μ (four copies in three files today).
- Repair script for prefilled accounts: branch from the latest chain
  version whose remainder plans into ≥ 1 full chunk, rebuild, re-integrate.
- Local Docker → staging canary on one pinned account → prod.
