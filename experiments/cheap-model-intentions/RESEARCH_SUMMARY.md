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
- **Prompt caching buys nothing for extraction.** Every corpus is unique; the
  only shared prefix is the ~1.4K-token template, below the cacheable minimum.
  (It *can* help matching, where the anchor account repeats — but see §11, where
  it stops paying once intentions are aggregated.)
- **Batch is slow and opaque, and that shapes iteration.** A 3-request Anthropic
  batch was still `in_progress` after 26 minutes (Anthropic's guidance is most
  finish within an hour, ceiling 24h; OpenAI's completion window is fixed at
  `24h`). Nothing errors, nothing reports progress beyond request counts. For
  the tight experiment loop, sync calls returned in 23–30 s, so **iterate on
  sync and reserve batch for the committed production sweep**.
- **Batch API hard limits** (both providers), the ones that actually bind:

  | | Anthropic | OpenAI |
  |---|---|---|
  | requests/batch | 100,000 | 50,000 |
  | input file size | 256 MB | 200 MB |
  | completion window | up to 24h | fixed `24h` |
  | batch creation rate | — | 2,000/hour |
  | results retained | 29 days | — |
  | caching inside a batch | supported | TTL won't survive it |

- **Per-model batch queue caps (`TPD`) are a real ceiling at archive scale.**
  `gpt-5.6-luna` 10M TPM / 1B tokens-per-day batch queue; `gpt-5.6-sol` and
  `-terra` 4M TPM / 200M TPD. So luna absorbs a whole-archive job in a day where
  sol/terra would need ~2 days for extraction alone.
- **Plan submissions from payload, not request count.** At ~3.05 chars/token
  plus 2.2% JSONL escaping:

  | job | tokens | payload | requests | OpenAI batches | days @1B TPD |
  |---|---|---|---|---|---|
  | extraction, whole archive | 391M | 1,219 MB | 4,278 | 7 | 0.39 |
  | aggregation, 1 call/account | 6M | 19 MB | 734 | 1 | 0.01 |
  | matching, exhaustive pairwise (raw) | 4,440M | 13,840 MB | 269,011 | **70** | **4.44** |
  | matching, exhaustive pairwise (agg) | 753M | 2,347 MB | 269,011 | 12 | 0.75 |
  | matching, coarse pass (300-tok sig) | 188M | 586 MB | 269,011 | 6 | 0.19 |
  | matching, chunk x chunk N=15 (agg) | 15M | 47 MB | 120 | **1** | 0.01 |

  Every job is size-bound, never request-bound. The spread is the argument for
  chunk x chunk on operational grounds alone: 1 submission and hours, versus 70
  submissions and ~4.5 days of queue for the raw pairwise sweep.
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
- **The artifact being costed may be the wrong unit for matching.** The
  intentions definition was designed for self-reflection, not for matching
  against other people; see §10, *Open questions on scope and framing*.

---

## 10. Recommendations and open questions

1. **Chunk the corpus regardless of model.** It is the highest-leverage change,
   improves coverage substantially, and reduces cost.
2. **gpt-5.6-luna chunked is the value pick** — ~92% of baseline coverage at
   ~3% of baseline cost. Haiku chunked is viable but ~5x luna's price for
   slightly worse coverage.
3. **Measure the aggregation step next — it is now the critical path.** One
   dedupe call on the reference account's 42 chunk-level intentions (~$0.01)
   answers two questions the rest of the plan depends on: the real compression
   ratio (is `~13/account` right?) and whether dedupe preserves specific,
   idiomatic themes or regresses toward the generic. Every aggregated figure in
   §11 rests on this, and §5 gives concrete reason to expect the regression.
4. **Repeat for confidence before committing at scale**: 3 runs per condition
   across 2–3 corpora would settle whether 12/13 is stable. ~$2 of API spend.
5. **Consider a two-tier policy**: cheap chunked pre-fill for everyone, Opus
   regeneration on activation, so the sharp artifact goes to people who engage.
6. Open: does the `distinct` criterion (not the numeric anchor) govern output
   count? Relaxing it is the clean single-variable test, and would show whether
   the cheap models' abstractness is a *merging* artifact.
7. **Measure single-call attentional recall for chunk x chunk** (~$2): run one
   chunk-pair, then exhaustive pairwise over the same accounts, and compare.
   That fraction sets how many re-partition rounds are needed, which is the only
   free variable in the chunk x chunk budget.
8. Cost optimisation now belongs in **extraction**, not matching (see §11) —
   and the earlier premise that matching cost would force compromises on
   extraction quality no longer holds.

---

### Open questions on scope and framing

Recorded after the study, before committing to an architecture. None of these
is measured, and each changes how the tables in §7 and §11 should be read.

**Time slices as a matching lever.** Chunking already partitions every corpus
by time (§6), so per-period intentions fall out of extraction for free, before
any aggregation. Intentions inferred from the latest slice are plausibly more
valuable to match than ones from years ago, which may be outdated. That gives
a dial between two failure modes: a pool that is too small (only the latest
slice of everyone), where no reasonable match exists, and a pool that is too
large (all periods), where a genuinely serendipitous match is more likely to
exist but must be paid for with quadratically more compute — the total volume
of intentions is the term every matching route in §11 is quadratic in. Two
consequences for the pipeline: aggregation as sketched (dedupe to ~13 per
account) collapses the time index, so if recency is to be usable downstream
the aggregated intentions must carry a last-seen period; and the trajectory
inferences that distinguished the baseline in §5 are exactly the ones that need
the old slices, so "latest only" has a quality cost as well as a recall cost.

**Whether the intentions definition is right for matching at all.** The
intentions prompt was designed for the reflective side of Loore: its
definition of an intention and its level of abstraction are chosen to help one
person gain clarity about their own life. Nothing here tests whether that is
the right unit for matching against other people's intentions, and there is
reason to doubt it — §5 found the cheap models' inferred intentions are true of
a large class of reflective people, and an intention that is true of many
people matches everyone and therefore no one. All of §11 costs the matching of
the *current* artifact; if the unit is wrong, those tables cost the wrong
thing. The cheap way to find out, before any sweep: a small-scale matching
experiment over a handful of accounts, or simply reading a dozen accounts'
intentions side by side and judging whether a match between them would make
common sense to the people involved or whether the intentions are too abstract
to act on. This question is upstream of every other decision on this page.

**Whether to include the largest accounts.** The biggest accounts (@visakanv
is the canonical example) carry the most data, so they incur the largest
extraction cost and the hardest aggregation (§11, the heavy tail: a 125:1
compression), and they have the largest followings, so convincing one of them
to use Loore would be the biggest publicity win. Against that: the people
behind very large accounts are the hardest to convince, and it may be hardest
to be genuinely useful to them. That is the highest-variance bet in the pool
and probably not the one to make first. The alternative is to focus on middle
accounts: enough data for the inferred intentions to be good, a realistic
chance of actually helping the person, and far more of them. Two facts from
§7 sharpen the choice. Cost is not a reason to exclude anyone (top 10 is
$2.59, top 100 is $16). And "largest" names two different sets — follower
count and corpus size are nearly uncorrelated — so an inclusion rule should
say which axis it means: the aggregation-quality risk is about corpus size,
the publicity and reachability question is about followers.

---

## 11. Matching: cost ceilings and candidate architectures

Extraction is linear in accounts; **matching is quadratic**. Rough ceilings for
the 734-account archive (269,011 account pairs, raw unaggregated intentions,
measured per-intention size: 100 tok o200k / 150.6 tok Opus 4.8):

| approach | calls | input | luna batch | opus-4.8 batch |
|---|---|---|---|---|
| exhaustive pairwise, raw | 269,011 | 4.44B | $493 | $17,672 |
| …with prefix caching, dense-first, sync | 269,011 | 1.25B | $251 (sync) | — |
| exhaustive pairwise, aggregated | 269,011 | 0.75B | $124 | — |
| coarse pass on 300-tok signatures | 269,011 | 188M | **$25** | — |
| **N=15 chunk x chunk (raw)** | **120** | **96M** | **$20** | **$373** |
| whole archive in one window (aggregated) | 1 | 0.95M | $0.47 | $8.44 |
| cluster-pair triage (200 clusters) | 19,900 | — | $3.32 | — |
| random control, 10k pairs | 10,000 | — | $4.60 | — |

Scaling of the exhaustive route (luna batch): 734 accounts $493 · 1,468
$1,973 · 2,936 $7,893 · 7,340 $49,342. Doubling the pool quadruples the bill —
exhaustive pairwise works once, at today's size, and never again.

### The same ceilings on AGGREGATED intentions (~13/account)

9,542 intentions total; 1,300 tok/account (luna) / 1,958 (Opus); 0.95M / 1.44M
archive-wide.

| approach | calls | input | luna batch | opus-4.8 batch |
|---|---|---|---|---|
| exhaustive pairwise | 269,011 | 753M | $123.75 | $3,777 |
| …with prefix caching (sync) | 269,011 | 439M | $184.71 | — |
| chunk x chunk **N=2** (pair 954K) | 3 | 3M | **$0.59** | **$11** |
| chunk x chunk N=3 (pair 636K) | 6 | 4M | $0.81 | $15 |
| chunk x chunk N=5 (pair 382K) | 15 | 6M | $1.25 | $23 |
| chunk x chunk N=8 (pair 239K) | 36 | 9M | $1.03 | $36 |
| chunk x chunk N=15 (pair 127K) | 120 | 15M | $2.11 | $70 |
| whole archive in ONE window | 1 | 1M | **$0.24** | **$4** |
| cluster-pair triage (200 clusters) | 19,900 | 19M | $3.32 | — |
| random control, 10k pairs | 10,000 | 28M | $4.60 | $140 |

Three consequences of aggregating first:

- **Caching stops paying.** $184.71 cached-sync vs $123.75 uncached-batch — now
  *worse*. The 72% saving on raw came almost entirely from size skew (a few
  enormous accounts anchored many times); uniform 1,300-token lists remove the
  skew and with it the reason dense-first ordering helped. Dense-first is a
  raw-intentions optimisation specifically.
- **Opus 4.8 becomes the default rather than the aspiration** — $11–70 for
  complete pair coverage. The question flips from "can we afford Opus?" to "how
  many independent re-partition rounds do we want?" (50 rounds of N=5 on Opus
  ~$1,150; 100 rounds on luna ~$125). Since attentional recall is chunk x
  chunk's only real loss and re-partitioning is the fix, buying dozens of rounds
  beats any single-pass optimisation.
- **The random control becomes a major line item** — $140 on Opus, twice the
  entire N=15 sweep. Verifying what you missed costs more than the search.
  Run the control on luna even when the main sweep is on Opus.

### Why similarity-ANN is the wrong prefilter

The obvious shortlist — embed intentions, cosine top-k, judge the shortlist —
retrieves **topical neighbours**. But the target is *complementarity*, not
similarity, and the valuable matches are cross-domain: one person's constraint
meeting another's unrelated capability. Those sit far apart in embedding space
by construction. So a similarity filter doesn't merely lose recall, it
systematically removes the class of match that justifies an intention market,
degrading the product to "here are people like you". Generating a "what would
complement this?" query and embedding *that* relocates the hypothesis rather
than removing it: it retrieves against a guess about the complement, and the
guessable complements (wants-X / offers-X) are exactly the boring ones.

Corollary for any LLM matching prompt: **instruct against similarity
explicitly**. Asked "are these compatible?", a model will score topical overlap
and reproduce the cosine filter in prose.

### Candidate architectures (in preference order)

**1. Chunk x chunk over the whole archive.** Partition accounts into N chunks
and evaluate every chunk-pair, including self-pairs. Complete pair coverage
**by construction** — every account pair co-occurs in exactly one call — so the
only loss is *attentional* (does the model notice this pair inside an 800K-token
call?), not combinatorial. Re-running with a different random partition gives
each pair another independent chance, in different company, which is itself a
serendipity generator. N=15 keeps pair size (~799K) inside a 1M window; N<=10
overflows it. This is the route that makes Opus 4.8 affordable for full
coverage. Amortisation: pairwise sends each account's list 733x, N-chunk ~N+1x.

**2. Cluster-pair triage.** Cluster intentions into ~200 themes; reason about
complementarity at the *cluster-pair* level (19,900 pairs, $3.32), then expand
only promising cluster pairs into member pairs. Rationale: **people often hold
similar intentions, so match them as groups** — one serendipitous connection
discovered between two clusters yields many concrete member-level matches, which
can then be ranked/filtered on profile compatibility. It also makes distance
explicit and *searchable*: sample cluster pairs at all distances rather than
only near ones, which is a structural way to hunt cross-domain matches instead
of hoping they survive a similarity filter.

**3. Learn the metric from an exhaustive pass.** Running the cheap exhaustive
pass once yields ~269K LLM-judged pairs — a labelled dataset, free as a
byproduct. Fit an asymmetric scorer (e.g. a bilinear form `score = aT W b`) over
the existing embeddings so that "near" means *complementary* rather than
*similar*. That gives a genuine complementarity-ANN, and it is the only route
that stays roughly linear as the account pool grows. It can only be built after
running exhaustive at least once — an argument for doing so while it is still
cheap.

**4. Coarse-to-fine cascade.** Exhaustive coarse pass on ~300-token account
signatures tuned for *recall* ($25), then full intention lists on survivors
(~$34 at 5% survival), then Opus re-rank on the top few hundred. Total ~$60.
Caveat: compression is where serendipity dies — the surprising match usually
turns on one specific detail, so keep signatures generous and stage 1
permissive. A false positive costs $0.0001; a false negative is invisible
forever.

**5. Random control — not optional.** Reserve budget for uniformly random pairs
(10,000 = $4.60). Any filter's false negatives are by definition the matches you
never see, so a random sample is the *only* way to estimate what a heuristic
misses. Establish the baseline against the exhaustive results now and keep it as
a standing regression metric: a new filter must be shown not to lose surprising
matches.

Also: use embeddings **inverted** — not to retrieve near things, but to force
sampling of pairs that are topically distant while sharing one facet.
Embeddings as a diversity instrument rather than a retrieval one is the use that
serves this goal.

### Aggregation (the dedupe step) — costed

Simplest shape: one call per account, "dedupe this list of intentions", raw list
in, ~13 out.

| | input | output | sync | batch |
|---|---|---|---|---|
| gpt-5.6-luna | 6.10M | 0.95M | $2.36 | **$1.18** |
| claude-opus-4.8 | 9.13M | 1.44M | $81.57 | **$40.79** |

Output is roughly half the bill in both cases — you are generating 13 full
intention blocks per account, not a short verdict.

**The workload is extremely skewed, and that is the exploitable fact.** Median
account has only **28** raw intentions (a light 28 -> 13 dedupe); p90 is 196;
the max is 1,624. Only **67 accounts (9%) exceed 200** raw intentions and only 4
exceed 1,000 — yet those 67 hold **47% of all raw intentions**. So:

| aggregation strategy | batch |
|---|---|
| all luna | $1.18 |
| all Opus 4.8 | $40.79 |
| **luna on the 667 light accounts, Opus on the 67 heavy** | **$13.10** |

The mixed split puts frontier judgment exactly where the compression is hard
(1,624 -> 13 is a 125:1 squeeze) and cheap inference where it is trivial, for a
third of the all-Opus price.

**Hierarchical merge** (fan-in 8: merge chunk outputs in groups, then a final
pass) costs only ~13% more — luna $1.34, Opus $45.85 batch, 902 calls — and
avoids asking any single call for a 125:1 compression. Worth preferring for the
heavy tail even though single-pass is nominally cheaper.

Full-pipeline totals (batch, whole archive):

| pipeline | extract | aggregate | match | **total** |
|---|---|---|---|---|
| all-luna | $42.96 | $1.18 | $2.11 (N=15) | **$46.25** |
| luna extract + mixed aggregate + Opus match (N=5) | $42.96 | $13.10 | $23.00 | **$79.06** |
| luna extract + Opus aggregate + Opus match (N=15) | $42.96 | $40.79 | $70.00 | **$153.75** |

The shape this suggests: **cheap model where volume dominates (extraction),
frontier model where judgment dominates (aggregation of heavy accounts, and
matching)** — and the whole archive still lands under ~$155.

**Extraction is the largest line item in every configuration** — the opposite of
where this investigation started. It opened as "matching will be unaffordable,
so extraction quality must be traded away"; it ends with matching costing
$2–70 and extraction costing $43. Any further cost optimisation belongs in
extraction, and the case for degrading extraction quality to fund matching has
disappeared.

**Prefer hierarchical merge for the heavy tail on quality grounds, not cost.**
Everything downstream now rests on aggregation output, and a single call
compressing 1,624 intentions to 13 is the most plausible place for that to break
— §5 found cheap models already tend to sand off exactly the specific, idiomatic
material that makes a match non-obvious. Fan-in 8 never asks any call for more
than an 8:1 reduction, for ~13% more.

**The `~13 per account` figure is an assumption, and the aggregated tables
depend on it.** If real accounts carry 20–30 genuinely distinct intentions, the
archive no longer fits one 1M window (0.95M is close to the limit already),
chunk x chunk needs a larger N, and the pairwise figures scale accordingly.
Cheapest way to find out: run the dedupe on the reference account's 42
chunk-level intentions — about a cent — which yields both the real compression
ratio and, more importantly, whether dedupe preserves the idiomatic themes or
regresses toward the generic. That is the single highest-value unmeasured
number in this study.

### Caching — a possibility, not a recommendation

Recorded because it was measured, and because it *would* matter on a pairwise
route. It is **not** part of any architecture recommended above, and it does not
survive aggregation.

Unlike extraction (every corpus unique), matching repeats one side of each
request, so the anchor account is cacheable. Anchoring the *larger* account and
sweeping triangularly puts the biggest lists in the cacheable prefix:

| | raw intentions | aggregated |
|---|---|---|
| uncached batch | $444 | **$123.75** |
| cached sync | **$251** | $184.71 |

**On raw intentions it wins (72% input reduction); on aggregated intentions it
loses.** The entire benefit came from size skew — a handful of enormous accounts
anchored many times — and aggregation flattens every account to ~1,300 tokens,
removing the skew and the reason dense-first ordering helped. So "process dense
accounts first" is a raw-intentions optimisation specifically, with a short
shelf life.

Three further limits: only the prefix is cacheable (the other account's list
differs every request), so ~half the payload is irreducible; OpenAI's auto-cache
TTL is minutes while batch runs async over hours, so cached-**sync** competes
with uncached-**batch** rather than composing with it (Anthropic's caching does
compose inside batches); and sync at luna's 10M TPM needs ~7.4 hours of wall
clock for the raw sweep.

**When it would be worth revisiting:** a pairwise route on raw intentions, on
Anthropic (where caching and batch compose), or any future design where one side
of the comparison is large and repeated. On the chunk x chunk route it is
irrelevant — 120 calls with no repeated prefix.

### Assumptions behind these figures

14 intentions/chunk (measured), ~100 o200k tokens per intention (measured),
300 output tokens per pairwise match call, 200-token matching prompt, 5%
survival at stage 1 (a guess — worth measuring on a few thousand pairs for ~$1;
it swings the cascade total). Output volume is the least certain input: at 1,000
tokens per match instead of 300, exhaustive pairwise on luna rises from $493 to
~$706 batch.

## 12. Lab instance (`lab.loore.org`) — infra and cost

**Decision (2026-09-02).** None of the above runs on prod. A separate GCP VM,
prod-shaped — systemd services, host Postgres, Redis, gunicorn, not Docker — so
that infra findings transfer 1:1; `ENCRYPTION_DISABLED=true` (the source data
is public, and KMS per node would make a 9M-node import slow and billable);
walled to the operator (firewall allowlist or IAP tunnel, admin-only, public
routes off); its own LLM API keys under a separate spend cap; Ubuntu 24.04.
Three uses share the one Community Archive import: intention-market R&D,
precomputed prefill bundles for signups (published to a private GCS bucket that
prod hydrates from after the consent step — one-directional, the lab holds no
prod credentials), and, later, Twitter thread reconstruction for Loore Commons.

Two traps. `deploy.sh` runs `flask db migrate` and pushes the generated
migration to `main`; that step must be disabled on the lab, with a read-only
deploy key as backstop. And the per-user Celery prefill chain is not the sweep
driver: extraction, aggregation and matching stay script-driven, writing
results into the lab DB, while the app is exercised for what prod will actually
do at scale — import, export rendering, serving large accounts.

**Prices** are Cloud Billing Catalog list prices for us-central1, pulled
2026-09-02, at 730 h/month.

| item | spec | $/month |
|---|---|---|
| VM | e2-standard-4 (4 vCPU / 16 GB), on-demand | 97.84 |
| disk | 100 GB pd-balanced | 10.00 |
| external IPv4 | in use | 3.65 |
| snapshots | weekly, ~4 retained | ~2 |
| internet egress | ~5 GB/month of batch uploads | ~0.60 |
| prefill bucket | ~1.3 GB, 3 versions retained | <0.10 |
| DNS | record at the registrar | 0 |
| **total, running** | | **~$115** |
| **total, VM stopped** | disk + IP + snapshots + bucket | **~$16** |

VM alternatives (add ~$16.50 for everything else): e2-highmem-2 (2 vCPU /
16 GB) $66.00 — same RAM, half the CPU, and the August export incident was
CPU-bound; e2-standard-4 Spot $58.71 — can be stopped at any time, the disk
survives but running imports don't; e2-standard-4 with a 1-year commitment
$61.64 — only once the box has proven long-lived. Resizing is a stop/start,
so nothing is locked in. Prod, for comparison, is an e2-medium with a 50 GB
disk and an IP, ~$33/month. Access layer: a firewall allowlist, an IAP TCP
tunnel or Tailscale cost $0; IAP in front of HTTPS needs a load balancer at
~$18/month and is not worth it here.

Unit prices behind the table: E2 $0.02181/vCPU-h + $0.00292/GB-h (Spot
$0.01309 / $0.00175); pd-balanced $0.10/GiB-mo (pd-standard $0.04, pd-ssd
$0.17); standard snapshot $0.05/GiB-mo (archive $0.019); in-use external IP
$0.005/h; internet egress $0.12/GiB, ingress free; GCS Standard regional
$0.02/GiB-mo (first 5 GB free), Class A ops $0.005/1K, Class B $0.0004/1K;
bucket-to-VM transfer inside a region free; KMS $0.03 per 10K operations.

**Sizing.** 387M tokens of text is ~1.2 GB raw; Postgres with 9M node rows and
indexes lands around 6–10 GB; the parquet snapshot a few GB; one extraction
sweep's batch payload 1.2 GB; OS and conda ~10 GB. 100 GB is comfortable,
50 GB tight. Egress is only batch uploads (extraction 1.2 GB, aggregation
19 MB, chunk × chunk matching 47 MB); result downloads and the archive
snapshot are ingress. A heavy month with several sweeps stays under $4.

**Bucket.** All 734 bundles are ~1.3 GB (compact rows ~1.2 GB across the whole
archive, profiles and intentions ~40 MB). Three retained versions ≈ 4 GB,
inside the free tier and $0.08/month without it. A refresh writes 734 objects
and each signup reads one, inside the free operation tiers. The bucket goes in
us-central1 next to prod so hydration transfer is free; prod's only hydration
cost is KMS, ~$0.04 for a 14K-node account.

**What dominates.** LLM spend, not GCP: $46–155 per full-archive pass (§11).
Running, the lab's infra is about one full-archive pass per month; stopped,
about a seventh of one.

---

## 13. Reproduction

See `README.md` in this directory for commands. Artifacts and rendered prompts
are gitignored by design; regenerate them from the snapshot. Key scripts:

| script | purpose |
|---|---|
| `estimate_prefill_tokens.py` | offline token/cost estimate for one account |
| `cost_table.py` | model cost comparison for a measured corpus |
| `chunked_batch_run.py` | N-chunk split + per-(chunk, model) run, batch or sync |
| `ca_corpus_token_census.py` | exact token census of the whole archive |
