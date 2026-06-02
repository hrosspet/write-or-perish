# Design: Batch API for profile generation (poll-driven)

**Status:** RFC / design — implementation follows once the approach is agreed.
**Issue:** part of #173 (Part A only). **Does not** address Part B (prompt
caching for agentic Voice/Text) — #173 stays open.
**Builds on:** the profile-pipeline fixes in PR #181 (eligibility, iterative
tree-collector, resumable regen). This work assumes those have landed.

## Goal

Cut profile-generation LLM spend ~50% by routing it through the providers'
**Batch API** (async, ≤24h SLA, ~half price) instead of synchronous calls.
Profile generation is the textbook case: a background Celery job with **no
user blocking** and **very large prompts** (~80K–200K input tokens per call).

Non-goals: prompt caching (Part B of #173); changing thresholds, eligibility,
or the chunking/integration logic itself.

## The governing constraint: chunks are *sequential*

Profile regen rebuilds in chronological ~90K-token chunks
(`_chunked_profile_loop`, `backend/tasks/exports.py`). **Chunk N's prompt
embeds chunk N-1's LLM output** as `{existing_profile}`
(`exports.py:~828`). So a single user's chunks **cannot** be submitted as one
parallel batch — they must go one at a time, each after the previous result
lands.

Consequence: the batchable unit is **one LLM call per user**, accumulated
**across the eligible cohort**. Many users' "current step" calls go into one
provider batch; we route results back by `custom_id`. This is exactly the
shape `batch_submit` already supports (one Anthropic batch for all requests;
one OpenAI batch per model).

Naively advancing one chunk per *hourly* beat would stretch a 47-chunk user
across 47 cycles. Instead we drive the chain with a **frequent poller** — in
practice batches finish in ~1–5 min (only *guaranteed* within 24h), so a
**~60s poller** advances each user roughly minutes-per-chunk, all at half
price, with nobody blocked. If a batch is slow, the chain just waits — it's a
background job.

## Reuse: the batch implementation already exists

`experiments/prompt_rct/run_rct.py` already implements submit/poll/collect for
both providers, but it's **gated out of prod** (`backend/__init__.py:168`
imports it inside a `try/except` that silently passes in Docker/staging). So
Part A is largely **extraction + reuse**:

| Existing (RCT) | Move to |
| --- | --- |
| `batch_submit(requests_by_provider, api_keys, phase)` → `{provider_key: batch_id}` | `backend/utils/llm_batch.py` |
| `batch_check_and_collect(batch_ids, api_keys)` → `(results_by_custom_id, still_pending, durations)` | `backend/utils/llm_batch.py` |
| `_convert_messages_for_anthropic(messages)` | `backend/utils/llm_batch.py` |
| `get_batch_api_keys(cfg)` (honors `OPENAI_API_KEY_BATCH`) | `backend/utils/llm_batch.py` |

Each batch result is `{content, input_tokens, output_tokens}` per `custom_id`
— the same fields `_save_profile` already consumes. **The prompt-RCT harness
is refactored to import these from `llm_batch.py` and its local copies are
deleted**, so there is exactly one implementation (and prod-critical batch
code no longer lives under `experiments/`). The RCT must keep working
unchanged against the shared module — that's an acceptance criterion.

Request shape (unchanged from RCT):
`{"custom_id", "model_id", "api_model", "messages", "max_tokens"}`.

## State machine

Each user's profile job moves through states, advanced by the poller:

```
        (hourly submitter)                 (poller, ~2 min)
eligible ───────────────▶ CHUNK_IN_FLIGHT ───────────────▶ collect result
 user                          │                                 │
 (≥80K new tokens,             │  batch still pending → wait      │ _save_profile
  no in-flight job)            ▼                                  ▼ (advance cutoff)
                          [more data?] ◀───────────────── yes → submit next chunk
                               │ no
                               ▼
                        INTEGRATION_IN_FLIGHT ──▶ collect ──▶ save integration ──▶ DONE
```

The **resume cursor is already durable**: every chunk is a committed
`UserProfile` row with `source_data_cutoff`, `parent_profile_id`, and
`generation_type` (PR #181 commits each chunk independently). So the only
*new* state we must persist is "which batch is this user waiting on."

## Persistence

New table (keeps the per-batch SDK ids and maps results → users):

```
ProfileBatchJob
  id              PK
  provider_key    str        # "anthropic" | "openai:<model>"
  batch_id        str        # provider batch id
  status          str        # "pending" | "collected" | "failed"
  kind            str        # "chunk" | "integration"
  custom_ids      json       # ids submitted → failed = submitted − succeeded
  submitted_at    datetime
  collected_at    datetime?
```

Routing back to a user is via a **self-describing `custom_id`** — no items
table needed:

```
custom_id = f"profile:{user_id}:{prev_profile_id or 0}:{chunk_num}:{kind}"
```

Plus guard columns on `User` to prevent double-submission and bound retries:

```
profile_batch_pending   bool   # set on submit, cleared on collect/fail
profile_batch_attempts  int    # consecutive batch failures for current step
```

(Alternative considered: store `batch_id` directly on `User`. Rejected —
a single provider batch covers many users, so the id belongs to the batch
record, not the user. The boolean guard is enough on the user side.)

## Components & files

1. **`backend/utils/llm_batch.py`** (new) — extracted submit/poll/collect +
   key/message helpers. Pure-ish, unit-testable with mocked SDK clients.
   `experiments/prompt_rct/run_rct.py` is refactored to import from here and
   its duplicate definitions removed (single source of truth).
2. **`backend/models.py`** — `ProfileBatchJob` model + `User.profile_batch_pending`.
   (Migration auto-generated on deploy per the Flask-Migrate workflow.)
3. **`backend/tasks/exports.py`**:
   - Refactor `_chunked_profile_loop` so the "build one chunk's prompt" logic
     is reusable by both the sync path and the batch submitter (extract a
     `_build_chunk_request(user, prev_profile, ...)` returning messages +
     metadata, without calling the LLM).
   - `submit_pending_profile_batches()` — the hourly seeder: for each eligible
     user (via `User.profile_eligible_query()`, the threshold check, **no**
     `profile_batch_pending`, and `use_batch(user)` true), build their
     *current* step request, accumulate across the cohort, `batch_submit(...)`,
     persist `ProfileBatchJob` rows, set guards. Users for whom `use_batch` is
     false fall through to the existing synchronous dispatch.
   - `poll_profile_batches()` — the ~2-min poller (below).
4. **`backend/celery_app.py`** — beat entries for the submitter (reuse the
   hourly `check_pending_profile_updates` cadence) and the poller (~60s).
   **Must add `from backend.tasks import <module>`** if the poller lives in a
   new module (per CLAUDE.md — unregistered task files are dispatched but
   never executed).
5. **`backend/utils/cost.py`** — apply the batch discount so `APICostLog`
   reflects true spend (see Accounting).
6. **Config** — a gating resolver
   `use_batch(user) = PROFILE_USE_BATCH or user.id in PROFILE_BATCH_USER_IDS`:
   - `PROFILE_BATCH_USER_IDS` (comma-separated ids, default empty) — the
     **canary** list, to validate on one real user first.
   - `PROFILE_USE_BATCH` (bool, default **False**) — the **global** switch to
     flip once the canary looks good.

   Both default off ⇒ ships dark. Plus optional `OPENAI_API_KEY_BATCH`.

## The poller (`poll_profile_batches`, ~every 60s)

```
for job in ProfileBatchJob where status == "pending":
    results, still_pending, durations = batch_check_and_collect(
        {job.provider_key: job.batch_id}, get_batch_api_keys(config))
    if job.batch_id in still_pending: continue          # not ended yet

    next_requests = []
    for custom_id, result in results.items():
        user_id, prev_id, chunk_num, kind = parse(custom_id)
        user = User.get(user_id)
        if kind == "chunk":
            profile = _save_profile(user, ..., result,           # advances cutoff
                        source_data_cutoff=<chunk latest_ts>,
                        generation_type="update"|"iterative",
                        parent_profile_id=prev_id)
            if chunk_num == 1: user.profile_needs_full_regen = False   # PR #181 rule
            if has_more_data(user, profile.source_data_cutoff):
                next_requests.append(_build_chunk_request(user, profile))   # chunk N+1
            else:
                next_requests.append(_build_integration_request(user))      # → integration
        elif kind == "integration":
            _save_integration_profile(user, ..., result)
            user.profile_batch_pending = False                    # DONE
        commit()

    job.status = "collected"; job.collected_at = now()
    if next_requests:
        batch_ids = batch_submit(group_by_provider(next_requests), keys, "profile")
        persist ProfileBatchJob(s); keep users' profile_batch_pending = True
```

Key properties:
- **Cohort batching**: all users' next steps in one tick go into one
  `batch_submit` call → one Anthropic batch (+ one OpenAI batch per model).
- **Resumable for free**: a crash/timeout between ticks loses nothing — the
  last saved `UserProfile` is the cursor; on restart the poller just re-checks
  pending `ProfileBatchJob`s.
- **Reuses PR #181's rule**: clear `profile_needs_full_regen` after the first
  committed chunk (here, the poller does it instead of the inline loop).

## Accounting

`batch_check_and_collect` returns real `input_tokens`/`output_tokens` per item,
so `_save_profile` logs `APICostLog` exactly as today — but the **price must be
halved** to reflect batch pricing. Add a discount to
`calculate_llm_cost_microdollars(model_id, in, out, batch=False)` (×0.5 when
`batch=True`), and tag the log `request_type="profile_batch"` so cost
dashboards can distinguish. (Long-context multipliers still apply to the
per-call input, but each chunk stays well under the threshold.)

## Failure modes & retry policy

Detection: each `ProfileBatchJob` stores the `custom_id`s it submitted, so on
collect, **failed items = submitted − succeeded** (`batch_check_and_collect`
silently drops non-`succeeded` Anthropic items / non-200 OpenAI lines).

Crucially, **prompt-too-long is a non-issue here**: chunks are capped at
`CHUNK_BUDGET` (~90K), far below model context windows, so the one
*deterministic* failure the synchronous path needs its shrink-and-retry for
(`_call_llm_with_retries`, `exports.py:232`) simply won't fire on chunk
updates. That leaves **essentially all failures transient**:

| Failure | Cause | Handling |
| --- | --- | --- |
| **Submission** (`batch_submit` returns no id) | network / auth / rate-limit at create | don't set `profile_batch_pending` unless an id came back → user re-seeded next cycle. Self-healing. |
| **Per-item transient** (one request errors in a good batch) | provider hiccup / overload | re-seed that user via batch next cycle. |
| **Whole batch `expired`** (OpenAI 24h window) / `failed` / `cancelled` | provider capacity | mark job failed; re-seed next cycle. |
| **Stuck `pending`** (no terminal status) | provider | staleness check (mirror `_is_task_stale`): `submitted_at` older than N h → mark failed, clear guard, re-seed. |
| **Double-collect** (poller crash mid-tick) | infra | idempotent: a chunk whose `source_data_cutoff` already has a child profile is skipped; job flips to `collected` atomically. |
| **Not selected for batch** (`use_batch(user)` false) | — | the synchronous path (PR #181) runs unchanged — the permanent fallback. |

**Retry policy:** since failures are transient, the primary mechanism is simply
**re-seed via batch next cycle**, bounded by `profile_batch_attempts` (≈3) plus
the staleness timeout so nothing loops forever. A synchronous run of a single
step is kept only as a **last-resort safety net** after the attempt budget is
exhausted (guaranteed progress) — but with prompt-too-long off the table, that
path should be rare-to-never, not a routine fallback.

## Rollout

1. Land with both flags off — ships dark, zero behavior change.
2. Unit tests green (mocked SDK clients); RCT verified against the shared module.
3. **Canary:** add one real user id to `PROFILE_BATCH_USER_IDS`; watch
   `ProfileBatchJob` transitions + `APICostLog` halving + a coherent profile.
4. **Global:** once the canary looks good, set `PROFILE_USE_BATCH=true` for
   everyone. Synchronous path stays as the permanent fallback.

## Testing strategy

- `backend/utils/llm_batch.py`: unit tests with mocked `anthropic`/`openai`
  clients — submit builds correct request shapes per provider; collect parses
  both providers' result formats; `custom_id` round-trips.
- Poller state machine: table-driven tests with `batch_check_and_collect`
  monkeypatched to return canned results → assert `_save_profile` advances the
  cutoff, the flag clears after chunk 1, "more data" enqueues the next chunk,
  "no more data" enqueues integration, and integration finalizes.
- Cost: `calculate_llm_cost_microdollars(..., batch=True)` == half of sync.
- Idempotency: re-running the poller over an already-collected batch is a
  no-op.

## What stays unchanged

The synchronous path (`update_user_profile` → `_chunked_profile_loop` →
`_call_llm_with_retries`), the trigger/thresholds, eligibility
(`profile_eligible_query`), and the integration logic. Batch mode is an
alternate *transport* gated by a flag, not a rewrite of the profile logic.

## Decisions

All resolved in review:

1. **`ProfileBatchJob` table** (not columns-on-User) — a batch spans many users.
2. **Integration step is batched too** — one transport end-to-end (one extra
   state hop).
3. **Retry policy:** transient-only failures → **batch re-seed** bounded by
   `profile_batch_attempts` + staleness timeout; synchronous run kept only as a
   rare last-resort safety net (prompt-too-long doesn't apply to chunked
   updates, so transient retry suffices in practice).
4. **Poller cadence: 60s.**
