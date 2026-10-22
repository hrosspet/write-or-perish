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

**One version per window.** The prompt is the generation prompt with
`EVENTS_IN_WINDOW_PARAGRAPH` inserted before `## INPUT`
(`window_generation_template`; inserted in code, so one-chunk builds, the
prompts page and the update and merge prompts never carry it). Every
window of the user goes out in one Batch submission, split into several
batches only above `PROFILE_BATCH_MAX_BYTES` (100 MB), all submitted in the
same task run.

**Models.** Per provider of the user's chat model, never another
provider's: `PROFILE_VERSION_MODEL_ANTHROPIC` / `_OPENAI` (Haiku 5.5 / GPT-6
Luna) and `PROFILE_MERGE_MODEL_ANTHROPIC` / `_OPENAI` (Opus 5.5 / GPT-6.1
Sol), checked at boot (`validate_profile_models`) and again per build
(`profile_models_for`). An OpenAI merge is a Batch request on
`/v1/responses` with the live call's body and
`PROFILE_MERGE_REASONING_EFFORT_OPENAI` as `reasoning.effort` (default
`high`); windows stay on `/v1/chat/completions`.

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
stopped).

**Running it.** `dispatch_advance` claims the per-user guard
(`profile_generation_task_id`), sets `profile_batch_pending` and pins the
user to Batch (`profile_force_batch`: never the full-price synchronous
fallback). `advance_profile_windows` (acks late) derives everything from the
database: the set (in-flight items, a full-rebuild flag, the set whose tip
is the update base, or a new set), its missing ranges, the due groups and
the final; checks the spend cap and the estimated cost of the rest against
the user's limit (over it: held, `profile_cap_held`, a note in the admin
column); renders and submits. It holds no pipeline lock; past
`PROFILE_WINDOW_RENDER_SECONDS` it submits what it has and dispatches a
new task. The poller collects window, group and final items (`collect`) and
dispatches the advance; the hourly seeder routes builds to it
(`wants_window_build`), opens its gate for a set with work due
(`set_work_due`) and clears a stuck `profile_batch_pending` that no job and
no live task carries (R7). The synchronous task hands such builds to the
same advance (`window_hand_off`), so accounts off the batch path use it too.

**Known limits.** An import older than a set's windows while the set is in
flight is not seen by that set (as for a chained build's first chunk). A
revert from the history during a build is merged into the final as a full
profile.
