# Design: equal-chunk planning for profile generation

**Status:** wired (PR #285). The planner sizes every chunk in both
pipelines, the continue rule replaces the pinned-account special case and
the minimum-chunk deferral, tokenizer families replace `token_multiplier`,
and `backend/scripts/replan_tail.py` repairs pre-filled accounts whose
chain stopped short. Remaining: the staging canary and the prod repair
run (see Rollout). Date: 2026-09-03.
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
- Windows include every node sharing the boundary timestamp (the
  incremental window's budget loop and `_preselect_node_ids` alike), or a
  node on the cutoff's second is never read.

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

Landed (PR #285):
- `backend/utils/chunk_plan.py` — `plan_chunks`, `next_window_budget`
  (the final window over-asks by `FINAL_CHUNK_OVERASK_UNITS`),
  `max_units_for_cap`; the constants `CHUNK_TARGET_UNITS` (T),
  `UPDATE_THRESHOLD_UNITS` (the 80k organic gate, formerly four copies in
  three files) and `CAP_MARGIN` (μ). Tests in
  `backend/tests/test_chunk_planner.py`.
- `backend/routes/export_data.py` — the incremental window's budget loop
  takes every row sharing the boundary timestamp (`_preselect_node_ids`
  had the same fix, but the chunk loop renders through the incremental
  path); the window reports `unit_count`; `count_remaining_units` sums
  the remainder over exactly the rows the window draws from.
- `backend/llm_providers.py` — `model_input_cap` (window minus output,
  `max_input_tokens`, `long_context_threshold`, whichever binds first);
  `fit_by_count(strict=False)` returns the last build instead of raising.
- `backend/tasks/exports.py` — `_chunked_profile_loop` plans every window
  (`count_remaining_units` → `next_window_budget` → `build_fitted_chunk`,
  the real-count check shared with the batch builder); no
  `MIN_CHUNK_TOKENS`, no `_has_more_source_after`; `_do_initial_generation`
  goes through the same loop (a corpus that plans into one chunk is saved
  as "initial"; `_single_pass_generation` is gone); tokenizer families,
  `TOKENS_PER_UNIT_PRIOR` by content class, `tokens_per_unit`,
  `record_token_ratio` (billed input tokens per unit, family-tagged);
  `should_continue_chain`; `build_chunk_prompt` in units on both sides;
  `source_tokens_used` is cumulative units; the sync heartbeat applies the
  continue rule before its interval gate.
- `backend/tasks/profile_batch.py` — `_build_next_profile_request` plans
  the same way; `_should_seed` uses the continue rule; batch items carry
  `chunk_units` (a pre-planner item without it still accumulates its
  billed tokens); `_remaining_token_count` is gone.
- `backend/config.py` — `tokenizer_family` on every model,
  `max_input_tokens: 922000` on the 1.05M-window OpenAI models,
  `token_multiplier` removed. `backend/models.py` —
  `User.profile_token_ratio_family` (migration auto-generates on deploy).
- `backend/routes/admin.py` — the users list marks a chain "stuck" by the
  continue rule.
- `backend/scripts/replan_tail.py` — repair for pre-filled accounts (dry
  run by default; `--apply`, `--seed`).
  `backend/scripts/simulate_chunk_plan.py` — dry run on the wired code.

## Rollout

- Deploy: the auto-generated migration adds
  `user.profile_token_ratio_family`. Existing `profile_token_ratio` values
  (the old residual-of-the-multiplier figure) carry no family and are
  ignored; every user re-measures on their next chunk from the family
  prior.
- **One-time catch-up on deploy.** Every account whose last update left a
  sub-minimum tail unread (the old deferral did this after most
  multi-chunk updates) now has unread data older than its latest version,
  so the continue rule runs one update for it at the next heartbeat /
  seed pass: one chunk call plus one integration call per account,
  regardless of the interval and 80k gates. Expect a burst of roughly two
  LLM calls per active profiled account in the first hours after deploy.
  `PROFILE_UPDATES_PAUSED` holds it back if it should be staged.
- Known edge of the continue rule: a node written while the user's own
  update is generating (after the window's cutoff, before the version is
  saved) reads as unfinished and gets a small chunk of its own at the
  next pass.
- Repair: `python backend/scripts/replan_tail.py --all-prefilled` (dry
  run) on prod, then `--user xiq --apply --seed` per account. The revert
  copy re-tips the chain; the continue rule and the planner do the rest;
  the old versions stay as history.
- Then the staging canary on one pinned account, then prod.

## Watch

- The tree header's index path grows with depth (about 300 chars per node
  at depth 120). Check nothing parses it, then shorten it.
- Claude-new tweet priors are estimated from o200k counts; the first
  measured chunk replaces them.
