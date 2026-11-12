# Design: Batch API for profile generation (poll-driven)

**Status:** Implemented in this PR, behind `PROFILE_USE_BATCH` /
`PROFILE_BATCH_USER_IDS` (both default off — ships dark). See the commits on
this branch.
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
- **Statuses by listing (2026-11-03).** Before the loop, the poller reads
  every pending job's status from the providers' list endpoints
  (`llm_batch.list_batches`: Anthropic `GET /v1/messages/batches`, 1,000 a
  page, newest first; OpenAI `GET /v1/batches`, 100 a page, every batch of
  the organisation, matched by id). It pages until every pending batch is
  found, the pages run out, 50 pages, or the listing has passed the oldest
  pending job's submission by an hour (only while the batches it sees come
  newest first; OpenAI documents no order). A job still in progress costs
  no call of its own; an ended one is collected from the listed object
  (`batch_check_and_collect(..., known=...)`); a job the listing does not
  show, or every job when a listing fails, is polled on its own as before.
  Peter: "I want to have as many accounts in the queue as the provider
  allows. I just don't want to spam them with querying for results
  thousands of times per minute." The recent-context, digest and
  poll-draft collectors still retrieve per job.

**An import's build waits for the imports to stop (#598, Peter
2026-11-05).** On the batch path an import does not seed at once. Its
hand-off (`routes/import_data._hand_off_profile_update_after_import`)
still invalidates what the import touches (re-tip or the regen flag), then
`seed_after_import` records `User.profile_import_quiet_from` and schedules
one seed (`seed_profile_batch_for_user`, `after_import=True`) for
`MIN_INACTIVITY` (30 minutes, the existing inactivity window) later. The
hourly pass and that seed build nothing while `import_settling`: a ChatGPT
or Claude import is running (its lock is fresh), or the last import
finished, or an entry was written in Loore after it, less than 30 minutes
ago (imported entries keep their own dates, so the newest entry alone does
not show an import). A Twitter import marks the time at its start and
after each committed batch. Once the imports are quiet the mark is cleared
and the usual gates decide, with the import's override of the refusal
backoff (an import's build skips the wait and the stop, voice review
2026-10-01), also when the import's own seed found an entry written after
the import and left the build to the hourly pass. A second import inside the window marks again
and schedules its own seed; the first one's seed then finds the imports
settling and does nothing, so both imports go into one build. The window
advance (`next_work`) holds a set's windows and merges the same way. The
admin's Build profile and a pre-fill seed at once, and the synchronous path
(accounts off the batch path) dispatches at once as before. The import's
result carries `profile_waits_for_imports` when a build is due, and the
import dialog says the profile will be built once the user has stopped
importing for 30 minutes.

## Accounting

`batch_check_and_collect` returns real `input_tokens`/`output_tokens` per item,
so `_save_profile` logs `APICostLog` exactly as today — but the **price must be
halved** to reflect batch pricing. Add a discount to
`calculate_llm_cost_microdollars(model_id, in, out, batch=False)` (×0.5 when
`batch=True`), and tag the log `request_type="profile_batch"` so cost
dashboards can distinguish. (Long-context multipliers still apply to the
per-call input, but each chunk stays well under the threshold.)

**Per-user monthly spend cap (Peter, 2026-10-20).** The batch path applies
the same cap as the synchronous task (`backend/utils/spend.py`,
`PER_USER_MONTHLY_LIMIT_USD` or the user's own limit; 0 = uncapped). The
seeder submits no chunk or integration for a capped user. Before each next
step the builder runs the cap check itself, because the check a cost row
dispatches runs 15 s later. So a user whose step crossed the cap is blocked
(the admin is emailed once) and gets no next step. A step already submitted
is collected, saved and its cost logged. The stop is not a failed attempt,
so the user never falls back to the full-price sync path. The chain stays
where it stopped, and `User.profile_cap_held` marks it. The hourly seeder
continues it from the saved version once the block lifts, at the start of
the next month or when the admin raises the user's limit. That includes a
pre-filled account not yet activated, which the seeder's eligible cohort
leaves out (its build started from an immediate seed). A deactivated
account stays paused.

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
| **Content-policy block** (2026-11-04) | the provider's content-policy filter refuses a request (HTTP 400, e.g. `bio_policy`, in the batch's error file) | final, never resent automatically: a chained step is not a batch attempt (no full-price fallback) and stops the account; a window set's item gets the outcome `blocked`: a window is skipped (`window_skip` row) and the profile finished without it, a group or final stops the account's profile build until the admin's confirmed Build profile (`refusal_backoff.lift_policy_block`). |
| **Provider queue full** (2026-11-03) | a run that fills the provider's batch queue on purpose | wait, never a failure: an Anthropic create refused with a 429 that is not the spend cap, or an OpenAI create refused on the enqueued-token limit (`SubmittedBatches.queue_full`), and an OpenAI batch failed in validation with `token_limit_exceeded` (`CollectedResults.queue_full`). No batch attempt (so no full-price fallback), no refusal backoff, no "missing" outcome for a window set (outcome `queue_full`, and `profile_batch_pending` cleared), no immediate advance; the next seed pass sends the work again. `User.profile_work_held` remembers it (next row). Logged as a warning, no Sentry event. Only while another profile batch of ours is pending in the same queue (OpenAI: the same model, whose enqueued-token limit it is): with nothing else of ours queued, the batch is over the limit by itself and would fail on every pass, so it counts as a failed attempt and logs an error, as before (review of #596). |
| **Work waiting outside the hourly cohort** (2026-11-03) | the hourly seeder's cohort leaves out accounts not yet activated (the pre-filled Beta cohort) | `User.profile_work_held` marks batch profile work that waits for a later pass: a full queue, a refused or failed chained step, a set item that failed (a refused final waits out the refusal backoff's hour; a window may wait out a day's provider stop), a refused submit, a stale job. The hourly seeder adds held accounts to its pass by the spend-cap hold's rule (not deleted; approved or never deactivated, so a deactivated account stays paused), after the same gates: the backoff, the stops and the attempt limits are unchanged. Cleared when the work goes out, or when nothing is left to do and nothing waits for time to pass (a refusal stop that only a new version lifts counts as given up). Peter: "yes, implement it pls", "yes pls". |

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

## Canary runbook (staging)

Validate on one real user before flipping the global switch. The compose
services pass env via explicit `environment:` lists, so the gating vars are
declared there (`docker-compose.yml`, backend + celery + celery-beat) and
interpolated from `.env.staging`.

1. **List the canary** in `.env.staging` on the VM (the "opt in" step):
   ```
   PROFILE_BATCH_USER_IDS=1     # the user id to canary
   PROFILE_USE_BATCH=false      # keep it to just that user
   ```
   `OPENAI_API_KEY_BATCH` is optional (batches fall back to the chat key).
2. **Deploy** the branch to staging (carries the code + compose wiring):
   ```
   git push origin <branch>:staging --force
   ```
   The deploy reads `.env.staging` at `docker compose up`, so set step 1
   *first*. If you change `.env.staging` after a deploy, restart so the
   processes re-read config (it's read at process start):
   ```
   docker compose -p wop-staging ... restart backend celery celery-beat
   ```
3. **Confirm the id.** Staging wipes its DB each deploy, so ids are assigned
   fresh on signup — be the first to log in to get id 1, or confirm via the
   admin dashboard / `flask shell` and adjust the allowlist (+ restart celery)
   if it differs.
4. **Verify the var reached the worker:**
   ```
   docker compose -p wop-staging ... exec celery printenv PROFILE_BATCH_USER_IDS
   ```
5. **Generate ≥80k eligible tokens** (e.g. import a ChatGPT/Claude archive),
   then wait for the hourly `seed_profile_batches` or kick it now from a
   `flask shell` in the backend/celery container:
   ```python
   from backend.tasks.profile_batch import seed_profile_batches
   seed_profile_batches.delay()
   ```
   The 60s poller advances it from there.
6. **What "healthy" looks like:**
   - celery logs: `Profile batch <id> (...) submitted` → `saved batch chunk
     profile <id>` → (if a chain forms) `saved batch integration profile`.
   - `ProfileBatchJob` rows go `pending` → `collected`; `source_data_cutoff`
     advances across the resulting `UserProfile` rows.
   - `APICostLog` rows for the run have `request_type='profile_batch'` and ~half
     the cost of the equivalent synchronous run.
   - `User.profile_batch_pending` returns to `false` when the user's pipeline
     finishes; `profile_batch_attempts` stays 0 on success.

## Independent versions for large builds (#545, 2026-10-22)

Code: `backend/tasks/profile_windows.py`, with small hooks in
`profile_batch.py` (seeder, poller, `_submit_requests`), `exports.py`
(prompts, update base, sync hand-off) and `utils/profile_versions.py`.

**When it applies.** A build whose remainder plans into two or more chunks
on the user's chat model, as the chained path plans it (a huge import, a
long backlog, a from-scratch rebuild of a large account), and every later
update of a profile built this way. A build that fits one chunk and a
chained update that fits one chunk run as before. A version the user saves
after a set's final takes the chained path again; a window of the set
collected after that version is not saved (its cost row is), and nothing
more of the set is sent. `PROFILE_INDEPENDENT_VERSIONS=false` turns the
path off everywhere; the update base then skips a set's windows and groups
and is the newest set final unless a version outside any set is newer, so
a user whose profile is a set gets chained updates on its final (a first
build stopped before its final is rebuilt on the chained path).

**Windows.** The remainder is cut into windows by the same planner
(`next_window_budget`), at the version model's input cap: Haiku 5.5's 100k
price tier for Anthropic users (about 43k units a window at the 2.2 prior),
GPT-6 Luna's 272k tier for OpenAI users. Each rendered window is counted
(`LLMProvider.count_tokens`) and rendered again smaller while it is over the
cap (`fit_window`); a window is never cut and never sent over the cap, and
the next window starts at the last entry the smaller one holds. One entry
larger than the cap is given up (a `window_skip` row and a Sentry alert).

**One metadata read per pass (#612).** A pass reads the metadata of the
writing it renders once (`exports.window_scope`,
`routes/export_data.WindowScope`: id, parent, date, units, deleted state
and origin of every row the export's walk can reach after the earliest due
range's start; no content) and cuts every window from it: the unit count
the planner sizes the window by, and each render while `fit_window` fits it
to the cap. `WindowScope.rows` is `_select_incremental_rows` computed in
memory: the same rows, and on SQLite in the same order
(`tests/test_window_scope.py` compares the two, and whole builds on both
paths); only each window's own rows are loaded and decrypted. Before,
every window read the metadata of all the writing after its start again,
once for its count and once per render, so the work grew with windows ×
entries (a build of a multi-million-unit import took over 20 minutes and
about 0.5 GB in one worker).

Rows that share a timestamp come in a fixed order now (the user's own,
then those addressed to them, by id; parents and replies by id), the order
SQLite returned them in. On Postgres the old walk gave them its query
plan's order, so there a window can now end elsewhere in such a run than
the old code would have ended it: a run is always taken whole by one
window (the next starts after its timestamp), so no row is lost or read
twice, also when a set the old code started is resumed.

The read is taken again before a window when the user's rows changed since
(their count, newest id, newest update, deleted count or newest deletion:
"Delete all my writing" and its restore leave `updated_at` alone) or an
account was deleted or restored. At render time (`ScopeChanged`, then a
new read) the rows a window's text is decided from must still be as read:
its own rows before they are loaded, the rows that put each one in scope,
the replies under them, and the rows the per-thread skip rule (§5a) walks
with the replies under the deleted ones. A row hidden, deleted or edited
during the pass, or a reply that went (deleted, made private, marked None,
its author's account deleted), is neither rendered nor decides anything
from the old read. A window whose rows change again right after a new
read, and the rest of that pass, read the database as before.

**One version per window.** The prompt is the generation prompt with
`EVENTS_IN_WINDOW_PARAGRAPH` inserted before `## INPUT`
(`window_generation_template`; inserted in code, so one-chunk builds, the
prompts page and the update and merge prompts never carry it). Every
window of the user goes out in one Batch submission, split into several
batches above `PROFILE_BATCH_MAX_BYTES` (15 MB; 100 MB before 2026-11-07,
when a multi-million-unit build ran the server out of memory), all
submitted in the same task run.

**Models (Peter, 2026-11-05).** Chosen per pass (`pass_models`):

| Pass | Versions | Merges |
|---|---|---|
| A from-scratch build that fits one window on the chat model, private writing, no consent (#596) | the user's chat model, with the events paragraph | the user's chat model, single-version prompt |
| More than one window, private writing, no consent | `PROFILE_VERSION_MODEL_ANTHROPIC` / `_OPENAI` (Haiku 5.5 / GPT-6 Luna) | `PROFILE_MERGE_MODEL_ANTHROPIC` / `_OPENAI` (Opus 5.5 / GPT-6.1 Sol at `PROFILE_MERGE_REASONING_EFFORT_OPENAI`) |
| Any from-scratch or multi-window build, public-only writing or `User.profile_both_providers` | `PROFILE_VERSION_MODEL_CROSS_PROVIDER` (GPT-6 Luna) | `PROFILE_MERGE_MODEL_CROSS_PROVIDER` (Opus 5.5; the single-version prompt for one window) |
| A regular update of a merged set: one window to read | the user's chat model, chained | the user's chat model |

Public-only (`utils/profile_providers.writing_is_public_only`, metadata
only, any doubt counts as private; nothing private may ever have been read
for the user):

- nobody has ever signed into the account (`utils/activity.sign_in_status`
  "never": no sign-in or terms acceptance on record, on an account newer
  than that record), so it never uploaded its own archive: a pre-fill
  waiting for its owner or a lab account built from the Community
  Archive. An account that ever uploaded its own archive is private;
- every entry of the user, deleted ones included, came from the Community
  Archive or the X API (`Node.provenance` `prefill_ca` / `prefill_x`) with
  its text unchanged since (no `NodeVersion` row, which the node route
  writes on every edit; a deleted one whose text and history the cleanup
  wiped counts as private), or is public while the user shares publicly;
- no other user's entry is a parent or a reply of theirs, and no profile
  prompt is their own;
- every profile row of the user records its sources
  (`source_origin_stats`) as pre-filled tweets alone (`twitter`) on an
  account Loore pre-filled, and none is the user's own text: a purged
  entry leaves its origin in the rows that read it, and a row without the
  record counts as private;
- `User.profile_private_read` is not set. A batch submission, a saved
  version and a window pass set it for good when the user's entries are
  not public-only (`note_private_read`), consent or not; nothing clears it.
  `backend/scripts/backfill_profile_private_read.py` (dry run by default,
  `--apply`, metadata only, idempotent) sets it for the accounts from
  before it that the rules above cannot vouch for. The check does not
  depend on that run. `profile_both_providers`
(default false) is the user's consent, set only by their own answer to
#599's question (`profile_both_providers_answered_at` records when; the
rules for asking are `utils/profile_providers.both_providers_ask`). It is
read from the database at each check (`agreed_to_both`), and a pass that
may cross providers checks it again before each slice is sent
(`_Submission.recheck`): a no given while a pass renders stops the rest of
that pass, and a new task plans it for the user's own provider.
Without either, every model of a pass is of the provider of the user's
chat model: checked at boot (`validate_profile_models`), per pass
(`profile_models_for`) and for every request before it is queued
(`_Submission.add` raises). An OpenAI merge is a Batch request on
`/v1/responses` with the live call's body (and, for the provider pair,
`PROFILE_MERGE_REASONING_EFFORT_OPENAI` as `reasoning.effort`, default
`high`); windows stay on `/v1/chat/completions`.

**A regular update of a merged set (Peter, 2026-11-05).** When the set's
only missing range is the writing after its last window, nothing of it is
in flight, and that range plans into one chunk on the user's chat model
(`chained_update_base`), the window is not read on its own: the user's
chat model writes a chained update, main's update prompt
(`build_update_template`, `build_chunk_prompt`) over the set's newest
profile and the window. The base is the set's final over its last window,
or, when the last window is itself a chained update, that version. The
output is checked for the three maps (no events section) and saved as a
`window_update` row with `[Covers data through B (N tokens)]`, parent the
set's base as a window's (the profile it updated is in its job item). It
stands in the window's place in the group and final merges, which the chat
model runs, and is labelled there as a profile of all the writing: "Covers
all the writing from <start> to B" and "Updates a profile of all the
writing up to A with the writing from A to B". Every chained version keeps
its place in later merges (Peter, 2026-11-05: "The last version of a chain
is heavily biased towards the last window. All chained versions should go
to the merge"). A set
still being built (no final over its last window) reads a retried window
independently, with the build's models. One window per pass: a remainder
that did not fit is chained on the new version in the next pass.

**Rows.** `UserProfile.window_set` (12 hex characters) and `covers_start`.
`window`: one window's version, its text starting with
`[Covers A to B (N tokens)]` for its own window; `source_tokens_used` is
cumulative, so the ladder and the gates work as before. `window_skip`: a
window given up, with `WINDOW_SKIPPED_TEXT`. `group`: a merge of consecutive
windows. The set's final is an `integration` row with the set's id, its
parent the tip window and `merged_profile_id` the user's version it merged.
Windows and groups are hidden from every reader of the profile
(`current_profile_query`), from the history and from version numbers: until
the final is saved, the previous profile stays in use (nothing is shown on
a first build).

**Checks.** A window's output must have the Surface, Shadow and Light Map
headings its prompt asks for and exactly one `EVENTS IN THIS WINDOW`
heading (`profile_output_problem`); a group or final, the three maps. A
model that declines in plain text fails this.

**Merge.** The integration prompt of #586, unchanged, over independently
labelled inputs: "Covers the writing from A to B" for a window or group,
"Covers all the writing from <start> to B" for the set's base (a chained
profile the set continues) and for a version the user wrote ("Written by
the user on D"), placed at its date. `PROFILE_MERGE_STRUCTURE=flat`
(default): one call over every window while it fits the merge model's cap,
falling back to the groups above it. `grouped`: up to
`PROFILE_MERGE_GROUP_SIZE` (30) windows go into one final call; above that,
fixed slices 1–30, 31–60, … are merged into groups and the final merges the
groups (a slice of one window enters the final as it is). A full slice that
ends before the first missing or in-flight window is final, so it can go
out while that window is retried, and is never redone unless one of its
windows is replaced. Every merge is counted first; a group or
final over the cap stops the build loudly (`profile_seed_error`, Sentry)
and is not retried until the merge settings change or a version is saved.

**Updates.** The 80k gate (now on `created_at`, R11) adds one or two windows
after the tip; the newest group and the final are redone, nothing else.

**Failures.** Content (refused, cut off, or not a profile): the cost row is
written, but not as a refusal for the user-level backoff; the window is
submitted again unchanged once, then a `window_skip` row is written and
Sentry gets an alert with ids and dates only. Provider (no result, an
expired or failed batch, a stale job): retried up to three submissions,
then the window waits a day (`refusal_backoff.UNBILLED_STOP_EXPIRY`) and
`profile_seed_error` says so. The attempt history is the outcomes the
collector writes back onto each job's items, read for seven days and keyed
by the window's start, not its set; a submission the provider refuses (no
batch id) is recorded as a failed job whose items are "missing", so it
counts as a provider failure. A range whose units are counted but which
renders nothing (threads holding only a system prompt) counts as covered.
Expired and cancelled OpenAI batches
have their finished results collected (#419); profile jobs are stale after
25 h. Merges keep the integration rule (one retry after an hour, then
stopped). Policy block (2026-11-04): an item the provider's
content-policy filter blocked (`bio_policy` and the other
`llm_batch.OPENAI_POLICY_BLOCK_CODES`, read from the batch's error file)
gets the outcome `blocked`, neither a content nor a provider failure, and
is never sent again automatically. A blocked **window** is skipped
(Peter, 2026-11-04: "skip the flagged windows, but finish their
profile"): a `window_skip` row takes its place over its real range, a $0
record of the block stops nothing (request type `profile_window`), the
admin column says "Skipped window <start>..<cutoff>: provider policy block
(bio_policy)", and the merges run over the other windows. The range
itself is remembered for good, per user, whatever the account's AI usage
(`ProfileBlockedRange`, no lookback): every later window plan, in any
set, cuts around it (`cut_around_blocked`): a window ends where the
blocked range starts, the next begins where it ends, and exactly that
range, entries imported into it later included, gets a `window_skip` row
without a request. So no rebuild resends it: an import that shifts the
windows, new writing that resizes them, the provisional ladder, Build
profile, or AI usage turned back on. Only `lift_blocked_ranges(user_id)`,
from a shell, lets it be read again; no button does. An account whose AI
usage does not allow AI gets no skip row (the range is recorded); a set
whose windows are all skipped sends no final. A blocked chained chunk
(the account stops, #597) is remembered as a range too. A range runs
from just before the blocked window's (or chunk's) earliest entry to its
cutoff, so older writing imported later, never sent, is read. Only the
window path cuts around a range, so a build whose remainder meets one
always takes it (`wants_window_build`); the chained batch builder builds
no chunk that meets one, and the synchronous task builds nothing then
(with the window path switched off, such an account gets no update). A blocked **group or final**
cannot be skipped: it is recorded as a profile block
(`refusal_backoff.record_policy_block`), `profile_seed_error` names it,
and the account builds nothing further (`next_work` returns "blocked",
also when the block is collected while an advance runs) until
`refusal_backoff.lift_policy_block`: the admin's confirmed Build profile.
`scripts/backfill_policy_blocks.py` records the blocks collected as
"missing" before this code, from the batches' error files, with the
same range records.

**Running it.** `dispatch_advance` claims the per-user guard
(`profile_generation_task_id`), sets `profile_batch_pending` and pins the
user to Batch (`profile_force_batch`: never the full-price synchronous
fallback). `advance_profile_windows` (acks late) derives everything from the
database: the set (in-flight items, a full-rebuild flag, the set whose tip
is the update base, or a new set), its missing ranges, the due groups and
the final; checks the spend cap (a user over their limit is held,
`profile_cap_held`, and the hourly seeder resumes the set when the month
turns or the admin raises the limit); estimates the cost of the rest;
renders and submits. A pass with windows to read goes out only when the
estimate (the windows and the merges after them, at Batch prices, with the
measured mean output per call: Peter, 2026-11-05, the expected cost, "Better
err on the job going through leading to $2-$3 overshoot, than postponing it
for a month") fits into what is left of the user's monthly limit; else
nothing is sent, the build waits (`profile_cap_held`, admin note "Waiting
for spend headroom: the build needs about $X; $S of $L used this month")
and the hourly seeder tries it again until the month turns or the limit is
raised. A user with no limit is not gated. Merges alone are held only by
the cap (Peter, 2026-10-22: "send it and let the cap stop it halfway"):
all of a build's windows go out in one submission, so the cap can only
hold the merges after them, and while the user is capped it also blocks
their replies until the 1st. It holds no pipeline lock; past
`PROFILE_WINDOW_RENDER_SECONDS` (600 s) it submits what it has and
dispatches a new task. One advance runs per user at a time
(`utils/run_locks`, 2026-11-07): the run holds a Redis key with a
15-minute TTL that it refreshes before every window render, token count
and slice submission. A second copy (its own message delivered again, a
second dispatch) exits without rendering; `_is_task_stale` never takes the
guard for stale while the key lives, whatever the result backend says
(a killed task's FAILURE); a run that dies leaves the key to expire. A run
whose key expired while it was frozen and was taken by another stops at
its next beat. The task's soft time limit is raised through the token
counts and the submission (`utils/soft_limit`; the SDKs do not send the
request again), so the advance's handler runs and the admin column shows
the stop. A copy that exits for a held key, an advance stopped by the
soft limit, and requests a soft limit kept from going out all set the
work hold (`profile_work_held`, no attempt counted): the hourly seeder
resumes the build after the key expires, also for an account outside its
cohort (a pre-fill waiting for its owner), which it reaches only through
the hold. The poller collects window, group and final items (`collect`) and
dispatches the advance; the hourly seeder routes builds to it
(`wants_window_build`), opens its gate for a set with work due
(`set_work_due`) and clears a stuck `profile_batch_pending` that no job and
no live task carries (R7). The synchronous task hands such builds to the
same advance (`window_hand_off`), so accounts off the batch path use it too.

**An import inside a set (#625, Peter 2026-11-11).** "Only the second
part since the new conversation should be regenerated": an import whose
oldest change (``routes/import_data._ProfileChanges``: entries created or
restored for AI to read, or moved across None and Chat/Train) is older
than the set's tip re-tips the set (`exports._retip_window_set`). The
windows that end before that date, and the groups that merge only them and
are newer than each of them, are copied into a new set as stored (the
encrypted column is copied; nothing is decrypted); the copy of the last
one gets the present moment as its render time, so the continue rule
reads everything after it as the set's unfinished range, and the new
set's final merges the kept windows with the new ones. The profile in use
stays as it is until that final is saved; the old set stays as history.
The versions of the user's the profile in use carries are recorded for
the new final (`plan_user_version_merge`), and a version the user saved on
the set's final is re-tipped the same way (`_window_set_under`). No window
ends before the date: the set's base is re-tipped as a chain, when the set
has a final (that copy is written through `retip_profile_chain`, which
decrypts and encrypts the version it copies, as main's chain re-tip
does). Anything of the user's set in the batch, a profile run under way
(an advance holding the user's run lock renders into the set it chose
before anything is in the batch; or a profile task holding the guard),
the window path off, or a set built on the user's edit of an older set's
final with nothing to keep: the rebuild from scratch, as before.

**Known limits.** An import older than a set's windows while the set is in
flight is not seen by that set (as for a chained build's first chunk); its
hand-off flags a rebuild from scratch. A revert from the history during a
build is merged into the final as a full profile.

### One-chunk builds as one window (2026-11-03)

Code: `profile_windows.wants_window_build` / `one_chunk_windows`,
`exports.single_integration_messages`,
`backend/prompts/profile_single_integration.txt`.

**When it applies.** A from-scratch build (no update base: a first build, a
step of the provisional ladder, a full rebuild) that fits one chunk on the
user's chat model, where the switch of that model's provider is on:
`PROFILE_ONE_CHUNK_WINDOW_OPENAI` and `PROFILE_ONE_CHUNK_WINDOW_ANTHROPIC`
(both default on; Peter, 2026-11-03: "yes, we should do the same for
Ant"). The switch decides the path only (two steps, or main's one chained
call); the models come from `pass_models`. Peter: "the events paragraph is
definitely needed" (in one test window, 0 of 3 plain-prompt Luna versions
kept a key recent event the writing states, 2 of 2 with the paragraph
did), and "we need one more step to convert it into the final profile".

**Models and window counts (Peter, 2026-11-05).** The window count comes
from the user's chat model first, as `wants_window_build` decides the path
(`plan_chunks` at the planner's 90k-unit target and the chat model's cap):

- Private writing, no consent, one chunk on the chat model
  (`one_window_pass`): one window sized for the chat model, read by the
  chat model, and the single-version integration by the chat model
  (`single_models_for`; no reasoning-effort parameter on an OpenAI chat
  model, as the live replies send none). Peter: "for 1 window updates on
  prod I'd like to use the chained version we just tested, with user
  preferred model producing both the version and the merge". On Opus 5.5
  one chunk holds up to about 135k units (the count rounds at 1.5 times
  the 90k target); on GPT-6 Astra or GPT-6.1 Sol (272k tier) about 108k at
  the tweets ratio (2.4 tokens a unit) and 135k at the threads prior.
- Private writing, no consent, two or more chunks on the chat model: the
  provider's pair, as any large build (Haiku 5.5 windows at its 100k tier
  and the Opus 5.5 merge; GPT-6 Luna windows and the GPT-6.1 Sol merge).
- Public-only writing (a pre-fill, the lab) or a user who agreed to both
  providers: GPT-6 Luna versions and the Opus 5.5 merge at any size, the
  windows sized for Luna's 272k tier (Peter's rule names "prefill, lab").
  One Luna window gets Opus's single-version integration; more get the
  many-version merge.
- The models are chosen again on each pass: a pre-fill whose owner signs
  in after Luna read the window counts as private from then on, so its
  integration runs on the user's chat model.

Before 2026-11-05 this path used each provider's pair (GPT-6 Luna, then
GPT-6.1 Sol; Haiku 5.5 windows, then Opus 5.5); that is replaced by the
rules above.

**The build.** A window set of one window (or a few Luna windows for
public-only writing), read with the events paragraph, then a final over
that version: two Batch rounds, one after the other, so a first profile
takes two batch turnarounds instead of one. Like every window build it
pins the account to Batch.

**The final's prompt.** A final whose only input is one window version (no
base, no version of the user's) uses `profile_single_integration.txt`: "a
profile of the user", the events list moved into the Surface Map with its
dates and left out as a section, the length the structure asks for, the
newest value of a fact that changed, and no fact, outcome or trend the
version does not give. The many-version prompt (`profile_integration.txt`)
is unchanged; its "series of profile updates" of 80k-110k tokens and its
trajectory request leaked into 2 of 6 single-version finals on 2026-11-03,
and its "keep the structure" kept the events list as a fourth section in 2
of 2 Opus finals. Not on the prompts page and not replaced by a user's own
integration prompt (written for many versions); the user's own generation
prompt shapes it, as in every merge. A one-window set with a version of the
user's merges both with the many-version prompt (two inputs, "Written by
the user on D"): the user's edit wins.

**Checks.** Every final (one window or many) is refused when it has an
`EVENTS IN THIS WINDOW` heading (`profile_output_problem(final=True)`); the
integration rule then retries it once after an hour. No many-version merge
of the 2026-10-21/22 tests had one (0 of 32 outputs). A set whose final
inputs are all skipped windows (the one window declined twice) sends no
final: a merge of the skip note alone would become the profile. The admin
column says "Stopped: every window of the profile build was given up".

**Later builds.** A set below one full chunk is provisional: the ladder
rebuilds it from scratch at its next step (a new set), and the 80k gate
does not add windows to it. From one full chunk on, its updates are the
set's: a regular one-window update is a chained update by the user's chat
model in the window's place, and the chat model merges the window and the
chained version with the many-version prompt (#589's rule of 2026-11-05).

**Live check, 2026-11-03** (window 23's two events versions, standard API,
$0.26): with the single-version prompt, Sol at high effort and Opus each
wrote three maps and no fourth section in 4 of 4 finals, with none of the
many-version wording; Opus still added one unsupported status in 1 of 2.
