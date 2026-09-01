# Cheap-model intentions eval — Opus 4.8 vs Haiku 4.5 vs GPT-5.6 Luna

> **Read `RESEARCH_SUMMARY.md` first** — it supersedes the per-experiment
> notes below with the full set of experiments and conclusions, including the
> chunking result and a prompt-fork correction that affects the coverage
> numbers recorded here (the runs below used the private-archive prompt; the
> admin path for tweet corpora uses `intentions_detection_public.txt`).

Can a model significantly cheaper than Claude Opus 4.8 infer usable *intentions*
from a Twitter/X pre-fill corpus? Run on @majamediaco (14,636 tweets), whose
Opus 4.8 intentions already existed as the reference.

Motivation: the pre-fill runs across hundreds of users, so the per-run price is
multiplied by ~10^3. Opus 4.8 costs ~$1.70/user (batch); the question is what a
16x cheaper model actually gives up.

**Date:** 2026-09-01. Prices verified same day against
[Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) and
[OpenAI pricing](https://developers.openai.com/api/docs/pricing).

## What's on this branch

`backend/config.py` adds two models to `SUPPORTED_MODELS`, marked
EXPERIMENT BRANCH ONLY:

| key | provider | ctx | $/MTok in | $/MTok out |
|---|---|---|---|---|
| `claude-haiku-4.5` | anthropic | 200K | 1.00 | 5.00 |
| `gpt-5.6-luna` | openai | 1.05M | 0.20 (0.40 >272K) | 1.20 (1.80 >272K) |

Everything else needed already exists on `main`: `backend/scripts/fetch_community_archive.py`
(corpus) and `backend/scripts/backfill_intentions.py` (the run).

## Reproducing

### 1. Corpus — fresh Community Archive snapshot

```sh
python backend/scripts/fetch_community_archive.py majamediaco ~/data/twitter \
    --parquet ~/data/twitter/community-archive-snapshot-2026-09-01 --refresh
```

Downloads the nightly parquet export (~873 MB) and writes
`community-archive-majamediaco.zip`. Snapshot used: `2026-09-01T07-00-21Z`
(16,112 unique rows; 14,636 importable after `compact_row` drops 1,476
retweets; replies kept, matching the pre-fill default `include_replies=True`).

Import it through the admin pre-fill flow, or seed a local dev DB.

### 2. Token + cost estimate (offline, no DB)

`estimate_prefill_tokens.py` mirrors the prod pipeline without a database:
`to_export_entry` -> `compact_row` -> node fields -> the #276 compact-run
rendering from `_build_user_export_incremental` -> substituted into
`prompts/intentions_detection.txt` -> Anthropic `count_tokens` per model.

```sh
python experiments/cheap-model-intentions/estimate_prefill_tokens.py majamediaco \
    --parquet ~/data/twitter/community-archive-snapshot-2026-09-01 \
    --out prompt.txt --json tokens.json
python experiments/cheap-model-intentions/cost_table.py tokens.json
```

Validated against the real export: the offline renderer estimated 387,613
stored units vs the pipeline's 387,868 (0.07% off).

### 3. The runs (sync path, local Docker dev)

```sh
docker compose exec -T backend python backend/scripts/backfill_intentions.py 21 \
    --model gpt-5.6-luna
docker compose exec -T backend python backend/scripts/backfill_intentions.py 21 \
    --model claude-haiku-4.5
```

`21` is majamediaco's local user id — substitute your own. Add `--dry-run`
to build the export and print the estimate without an API call.

Both keys resolve via the legacy `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
fallback in `utils/api_keys.get_api_keys_for_usage`, so the local `.env` is
enough.

## Results

| | model | corpus seen | input | output | cost |
|---|---|---|---|---|---|
| baseline | claude-opus-4.8 | 100% | 667,004 | 2,082 | $1.69 (batch) |
| | gpt-5.6-luna | 100% | 509,019 | 1,400 | $0.2061 (sync) |
| | claude-haiku-4.5 | **~32%** | 190,473 | 1,529 | $0.1981 (sync) |

Haiku's 200K window cannot hold the export. `fit_by_count` pre-sized it to
98,392 of the corpus's 305,196 stored units — the **newest ~32%**
(`keep=newest`). This is the single-chunk result, not a map-reduce.

### Cost-model accuracy

The pre-run estimates held: Opus 4.8 $1.704 predicted vs $1.69 actual (0.6%);
luna $0.209 vs $0.2061 (1.4%). luna's input tokens were predicted with
tiktoken `o200k_base` at 508,826 vs OpenAI's reported 509,019 — **0.04% off**,
confirming luna uses an o200k-compatible tokenizer (no `token_multiplier`).

### Accuracy vs baseline — full union of themes

**E** = Endorsed, *i* = Inferred, — = absent.

| # | Theme | Opus 4.8 | luna | haiku |
|---|---|---|---|---|
| 1 | Cultural stance on sincerity (T1) | **E** | — | — |
| 2 | Beam my signal / findability | **E** | *i* evidence dropped | **E** same evidence |
| 3 | Writing to see and understand more | **E** | — | — |
| 4 | Loving fully despite heartbreak | **E + conflicted** | **E** conflict dropped | *i* **contradicts** |
| 5 | Work: creative + interesting + sustainable | **E** | — | — |
| 6 | More beauty in the physical world | **E** | **E** same evidence | **E** reframed |
| 7 | Help people unlock their creativity | **E** | **E** | — |
| 8 | Stay porous / permeable + sovereign | **E** | **E** same evidence | — |
| 9 | Bridge between art and capital | *i* | **E** promoted | — |
| 10 | Gatherer / host at centre of a scene | *i* | *i* | *i* |
| 11 | Integrated, un-compartmentalized self | *i* | — | *i* |
| 12 | Memes / narrative as intentional craft | *i* | **E** promoted | — † |
| 13 | Place / relocation arc | *i* | — | — |
| 14 | A lucid dreamer | — | **E** | — |
| 15 | A permanent creative third space | — | **E** | — |
| 16 | Make poetry more popular | — | **E** | — |
| 17 | Be both serious and playful | — | **E** | — |
| 18 | A life of sustained pace | — | *i* | — |
| 19 | Remaining affectable without numbing | — | *i* | — |
| 20 | Self-disclosure as permission-giving | — | — | **E** |
| 21 | Notice and amplify what's alive | — | — | **E** |
| 22 | Hold optimism against fatalism | — | — | **E** † |
| 23 | Make space for unstructured wandering | — | — | **E** |
| 24 | Cultivate grounded presence | — | — | *i* |
| 25 | Surfacing what goes unnoticed | — | — | *i* |
| | **totals** | **13** (8E+5i) | **14** (10E+4i) | **11** (6E+5i) |

† haiku's #22 rests on the same supporting passage the baseline files under #12.

## Findings

1. **Haiku's single-chunk output contradicts the corpus.** One inferred
   intention asserted a disposition around commitment that the full archive
   directly refutes elsewhere. A confident wrong claim about a person's inner
   life, caused by seeing only the newest third. This is the decisive result.
2. **Luna strictly dominates Haiku** at the same price: full corpus, 5/8 vs
   2/8 endorsed coverage, no contradiction. Single-chunk Haiku is not viable;
   matching luna would need a 3-chunk map-reduce (~$0.31) that does not exist.
3. **Volume is not coverage.** Luna emits more intentions than the baseline
   (14 vs 13) while covering 5 of its 8 endorsed — a partly different reading,
   not a lossy subset.
4. **Only 3 of 25 themes are found by all three**, and the two cheap models'
   non-baseline themes are disjoint. Neither converges on a "true list".
5. **Both cheap models lose conflict detection.** The prompt explicitly allows
   Endorsed-and-conflicted; only Opus used it.
6. **Luna files better than it recalls** — it promotes two baseline *Inferred*
   to *Endorsed* with direct quotes, which the prompt asks for.
7. **What only the baseline finds is the concrete, idiomatic material** (the
   cultural-stance intention, the place/relocation arc). That is the specificity gap.

Open option not yet tested: two-tier — luna for the pre-fill everyone gets,
Opus 4.8 regeneration on activation.

## Ablation — removing the count anchor

`prompts/intentions_detection.txt` line 26 anchors a count:

> Be selective overall. Surface the genuinely durable, recurring ones —
> **typically 6–14 across both groups** — not every passing theme. Each must be:

All three runs landed inside 6–14 (13 / 14 / 11), two at the ceiling, and
Haiku hit 11 on *a third* of the archive — so the count looked
corpus-independent and possibly binding. If it were, coverage and novelty
would be mutually exclusive: every novel theme would displace a baseline one,
and the measured 5/8 coverage would be an artifact of the cap rather than a
recall measurement.

**Reproduce:** delete that sentence, keeping the trailing `Each must be:`, then
re-run both models. (This drops the selectivity instruction along with the
number, so it is a max-recall probe, not a clean anchor-only ablation.)

### Result: the anchor was not binding

| | with anchor | without anchor |
|---|---|---|
| luna | 14 (10E + 4i) | **14** (9E + 5i) |
| haiku | 11 (6E + 5i) | **9** (6E + 3i) |

Neither expanded; Haiku contracted. Counts are model-driven, not
anchor-driven, so the "ceiling forces a coverage/novelty tradeoff" hypothesis
is **refuted** — 5/8 was a real measurement, not a cap artifact.

### Coverage reshuffled rather than improved

luna still covers 5 of the baseline's 8 Endorsed — but a *different* 5. It
**gained #5** (work sustainability) as a properly-evidenced Endorsed block,
and **lost #2** (findability). It also strengthened #4, now resting on a
direct self-statement rather than inference.

So at least one miss was **ranking, not recall** — luna can reach the theme.
But prompt-tuning the anchor doesn't close the gap, it moves it around, which
argues the remaining gap is capability rather than prompt.

**Theme #1 (the cultural-stance intention) appears in none of the four
cheap-model runs.** That looks like genuine recall failure.

### Haiku's contradiction disappeared

The contradicted block is gone. The same supporting passage is now applied to
**working pace** instead of romance — the correct reading.
This suggests the contradiction was partly a **padding artifact**: stretching a
theme into a domain it didn't belong in to fill slots. Haiku also lost love
coverage entirely, and picked up the supporting evidence for baseline #3, folded into a
differently-framed intention.

### Caveat: single-run variance is high

Between conditions nearly every title was reworded, Haiku dropped 3 intentions
and added 1, and luna swapped one baseline theme for another — at n=1 per
cell. Some of the differences reported above (including in the main table) are
within sampling noise. A decision-grade eval needs several runs per model, and
ideally more than one user's corpus.

## Side validation of PR #277

`fit_by_count` pre-sized both models correctly on attempt 1 — Haiku went
straight to budget=98,392 instead of burning prompt-too-long retries, because
it calibrates against the corpus sum (the `faaa603` fix) rather than the 1M
placeholder. Zero rejected calls, ~30 s per run.

## Not committed

The generated artifacts (`intentions_majamediaco_*.txt`) and the rendered
prompt are **deliberately excluded** — this repo is public and they are a
named person's tweet corpus and inferred psychological profile. They are
gitignored; regenerate them with the commands above.
