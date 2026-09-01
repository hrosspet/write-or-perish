# Cheap-model intentions inference — research summary

**Date:** 2026-09-01 · **Branch:** `experiment/cheap-model-intentions-eval`

**Question.** Loore pre-fills new accounts from their public tweets and infers
an *intentions* artifact with Claude Opus 4.8 (~$1.70/user, batch). At the scale
needed to seed intention markets — hundreds of accounts, potentially the whole
Community Archive — is a significantly cheaper model good enough?

**Short answer.** The model choice turned out to matter far less than **how the
corpus is windowed**. Chunking a large corpus into ~1/3 slices moved theme
coverage more than any model swap did, and it costs less rather than more. A
model ~25x cheaper than the baseline recovers ~12 of the baseline's 13 themes
when chunked. Extracting intentions for the entire Community Archive
(734 accounts, 9.0M tweets, 387M tokens) costs **~$43**.

> **Privacy note.** This document deliberately contains **no quotes from any
> individual's tweets** and no reproduction of inferred profiles. Themes are
> referred to by neutral descriptors and index numbers. Generated artifacts are
> gitignored; this repo is public.

---

## 1. Setup

**Corpus.** Community Archive nightly parquet snapshot `2026-09-01T07-00-21Z`.
Reference account `@majamediaco`: 16,112 unique rows → **14,636 importable**
tweets after the importer's filters (1,476 retweets dropped by `compact_row`;
replies kept, matching the pre-fill default `include_replies=True`).
~1.24M chars of content, ~305K stored units (`chars/4`).

**Pipeline.** `fetch_community_archive.py` → Twitter importer → nodes →
`build_user_export_content()` in the #276 compact format → `{user_export}`
substituted into the intentions prompt → LLM.

**Prompt.** Two forks exist and the distinction is load-bearing (see §6):

| path | prompt | used for |
|---|---|---|
| `backend/tasks/intentions.py` (admin "Infer intentions") | `intentions_detection_public.txt` | tweet/public corpora |
| `backend/scripts/backfill_intentions.py` | `intentions_detection.txt` | private archives |

**Models added on this branch** (`backend/config.py`, marked experiment-only):
`claude-haiku-4.5` (200K ctx, $1/$5) and `gpt-5.6-luna` (1.05M ctx, $0.20/$1.20
base; OpenAI applies 2x input / 1.5x output above 272K tokens).

**Pricing** verified 2026-09-01 against both providers' live docs. Two findings
worth recording:

- **Anthropic has no long-context premium.** Claude 4.6+ serve the full 1M
  window at standard pricing, no beta header; batch and cache discounts apply
  across it. Only pre-4.6 models are 200K-capped, so no tier can be tripped.
- **Claude Sonnet 5 is $2/$10, not $3/$15** — the increase scheduled for
  2026-09-01 was cancelled and the introductory price became standard.
- **OpenAI's >272K surcharge is real** and `config.py` already modelled it
  correctly (`long_context_threshold: 272000`, 2.0/1.5 multipliers).

---

## 2. Experiment 1 — token accounting for the compact export

Rebuilt the export pipeline offline (no DB) and counted real tokens per model.

| tokenizer generation | models | tokens | vs stored units | chars/token |
|---|---|---|---|---|
| Opus 4.7-gen | opus-4.8 / opus-5 / fable-5 / sonnet-5 | 666,673 | 2.18x | 2.33 |
| previous gen | sonnet-4.6 / haiku-4.5 | 550,350 | 1.80x | 2.82 |
| o200k (OpenAI) | gpt-5.x | 508,826 | 1.67x | 3.06 |

**Conclusions.**

1. The #276 compact format holds at scale — 2.18x measured here vs 2.16x in the
   original commit on a smaller corpus.
2. **Tokenizer generation is a real cost variable.** The same corpus is 21%
   more tokens on the newer tokenizer. This can invert a per-MTok comparison:
   Sonnet 4.6 and Sonnet 5 have the same nominal list price for many workloads,
   but the older tokenizer makes 4.6 cheaper per corpus.
3. `tiktoken o200k_base` predicted OpenAI's reported usage to within **0.04%**,
   so local estimation for OpenAI models needs no API calls and no fudge factor.

---

## 3. Experiment 2 — whole-context comparison (first pass)

One intentions run per model over the full corpus.

| | corpus seen | input | output | cost | intentions |
|---|---|---|---|---|---|
| opus-4.8 (baseline) | 100% | 667,004 | 2,082 | $1.69 batch | 13 (8E + 5i) |
| gpt-5.6-luna | 100% | 509,019 | 1,400 | $0.2061 sync | 14 (10E + 4i) |
| claude-haiku-4.5 | **~32%** | 190,473 | 1,529 | $0.1981 sync | 11 (6E + 5i) |

Haiku's 200K window cannot hold the export; `fit_by_count` sized it down to the
newest ~32% of the corpus.

**Conclusions.**

1. **Cost predictions were accurate** — opus $1.704 predicted vs $1.69 actual
   (0.6%), luna $0.209 vs $0.2061 (1.4%). The cost model is trustworthy.
2. **Truncated Haiku produced a claim the full corpus contradicts.** One
   inferred intention asserted a disposition that the archive directly refutes
   elsewhere. It was built on a *real, correctly-quoted* passage that Haiku
   transplanted into the wrong life-domain. **Evidence-grounding checks cannot
   catch this failure mode** — the citation verifies, the inference is still
   wrong. For a user-visible artifact about someone's inner life this is the
   worst outcome, worse than omission.
3. Single-chunk Haiku was therefore not viable as configured.

---

## 4. Experiment 3 — is the prompt anchoring the output count?

The prompt contained *"typically 6–14 across both groups"*. All runs landed
inside that range (13/14/11), two at the ceiling, and Haiku hit 11 on a third of
the archive — suggesting the count might be prompt-driven and that coverage and
novelty were being forced to trade off against a cap.

Removed the sentence and re-ran both models.

| | with anchor | without anchor |
|---|---|---|
| luna | 14 | **14** |
| haiku | 11 | **9** |

**Conclusions.**

1. **The anchor was not binding.** Neither model expanded; Haiku contracted.
   Output counts are model-driven, so the measured coverage figures are real
   measurements, not cap artifacts.
2. The count is more plausibly governed by the prompt's **semantic** constraints
   — *"distinct — no two that are the same intention reworded"* and *"do not
   fabricate or pad"* — which make the count a function of how aggressively a
   model merges near-duplicates. That is a model capability, not a parameter.
3. Removing the sentence also removed the selectivity instruction, so this is a
   max-recall probe rather than a clean anchor-only ablation.
4. Haiku's contradicted claim **disappeared** in the no-anchor run, and the
   underlying passage was re-applied to the correct domain — suggesting the
   contradiction was partly a **padding artifact** (stretching a theme into a
   domain to fill slots).

---

## 5. Experiment 4 — quality of the *Inferred* blocks

`Inferred` blocks must cite evidence. Extracted every quoted particular from
those citations and checked it against the corpus.

| model | inferred | cited particulars | exact match | unverifiable |
|---|---|---|---|---|
| opus-4.8 (baseline) | 5 | 14 | 11 | **3 (21%)** |
| luna | 4 | 11 | **11** | **0** |
| haiku | 5 | 18 | 13+2 partial | 3 (17%) |

**Conclusions.**

1. **Cheap models are not worse at evidence fidelity.** The baseline had the
   highest unverifiable rate of the anchored runs. "Unverifiable" mostly means
   the model rendered its own characterisation in quote marks rather than
   fabricating — a formatting liberty every model takes — but it means ~20% of
   cited particulars can't be grep-checked in any model's output.
2. **The structural difference is trajectory vs character.** The baseline's
   inferred set contains *temporally extended, biographical* inferences —
   tracking an arc across years and noting an outcome materialising — and cites
   named people, projects and events. The cheap models' inferred sets are almost
   entirely *dispositional traits*: no time-indexed inference, no named
   particulars, in any cheap run.
3. Consequence: the cheap models' inferred intentions are **valid but
   low-information**. Nothing is false, but many would be plausible about a
   large class of reflective people. For an artifact whose value is making
   someone feel seen, generic-but-true is a failure mode invisible to any
   correctness check.
4. **Boundary judgment does not uniformly favour the expensive model.** Luna
   promoted two of the baseline's *Inferred* to *Endorsed* with direct quotes,
   which the prompt explicitly requires; it also under-filed one quotable
   aspiration as Inferred. Errors run both ways.

---

## 6. Experiment 5 — chunking (the decisive result)

Split the corpus into 3 time-ordered chunks of roughly equal *rendered* weight,
so the whole archive fits Haiku's window and the comparison is
coverage-matched. One intentions run per (chunk, model); **no aggregation step**
— chunk outputs were compared manually.

A prompt confound was discovered mid-experiment and corrected: the baseline had
been produced by the admin path (public prompt) while all earlier runs used the
private-archive prompt. The final runs use `intentions_detection_public.txt` for
both models, matching what production would use for a tweet corpus.

**Coverage of the baseline's 13 themes** (T1–T13, neutral descriptors:
cultural-stance / findability / writing-as-perception / love-and-risk /
work-sustainability / physical-world beauty / drawing out others / openness /
art↔capital / hosting / self-integration / narrative craft / place):

| condition | corpus seen | coverage | intentions emitted |
|---|---|---|---|
| luna, whole context | 100% | **8/13** | 14 |
| haiku, single chunk | 32% | **~2/13** | 11 |
| **luna, 3 chunks (public prompt)** | 100% | **12/13** | 42 |
| **haiku, 3 chunks (public prompt)** | 100% | **~10/13** | 35 |
| luna, 3 chunks (private prompt) | 100% | 12/13 | 38 |

**Conclusions.**

1. **Windowing dominates model choice.** Luna went 8/13 → 12/13 on the same
   model and corpus purely by reading it in thirds. Three themes I had
   previously attributed to a capability gap — including one absent from *all
   four* whole-context cheap runs — appeared immediately when chunked, with the
   supporting quotations the baseline had used. **It was context dilution, not
   recall failure.**
2. **Haiku with equal coverage nearly closes the gap** (~2/13 → ~10/13), and its
   corpus-contradicting claim disappeared. Its earlier failure was the
   truncation, not the model. It still misses two themes outright.
3. **Chunking is cheaper, not more expensive.** Splitting puts every chunk below
   OpenAI's 272K threshold, so the same token volume bills at the base tier
   instead of the long-context tier — roughly half. Template re-payment and
   extra output tokens are ~3% of the bill. **The recall win is essentially
   free**, so chunk size should be tuned for quality, not cost.
4. **The prompt fork changes filing confidence, not coverage.** Luna scored
   12/13 on both prompts, but the public prompt moved one theme from *Endorsed*
   (with a direct quotation) to *Inferred* — the intended effect of its
   "performed register / ground every claim in what was posted" framing.
5. Chunked Haiku produced the **first ambivalence marker any cheap model
   managed** (`*active, with tension*`), an affordance the prompt allows and
   only Opus had used before.
6. **Cross-model agreement on themes the baseline missed.** Chunked luna and
   Haiku independently surfaced two themes absent from the baseline. The
   baseline is a strong reference, **not ground truth** — it has its own blind
   spots.
7. **Aggregation is untested and is where the risk now sits.** 42 chunk-level
   intentions must become ~13. Themes fragment across chunks under different
   names; a naive merge will over-collapse or leave duplicates.

**Measured cost, 3 chunks, sync:**

| | input | output | sync | batch (calc) |
|---|---|---|---|---|
| gpt-5.6-luna | 510,949 | 4,686 | **$0.108** | $0.054 |
| claude-haiku-4.5 | 552,633 | 4,567 | $0.575 | $0.288 |
| opus-4.8 (whole, baseline) | 667,004 | 2,082 | $3.387 | $1.694 |

---

## 7. Experiment 6 — full Community Archive token census

Exact o200k token counts for every account, rendered in the compact export
format (`ca_corpus_token_census.py`, ~4 min, no API calls). Validated at
**+0.59%** against the one account measured through the API.

| | |
|---|---|
| accounts | **734** |
| tweets (non-RT, deduped) | **9,016,596** |
| export tokens | **387,060,714** |
| chunks @ 100K | 4,278 |
| input tokens (incl. per-chunk template) | 391,107,702 |
| output tokens (~1,500/chunk, measured) | 6,417,000 |
| **cost — Batch** | **$42.96** |
| cost — sync | $85.92 |

Priority tiers by follower count: top 10 = $2.59 · top 100 (≥8,639 followers,
3.3M tweets) = **$15.95** · top 500 = $38.90 · all 734 = $42.96. Median account:
174K tokens, **$0.019**.

**Conclusions.**

1. **Cost is not a constraint at any plausible scale.** The whole archive is
   ~$43; the top 100 accounts ~$16. Aggregation adds ~$1.30.
2. **Follower count and corpus size are nearly uncorrelated** — the most-followed
   account has a small corpus, and the largest corpus belongs to an account with
   half its reach. Follower-ranking buys reach, not cost control; cost is driven
   by a handful of prolific writers and is small regardless.
3. **Throughput fits.** 391M tokens is ~39% of gpt-5.6-luna's 1B tokens/day
   batch queue. At ~305KB per 100K-token chunk the 200MB batch-file cap implies
   ~7 submissions; 4,278 requests is well under the 50K/batch limit.

---

## 8. Operational findings

- **PR #277 validated.** `fit_by_count` pre-sized both models correctly on the
  first attempt (Haiku straight to a fitting budget), with zero prompt-too-long
  retries and zero wasted spend — it calibrates against the corpus sum rather
  than the 1M placeholder.
- **OpenAI Batch needs the `api.files.write` scope.** A restricted key 401s on
  the input-file upload; `batch_submit` catches it, logs it, and returns no
  batch id, so the caller sees a generic "submit failed". Production's key has
  the scope; the local dev key did not. Worth surfacing this error rather than
  logging it.
- **Batch payload caps are size-bound, never request-bound.** One user's prompt
  is ~1.6MB as JSONL (+2.2% for JSON escaping), so a 256MB Anthropic batch holds
  ~158 users and a 200MB OpenAI batch ~123 — versus request caps of 100K/50K.
  Splitting a user across chunks does not change this; the bytes are the same.
- **Chunk balancing must count rendered weight, not stored content.** Balancing
  on node `token_count` alone under-counts chunks made of many short tweets
  (each entry carries ~5.6 stored units of `[timestamp]` scaffolding); ignoring
  it put one chunk 5K tokens over Haiku's window.
- **Prompt caching buys nothing here.** Every corpus is unique; the only shared
  prefix is the ~1.4K-token template, below the cacheable minimum.
- The backend container mounts only `backend/`, so files written elsewhere in
  `/app` are lost on recreate. Copy results out before `docker compose up -d`.

---

## 9. Threats to validity

- **n=1 corpus, n=1 run per cell.** Between the anchor conditions nearly every
  intention title was reworded and Haiku's count moved by 2 — run-to-run
  variance is substantial. Coverage numbers should be read as ±1–2.
- **Coverage is scored against one baseline that is not ground truth** (see
  §6.6). "12/13" measures agreement with Opus 4.8, not correctness.
- **Early rounds carried a prompt confound** (private vs public fork). Corrected
  in §6; the chunking and anchor findings are within-condition and unaffected.
- **Keyword matching over-reports coverage** — spot checks found false positives
  from substring hits inside unrelated evidence lines. Final numbers in §6 are
  manually verified; treat automated coverage scoring as a triage aid only.
- **Aggregation is unmeasured.** Every chunked result reports raw chunk output.

---

## 10. Recommendations and open questions

1. **Chunk the corpus regardless of model.** It is the highest-leverage change,
   improves coverage substantially, and reduces cost.
2. **gpt-5.6-luna chunked is the value pick** — ~92% of baseline coverage at
   ~3% of baseline cost. Haiku chunked is viable but ~5x luna's price for
   slightly worse coverage.
3. **Build and evaluate the aggregation step** — the largest untested risk.
4. **Repeat for confidence before committing at scale**: 3 runs per condition
   across 2–3 corpora would settle whether 12/13 is stable. ~$2 of API spend.
5. **Consider a two-tier policy**: cheap chunked pre-fill for everyone, Opus
   regeneration on activation, so the sharp artifact goes to people who engage.
6. Open: does the `distinct` criterion (not the numeric anchor) govern output
   count? Relaxing it is the clean single-variable test, and would show whether
   the cheap models' abstractness is a *merging* artifact.

---

## 11. Reproduction

See `README.md` in this directory for commands. Artifacts and rendered prompts
are gitignored by design; regenerate them from the snapshot. Key scripts:

| script | purpose |
|---|---|
| `estimate_prefill_tokens.py` | offline token/cost estimate for one account |
| `cost_table.py` | model cost comparison for a measured corpus |
| `chunked_batch_run.py` | N-chunk split + per-(chunk, model) run, batch or sync |
| `ca_corpus_token_census.py` | exact token census of the whole archive |
