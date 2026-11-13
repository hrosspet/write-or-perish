# Technical Roadmap: Loore Four-Feature Ecosystem

**Date:** November 24, 2025
**Last updated:** April 6, 2026
**Status:** Alpha (live at loore.org) — Phase -1 complete, Phase 0 in progress

**Purpose of this document:** Technical infrastructure roadmap. Details what has been built, what infrastructure gaps remain, and the phased build sequence for each feature. This is the implementation-level reference — for product vision see FOUR-FEATURE-ECOSYSTEM.md, for parallelization strategy see ACCELERATED-DEVELOPMENT-ROADMAP.md.

---

## Executive Summary

This document outlines the technical infrastructure needed to evolve Loore from a journaling app into a complete four-feature ecosystem:

1. **Effortless Journaling** (Input) - ✅ Complete
2. **Download** (Consume) - Context-aware content recommendations from MemeOS
3. **Upload** (Broadcast) - AI-aided sharing to appropriate audiences
4. **Intention Market** (Matchmaking) - Serendipitous connection through complementary needs/offerings

The roadmap includes both feature-specific infrastructure and cross-cutting concerns (testing, monitoring, development velocity).

---

## Current State: Already Built ✅

These technical capabilities are production-ready and form the foundation:

### Core Features
- **Verified email change + rename-safe permalinks** (#260, #253; 2026-09-15) - Email changes go through a confirmation link to the new address (binds only when confirmed from inside the requesting account's session — the link alone signs nobody in; old address notified; the only unconfirmed write left is the admin's `update_email`); username renames of an account with public writing append `username_history`, so old `/@handle/...` URLs 301 (uncacheable, query string kept) to the current handle on every public route, in the permalink API and in the public profile API, and the former handle stays reserved on every path that issues a username (rename, magic-link and X signup, whitelist); the redirect exists only where the target page is public (an account gone private answers 404, like a handle nobody held), handles resolve case-insensitively to one canonical URL, and a rename by an account with nothing published leaves no history (the sharing toggle does not matter: paused sharing around a rename keeps the handle reserved, the redirect resumes with the sharing)
- **Asynchronous processing via Celery** - Long-running tasks (transcription, LLM, TTS) run in background with polling
- **No AI reply where AI usage is None (2026-10-01)** - `llm_nodes.reply_refusal`: a reply needs the node it answers, every node above it the model would read (the `_load_node_chain` walk) and the reply itself to be 'chat'/'train'. `create_llm_placeholder` raises `AIUsageRefused` before any write and the routes answer 403 `{"code": "ai_usage_none", "scope"}`; `generate_llm_response` asks again at run start and before a continuation; `_load_node_chain` leaves 'none' nodes out. Voice: `voice_turn_refusal` gates streaming init (label Voice), `/voice/from-node`, legacy `POST /voice` and the finalize chain (a late refusal saves the entry with a warning, no reply, no cache warm); `GET /api/voice/availability` drives the Voice screen's explanation. `speech_allowed` no longer exempts LLM replies
- **Newcomer prompts (#391, #392; 2026-10-02)** - `own_entries.has_own_entries(user_id)`: one EXISTS query (`node_type='user'`, `origin`/`source_key`/`prompt_key`/`deleted_at` NULL, no linked prompt version), no decryption; exposed as `user.has_own_entries` on the dashboard user. It switches the homepage / Voice / Text question to the welcome one and gates `_pending_polls_for`; the updates fetch moved to `useSessionUpdates`, which skips a session that starts on `/welcome`
- **Replies while they are written (#367, 2026-09-28)** - providers take a `StreamListener` (text deltas, tool-call starts, a say on mid-stream restarts); the reply's text so far lives on `Node.streaming_content` (throttled, one DEK per generation, never bumps `updated_at`, cleared in the finalize commit; `content` stays final-only) and `/api/sse/nodes/<id>/llm-stream` serves it (snapshot/delta/done). A provider failure after text arrived completes the node with that text and a note. Voice (`STREAMING_VOICE_TTS`, default off): a TTS worker thread inside the LLM task speaks each node of the turn while it is written — a streaming version of the batch text preparation (`tts_stream_text.SpokenTextProjector`: edge timestamps, quote markers, share fences, proposal blocks), section-bounded chunks cut only at confirmed sentence boundaries (`ChunkPlanner`), sized by the time left before the listener's queue runs out (`SpeechSchedule`, which on a simulated clock reproduces `section_aware_chunk_text`). Design: issue #367 comment 5870489780
- **Voice turn timing + first-audio latency (#371, 2026-09-29)** - `backend/utils/voice_timing.py` marks each stage of a voice turn (finalize, transcription, LLM task/request/first text, first chunk cut, TTS first/last byte, published) as a `voice-timing` log line and in a Redis hash per reply node (7 days); the browser sends its marks (recording stopped, chunk_ready, `playing`) clock-corrected to `POST /api/voice/timing`, which joins them; `GET /api/voice/timing` returns recent turns + medians. Changes from the measurements: the TTS worker cuts the first sentences (>= MIN_FIRST_CHUNK_CHARS) while nothing is queued, `finalize_draft_streaming` polls every 0.25 s, and a racing duplicate chunk upload returns 200 instead of an IntegrityError 500
- **Account failures (#369, #360 item 3, 2026-10-02)** - `account_failure_kind` (`llm_providers.py`) recognises calls refused for an account reason (spend limits and caps, billing/credits/quota, invalid or revoked key, permission, unknown model id) for both providers; `_retry_mid_stream` raises `ProviderAccountError` at once (fixed user-facing text, provider/kind/model attributes, SDK error as `__cause__`), the continuation loop and the digest rebuild don't retry it, and no other provider is tried. One-item batch submits raise it too; batch polls report it and keep polling. `batch_submit` reports it and lists the refused provider keys (`SubmittedBatches.account_refused`), so the profile seeder doesn't count them as batch attempts. `ModelUnavailableError` (unknown model) tells the user to choose another model. `backend/utils/provider_alerts.py`: one email to `SPEND_ALERT_EMAIL` (subject names the deployment, body the key role) + one Sentry event per cause per `PROVIDER_ACCOUNT_ALERT_THROTTLE_SECONDS` (default 21600, Redis `SET NX EX`), and a stable Sentry fingerprint `[provider-account-failure, provider, kind, key role(, model)]` applied in `before_send` to the error and errors raised from it (`__cause__` only), plus log lines tagged with `log_extra`.
- **Todo merge by edits (#234, 2026-10-06)** - `voice_todo_merge._run_merge` calls `utils/todo_merge_edits.run_todo_merge`: the conversation's model replies with `{edits: [{old_text, new_text}], updated_content}` (structured output, `TODO_EDITS_SCHEMA`; `get_completion(output_schema_name=...)` names it for OpenAI), applied to the newest list with `utils/text_edits.apply_text_edits` (shared with `update_artifact`). One retry with the reason after an anchor error, a full rewrite of a list with tasks, an unparseable reply, a kept-lines refusal (`lines_not_kept`: every previous line stays, only `[ ]` → `[x]`) or a nesting refusal (`lines_moved_under_new`: no existing sub-item ends up under a new line); a second refusal fails with nothing saved. Every call gets its cost row. The comparison script's `--current-prompt` mode runs the same function.
- **Empty cut-off output in background jobs (#368, 2026-09-29)** - `is_empty_truncated(response)` (`llm_providers.py`) is the #366 chat-path test for background jobs. Profile chunk loop / integration (`exports.refuse_truncated_profile`), profile batch collects, recent context and intentions write the cost row, save no version and raise `EmptyTruncatedOutputError`; poll drafts are marked `draft_failed`. An empty cut-off chunk ends the chunk loop, so the chain tip stays the last good version. `batch_check_and_collect` results now carry `truncated` (Anthropic `stop_reason`, chat/completions `finish_reason == "length"`). Profile chunks and integration refuse any cut-off output, empty or partial. Refusals back off (`backend/utils/refusal_backoff.py`: refused calls' cost rows carry `request_ref="refused:truncated"`; after the 1st refusal in a row the job waits 1 h; after the 2nd it stops for that user, logs a tagged Sentry error and sets the admin Profile column's seed error) in the sync trigger, the batch seeder (pinned accounts included) and the recent-context check. A stopped job runs again only after a saved version; an import that triggers a rebuild skips the wait and the stop, and the admin "Build profile" button asks for confirmation before overriding them (`force`). A refused integration is retried alone (`exports.integration_due`, `update_user_profile(integrate_only=True)`, the batch seeder with `allow_chunk=False`). Voice todo merge, the saved-references digest and `scripts/backfill_intentions.py` write the cost row of an empty output; a refused intentions run shows as "failed" in the admin column (voice review, 2026-10-01).
- **Recent context through the Batch API (#380, 2026-10-02)** - `check_pending_recent_context_updates` (beat, 10 min) applies the same gates and builds the same prompt as the direct path (`_prepare_recent_context`), sizes it with `fit_by_count` against the model's input cap (a batch item has no prompt-too-long retry), and submits one batch per provider/model via `llm_batch.batch_submit`, persisted as `RecentContextBatchJob` (mirrors `ExternalDigestBatchJob`; users with a pending item are skipped). `collect_recent_context_batches` (beat, 60 s) claims an ended job with a conditional status update (overlapping runs save once), writes each cost row at batch price on the user, and saves the `UserRecentContext` unless the account opted out meanwhile or a saved summary already covers the window; each item gets an `outcome`. Failed items (no result, empty output, or the job abandoned after `llm_batch.BATCH_JOB_MAX_AGE` = 25 h, now shared with the digest collector) count in `refusal_backoff.recent_context_*` next to refusals; each item records whether it was `billed`. Two billed failures stop the user until a new version (#368); a stop with fewer billed failures lifts after `refusal_backoff.UNBILLED_STOP_EXPIRY` = 24 h (heuristic), so an outage does not stop a user for weeks. The check and the collector's claim-to-save step run under one Redis lock (`recent_context_batch_lock`, non-blocking, 30 min TTL; unlocked without Redis), so overlapping checks or a check during a collection cannot submit a user twice. Prompts keep reading the previous summary until a new one is saved.
- **Model picker (#355, 2026-09-24)** - the picker shows a short list (the models flagged `featured` in `SUPPORTED_MODELS`, currently Opus 5.5 / Opus 4.6 / GPT-6 Astra, plus the selected model) and "More models…", which swaps in the full list grouped by provider, newest first. Each action carries its own picker joined to its right edge (`[LLM Response | model]`, `[Read | model]`). Reads run only on the models flagged `read` (GPT-6 Sol/Luna, GPT-5.6 Sol/Luna; default `READ_DEFAULT_MODEL` = GPT-6 Luna): the read routes refuse any other model, and a read reached another way (a reply asked for under a read prompt or read reply) is switched to the read model when its placeholder is created. Defaults never cross: a read takes the model of the thread's last read (else GPT-6 Luna) and a chat reply walks past read replies to the nearest chat reply (else the preference, else `LLM_NAME`). Deprecated: Opus 4.7/4.8/5, Fable 5, GPT-5.4/5.5, Sonnet 4.6; short display names ("Opus 4.6"); code fallbacks moved from `claude-opus-5` to `claude-opus-4.6` and the app refuses to boot on a deprecated `LLM_NAME`. **Read-only models (2026-10-02):** a `"chat": False` flag keeps a model to reads (only the Read picker offers it; refused as account default, `LLM_NAME` and poll model; a non-read reply sent with it is refused with a 400 and never moved to another model); GPT-6.1 Sol and Sonnet 5.5 added as read-only read models, Haiku 5.5 on 2026-10-08 (prompts over 100k tokens bill at 5x input and output, so every Read is in that tier).
- **Multi-model LLM provider abstraction** - Unified interface for OpenAI and Anthropic (active models and flags in `SUPPORTED_MODELS`) with centralized model config (up to 1M context windows), automatic routing, shared LLM factory, centralized max_output_tokens (10k cap), and response truncation detection
- **Unified agentic Voice workflow** (#81, supersedes Reflect/Orient #70) - Single voice-first mode where AI can use tools (create todos, create GitHub issues) during conversation. Includes proposal tracking with explicit IDs and lifecycle statuses, confirmation nodes for merge outcomes, serialized concurrent todo merges, proactive todo proposals when AI is in planning mode, tab-kill resumability via URL param sync, audio chunk flushing on pause, and TTS resume from Log. Within-turn tool chains play as one continuous audio queue: TTS is dispatched per node at its own finalization so the interim step ("(on it…)") is playable while the continuation is still generating, and the Voice UI transitions playback → "Thinking…" → playback when the follow-up isn't ready before the interim finishes.
- **Iterative profile generation with automatic updates** - AI-powered analysis of user's full writing archive in planned equal chunks (~90k content units each, `docs/design/chunk-planner.md`), with automatic profile regeneration after new entries, profile integration step, and recent context summaries for profile freshness (#71, #80)
- **Context artifact tracking** - Unified context artifact system for system nodes with URL-style params for user_export placeholder, collapsible sections for context data in detailed node view (#77, #79)
- **Frequent audio upload + batched transcription** (#74) - 15-second upload intervals with batched transcription and draft recovery for resilient voice recording
- **One recording device per session (#388, 2026-10-21, branch `fix/388-recording-device-key`)** - `Draft.streaming_device_key` (`backend/utils/recording_device.py`): init stores the recorder's `device_key` (web: one per page load, `utils/recordingDevice.js`; iPhone: one per install, `RecordingDevice`); "Continue recording" starts the recorder, holding its chunks, then calls `POST /api/drafts/streaming/<id>/claim` (409 `session_live` while another device records, 400/404 once ended), which makes its key the writer, stamps the session and returns `next_chunk_index` (one past the highest stored chunk). Chunk uploads, finalize, release and the transcription stream sent with another key get 409 / an SSE event `session_taken_over`; the old device stops and says the recording continued elsewhere. A chunk upload encrypts its files under names of their own first (the KMS call), then re-checks the key with the draft row locked (`lock_writer_key`, 5 s lock wait, 503 `session_busy`) and only moves the files into place, inserts and commits; a claim takes that lock before it counts chunks. Requests without a key (older clients) are unchecked
- **Audio download** - Server-side audio download with ffmpeg merge and mp3 conversion
- **System prompt management** - Prompts page for viewing and editing AI system prompts (#72). System prompt nodes linked via FK instead of duplicating content (#73). One-off system prompt edits per thread with confirmation dialog. Default prompt propagation to existing users.
- **Todo page** - Collapsible nested sub-lists, improved parsing, voice label display ("Voice" instead of "voice_session")
- **Cmd+K keyword search** - Global search with date filtering, diacritics-insensitive matching, and infinite scroll pagination
- **OAuth authentication flow** - X login matched by numeric X id only, never by handle (2026-09-17: handle matching let whoever held a handle log into a same-named magic-link account); admin whitelist by handle resolves the X id via the Community Archive (optional, explicit paid X lookup) and creates the account unapproved — approval is a separate manual step after the pre-fill; logout drops the stored X token; Connect X on the Account page (#311, 2026-09-23) attaches an X login to a signed-in account (same id-only rules: never over an existing X id, never an X id another account holds; an unclaimed placeholder holding it is named as such), X-only accounts can add an email and email accounts can disconnect X; the login page says X sign-in makes a new account unless X is connected; every X callback must carry the request token its own session was sent to X with (closes the OAuth 1 login CSRF and forced linking)
- **Tree-structured node system** - Hierarchical content organization with parent/child relationships, denormalized `human_owner_id` for efficient access control
- **Version history tracking** - Edit history for nodes with timestamps for audit trails
- **Chunked file upload** - Handle large audio files (>10MB) by splitting into chunks and server-side assembly
- **Audio transcription pipeline** - Convert speech to text with file compression and chunk splitting for large files. Resumed (recovered) recordings are partitioned into independent MediaRecorder subsessions and transcribed separately, so pre-resume audio is no longer silently dropped at the finalize merge (#124)
- **TTS with SSE streaming** - Text-to-speech via Server-Sent Events instead of polling (#67), with correct cost attribution to requesting user. Section-aware chapters: markdown h1/h2 headings become navigable chapters with an in-player dropdown (live now-playing indicator that advances during playback + jump-to-chapter seek) and gapless tiny-first-chunk fast start (#145, #140). Node TTS speaks links as their text and addresses short (`backend/utils/spoken_links.py`, #461): one streaming machine used whole by the batch task and per delta by `SpokenTextProjector`, so both say the same; inline code is read as written (links in it included). A fenced code block (backticks or tildes; after list markers too, `1. ```bash`; indented only inside a list item) is spoken as `CODE_BLOCK_SPOKEN` ("Code omitted.", after the item's marker) in place of its opening line, and its content and closing line are dropped before chunking, so a `# comment` in it makes no chapter on either path (#646). Fences are paired line by line; where in doubt a line is read rather than taken for a fence (known limits in the module docstring). Each chunk's cost is an `APICostLog` row committed as the chunk is made (batch task and voice worker), so audio made before a failure is recorded; `TTSChunk.text_digest` (HMAC-SHA256 with `SECRET_KEY` of text, model, voice and chapter pause) lets a retry keep the chunks an earlier run made and make only the rest (#526)
- **Claude data export import** - Import conversation history from Claude export files (#64)
- **Background ChatGPT/Claude imports** (2026-10-20, branch `fix/background-chat-import`) - Analyze stashes the analyzed conversations on the server, encrypted with one data key (`backend/utils/conversation_stash.py`, 24 h TTL, hourly sweep) and returns a token; confirm checks the token, allows one running conversation import per user (a second confirm of the same import gets the running job's id) and queues `backend.tasks.chat_imports.import_conversations`, which commits every ~500 nodes at a conversation boundary, saves its place, and resumes after a deploy, a crash or the time limit; nodes are encrypted with one data key per committed chunk (`Node.set_content(dek=)`), so a 43k-message import makes ~85 KMS calls instead of 43k. Progress "N of M messages" via `/import/status`; web and iPhone
- **Background import recovers from a killed worker** (#546, 2026-10-21, branch `fix/546-import-crash-recovery`) - A running ChatGPT/Claude import's lock is touched every 20 s by a heartbeat thread (`conversation_stash.keep_alive`); a running lock untouched for 3 min means the run's process died. The beat task `resume_stalled_imports` (every minute) sends such a task again under the same id, so the client's polling follows it; it continues from the saved place. A lock waiting in the queue keeps the 30-minute staleness. The run counts the deaths of its task (`claim_lock`: a lock still `running` for the same task; a saved commit clears the count, `clear_crashes`) and stops after 3 resumes in a row without progress with "The import stopped several times… Press Confirm Import again"; a new confirm then continues from the saved place. Before, Redis handed the task out again only after its 1 h visibility timeout and the lock blocked a new confirm for 30 min
- **Word and PDF import** (#521, #528; PDF 2026-10-16) - A .docx becomes entries, one per chapter (mammoth → Markdown, size and structure checks first). A single .pdf is read with pypdf in a child process with CPU, memory, wall-clock, page and text limits (`backend/utils/pdf_import.py` + `pdf_extract_worker.py`, so a crafted PDF cannot hold up a web worker); chapters come from its outline, running headers/footers and page numbers are dropped, lines are de-hyphenated and reflowed. The confirm step asks whose writing it is: the user's own becomes entries (`origin="pdf"`), someone else's one saved Reference (`ExternalItem.source="document"`; title and author kept encrypted inside its text, `backend/utils/document_reference.py`). Scanned or password-protected PDFs, old .doc files and PDFs inside a zip get specific messages
- **Import AI usage follows the account (#593, Peter 2026-11-10)** - `ImportData.js` sets the dialog's AI usage to `user.default_ai_usage` each time a confirm dialog opens ("none" if the user isn't known); the iPhone `ImportModel` does the same from `app.user.defaultAIUsage`. The confirm routes (`/import/confirm`, `/import/twitter/confirm`, `/import/{chatgpt,claude}/confirm`) resolve a missing `ai_usage` to `current_user.default_ai_usage` (`_requested_ai_usage`); a named value is kept, so an older app that sends "none" works as before. `_apply_settings_to_skipped` is unchanged: a re-import applies the value sent to alive duplicates.
- ✅ **A re-import's profile hand-off counts only what it changed (#625, Peter 2026-11-11)** - Every importer records its changes in `routes/import_data._ProfileChanges`: entries created or restored that AI may read, and alive duplicates whose new AI usage crosses `AI_ALLOWED` (recorded in `_apply_settings_to_skipped`; a privacy or Chat↔Train change is not a change). `_profile_after_import`: entries added → `_hand_off_profile_update_after_import` dated from the oldest changed entry's `created_at`; entries only removed (Chat→None) → no hand-off, the result carries `ask_profile_rebuild` (`{token, entries}`, an itsdangerous token of the user and that date, `PROFILE_REBUILD_QUESTION_MAX_AGE` 7 days) when the account gets profiles, allows AI and its profile in use has a cutoff at or after that date; nothing changed → no profile work. `POST /api/import/profile-rebuild` runs the hand-off for a yes, once per question: the question's id (`User.profile_rebuild_offer_id`, in the token) is cleared by the UPDATE that checks it, and the yes is a no-op (`200 {"status": "skipped", "message"}`) when a rebuild is flagged, profile work is in the batch or a profile task runs, or a profile version was saved after the question was asked. Web `ProfileRebuildQuestion.js` and the iPhone `ImportFinishedDialog` show it in the import result. A background conversation import keeps its changes in its saved state (a state from before carries the old earliest-message date), also across a re-run with other settings. `exports.revert_profile_for_import` re-tips a window set instead of flagging a full rebuild (`_retip_window_set`): the windows that end before the date and their current groups are copied (raw column, nothing decrypted) into a new set, the last copy stamped with the present render time so the continue rule reads the rest; also when the chain starts on a set's final (`_window_set_under`); no window before the date → the set's base is re-tipped as a chain (`retip_profile_chain`, which decrypts and encrypts the copied version, as main's chain re-tip); the window path off → the full rebuild as before; work in flight or a profile run under way → the date waits until it is collected (#639, below).
- ✅ **An import while a profile build is in flight waits for it (#639)** - `exports.revert_profile_for_import` no longer flags a rebuild from scratch while the user's window work is in the Batch queue or being rendered (`import_retip_waits`: window items in pending jobs, the user's run lock held by another run, or an advance's claim: the guard with `profile_batch_pending`, not stale). It returns `"deferred"` and keeps the date in `User.profile_import_retip_from` (the oldest of several imports'; a later import merges its date with it). `apply_waiting_import_date` applies it once nothing is in flight: in `profile_windows.next_work` before `resolve_set` (its own run excluded), in `profile_batch._should_seed` and in `maybe_trigger_incremental_profile_update`; with no profile version yet, or a rebuild from scratch flagged, it is dropped (that build reads everything); `_profile_waits_for_imports` says the profile waits for the imports while a date waits. Both the hand-off and the applier lock the user's row (`_lock_user_row`, SELECT … FOR UPDATE) before reading the date; a re-tip that raises is rolled back, keeps the date, sets `profile_seed_error` ("Re-tip after an import failed") and returns "error", so the seeder continues with the next user; a waiting date keeps `profile_work_held` (`_work_waits`). The import hand-off records a date also when the user has no profile version but a first build is in flight. A synchronous task's guard alone no longer holds the re-tip (it chooses its base when it starts; `base_moved` stops it). Before, a first build's window collected after the import cleared the rebuild flag (`_collect_window`), so the imported entries inside its ranges were never read.
- ✅ **A re-import changes every entry of a split item (#638, 2026-11-12)** - New column `Node.split_item` (String(32), indexed): a random uuid4 hex shared by every node of an imported item saved as several nodes (`_import_file_nodes` for a document's chapters, `_add_imported_message_nodes` for parts above `NODE_CHAR_CAP`); NULL for single-node items and native nodes. `routes/import_data._apply_settings_to_skipped` takes the keyed nodes a re-import matched and applies privacy and AI usage to every alive node of their items, found by `_split_item_nodes`: the nodes of the tip's owner with the tip's token; for a tip without one (imported before the column) the re-imported text's shape (`_item_shape`: per entry, its date offset from the item's first entry, `CHAPTER_STEP` 1 s per chapter and `PART_STEP` 1 ms per part, and its `token_count`) is walked up from the tip: every parent must be alive, unkeyed, tokenless, of the same `user_id`, `human_owner_id`, `node_type` and `origin`, dated exactly the shape's step earlier, and of the shape's `token_count` unless it has a `NodeVersion`; if any step fails, only the tip is taken and the item counts in the result's `partly_updated` (web `partlyUpdatedNote`, iPhone `ImportResult.notes`). Ids and these columns only, no content, 1,000 values per query. Every importer (Markdown/Word/PDF confirm, ChatGPT/Claude background job, Twitter) collects its matches in `_Matched` (every matched key, deleted ones included, with the re-imported parts), and the restored ones go through a second call that is not counted as `updated` (a restored item's other parts; the old chapters of a Word/PDF document whose deleted key `_release_deleted_docx_key` gives up, only by its token: nothing walks up from a given-up key). `updated` counts items in which an entry changed. Each changed entry goes through `_ProfileChanges.setting_changed` (A.79). `_invalid_settings` makes `/import/confirm`, `/import/twitter/confirm` and `/import/{chatgpt,claude}/confirm` answer 400 for a privacy level or AI usage outside `VALID_PRIVACY_LEVELS` / `VALID_AI_USAGE`. `backend/scripts/count_split_items_left_open.py` (read-only, metadata) counts older items whose keyed entry is None while an entry before it still allows AI, including parts imported before 2026-08-27 without an origin.
- **Dollar-cost tracking** - Per-user API cost tracking via APICostLog model, replacing token-based tracking (#61)
- **Pin-to-profile** - Pin selected nodes to user profile page (#60)
- **Cmd+Click navigation** - Open nodes in new tab across all previews
- **Dev-update channel** (#207) - In-app "what's new" surface: user-facing changelog authored as a markdown file in the repo (`backend/user_changelog.md`, per-section per-user read/skip state, date-gated so new users get no backlog), persistent targeted notifications (profile-generation-complete wired as first producer), and admin polls with two-phase opt-in (LLM drafts an answer from the user's profile only on explicit request; nothing reaches the admin until the user explicitly sends). `DEV_UPDATES_V1` killswitch. This modal is the announcement channel for dark-shipped features (e.g. the public side / Commons).
- **Issue-close notifications** (#236) - v0 of the automated issue→fix→verify loop (#237): inbound GitHub webhook `POST /api/webhooks/github` (HMAC-verified via `GITHUB_WEBHOOK_SECRET`, fail-closed) reads the `loore:{username}` label on closed issues and notifies the submitting user through the dev-update channel — `fix_ready` (completed) / `issue_declined` (not planned) rendered as an eyebrow verdict over the quoted issue title. "Take a look" opens external links in a new tab, keeps the modal open, and never marks read (skip semantics); only "Got it" dismisses.
- **Platform label on issues Loore files** (2026-10-02) - An issue filed from the iPhone app gets the `ios` label, one filed from the web app gets `web`, none when the app is unknown (`utils/client_platform`: `X-Loore-Client` header, else the User-Agent). Inside a reply turn (apply_github_issue) the app is the one the turn's request stamped on the reply placeholder (`_client`).
- **Quote-as-response over saved references** (#208) - Feature 2 (Download) v1: external-content substrate (`ExternalItem` — references ≠ lore; X OAuth nightly sync in each user's own 3am with early-stop pagination + revocation handling + `APICostLog` metering; Community Archive fetch; craft-mode JSON import with enrichment folding), agentic `semantic_search` across own archive + references (labeled previews, server-side canonicalization of `{quote:label}` → absolute markers), `read_full` tool (query intent) vs quote markers (presentation only, verbatim cards — never LLM-retyped), `external_digest` topic-map artifact, surfacing-history metadata instead of cooldown rules. Ships as a per-user Account toggle (`external_content_enabled`, "External references", default off); since #329 the toggle gates only saved references — #197's agentic own-archive search is on for everyone; `SEMANTIC_SEARCH_AGENTIC` env is killswitch-only. Follow-ups: #231 (tripwire), #233 (overnight batch pre-selection — lags one day by design).
- **Chrome clipper** (#232, 2026-09-12) - MV3 extension (`extension/`, unpacked install): one key press captures the active tab in-browser (Readability + Turndown → Markdown, capped at `NODE_CHAR_CAP`; tweet extractor on x.com) → `POST /api/external/clip` → tab closes. New `web_clip` source keyed by sha256(canonical URL) + `ExternalItem.title`; tweet URLs route to `twitter_bookmark` by tweet id (dedupes with the sync). Auth via personal API tokens (`ApiToken`: sha256 at rest, `external:write` scope only, `token_or_login_required` decorator; minted/revoked on the Import page, plaintext shown once). Permissions: `activeTab` + `scripting` + `storage`, hosts limited to loore.org/staging/localhost. Same-day follow-ups: References log (`/references`) + reference page (`/references/:id`, tweet embed via X's widget, stored text under a toggle), user-only `read_at` mark (POST/DELETE `/api/external/items/<id>/read`) beside the AI's `surfaced_count`/`last_surfaced_at`; References search (`scope=external` on both search endpoints, `SearchModal scope` prop, ⌘K is page-aware). 2026-09-13: `ExternalQuoteBubble` footer carries the owner's read toggle (quote payload gained `user_id`/`read_at`; same POST/DELETE endpoint) and the good/bad-quote verdict (`ReferenceFeedback`, `ExternalItem.feedback`/`feedback_at`, `POST /api/external/items/<id>/feedback` with `good`|`bad`|null; in a node's quote bubble only when the node is LLM-authored (`showRecommendationFeedback`, #363 — a user's own quote gets the read toggle alone); also on the reference page; surfaced on the model's search previews as "the user rated this a good/bad quote"; the in-thread `<quoted_reference>` block carries `read="YYYY-MM-DD"` / `verdict="good|bad"` for user-authored quotes; the assistant's own quotes keep their raw marker in history, so the user's marks on them reach the model as a bracketed note in the synthetic final user message (`_reference_marks_note`); exports render the same marks on the "--- Saved reference ---" header). Not built: the bookmarks-page bulk harvest mode from #232's original text.
- **Twitter cold-start pre-fill** - Admin "Pre-fill from CA" (`POST /api/admin/users/<id>/prefill`): imports a handle's public tweets from the Community Archive (keyset-paged REST, shared client `backend/utils/community_archive.py`) into a whitelisted account as private `origin=twitter` nodes and pins the user to the BATCH profile pipeline (`User.profile_force_batch`) so the bootstrap profile is never built on the synchronous full-price path. Imports no longer dispatch the sync profile task for batch users. Chunk sizing has since moved to the equal-chunk planner (next bullet). Follow-up: nightly parquet snapshot on the VM for very large accounts (REST works, just slower). **Provenance (2026-09-21, #295 step 0):** imported nodes carry `Node.provenance` — `prefill_ca` (public by construction), `prefill_x` (the pre-fill refuses protected accounts, so public at fetch) or `archive_upload` (the user's own export; may hold a protected account's tweets) — required keyword on `create_twitter_nodes`, defaulted on the message importers, re-stamped on restore; `origin` stays platform-only and dedup on `source_key` is unchanged. Saved references got the matching `ExternalItem.public_source` (True / False / NULL = vouched public by source / assessed and not vouched / unknown); the clipper reports a protected-account lock it saw (`author_protected`, never its absence), and the nightly `verify_public_source_sweep` (`external_sync.py`, hourly beat gate `verify-public-source-nightly`, runs at 05:00 UTC — a daily interval would start over on every deploy) resolves unknown tweets through X's free oEmbed endpoint (`publish.x.com/oembed`: 200 → True, 403/404 → False, 429/5xx/network → stop for the night; ≤300 lookups counted per distinct tweet id, each verdict committed as it lands, `public_source_checked_at` rotates unanswered rows behind never-asked ones; pre-column Community Archive rows are vouched True by the same sweep). Recorded now because the open "public tweet → public on Loore?" decision and the shared reference tier (both on #295) need it and it cannot be recovered later; nothing reads either column yet.
- **Community Archive personal feed — PoC** (2026-09-13, branch `feat/ca-tweets-feed-poc`): `{ca_tweets?days=N}` prompt placeholder renders the last N days of the whole Community Archive corpus (nightly parquet snapshot under `COMMUNITY_ARCHIVE_SNAPSHOT_DIR`, duckdb, ~4.7k tweets / ~250k tokens per day, retweets dropped) in the compact tweet format, no per-tweet timestamps, each tweet numbered so the model cites `#n` and the task expands it to the x.com link on collect; the window ends at the newest tweet in the snapshot. A turn carrying it is sent through the Anthropic Batch API as a one-item batch (`_ca_batch_submit` / `_ca_batch_poll`: submit, store the batch id on the node's `tool_calls_meta` as `_batch`, re-queue the same Celery task every 120 s; a poll is one provider call made before any context is built, and only the run that finds the batch ended renders the prompt and runs the normal finalize; cost logged with the batch discount). Pre-flight validation refuses unknown keys / bad `days` before any node exists; admin-only while a PoC and `days` capped at 3 (one request is a few cents to a few dollars); a beat sweeper (`resume_stuck_feed_batches`, every 10 min) re-dispatches a submitted batch whose poll heartbeat stopped (hard worker kill). The reply is structured output (`FEED_SCHEMA`: verdict + up to 20 picks with why / relevance % / recommend); on collect each pick is upserted as a saved reference (`ExternalItem`, source `community_archive`, surfacing bumped) plus a `FeedPick` row (rank, relevance, recommended, why) so the model's claims can be judged against the user's read mark and good/bad verdict; the node text is the verdict, the picks render from `GET /api/nodes/<id>/feed-picks` (`FeedPicks` component: one run of full tweets with the relevance number in the margin, read picks fade). Own handle excluded from the render. Not yet: snapshot refresh (a beat task calling `ensure_snapshot`), a follows-only scope, windowing a day into ~100k-token requests with a shared cached prefix, any UI. Scoping data (corpus size, per-day token costs, cluster coverage): the 2026-09-13 "Community Archive daily tweets" artifact.
  - **Quote-tweet form (2026-09-16, PR feat/feed-v2):** the reply is now the whole feed in the node text — the verdict, then for each pick Loore's quote-tweet (two or three sentences addressed to the user on why this tweet meets their situation) over a `{quote_ext:ID}` marker that renders as the quoted tweet with the user's read mark and good/bad verdict, and that a later read in the same thread sees with those marks. A read never runs under the agentic system prompt (Voice / Text mode prompt nodes are dropped from the context wherever they sit; the sharing around them stays); every read node is AI usage `chat` and the editor / cascade refuse `train` on them (no licence to train on public tweets); admin cancel-and-rerun on a pending or failed reply (`POST /api/read/<id>/rerun`, batch again or live with the same structured output); the prompt tag reads "Read" for both prompts.
  - **Batch poll hardening (2026-09-18, PR feat/feed-v2, after the 2026-09-17 prod failure):** a poll is one provider call made before the context is built (the chain and the `{ca_tweets}` render happen only on the run that finds the batch ended); while the batch is submitted only the provider's verdict on the item (`BatchItemFailed`) or the poll cap fails the node, any other error (a worker restart mid-render, KMS, the network, the DB) logs and re-queues the poll with the entry still submitted; the entry flips to `ended` in the same commit as the completion, so an interrupted collect fetches the same result again and never pays for a second batch; the key type is stored on the entry at submit. A persistent error therefore retries until the existing 800-poll cap (~27 h) rather than failing at once, except a reply the collect cannot use (truncated or unparseable, `FeedReplyError`), which is terminal because the stored result is immutable; no separate error cap was added. Spend cap during the batch window: a batch is billed when the provider processes it, not at submission, so a poll that finds the user capped while the batch is still queued asks the provider to cancel it (entry `cancelling`) and the provider's answer decides: an item that ran before the cancel took effect is billed and therefore collected and logged as usual; one that never ran (Anthropic result `canceled`, OpenAI `cancelled` without the item in the output file, `BatchItemCancelled`) is not billed, gets no cost row, and the node ends `cancelled` with a line saying why. A batch that had already ended is collected and billed regardless of the cap.
  - **Second feedback round (2026-09-19, branch `fix/read-feedback-round2`):** the read refreshes the cached export before rendering (the archive exports ~07:00 UTC; nothing on prod refreshed the snapshot after a pre-fill, so a 09-19 read served 09-14/09-15) and a beat sweep (`refresh_community_archive_snapshot`, every 30 min) keeps it current, both only where a snapshot already exists and under a file lock; what the reader has already seen — read-marked picks, X bookmarks and likes, clipped tweets — is dropped from the render before the model sees it (`seen_tweet_ids`; an unread earlier pick stays a candidate and the model decides); each render's numbering is pinned on the reply (`FeedRender`: export, UTC window, counts, tweet ids in order) so a batch collected hours later resolves its numbers against what was sent, fetching only the picked tweets by id (a batch from before pinning re-renders as before); turn semantics — only a read feeds the day in: the first reply under the prompt, or a reply asked for directly under a read reply (a read-again: the day minus what was seen since, the earlier picks resolved in the assistant turn without marks, the reader's marks and the request in the closing user turn); a user message after a read reply is a chat turn (stub where the day was, picks and marks in context, no feed schema). The marks note is no longer gated on an agentic chain (a read turn never is, so the model had never seen the verdicts). A chat turn under a read reply is a Text-mode session (#323, decided 2026-09-21): the reply box posts to `POST /textmode/from-node/<id>` (a recorded reply: `save-as-node` with `agentic`), which attaches the textmode prompt under the read reply when no agentic prompt sits above, so the conversation about the picks runs with the assistant's tools and can move the user's intentions and memory; read and read-again turns still drop every agentic prompt from the context. The Read button inside a read thread reads further from any node (`POST /read/from-node/<id>` makes a read turn under it marked `_read`, no second prompt, whole thread in view: comments, chat replies, earlier picks with marks); outside one it attaches `read_thread` as before; the button reads "Relevant tweets" (was "Read the archive": archive is the user's own; briefly "Read the day"), tooltip "Loore reads the last day of Community Archive tweets and shows you the ones relevant to this thread". Opening a pick (the quote, or Open on X in the legacy list) marks it read, and so does a good / bad verdict (server-side, in the feedback route); Mark as unread stays for the ones to come back to. Thread page: the window line above the verdict (local time, counts, how many seen tweets were left out), a tail with the list's read state and Mark all as read, the reply box says a reply talks about the picks; under every node of a read thread the action row carries "Read further" beside LLM Response and the model selector (the selector sets the model for both; LLM Response is disabled directly under the picks — tooltip "To chat about the recommendations, send your reply first. To read further, use the button on the right." — and enabled again under a comment with "Chat about the picks"), so the action stays at hand in a long conversation without pinning anything to the viewport — small screens.
  - **One row per tweet and a recommendation log (#352, 2026-09-24, branch `feat/352-read-picks-accuracy`):** a pick of a tweet the user has not saved gets source `read_pick` and is not a reference (the references list and counts, the digest, the embedding sweep and reference search skip it); a pick of a tweet the user already has reuses that row, and saving a picked tweet (clip, bookmark sync, JSON import, Community Archive import) turns the pick's row into the reference in place, so one tweet has one read mark and one verdict; Delete on a saved reference that has Read picks turns it back into a pick. The seen filter drops only tweets marked read, from any source (bookmarks and clips not marked read stay in). Every reference an agentic reply quotes is logged like a pick (`FeedPick.kind='quote'`), and every pick carries `decided_at` (the read's submit / the turn's start) and what the model could know then (`prior_read`, `prior_verdict`); a new `ReferenceAction` table logs each open, read / unread mark and verdict with the reply it was done in. Which verdicts count for which recommendation is computed when read (`utils/reference_log.outcomes`): blind picks of a tweet (made before any verdict) share the latest verdict, opens and read marks across all Reads, quotes within their thread; a recommendation made after a verdict is judged on its own and shows "You rated this good on …". `backend/scripts/recommendation_report.py` prints the counts per kind, model and mode; `backend/scripts/backfill_read_picks.py` brings existing rows into this shape (prod run after deploy).
  - ✅ **Replies under a Read keep the user's AI usage (#362, 2026-09-25, revised in the 2026-09-29 voice review; PR #365):** a Read's own nodes are 'chat' (`FEED_AI_USAGE`) because they quote other people's tweets: the read prompt and the recommendation reply that presents the picks (a `read` or `read_again` turn; the read routes stamp it, and `create_llm_placeholder` and the completion task lower a 'train' placeholder for such a turn). Nothing below those nodes inherits 'chat'. One helper, `llm_nodes.reply_ai_usage`, gives a new reply's default: walking up from the parent, it skips the read prompts and the read replies (`_Chain.reads`) and takes the nearest node left, user or LLM; none left (a Read at the root) → the account's `default_ai_usage`. Skipped nodes pass nothing on, 'none' included. Every LLM reply after the recommendation (a `chat` turn) takes the thread's setting like any other reply and can be 'train'; the per-call key decision (`ca_turn` forcing chat keys, `PayloadLicence.note_external`) still keeps the verbatim tweets off the training key. The node editor and the settings cascade refuse 'train' only on `is_feed_node` nodes, as before the PR. The reply form takes the default from `GET /nodes/<id>` (`reply_ai_usage`, walked only in the owner's read thread); Voice (`/voice/from-node`, `/voice/`), Text mode (`/textmode/message`, `/textmode/from-node`) and the streaming voice path use it directly. The reply box under a read reply sends the form's AI usage to `/textmode/from-node`; a 'none' note there is saved as a plain reply. Existing replies under Reads are left as they are.
  - ✅ **Read tracking (voice review 2026-10-02, branch `feat/read-tracking`):** new `FeedRender.opened_at` (first fetch of the finished reply by its owner: `GET /nodes/<id>` of the reply, or `llm-status` returning it to a poll that carries `?visible=1`; one UPDATE `WHERE opened_at IS NULL`. The web thread page sends the flag only while `document.visibilityState` is `visible`, via the `reportVisible` option of `useAsyncTaskPolling`, plus one extra poll when a tab that was hidden at completion is shown; a request without the flag, such as the iPhone app's, never sets it through llm-status) and `FeedRender.dropped_picks` (picks Loore could not show, `ca_feed.count_dropped_picks`, called by `refs_from_render` on the batch collect and by `_collect_feed_reply` on the live rerun: the tweet left the snapshot, or the number is outside the render). `recommendation_report.py` prints a Reads table: completed Read replies per model / variant / turn, empty ones split into "model picked nothing" and "all picks dropped", and the opened share. A feed reply that fails with `FeedReplyError` (unparseable or truncated) logs its cost row (`request_type="conversation"`, `request_ref="node:<id>"`) before failing; a batch entry stamped `failed_cost_logged_at` keeps a re-collect of the same batch from logging twice.
  - ✅ **Glean for every user (#435, 2026-10-09, branch `feat/435-glean`):** `utils/glean.py`. Gate `placeholders.ca_tweets_allowed` = admin, `GLEAN_FOR_ALL`, or `GLEAN_USER_IDS`; only admins may change the `{ca_tweets}` window or scope (`placeholders.check_ca_tweets_options`, 2026-10-14: for anyone else every placeholder is `{ca_tweets}` or exactly `days=1` and/or `scope=all`, checked in `create_llm_placeholder` and again in the completion task); per-user switch `User.glean_enabled` (NULL = default from X / Community Archive data: `twitter_id`, `prefilled_handle`, an `ExternalAccount` for X, nodes with `origin='twitter'`, saved tweet `ExternalItem`s), exposed as `glean_available` / `glean_enabled` on the current-user payload and saved by `PUT /dashboard/user`. `Node.started_from = "glean"` on the root of a thread started from the Glean card (`entry: "glean"` on `/textmode/start`, save-as-node, uploads, `POST /voice` and the streaming finalize); `GET /nodes/<id>` reports `glean_thread`. `POST /read/from-node` needs the switch, always creates the reply, and every read turn's placeholder carries the `_live` marker (`create_llm_placeholder(read_live=True)`). Rework after Peter's local test (2026-10-09): the Batch API is opt-in, only for a read marked `_read_batch` (`/read/start` passes `read_live=False`; the admin rerun API with `live: false`), so no glean can end up in a batch; a read never downloads a newer archive export inline (`ca_feed.request_snapshot_refresh` queues `refresh_community_archive_snapshot`; the transfer runs under `community_archive._download_lock`, outside the readers' lock); one run renders into a reply (a superseded run stops before rendering, `record_feed_render` inserts in a savepoint, the rerun API refuses a live glean still running). The rerun buttons are gone (the admin API stays); a failed gleaning shows `llm_task_error` (owner only on `GET /nodes/<id>`). Read models follow the provider of the user's chat model (`llm_nodes.glean_provider`, `resolve_read_model(user=…)`, `GLEAN_MODEL_ANTHROPIC` / `GLEAN_MODEL_OPENAI`, any active read model, checked at boot; both GPT-5.6 Luna since 2026-10-14 while Glean is tested) by default; everyone who gleans may name any read model, another provider's too, under their own node (`glean.may_choose_glean_model`; Peter, 2026-10-09), through the Glean picker (`/nodes/<id>/suggested-model?purpose=read` gives its default). `/voice/from-node` on a gleaning returns `mode: ready` (never played). `ExternalItem.author_name` from `profiles.parquet` (render and fetch-by-id refs carry `display_name`; `scripts/backfill_pick_author_names.py` fills past picks).
  - ✅ **Home page by purpose (#436, 2026-10-09, branch `feat/436-home-glean`):** `HomePage.js` renders `purposeCards` (Reflect → `/voice`, `/textmode`; Glean → `/voice?glean=1`, `/textmode?glean=1`) for users with `glean_enabled`, with the unchanged Share card in a row below, the old Voice / Text grid otherwise; the admin Read card and its `/read/start` call are removed from the page. NavBar's first link reads "Home". After Peter's local test (2026-10-13): each purpose card is a `ModeCard` (the Voice / Text cards' glow and lift through the shared `cardSurfaceStyle` / `useCardReveal`) started by one button laid over the whole card; a Voice | Text radio group (`ModeSwitch`) only chooses the mode, remembered per card in `localStorage` (`loore.home.reflectMode`, `loore.home.gleanMode`; Voice when unset or when storage fails); `.home-mode-grid` in `index.css` gives two 280 px columns with Share centered below, one column at 620 px and narrower.
  - ✅ **iPhone app: Glean (#475, 2026-10-09, branch `feat/475-ios-glean`):** `CurrentUser.gleanEnabled` gates everything (`UserCapabilities.gleanEnabled`); `HomeView` renders `HomePurpose` cards; routes `.voice(…, glean:)` / `.textMode(glean:)` parse `?glean=1`; `TextModeView` and `VoiceTurnController` send `entry: "glean"` only for a new thread; `VoiceTurnController.glean()` posts `/read/from-node/<anchor>` with `{}` (the server's default, as the web's Voice screen), polls llm-status (2 s) and hands the gleaning to `AudioCenter.openGleaning`, never to TTS; the Live Activity's `ContentState` carries the Glean button (`GleanVoiceIntent`) and a `gleaning` phase; `ThreadModel` mirrors NodeDetail's Glean rules (`offersGleanInMenu`, `readActions`, the gleaning's head); `ExternalQuoteBubble` is the new card (`author_name` on quotes and references; `Router.push` opens the reference page over the thread). No backend change.
  - ✅ **Reference page as the gleaning's card (2026-10-13, branch `feat/reference-page-card`):** web `ReferenceDetailPage` renders `.ref-card` (index.css: 1px `--border-hover`, radius 12, no accent rail) for every kind; `utils/references.js` gained `TWEET_SOURCES` (with `twitter_like`), `referenceKind`, `sourceLine`, `sourceLinkLabel`, `hostOf`; `tweetId` and `components/TweetEmbed.js` are gone (no X widget anywhere); the link out posts `{via: 'open'}` to the read route; `SpeakerIcon` takes `buttonStyle`. iPhone: `ReferenceDetailView` mirrors it (`ReferenceUtils.kind/sourceLine/sourceLinkLabel/host`), `TweetEmbedView` and `EmbedWebView`'s message channel removed, and `MarkdownStyle.face = .readingSerif` sets the stored text in `LooreFont.reading` (the system serif, New York; no Newsreader is bundled). No backend change.
  - ✅ **A gleaning pick's two verdicts (#502 merge train, 2026-10-14):** `FeedPick.intro_verdict` ('good' | 'bad' | NULL) + `intro_verdict_at`, value only, on the kind-'read' row (hidden and purged with it). `POST /api/nodes/<id>/feed-picks/<item_id>/verdict` `{intro?, tweet?}` (owner, live gleaning, the gleaning's own pick; one request for the pick-level tap); the tweet part goes through `external.apply_item_feedback`, the same path as `/external/items/<id>/feedback` (ReferenceAction verdict against the gleaning, marks read, #352 outcomes). `_recommendation_fields` adds `recommendation_kind` and `intro_verdict` (owner only) to `resolve-quotes` / `feed-picks`. `llm_completion._intro_verdicts` feeds the marks note ("rated your quote-tweet introducing it good/bad"). Web `PickVerdict.js` (under the card in `ExternalQuoteBubble` when `recommendation_kind` is 'read'), iOS `PickVerdictView.swift`.
- **Equal-chunk profile planner** (PR #285, `docs/design/chunk-planner.md`) - Every profile chunk is planned over the remainder in stored content units (`backend/utils/chunk_plan.py`): the chunk COUNT is rounded (T = 90k units), so a fixed corpus is always covered with no dropped tail (the "reads as 2025" pre-fill symptom); the model's real-token input cap (`model_input_cap`: window minus output, hard input ceiling, pricing tier — #259) only ever raises the count; per-model `tokenizer_family` with a per-user measured tokens-per-unit ratio (`User.profile_token_ratio` + `profile_token_ratio_family`) replaces `token_multiplier`; the continue rule (`should_continue_chain`: unread data OLDER than the last version = unfinished chain, continue regardless of the gates) replaces the pinned-account special case and the minimum-chunk deferral in the sync heartbeat and the batch seeder alike; export windows never split a timestamp; the update prompt's proportionality and `source_tokens_used` are in units on both sides. `backend/scripts/replan_tail.py` re-tips pre-filled accounts whose chain stopped short; `backend/scripts/simulate_chunk_plan.py` dry-runs a plan with local token counts.
- **Profile, intentions and poll-draft input is the user's writing only (#550, 2026-10-20)** - `build_user_export_content(omit_artifacts=True)`: no artifact preamble (system prompts, profile versions, todo lists), no `[System Prompt …]` / `[User Profile …]` / `[User TODO …]` / `[Artifact …]` reference lines, no system-prompt node (linked prompt or a detached, edited one: `prompt_key` stamp); its replies keep their index paths, and a thread that is only a system prompt is left out. Profile windows render through `exports.render_profile_window` (sync loop, batch builder and `simulate_chunk_plan.py` alike); intentions inference, its backfill script and `recent_window` poll drafts pass the flag too. The budget resolver counts every node at its stored units there, as the window selection does, so a window's newest entries are no longer left out of the text while the resume cursor passes them (before, the resolver also counted the prompt preamble and the reference lines, which the selection never counted, and dropped that much of the newest writing). Recent context, the raw window, `{user_export}` and the data download render as before.
- **Profile windows render every selected row (#552, 2026-10-21)** - `build_user_export_content(whole_window=True)` (incremental path only; `render_profile_window` passes it): the `ExportQuoteResolver` runs without a budget (`max_tokens=None`) and only resolves quotes, and the render set is the window's own selected rows, so a node the resolver embedded appears only inside its quote. Before, the resolver re-applied the unit budget after the selection had sized the window: quote embeds (charged at chars/4 of the quoted text), a run of equal timestamps at the window's end and a first row larger than the budget pushed selected rows out of the text while `latest_node_created_at` moved past them, and an embedded older parent hid the reply under it. The window's `node_ids`, `unit_count` and cursor are unchanged. Recent context passes the flag too (`_build_recent_export`; it matters only when the summary's window is cut to a budget), so its `source_data_cutoff` covers only rows it read. For every export, `ExportQuoteResolver._get_node_metadata` applies the access and `none` checks before it uses cached entry content, the same checks as its database path. The raw window, `{user_export}`, intentions, poll drafts and the data download otherwise render as before.
- **One read tool for the artifacts index (#218, 2026-10-21)** - `read_todo` is no longer in `VOICE_TOOLS`; `read_artifact(kind="todo")` resolves the newest `UserTodo` (ai_usage checked) and stores `{kind: "todo", todo_id}` in `tool_calls_meta`, never content. `_reads_todo` keys the injection (`_retrieval_injection_text`) and the version pin (`_retrieval_pin`, artifact_type `todo`) on the kind, and also accepts the `read_todo` results older threads carry; the executor still serves a `read_todo` call, since older threads' pinned prompts name it. Web and iOS render a todo read as "Read todo list" with a link to the todo page. The provider payload carries earlier tool calls only as text, so no Anthropic or OpenAI request references a tool it was not given (`tests/test_todo_read_fold.py`).
- **Independent profile versions for large builds (#545, 2026-10-22, branch `feat/independent-profile-versions`)** - `backend/tasks/profile_windows.py`: a build that plans into ≥ 2 chunks on the chat model is a window set (`UserProfile.window_set`, `covers_start`; rows `window`, `window_skip`, `group`, final `integration`). Windows planned at the version model's cap and fitted by counted tokens, generation prompt + `EVENTS_IN_WINDOW_PARAGRAPH`, one Batch submission (sliced at 100 MB); per-provider version/merge models (`PROFILE_VERSION_MODEL_*`, `PROFILE_MERGE_MODEL_*`, boot-checked, never across providers); outputs checked for the three maps (+ one events section); merges flat while they fit the merge model's cap, else in fixed groups of `PROFILE_MERGE_GROUP_SIZE` (`PROFILE_MERGE_STRUCTURE`, flat default), token-counted, over-cap stops once; OpenAI merges on `/v1/responses` with `PROFILE_MERGE_REASONING_EFFORT_OPENAI` (default high). `current_profile_query` keeps windows and groups out of replies, recent context, pins, the profile page and the progress watcher. Attempt history = outcomes on job items (content: 1 identical retry then skip; provider: 3 submissions then 24 h). Per-user spend cap before each submission (held, resumed by the seeder when it lifts; Peter, 2026-10-22); windows go out only when the build's expected cost (`estimate_cost_usd`, measured mean output per call) fits into the user's remaining monthly headroom, else it waits (`profile_cap_held`, "Waiting for spend headroom" note; Peter, 2026-11-05). Models per pass (`pass_models`, Peter 2026-11-05): own provider's pair; `PROFILE_VERSION_MODEL_CROSS_PROVIDER` / `PROFILE_MERGE_MODEL_CROSS_PROVIDER` (Luna / Opus) when `writing_is_public_only` (provenance `prefill_ca`/`prefill_x` or public with sharing on, no foreign thread entries, no user-written profile or prompt) or `User.profile_both_providers`; a regular one-window update of a merged set (`chained_update_base`) is a `window_update` row, main's update prompt over the set's final (or its newest chained version) by the chat model, merged in the window's place by the chat model and labelled "Covers all the writing"; `_Submission.add` refuses a request to a provider the writing may not reach. Imports settle first (#598): the batch hand-off calls `seed_after_import` (`User.profile_import_quiet_from` + one seed `MIN_INACTIVITY` later); the hourly pass, that seed and the window advance build nothing while `import_settling` (a fresh conversation-import lock, or an import or a new entry less than 30 min ago); the admin's Build profile, pre-fills and the sync path do not wait; the import result's `profile_waits_for_imports` drives one line in the import dialog. R7 stuck-pending clean-up, R11 `created_at` gate, OpenAI expired/cancelled partial results, 25 h stale. Kill switch `PROFILE_INDEPENDENT_VERSIONS`.
- **One-chunk builds as one window; polling by listing; a full queue waits (2026-11-03, branch `feat/single-chunk-integration`, on #589)** - `profile_windows.wants_window_build`: a from-scratch build that fits one chunk on the chat model is a one-window set where `PROFILE_ONE_CHUNK_WINDOW_<PROVIDER>` is on (both on by default); models per pass (`pass_models`, 2026-11-05): `one_window_pass` + `single_models_for` (the chat model for the window and the final) for private writing, the cross-provider pair (Luna windows sized for Luna, Opus) for public-only writing or consent; its final uses `exports.single_integration_messages` / `profile_single_integration.txt` (many-version prompt unchanged, hash-pinned in tests). `profile_output_problem(final=True)` refuses an events heading in any final; `MergePlan.nothing_read` holds a final over skipped windows only; `set_state` skips the 80k gate for a provisional set. `llm_batch.list_batches` (Anthropic 1,000 / OpenAI 100 a page, ≤ 50 pages, stops 1 h past the oldest pending job while newest first), `batch_check_and_collect(known=)`; `SubmittedBatches.queue_full` / `CollectedResults.queue_full` (Anthropic 429 not the spend cap; OpenAI `token_limit_exceeded`): no batch attempt, window-set outcome `queue_full`, while another of our batches is pending in that queue (`profile_batch._queue_holds_others`); alone, a failed attempt as before.
- **Spend cap carry (#594, Peter 2026-10-22, branch `feat/594-spend-cap-carry`)** - `backend/utils/spend.py`: `User.spend_carry_microdollars` + `spend_carry_month` ("YYYY-MM" it counts toward; NULL = never rolled, starts at 0). `rolled_spend_carry` advances it month by month, `carry = max(0, carry + spend(month) - limit)` with the current effective limit (no history of limits; 0 = carry 0); a month in `spend_carry_free_month` (no limit at some time: marked by a roll that finds no limit, and `set_user_spend_limit` rolls with the old limit before a new one applies) adds nothing; `roll_spend_carry` stores it and sets `spend_blocked_month` when the carry alone reaches the limit (no email). Rolled by the hourly beat task `spend_monitor.roll_carries` (crontab :07, users with a stale month only), by `enforce_user_spend_cap` and `reconcile_user_spend_block` (which now compare carry + month spend with the limit), and by the profile seeder before its cap check; `profile_windows._short_of_headroom` uses limit − carry − spend. `spend_cap_lift_date` / `spend_cap_payload`: the 402 and `user.spend_cap_lifts_on` carry the ISO date the cap lifts; the text names it only when later than the start of next month. Admin: `spend_carried_usd` in `GET /admin/users`, `POST /admin/users/<id>/clear_spend_carry`. Web: `utils/spendCap` keeps the server's date for the banner and toasts. iPhone: shows the server's banner text; its toasts compute next month until its next build.
- **Both providers for background tasks: the question (#599, Peter 2026-11-05, on branch `feat/independent-profile-versions`)** - `User.profile_both_providers` is now set only by the user's own answer, stamped in `profile_both_providers_answered_at` (NULL = never answered, not a no). `PUT /api/dashboard/user` takes `profile_both_providers` (strictly true or false; no admin path). The rules for asking live in `backend/utils/profile_providers.both_providers_ask` and reach the client as `user.ask_both_providers` (`model`, `visit`, `import`): never after a yes or for an account set to AI usage None; `model` and `visit` (after at least one entry, written or imported) while never answered; `import` also after a no at least 30 days old (`REASK_AFTER_NO`), on an import result that added entries AI may read (`ask_both_providers` in every import result). Web: `BothProvidersDialog` (two equal buttons, nothing preselected, a close records nothing) opens on the first suitable page of a visit (`useBothProvidersQuestion`: never on Voice, Text, Welcome, Account or Import, during a recording (`utils/recordingActivity`, fed by `useStreamingMediaRecorder`) or over another dialog; a close without an answer holds it for the tab), after the user's own preferred-model pick on the Account page, and inside the import result; the Account page's Background tasks setting shows and changes the answer. A pass reads the consent from the database (`agreed_to_both`), and a pass that may cross providers checks it again before each slice goes out (`_Submission.recheck`): a no stops the rest of the pass, which a new task plans for the user's own provider. iPhone app: not yet.
- **Artifact writes replayed in later turns; versions on every read and write (#604, Peter 2026-11-06, branch `feat/artifact-write-replay-versions`)** - The history replays each of the user's own replies' `update_artifact` calls on the tool channel (`_replayed_write_blocks`): a `tool_use` block after the reply's text (the stored input without the redacted content keys, id `loore_<node>_<index>`) and a `tool_result` in the user message after it with `_artifact_write_report` (the same text the turn's next round got: "'memory' updated to v13 (from v12). Changes:" plus the unified diff, the full text for a creation, past `ARTIFACT_ECHO_DIFF_THRESHOLD_CHARS` or when the previous version may not reach a model; no text for a version set to AI usage None or gone), built from the stored `artifact_id` / `previous_artifact_id`, licence-noted (#326), DEKs prefetched in one batch. Every version the replay names comes with its text: a diff only against a version the prompt already shows (the inline pins, `_pinned_inline_versions`, or one an earlier answer showed, tracked in chain order), else the full new text; a refusal replays the current text it was shown unless that version is already shown; a write recorded before `previous_artifact_id` existed names no number (review of #606). `llm_providers.responses_input` turns the blocks into `function_call` / `function_call_output` items for OpenAI; Anthropic takes them as they are. Version numbers and the stored-id fetch live in `backend/utils/artifact_versions.py` (`stored_version` is the one place #605's deleted filter goes). Inline copies start with "[memory · v12, as of the start of this conversation]" (render cache key v4); `read_artifact` results and write results name the version. `update_artifact` takes `base_version` (required unless creating): missing, stale or ahead, or edits that don't match, are refused (`_base_version_error`), the entry keeps `current_artifact_id`/`current_version` (ids only) and the next round gets the current text (`_artifact_current_text`, ai_usage-checked, pinned on the interim node). Success entries also keep `version` / `previous_version`. `GET /api/artifacts/versions/<id>/changes?from=<id>` (owner-only, no admin path) returns the line diff for "Show changes" in the web Actions-taken row (`DiffRows`, shared with the version history drawer). iPhone app: not yet.
- **Provider content-policy blocks and safety identifiers (2026-11-04)** - Every OpenAI Responses and Chat Completions request, live (`LLMProvider._call_openai`) and batch (`llm_batch.batch_submit`, `openai_batch_submit_one`), carries `safety_identifier`: `loore-` + 32 hex of an HMAC-SHA256 of the user id keyed with SECRET_KEY (`backend/utils/safety_identifier.py`; `loore-system` for a call with no user). The OpenAI batch collectors read the batch's error file (and non-200 output lines), log each failed item's status, code and type, and name the items blocked by the provider's content-policy filter (`OPENAI_POLICY_BLOCK_CODES`: bio_policy, invalid_prompt, image_content_policy_violation): `CollectedResults.blocked`, `BatchItemPolicyBlocked` for a one-item batch, `llm_providers.policy_block_code` for a live call. A block is final: `refusal_backoff.record_policy_block` writes a $0 cost row (`request_ref="policy_block:<code>"`) and a tagged Sentry event; from then on the job is stopped for the user (profile batch seeder and sync trigger, recent context, digest sweep) through imports, saved versions and other calls of the job that go through, until `refusal_backoff.lift_policy_block` writes a `policy_block_lifted` row: the admin's confirmed Build profile for a profile (no UI for the others). A blocked profile step is not a batch attempt, intentions give up without the resubmit, poll drafts and Read batches fail as before, and the continuation loop and digest rebuild do not retry it. Window sets (#545): a blocked window, group or final gets the outcome `blocked` (not a provider failure, so no 24 h stop expiry). A blocked window is skipped (`profile_windows.skip_blocked_window`: a `window_skip` row, a $0 `profile_window` record that stops nothing) and the profile is finished without it; its range is remembered per user for good (`ProfileBlockedRange`, new table) and every later window plan cuts around it (`cut_around_blocked`), so no rebuild resends it (only `lift_blocked_ranges` from a shell); a build whose remainder meets a range takes the window path, and neither the chained builder nor the sync loop builds over one; a blocked group or final stops the account (`next_work` returns "blocked") until the lift. `scripts/backfill_policy_blocks.py` records blocks collected as "missing" before this code.
- **Tweet-seed opt-in** (`User.prefill_consent` yes/no/NULL + `prefill_consent_at`, `PUT /api/dashboard/user`): X-login signups are asked "Start from what you've already written?" on `/alpha-thank-you` (dead time, signup already done — a hesitation costs nothing) and once more on `/welcome` if unanswered. Two equal-weight buttons, nothing preselected, always escapable: NULL = "we don't know" is a real state distinct from "no". Answer surfaces in the admin user list next to the Pre-fill button. Not yet: auto-kickoff of the pre-fill on approval; Settings entry for changing the answer.
- **Crawlable public pages (SEO/no-JS pass)** - A cookieless, JS-less request to any published article gets the complete text in the initial HTML: Flask blueprint (`backend/routes/public_pages.py`) renders public routes into the built SPA shell (tag-surgery on the minified `index.html`; React re-renders over the injected content on mount, so JS users get the unchanged app). Semantic `<article>` markup, per-route title/description/canonical/OG/Twitter tags, JSON-LD Article schema, `article:*` times; `sitemap.xml` from published-public nodes only; per-author Atom feeds (`/@author/feed.xml`) and markdown representations (`/@author/slug.md`); explicit robots.txt (verified UA tokens) welcoming search + AI crawlers, disallowing app-internal routes. Status semantics: 200 published / 404 private-or-missing (identical) / 410 revoked-or-deleted. nginx proxies the public paths to gunicorn with `error_page` fallback to the static shell when the backend is down. Redis page cache (5-min TTL, shared across workers) bounds per-request KMS decrypts under crawler load, invalidated on publish/revoke/edit/delete/toggle. Privacy: SSR reuses the commons `_public_alive` predicate, which now ALSO requires the author's `public_sharing_enabled` — flipping the toggle off takes the author's posts and replies down everywhere (JSON API included) immediately.
- **Settings cascade to replies** - Changing a node's privacy or AI usage when it has replies asks (like the delete dialog) whether to apply it to this node only or to this node and all my replies: `PUT /api/nodes/<id>` with `apply_to_descendants` walks the subtree level-batched and applies only the setting that actually changed to every alive reply the user may edit (own nodes + LLM nodes they are the human owner of); other users' replies are walked through untouched. Shipped alongside: a per-thread edit of the agentic system prompt no longer switches the thread out of agentic mode — `Node.prompt_key` carries the session kind independently of the prompt-version link (#283), and the Log/thread mode badges, search exclusion and node serializer follow it.
- **AI replies private at creation; no automatic reply in shared threads; public quotes only public (2026-11-12)** - `create_llm_placeholder` (and a tool round's continuation) creates every AI reply private; a change to public or circles while it is generated is refused (`reply_in_progress`). `llm_nodes.shared_with_others` (privacy other than private) is the test of a shared thread: `auto_generate_refusal` keeps Text mode, recorded entries, uploads and the clients' automatic `POST /nodes/<id>/llm` (`auto_generate: true`, 409 `auto_generate_off`) from replying there. `utils/publish_quotes.py` refuses (409 `quotes_not_public`) making a node public, a public node's new text and a new public entry when a `{quote:ID}` of an entry the author can open is not public, to the resolver's depth
- **Write tool calls wait for approval when the history holds text by others (2026-11-12)** - `utils/foreign_text.history_by_others` reads a reply's chain (other people's nodes and AI replies, quotes of their nodes in the requester's entries, saved references, Glean replies' picks; not placeholders or tool results); then `_execute_tool_calls` keeps `update_artifact` and the `apply_*` calls as `tool_pending` Draft rows of the reply node (`utils/tool_approvals.py`) and tells the model they wait. `GET /api/tool-approvals?node=`, `POST /api/tool-approvals/<id>/approve|reject` (one conditional claim per decision); `GET /api/nodes/<id>` carries `pending_approvals`
- **Long-thread limit (#648, Peter 2026-11-12, branch `feat/648-long-thread-limit`)** - `utils/thread_size.py` estimates the next reply's prompt with no content read and no provider call: the requesting user's newest conversation cost row on the path (the first call of the newest non-Read AI turn: `APICostLog.input_tokens`, `request_ref` "node:<id>") plus the `token_count` of what was written since, at that call's tokens per character (new column `APICostLog.prompt_chars`; fallbacks: the user's latest measured call on the same tokenizer, the profile ratio, `TOKENS_PER_UNIT_PRIOR`, now in `utils/tokens`; a base from another tokenizer counts as its characters at this one's ratio). Only an estimate on the thread's own ratio is certain: `create_llm_placeholder` raises `ThreadTooLong` at or over `THREAD_PROMPT_TOKEN_LIMIT` (250k; `THREAD_PROMPT_NOTICE_FRACTION` 0.9) only then: 409 `code: thread_too_long`, or `llm_error_code` where the entry is kept; Voice availability, streaming init and from-node refuse before recording; the finalize keeps the recording with `warning_code`. The completion task measures the assembled prompt in characters before its first call (the base's part as it is now, so edits count both ways; a Glean turn whole, tweets at the tweets prior), counts an uncertain one with `LLMProvider.count_tokens` (free) before it refuses or fits an uncapped `{user_export}`, and refuses with a `_refused` marker (`llm-status` `error_code`) and a $0 `thread_limit` cost row under the asked-under node (a base only with characters; dropped once the user edits or deletes an entry); tool rounds are not measured. The text says the thread is too long, or (`size_reason`) that an entry or what it quotes is too large. `GET /api/nodes/<id>` and a finished reply's llm-status carry `thread_length` (null / notice / limit) and `thread_length_message` for the owner. Bytes sent under the limit are unchanged
- **Background jobs pause for accounts nobody uses (#647, Peter 2026-11-12, branch `fix/647-pause-unused-accounts`)** - `utils/activity.in_use_user_ids` tells which accounts were used within `UNUSED_ACCOUNT_PAUSE_DAYS` (30, a setting; 0 = off): `User.last_seen_at` (the before_request touch of the signed-in app, the X sign-in, an account restore; jobs never write it and the apps run no idle timer that calls the server) or the clipper token's `ApiToken.last_used_at`, two queries over the ids. The hourly X bookmark gate (`external_sync._sync_due`) skips the local-3am sync of an unused account and stamps `ExternalAccount.sync_paused_at`; once the owner is back the next hourly run sends one catch-up sync at any hour and clears it (no catch-up when a "Sync now" finished after the pause began). The digest sweep leaves out unused accounts; their digest stays stale and is rebuilt once in the owner's first night back. Profile updates, recent context, embeddings, Glean and the cache pre-warm are not paused: they run on new writing, a recording or the owner's click.
- **Voice mode and waiting writes (2026-11-12)** - The spoken announcement is its own cached clip (`utils/announcement_audio`: one per variant, one change / several, keyed by TTS model, voice and text, under `<AUDIO_STORAGE_PATH>/shared/announcements/`). `GET /api/voice/approval-announcement?node=` says whether a Voice reply's calls wait now (`tool_approvals.announcement_count`) and gives the clip, which the players append after the reply's last chunk and drop once the calls are decided; the reply's own audio and text are unchanged. `waiting_in_thread` (thread walk by recursive CTE) gates Voice: `POST /api/drafts/streaming/init` (label Voice) and `POST /api/voice/` answer 409 `approval_pending`, and finalize saves a recording without a reply. Web `VoicePage` and iPhone `VoiceTurnController` show the dialog after the queue has played and disable recording; the iPhone's Live Activity drops Record. With the long-thread limit (#648) a Voice turn has two refusals; where both apply the waiting change answers first (streaming init, `POST /api/voice/`, from-node and finalize ask it before the length), and the length follows once it is decided. On the iPhone both go through `VoiceTurnController.newTurnRefusal`; a reply that both waits and reaches the limit plays out (reply, clip, dialog) with the limit in place of Continue, on the web and the iPhone.
- **Every refusal of a Voice turn said aloud (#651, branch `feat/651-speak-voice-refusals`)** - `utils/voice_refusals.py` holds one sentence per refusal code (`monthly_spend_limit_reached`, `ai_usage_none`, `approval_pending`, `thread_too_long`); `announcement_audio.ensure_refusal_clip` caches each as its own clip per TTS model and voice (`refusal-<code>-<key>.mp3`, beside the announcements, whose keys and names are unchanged), served by `GET /api/voice/refusal/<code>.mp3` (no-cache with an ETag: the address names no voice). The clip routes and the approval announcement's need Voice mode on the account (403 otherwise), so no clip is made or billed for an account that may not use Voice. A recording saved without a reply carries `warning_code` for each refusal (`voice_refusals.skipped_reply_code`, SSE `all_complete` and the streaming status). Web `utils/spokenRefusal.js` and iPhone `SpokenRefusal`/`RefusalVoice` play the clip on its own (never in the reply queue; a refusal during the turn's own audio waits for it to end, a new recording stops it) and fall back to the device's speech (`speechSynthesis`, `AVSpeechSynthesizer`) when the clip can't be had within 4 s; the iPhone keeps the app running for the fetch with a background task. Refusals are not failures: no error sound, except the one #648 put before the length's sentence on the web.
- **The recent raw window holds its ~10k tokens of entries (#525, 2026-10-21)** - `{user_recent_raw}` (the legacy budgeted path of `build_user_export_content`) no longer counts a deleted node at its former size until the purge. The SQL pre-selection counts deleted rows as 0 and passes at most `MAX_WINDOW_DELETED_ROWS` (2000) of them. A deleted row that holds the place of an alive entry below it (`_connecting_tombstones`) costs `TOMBSTONE_UNITS` (6) in the quote resolver and shows as a placeholder line; a run of such rows shows as one line ("[N nodes deleted by author]"). A deleted row with nothing alive below it costs 0 and is not shown (`format_node_tree(prune_tombstones=True)`). A deleted row's text is never decrypted (nor its DEK unwrapped at render time), and a quote of it is never filled in. Each row of the window whose parent is outside it starts a block (`_window_places`, then `_window_blocks` over the rows the resolver kept): the thread's root, or the newest part of a thread that started before the window, under the "> [Continuation of thread started …]" line the incremental path already used, so a session longer than the window no longer empties the next session's window. A block whose rows are all deleted is left out (§5a); rows under someone else's root or below a node marked None stay out, as when the thread renders whole. The "[Covers …]" dates come from the rows shown. The incremental path's selection is unchanged (its resolver skips deleted rows' text too).
- **Entry headers no longer grow with thread depth (#592, 2026-11-10)** - Every export rendering (profile windows, intentions, recent context, `{user_recent_raw}`, `{user_export}`, the user's data download) labels each entry by its place in its thread: a linear stretch is numbered in order (`## [4.199]`, was 199 × ".1") and a label grows only at a branch point, where each branch gets a letter (replies to 2.2: `2.2a.1`, `2.2b.1`; past 26 siblings `aa`, `ab`, …; `_reply_labels`, `_branch_letters` in `routes/export_data.py`). The heading is level 2 along a thread's first stretch and one level deeper per branch point (at most 6). The separator before a later branch names the entry it replies to ("BRANCH: another reply to 2.2"), also under deleted and inaccessible placeholders, and a reply the walk leaves out takes no letter and no separator. Siblings with one timestamp are ordered by id. A quote resolved by reference reads "[See entry 2.2b.1]" when the export shows the quoted entry as an entry (before or after the quoting one: `_QuoteReferences` leaves a marker with a per-export tag and fills it in once the document is complete), else "[Quoted entry by User (alice) - <time>]", or the inline-quote texts for a deleted or inaccessible node. A 200-message ChatGPT conversation's headers went from 52,090 to 12,188 characters.

### User Plan Tiers

Users have a `plan` column (string, max 16 chars) on the User model. The plan controls feature gating via `User.has_voice_mode` and the `@voice_mode_required` decorator. New plans/features can be gated by adding similar properties to the User model.

| Plan | Default for | Voice Mode | Notes |
|------|-------------|------------|-------|
| `free` | — | No | No paying users have this tier currently. Reserved for future free-tier signups. |
| `alpha` | All new registrations | Yes | Current default. All existing users were migrated to this tier. |
| `pro` | — | Yes | Future paid tier. No users on this plan yet. |

- **Admins** (`is_admin=True`) bypass all plan checks.
- Plan tiers that grant Voice Mode are defined in `User.VOICE_MODE_PLANS` (`models.py`).
- The frontend receives `voice_mode_enabled` (boolean) from the `/dashboard` endpoint — it does not check plans directly.
- **Subscriptions (#438, 2026-11-12, branch `feat/438-stripe-subscriptions`, dark):** Stripe Checkout with Managed Payments (Stripe, through Link, is the merchant of record). Behind `PAYMENTS_ENABLED` (default off: no billing route, UI, gate or message differs). The paid feature set is `User.on_paid_plan` = a plan set by hand (alpha, pro) or a trialing / active subscription; it replaces the inline `plan in VOICE_MODE_PLANS` checks (Voice mode, profile and recent-context updates, `profile_eligible_query`). A Stripe event never writes `plan`: the subscription lives in its own columns (`stripe_customer_id`, `subscription_level/status/period_end/trial_end/cancel_at_period_end/trial_used/ever_paid/end_reason`), written only from a subscription re-read from Stripe (`utils/stripe_billing.apply_subscription`). Switch on: new accounts start on `free`, and each account is in one of three access states (`User.subscription_access_state`). `none` (never had a subscription, or its first payment failed) is not let in: the before_request gate `block_accounts_without_subscription` answers 402 `no_subscription` on every /api and /media route outside a short allowlist (who-am-I, billing, terms, account deletion); the web shows only the subscribe page, the iPhone one calm screen. `active` (trialing or active) gets everything. `lapsed` (a subscription its owner cancelled, recorded for good in `subscription_cancelled_by_owner`, or past due / ended after a paid invoice; "paid" is `subscription_ever_paid`, set only by a paid invoice above zero) keeps reading, writing, export and Settings, makes no billed call (`refuse_billed_call`: transcription and dictation, search by meaning, todo merges, poll drafts, X sync; API-token requests pass the same gate, `refuse_not_let_in`), gets no AI replies (`SubscriptionRequired` in `create_llm_placeholder` → 402 `subscription_required`; routes that save the writing answer 2xx with `subscription_required`) and no background model jobs, and its owner can still play their own recordings. Levels Standard / Supporter / discounted (code only) at 30 / 99 / 12 USD a month plus tax (tax-exclusive prices), one product each so the customer portal can switch Standard ↔ Supporter (an upgrade's prorated difference is invoiced at once, `always_invoice`; a downgrade is a subscription schedule that takes effect at the period end, nothing refunded; the discounted price is never offered there), 14-day trial once per account, monthly model-cost ceiling 50 / 100 / 50 USD as the spend cap of a subscribed account (an admin's per-user limit still wins). `flask stripe-sync-catalog` creates the products, prices (by lookup key) and portal configuration, and archives the earlier single product, in whatever Stripe account the key belongs to. Webhook `POST /api/webhooks/stripe` (signature, each event applied once in `stripe_event`). One open Checkout Session per account (`checkout_session`; opening one expires the last) and one live subscription (a later duplicate is cancelled and refunded; a second trial is cancelled). Own discount codes (`discount_code`, admin panel), rate-limited per account; a use is counted when its subscription is created (`discount_redemption`, one per subscription). A trial has its own ceiling over the whole trial (`SUBSCRIPTION_TRIAL_SPEND_LIMIT_USD`, 30 USD); from the first payment the level's monthly ceiling applies to the whole calendar month, trial usage in it included; a failed payment ends access at once (lapsed after a paid invoice, not let in before one) and a retry that succeeds restores it; a dispute or a full refund of the latest payment ends access and cancels; production takes a live key and live events only. Web Settings: Subscribe / manage-or-cancel (Stripe customer portal) / code field; iPhone Settings shows the state only. A failed payment is said on load (the web adds the link to update the card).
- **The alpha free month (#438, 2026-11-12/13, branch `feat/438-alpha-free-month`, dark):** `flask alpha-free-month --start YYYY-MM-DD [--apply] [--change-date] [--undo] [--user NAME]` moves approved, non-admin accounts on the hand-set `alpha` plan to `free`, sets `User.alpha_free_granted_at`, and gives every tester the same `alpha_free_start` (00:00 UTC of the date) and `alpha_free_until` (+ `ALPHA_FREE_MONTH_DAYS`, 30). Before the start, `alpha_free_month_upcoming` makes `plan_set_by_hand` true (the alpha access, Checkout refused). `alpha_free_month_active` (start ≤ now < end) joins `on_paid_plan`; `alpha_free_month_clause` (end > now) joins `paid_features_clause` and `profile_eligible_query`; an ended month leaves the account `lapsed`. From the month's start to the first paid invoice, the tester's spend is counted once against Standard's ceiling (`alpha_unpaid_since` in `utils/spend.py`, the trial-ceiling mechanism counted from `alpha_free_start`), across calendar months and the stretch after the month's end before the first charge; the monthly ceiling is Standard's too (`ALPHA_UNPAID_CEILING_LEVEL`). The first paid invoice reconciles the monthly block when the ceiling changes. An `alpha` account approved or restored after the grant gets the same dates (`convert_if_alpha`); `--undo` leaves accounts whose plan changed or that have a live subscription. `checkout_levels` offers testers the discounted level without a code; their Checkout carries `subscription_data.trial_end` = the month's end (at least 49 h ahead; Stripe's minimum is 48 h) and no trial after it. `subscription_state` adds `alpha_tester`, `free_month_upcoming`, `free_month_active`, `free_month_start`, `free_month_until`.
- **Referrals (#439, 2026-11-12, branch `feat/439-referrals`, dark):** `utils/referrals.py`. `GET /api/referrals/link` makes the account's random code (`User.referral_code`, `secrets.token_urlsafe(12)`) and answers `{FRONTEND_URL}/invite/<code>`; no route turns a code into anything. The web keeps the code in localStorage (30 days) and sends it with the sign-in: `/auth/login?invite=` (kept in the Flask session through the X round trip) and `/auth/magic-link/send {invite}` (inside the signed token and in the asking browser's session; the link records it only when opened in that browser). `record_invite` writes a `referral` row (unique friend) for a new account or an account nobody signed in to, never the inviter's own, never a deleted inviter's. The webhook's `invoice.paid` / `invoice.payment_succeeded` of the friend's current subscription with `amount_paid > 0` calls `grant_on_first_payment`: the referral row is locked, `rewarded_at` set once, and each side gets a customer balance transaction of `referral_credit()`: `REFERRAL_CREDIT_FRACTION` (0.25) of `SUBSCRIPTION_PRICE_CENTS_STANDARD` in `SUBSCRIPTION_CURRENCY`, the same for both whatever their level, tax or subscription (Peter, 2026-11-13; no invoice preview; Stripe takes it off the invoice total, after tax); the inviter's at most what the friend paid on that invoice when paid in the same currency, with metadata `loore_referral=<id>:<side>` (looked up before creating, so a retry after a lost answer makes none) and an idempotency key. A refusal Stripe would repeat is recorded as `failed` and logged; other errors answer 503 so Stripe resends. Notices (`referral_reward`) name nobody. `/api/admin/referrals` (with `credit`, the amount each side gets now) and the Invitations table. `subscription_ever_paid` now also counts an invoice paid by credit (total above zero).
- **Payments follow-ups (#438/#439, 2026-11-13, branch `fix/438-referral-amount-and-followups`, dark):** **Invites replace the reusable link** (`User.referral_code` and `GET /api/referrals/link` are gone): `Invite` (inviter, random code, kind "x"/"email", target = the normalised handle or address only while open, month "YYYY-MM", expires_at, state open/used/withdrawn/expired) and `Referral.invite_id`. `GET/POST/DELETE /api/referrals/invite` (state, make `{"to"}`, withdraw); `utils/referrals.create_invite` (let-in accounts only; not oneself; one open at a time; none once an invite made this month was used; addresses with an account accepted alike, never revealed), `redeem_invite` at sign-in (the code from the X round trip's session or the email token; the sign-in's handle/address must equal the target; new or never-signed-in accounts only; first inviter kept), `invite_mismatch` → `/invite-mismatch?for=<kind>` with the proven identity held in the session (`invite_choice`, as long as a sign-in link lasts) until `POST /auth/invite-choice/continue` (the account is made then, no inviter) or `/cancel` (nothing made; the X token dropped); `expire_due` (hourly `tasks.invites.expire_invites`, background queue). The purge withdraws open invites and clears every target. The admin list shows each invite's state, never its target. The fixed referral credit above. The trial's ceiling and the free month's lift only with the first payment (`stripe_billing.handle_event`), so `spend.spend_cap_payload` sends for them `lifts_on: null`, `lifts_with_payment: true` and "It lifts with your first payment." (`TRIAL_CAP_MESSAGE`, `FREE_MONTH_CAP_MESSAGE`; `cap_lifts_with_payment`); the user object and `spend_capped_fields` carry `spend_cap_lifts_with_payment` only then. Web `utils/spendCap` and iPhone `SpendCap.liftsWithPayment` (set from the user, each spend-cap 402 and a saved entry's answer) word their own banner and toasts for it; a kept Voice recording's and an upload's skip text have an `_UNTIL_PAYMENT` variant; the spoken usage-limit sentence is now true of every limit. Tests with payments on for the train's hand merges: `test_train_merge_voice_gate.py`, `test_train_merge_paid_invoice.py`, and additions to `test_bookmark_nightly_sync.py` and `test_external_digest_nightly.py`.

### Authentication & User Management
- **Magic link email authentication** - Passwordless login via email magic links, replacing Twitter-only OAuth (#54)
- **Dedicated login page** - Standalone login page with redirect flow (#53)
- **User plan tiers** - Standardized plan system (free/alpha/pro) with admin dashboard management, migration scripts, and feature gating via `User.has_voice_mode`
- **Terms of service acceptance** - Terms acceptance tracking with admin ability to reset on account deactivation
- **Delete all my content / admin data purge** (#268, 2026-10-06) - `backend/utils/user_purge.py` deletes all of one user's data in one set-based, chunked, resumable operation (ids and metadata only; no content is decrypted, only the stored X tokens, to revoke them): stops in-flight work first, deletes the user's nodes deepest first (tombstones where another user replied below), every dependent table, profiles, artifacts, drafts, shares, saved references, the X connection (its tokens revoked at X first; a failed call is logged and never stops the purge) and files; keeps cost rows on the `loore-erased` system account without anything that names the person; refuses AI and system accounts. Users ask from the Account page: everything is hidden at once (nodes soft-deleted, per-user rows left out of every ORM query by `backend/utils/hidden_rows.py`, what was hidden recorded in `user_data_purge_hidden`), "Restore my writing" brings exactly that back within 30 days (`/api/account/data/restore`), and the purge then deletes what the request hid (`Scope`), keeping what the user wrote afterwards; an admin's purge after a dry run (`/api/admin/users/<id>/purge_data`) works the same way, and the admin can undo it until it starts (`/api/admin/users/<id>/undo_deletion`; Peter, 2026-10-13). Asking again while a deletion waits (Peter, 2026-10-15) adds what the user wrote since to the same job (`extend_waiting_purge`, a conditional update so the beat's claim or a restore has one winner), moves its date to 30 days after the latest request and makes it the user's own; `deletion_status.new_writing` tells the Account page whether to offer it. The `user_data_purge` table and the `process-user-data-purges` beat entry drive the grace period and resume a purge after a crash. **#590 (2026-11-10): renamed "Delete all my content", and a fresh start:** the account is not on hold while the request waits. Jobs that read after it see only the new content and run as for a new account (`hidden_rows.read_before_deletion` replaces the account-wide `writing_on_hold`; an account deletion still holds everything); a result read before it is dropped with its cost row written; provider batches carrying only the user's old work are cancelled at the request (their rows stay pending, so late results are collected and costed); the X bookmark connection ends at once (revoked at X); a restore makes the restored profile current again and drops the versions Loore built since, keeping a version the user saved by hand, which merges at the next update; the purge after 30 days deletes only what the request hid and keeps what came after, a new X connection included.
- **Account deletion** (#269, 2026-10-09) - `backend/utils/account_deletion.py` is the identity layer on the purge: `User.deleted_at` hides the account at once (the session loader, API tokens, public pages, sitemap, Commons, member access checks and background jobs all treat it as gone; its nodes show as tombstones to others), and a `user_data_purge` job with `delete_account` runs after 30 days (an admin's too, restorable the same way and undoable by the admin; Peter, 2026-10-13): the purge, then tombstones moved to `loore-erased`, other rows' references cleared, `username_history` / `api_token` / `changelog_read_state` deleted, every foreign key into `user` counted to 0, the user row deleted. Handles go to `released_username` for 365 days. Email accounts confirm from a mailed link (`/api/account/delete`, `/delete/confirm`); a sign-in during the grace period leads to `/account-restore`; admins use `/api/admin/users/<id>/delete_account` with a dry run and blast radius

### UI/UX
- **Warm literary design system** - Complete UI redesign across all app pages with cohesive dark theme (#56)
- **Responsive mobile design** - Mobile-optimized modals, button wrapping, and spacing
- **Typography system** - Cormorant Garamond (serif headings), Outfit (sans body text), refined font weights and contrast
- **Loore branding** - Full rebrand with custom icons, waveform logo, and literary-themed landing page
- **Welcome & onboarding flow** - Public about pages, alpha thank-you page, welcome page with admin welcome flow (#59)
- **Renamed navigation** - Dashboard → Profile, Feed → Log for clearer semantics; the code followed in #298 (2026-09-16): `backend/routes/log.py`, `GET /api/log`, `Log.js`, the `/feed` route redirects
- **Orphaned-session delete dialog** (#302, 2026-09-16) - `GET /api/nodes/<id>/delete-impact` (`backend/utils/node_deletion.orphaned_system_prompt_id`, mirrors `soft_delete_node`'s selection) tells NodeDetail whether a delete would leave a text/voice session with only its system prompt alive; the follow-up dialog's answer rides on the same `DELETE /api/nodes/<id>` as `delete_orphaned_prompt`, re-checked under the root's row lock so an entry that arrived in between keeps the session (`orphaned_prompt_deleted` in the response). Log side: a prompt root with no alive direct child is titled by its first alive descendant (shallowest, then oldest); pinned replies resolve to their thread root only when the root is the user's own (`can_rename` hides rename otherwise); keyset cursor paging (`?cursor=` / `next_cursor` on `(coalesce(pinned_at, created_at), id)`) so deletes and inserts between pages neither skip nor repeat a thread; `backend/utils/thread_tree.py` holds the recursive walks shared by the Log, deletion and the public page cache — `subtree_walk(root_ids, viewer_id)` is the single definition of remaining content (alive nodes reachable through nodes the viewer can access) used by the Log fallback, `delete-impact` and the post-delete re-check; DELETE responses carry `deleted_pinned_ids` so the Log drops every card the cascade took
- **Delete an artifact** (#603, 2026-11-06; iPhone: a `.contextMenu` on each bubble of `ArtifactsNavRow`, Delete from the list's `deletable`) - `DELETE /api/artifacts/<kind>` soft-deletes every version of a custom artifact (`UserArtifact.deleted_at`; built-ins in `UserArtifact.DEFAULT_KINDS` and `external_digest` answer 403, someone else's kind 404). Every reader goes through `UserArtifact.live()` / `get_live()` / `latest_for()` / `version_count()`, so the list (`deleted_kinds` lists the user's deleted kinds for the "This artifact was deleted." link), the history, the AI's artifacts index, session pins (`Node.get_user_artifacts`), `read_artifact`, the `update_artifact` echo, the node display and exports leave it out from the moment of the delete, and a kind made again counts from v1. The #192 render cache variant carries the user's last delete (`artifact_deletion.deletion_stamp`), so a render or pre-warm from before the delete is never served. The daily `cleanup_deleted_nodes` also runs `purge_deleted_artifacts`: after `SOFT_DELETE_GRACE_DAYS` the session pins (`NodeContextArtifact`) and the rows go, and the kind's `ArtifactView` pings when no live version is left. Web: a ⋮ menu (`BubbleKebabMenu inline`) at the end of the meta line of every artifact, Profile and Todo, with Edit, and Delete for custom artifacts; the iPhone menu is part 2
- **A finalized node's audio files go with it** (2026-11-06) - `cleanup_deleted_nodes` (now `finalize_deleted_nodes`, callable without Celery) deletes a node's audio folders (`audio_storage.node_audio_dirs`: `user/<author>/node/<id>/`, `nodes/<author>/<id>/` and its `streaming/` session, for the author and the human owner, so an AI reply's folder under the model account is found) before it purges the row or wipes a tombstone; a file it cannot delete is logged by node id (`audio_errors`) and never holds the rows back. Each run then runs the audio sweep (`backend/utils/audio_sweep.py`; by hand: `backend/scripts/purge_orphaned_node_audio.py`, dry run by default), whose errors never stop the cleanup: it deletes node folders whose row is gone or deleted more than `SOFT_DELETE_GRACE_DAYS` ago, a gone reference's or profile's speech folder (`user/<uid>/item|profile/<id>/`), streaming session folders no unfinalized node or draft names once unchanged for the grace period, and superseded speech (`tts.mp3`, `tts_chunk_<i>.mp3`) of live rows that no row points at (#66: an edit's "regenerate" and a regeneration mark the old speech's folders changed with `audio_storage.mark_speech_superseded`, so it goes a day later at the earliest); never a file any row points at (profile versions and merged reference copies share files), a live node's recording, a recording in a folder whose node row is gone until unchanged for the grace period (it may be the only copy of a node whose commit failed), an in-grace or hidden row's files, nor a folder changed in the last 24 h. Deleting a reference (`DELETE /api/external/items/<id>`) deletes its speech at once (`audio_sweep.delete_row_speech`), and the staging seed's `--force` deletes the nodes' audio with them
- **Select and copy text in the iPhone app** (2026-11-10) - Reading text is a non-editable, selectable `UITextView` (`ios/Loore/Markdown/SelectableText.swift`): `MarkdownView` renders each run of paragraphs, headings and lists as one (`MarkdownFlow`), so a selection can cross paragraphs; plain reading text uses `ReadingText`; cards that open on a tap pass their action to the text (`.readingTextTap`). Details in `ios/PROGRESS.md`

### CI/CD & DevOps
- **GitHub Actions CI pipeline** - Automated testing on PRs with backend tests, frontend tests, linting, and security scans (path-filtered to skip unchanged areas)
- **Automated deployment pipeline** - Push to main triggers tests + frontend build + SSH deployment to production VM. Concurrency group prevents simultaneous deploys.
- **Staging environment** - Docker Compose on same VM as production, isolated via project name `wop-staging`. Frontend pre-built in CI (VM runs out of memory). Ephemeral DB reset on each deploy. (#68, #69)
- **Local Docker Compose development** - One-command setup (`make dev`) for full stack: PostgreSQL, Redis, Celery, Flask backend (hot reload), React frontend (HMR). Volume-mounted source code for live editing.
- **Security scanning** - Bandit for Python security issues and Safety for vulnerable dependencies run on every commit
- **Code quality checks** - Flake8 linting for backend and frontend lint checks catch issues early
- **One run at a time for long jobs** (2026-11-09) - After a profile build for a multi-million-unit import ran the server out of memory (its message was delivered again and a second run started): the broker's visibility timeout is three hard time limits (`BROKER_VISIBILITY_TIMEOUT` in `celery_app.py`), so a running task's message is not delivered again; `backend/utils/run_locks.py` gives the profile window advance a per-user Redis lock with a heartbeat (a second copy exits; `_is_task_stale` keeps the guard while the run beats; a dead run's lock expires) and the embedding sweep a single-run lock, whose new rows are inserted with `ON CONFLICT DO NOTHING`; `backend/utils/soft_limit.py` lets Celery's soft time limit through token counts and batch submissions (the SDKs no longer retry it or turn it into a connection error); `PROFILE_BATCH_MAX_BYTES` 15 MB and `PROFILE_WINDOW_RENDER_SECONDS` 600 by default; KMS calls retry a dropped connection on a new client
- **One metadata read per profile pass** (2026-11-09, #612) - A profile window pass reads the metadata of the writing it renders once (`exports.window_scope`, `WindowScope` in `routes/export_data.py`) and cuts every window's unit count and renders from it, where each window read all the writing after its start again (windows × entries); the same windows, rows and prompts as before on SQLite (`tests/test_window_scope.py`), and on Postgres a fixed order for rows that share a timestamp (was the query plan's; no row lost or doubled); read again when the user's rows change during the pass, and every row and reply a window is decided from is checked at render time

---

## Critical Infrastructure Gaps (Priority: BLOCKING)

**These are architectural changes that must be addressed before or alongside feature development.**

### Privacy & Encryption Infrastructure

✅ **Node visibility levels and access control** - COMPLETED: Added privacy_level column (private/circles/public) and ai_usage column (none/chat/train) with API enforcement to control who can read each node

✅ **No exception text in responses, logs or Sentry (2026-10-20)** - COMPLETED: A database error's text can quote the values being saved. Route error responses carry a message written for the user, never `str(e)` (no `details`); log lines carry the exception type and non-content ids. The engine runs with `hide_parameters=True` (`SQLALCHEMY_ENGINE_OPTIONS`). `backend/utils/error_privacy.py`: tracebacks in the web app's log keep types and frames without messages (Flask's unhandled-exception line, `logger.exception`), and Sentry's `before_send` / `before_breadcrumb` replace exception messages with `[message removed]` (plus SQLSTATE, constraint/table/column names, HTTP status, error code or errno), remove the quoted exception text from log messages and breadcrumbs, and strip the values a database error quotes: Postgres DETAIL / CONTEXT / LINE blocks, "Failing row contains", "Key (…)=(…)", an unconvertible value and `[parameters: …]` (Peter, 2026-10-20; follows #531)

✅ **Application-level encryption with GCP KMS** - COMPLETED: Envelope encryption (v2) using AES-256-GCM with KMS-wrapped DEKs. REST transport for gevent compatibility. In-memory DEK cache (up to 4096 entries) to minimize KMS calls. Format: `ENC:v2:<wrapped-dek>:<nonce+ciphertext>`. Audio files also encrypted at rest via `encrypt_file()` — uploads, streaming chunks, and TTS output all get `.enc` extension on disk while DB URLs remain clean (media route handles `.enc` fallback).

**Embedding isolation architecture** - Generate embeddings server-side from plaintext, store with user_id in pgvector, always filter queries by user_id + visibility

**User promise and transparency** - Clear communication: "Journals encrypted at rest. Semantic indexes power search/AI for your account only, never exposed publicly unless you choose"

✅ **Data migration for existing nodes** - COMPLETED: Migration script (`scripts/encrypt_existing_content.py`) encrypts all existing node content, versions, drafts, profiles, and transcript chunks. Supports v1→v2 re-encryption.

### Authentication & Authorization

**API token system (JWT)** - Replace session-only auth with JWT tokens for mobile apps, external integrations, and API access with refresh token rotation

✅ **Fine-grained permission system** - COMPLETED: Authorization layer with `can_user_access_node()`, `can_ai_use_node_for_chat()`, and `can_ai_use_node_for_training()` functions

**OAuth integration framework** - Reusable OAuth client for Twitter, LinkedIn, Substack, GitHub with token storage and refresh handling

### Data Infrastructure

**Cloud file storage with S3/R2** - Migrate from local file storage to cloud object storage for audio files with encryption at rest and CDN integration

✅ **File encryption at rest** - COMPLETED: All audio files (uploads, streaming chunks, TTS) encrypted at rest using `encrypt_file()` with envelope encryption (AES-256-GCM + KMS-wrapped DEK). Binary format: `[4-byte DEK length][wrapped DEK][nonce][ciphertext]`. Files get `.enc` extension; media route transparently serves encrypted files. Transcription tasks use `decrypt_file_to_temp()` to decrypt before sending to OpenAI

**Email sending infrastructure expansion** - Currently magic link auth emails work via SMTP. Expand to SendGrid/AWS SES for notifications, digests, and template management at scale

**Real-time communication with WebSockets** - WebSocket server for live notifications (matches, shares, messages) without polling, using Socket.IO or similar

### Compliance & Safety

**Audit logging system** - Track all data access (who read which node when) for compliance and security investigation with tamper-proof logs

**GDPR compliance enhancements** - Expand export/delete to include audit logs, encryption keys, and third-party data with clear consent management UI

**Content moderation pipeline** - Automated + manual moderation for public shares and marketplace using OpenAI Moderation API and admin review queue

**User reporting and blocking** - Allow users to report abusive content/users and block connections with admin dashboard for review

**Spam detection and rate limiting** - ML-based spam detection for shares and intentions with progressive penalties (warnings → temp ban → permanent ban)

### License & Legal Compliance (Priority: BETA BLOCKER)

**These items must be addressed before Loore moves beyond the small alpha circle. See `docs/LICENSE-TODO.md` for full details.**

**Legal entity disclosure** - Identify the legal entity operating Loore (personal name, OSVČ, s.r.o.) and add to the terms so users know who they're contracting with

**Jurisdiction & governing law** - Specify Czech Republic / EU jurisdiction and applicable law (required for enforceable agreement and GDPR compliance)

**GDPR rights section in terms** - Add explicit mention of data subject rights: access, rectification, erasure, portability, right to object, right to lodge complaint with ÚOOÚ

**Account deletion feature** - Build account deletion, then update terms to describe what's purged, retained, sent to AI providers, and in backups. ✅ Built: the data purge (#268) and the account layer (#269). ✅ The web terms describe it (no new terms version; Peter, 2026-10-09). 🔧 The iPhone app's terms and in-app entry are in #482

**Data export / portability** - Implement GDPR-required data export feature and document it in terms

**Age restriction** - Set explicit minimum age for the service (16 is GDPR default in CZ) and add to terms

✅ **Backup documentation** - COMPLETED (runbook): `docs/RUNBOOKS.md` → "Backups and restore" documents what's backed up, the locked 28-day retention, encryption status, and that deleted data persists in backups for up to about 29 days, inside the 30 days the terms promise after an account's final deletion.

**Cookie / tracking disclosure** - Disclose cookie usage (session cookies at minimum); may need cookie banner for EU compliance

**Content moderation policy** - Define procedures for illegal public content before public release

**Separate consent step for AI training** - Build distinct UI action for AI training consent (not just a toggle); update license to reflect UX

**Third-party service audit** - Audit deployed application for undisclosed third-party services (analytics, CDN, font providers) and add to terms

**Link to full OpenAI Content Sharing Agreement** - Provide direct link to full text or host a copy (currently only summarized)

**License versioning** - Maintain version numbers so it's clear which version a user accepted and when terms changed (v2.0 already in use)

### Mobile & Offline Support

**Native iPhone app** (🔧 in progress, PR #384, 2026-10-01; awaiting device testing) - SwiftUI app in `ios/` (XcodeGen project, iOS 17+, two dependencies: swift-markdown and ZIPFoundation) against the unchanged backend: cookie session kept in the Keychain (magic link pasted into the app, X login in a web view), the web's endpoints, SSE streams and polling, a native recorder producing the same fMP4 chunks as Safari's MediaRecorder (chunk 0 = init + first segment) with a persisted, resumable upload queue and background `URLSession`, an `AVQueuePlayer` reply queue with chapters, Now Playing and an audible thinking cue that keeps the audio session alive while the phone is locked. CI: `.github/workflows/ios.yml` (build + 334 unit tests on PRs touching `ios/**`). Design `docs/IOS-APP-DESIGN.md`; parity table and known gaps in `ios/PROGRESS.md`; device and staging checklists in `ios/README.md`. Backend follow-ups proposed in the design doc §14 (universal links, sliding 30-day sign-in, `.mp4` content type, push)

**Mobile-optimized API design** - RESTful API with pagination, partial responses, and efficient payloads for mobile bandwidth constraints

**Offline-first data sync** - Conflict resolution for offline writes using operational transforms or CRDTs when user reconnects

**Mobile push notifications** - Push notification infrastructure (FCM/APNS) for match alerts and share responses on mobile devices

---

## Development Infrastructure (Priority: URGENT)

**These must be built to enable fast, safe iteration on new features.**

### Testing Infrastructure

**Backend testing framework with pytest** - Unit and integration tests for API endpoints, models, and tasks to catch regressions before deployment

**Frontend testing framework with Jest/React Testing Library** - Component tests, hook tests, and integration tests to ensure UI reliability

**Test database setup with fixtures** - Automated test DB creation/teardown with seed data for reproducible test scenarios

**Celery task testing utilities** - Mock async tasks in tests to avoid expensive LLM API calls and enable fast test execution

**API mocking for external services** - Mock OpenAI, Anthropic, Twitter APIs in tests to eliminate flakiness and cost

**End-to-end testing with Playwright/Cypress** - Critical user flows (login, create node, LLM response, voice recording) tested in real browser

**Test coverage tracking with coverage.py and jest --coverage** - Monitor which code is tested to prioritize where tests are needed most

**Test coverage reporting in CI** - Add coverage reports to CI pipeline to track test coverage over time and enforce minimum thresholds

### Development Velocity Infrastructure

✅ **Local development Docker Compose setup** - COMPLETED: One-command setup (`make dev`) for PostgreSQL, Redis, Celery, backend, and frontend (#68, #69). Staging environment also deployed via Docker Compose on same VM.

**Database migration testing** - Automated checks that migrations run cleanly forward/backward without data loss

**API documentation with Swagger/OpenAPI** - Auto-generated API docs from Flask routes so frontend knows exactly what endpoints exist and their contracts

**Component storybook for frontend** - Isolated UI component development and testing without needing full app context

✅ **Hot reload for both backend and frontend** - COMPLETED: Docker Compose dev setup includes Flask dev server with auto-reload and React dev server with HMR. Volume-mounted source code.

**Logging infrastructure with structured logs** - JSON-formatted logs with request IDs for debugging production issues quickly

**Error monitoring with Sentry** - Automatic error capture with stack traces, user context, and breadcrumbs to debug production issues

**Performance monitoring and profiling** - Track slow endpoints, database queries, and LLM calls to optimize bottlenecks

**Feature flags system** - Toggle new features on/off without deployment to enable gradual rollouts and A/B testing

**Database query performance monitoring** - Log slow queries and add indexes proactively to prevent performance degradation as data grows

### Security & Reliability

**Rate limiting per user and per endpoint** - Prevent abuse and control costs with configurable limits on API calls, LLM usage, and file uploads

**Input validation and sanitization library** - Centralized validation for all user inputs to prevent injection attacks and bad data

**CSRF protection for state-changing endpoints** - Prevent cross-site request forgery attacks on POST/PUT/DELETE routes

**Content Security Policy (CSP) headers** - Mitigate XSS attacks by restricting what scripts/styles can load in browser

**Automated Dependabot PRs** - Set up GitHub Dependabot to automatically create PRs for dependency updates (complement to existing Safety checks)

✅ **Database backup and restore automation** - COMPLETED: nightly `pg_dump` + media sync to GCS with locked 28-day retention (`scripts/backup.sh`, systemd timer installed by `deploy.sh`, restore procedure + quarterly restore test in `docs/RUNBOOKS.md`)

**Health check endpoints** - `/health` and `/ready` endpoints for load balancer and monitoring to detect unhealthy instances

**Circuit breaker for external APIs** - Fail fast and retry with backoff when OpenAI/Anthropic APIs are down to prevent cascading failures

---

## Feature 2: Download (MemeOS Integration)

> **✅ SHIPPED as quote-as-response (#208, 2026-07-03)** — reframed away from MemeOS + sidebar recommendations; see the Already Built entry and FOUR-FEATURE-ECOSYSTEM.md Feature 2 for the shipped shape. Item statuses below: embeddings/semantic search/RAG ✅ (via #155/#197/#208; #329/#330 turned own-archive search on for everyone and bounded the scan to chunked numpy with streamed rows — 62k-node archive ≈0.2 s CPU / 37 MB peak; the remaining cost is pulling the vectors out of Postgres, which is #331 pgvector: built 2026-10-21 as an optional SQL ranking over copies of the vectors in a `vec` schema, off until the extension is installed and `flask pgvector-enable --apply` has run, see docs/RUNBOOKS.md); MemeOS client ❌ dropped; recommendation engine ✅ superseded by quote-as-response; theme extraction ✅ as the `external_digest` artifact; spaced-repetition ranking ❌ replaced by legible age/surfacing metadata + prompt instruction (no hidden 70/30 fudge).

**Vector database with pgvector extension** - Store and query embeddings for semantic search over nodes and bookmarks, enabling 10ms similarity searches at scale — 🔧 #331 built (2026-10-21), off until enabled on prod

**Automatic embedding generation pipeline** - Async task to generate embeddings on node create/edit using OpenAI's text-embedding-3-small model ($0.02/1M tokens)

**Semantic search API** - Query interface with cosine similarity ranking to find relevant content based on natural language queries

**RAG (Retrieval Augmented Generation) system** - Build context from top-K similar nodes and feed to LLM for conversational archive exploration

**MemeOS API client or shared database layer** - Integration layer to query bookmarks with filtering by tags, due dates, and spaced repetition status

**Context-aware recommendation engine** - Analyze current writing + thread + profile to surface relevant bookmarks with 70% semantic + 30% urgency ranking

**Theme extraction service** - Use lightweight LLM (GPT-4o-mini) to extract 3-5 key topics from writing context for targeted bookmark retrieval

**Spaced repetition integration** - Factor in review schedules when ranking bookmarks to surface overdue content at the right time

---

## Feature 3: Upload (Broadcasting Internal State)

**Content analysis pipeline** - LLM-based classification to detect shareable insights (mental models, frameworks, lessons) with shareability scoring (0-1 scale)

**Multi-channel publishing adapters** - OAuth integrations and API clients for Twitter, LinkedIn, Substack, GitHub Gists with rate limiting and error handling

**Content transformation engine** - Convert raw journal entries to polished shares with extraction, formatting, contextualization, and audience-appropriate framing

**Privacy analysis system** - Detect sensitive information (names, health, finances) and flag potential risks before sharing with multi-layer protection

**Share approval workflow** - UI and backend state machine for suggested/approved/published/rejected with preview, edit, and rollback capabilities

**Tiered audience management** - Define and manage private circles, professional networks, and public visibility with granular per-share controls

**Multi-entry synthesis** - Track themes across journal entries over time and suggest combining related content into long-form essays

**Draft generation for external platforms** - Create formatted drafts in Substack/LinkedIn with preview links, never auto-publish without explicit user consent

**OAuth credential management** - Secure storage and refresh token handling for third-party platform integrations with per-channel revocation

---

## Feature 4: Intention Market (Matchmaking)

**Intention data model with structured metadata** - Store needs/offerings/opportunities with topics, time commitment, format preferences, capacity, and expiration dates

**Semantic matching algorithm** - Embed intentions and calculate match scores using 40% similarity + 25% topic overlap + 15% format + 10% time + 10% reputation

**Real-time match notification system** - Detect complementary intentions (need↔offering) and notify both parties with match score and reasoning

**Connection management infrastructure** - Handle match acceptance, private messaging threads, and bidirectional relationships with activity tracking

**Reputation scoring engine** - Track completion rates, responsiveness, feedback scores, and giving/taking ratio to weight match quality

**Intention expiration and renewal system** - Auto-expire stale posts (30-90 days) with renewal prompts based on capacity and user activity

**Market discovery API with filtering** - Browse/search intentions by type, topic, recency with personalized feeds based on user's posted intentions and profile

**Feedback and outcome tracking** - Collect post-collaboration ratings and success metrics to improve future matching algorithm weights

**Group matchmaking logic** - Identify multiple users with similar intentions (e.g., 5 people wanting daily writing accountability) and suggest forming groups

---

## Cross-Feature Infrastructure

**Webhook system for inter-feature communication** - Event bus to trigger actions across features (e.g., journal entry → MemeOS recommendation → share suggestion → intention extraction)

**Unified notification framework** - Consolidate alerts from all features with digest options, priority levels, and user-configurable frequency (real-time/daily/weekly)

**Global embedding service** - Centralized embedding generation for nodes, bookmarks, intentions, and profiles to ensure consistency and reduce API costs

**User preference learning system** - Track accept/reject patterns across all AI suggestions to personalize recommendations, shareability thresholds, and match criteria

🟡 **Cost tracking and quota management** - PARTIALLY DONE: Dollar-cost tracking per user via APICostLog model (#61). Still needed: per-feature breakdown, tier enforcement, and quota limits.

**Analytics and insight dashboard** - Track usage patterns, feature adoption, flywheel metrics (journal→download→upload→market→journal cycle time) for product iteration

**Privacy and consent management** - Centralized system for feature-level opt-ins, data sharing permissions, and GDPR-compliant export/deletion across all four features

**Rate limiting and throttling** - Prevent abuse and control costs with per-user limits on recommendations, shares, matches, and API calls

**Caching layer with Redis** - Cache embeddings, LLM responses, match scores, and recommendation results to reduce latency and API costs

**Background job prioritization** - Celery queue management with priority levels (urgent: user-triggered vs low: batch processing) and failure retry logic
  - 🔧 Interactive/background queue split (#379, 2026-10-21): built, off until `CELERY_SPLIT_QUEUES` is switched on on prod (docs/RUNBOOKS.md → "Celery queue split").
  - ✅ Memory limit on the workers that run heavy jobs, and heavy jobs (profile window passes, imports, pre-fills, the Community Archive refresh) one at a time through a Redis slot (#613, 2026-11-09): live from the deploy, with the split off or on (docs/RUNBOOKS.md → "Celery queue split" → "Memory limits", "Heavy-job slot").

---

## Chronological Implementation Order

Based on current project state, dependencies, and strategic value, here's the recommended build sequence:

### Phase -1: Privacy & Encryption Infrastructure (Weeks 1-3) 🔒 ✅ **COMPLETED**

**Priority: BLOCKING - Must be done first before building sharing/marketplace features**

**Status: COMPLETED** - Two-column privacy system implemented for both Nodes and UserProfiles

**Why first:** Currently all nodes are public. Features 3 (Upload) and 4 (Intention Market) require granular privacy controls. Retrofitting privacy after building on public-only architecture would require rebuilding everything. This is a one-time breaking change that must happen now.

**Implementation Decision:** Two-column approach (more flexible than original 5-level design)
- Column 1: `privacy_level` - Who can access (private/circles/public)
- Column 2: `ai_usage` - How AI can use content (none/chat/train)
- This provides clearer separation of concerns and more flexibility

### Completed Items:

1. ✅ **Node privacy schema** - Added `privacy_level` and `ai_usage` columns to Node model
2. ✅ **UserProfile privacy schema** - Added `privacy_level` and `ai_usage` columns to UserProfile model
3. ✅ **Privacy utilities module** - Created enums, validation functions, and authorization checks (backend/utils/privacy.py)
4. ✅ **Fine-grained permission system** - `can_user_access_node()` checks privacy level and ownership
5. ✅ **AI usage permissions** - `can_ai_use_node_for_chat()` and `can_ai_use_node_for_training()` functions
6. ✅ **API endpoints for privacy** - Updated all Node and UserProfile creation/edit endpoints to accept and validate privacy settings
7. ✅ **Authorization enforcement** - GET /nodes/<id> enforces privacy with authorization check. Privacy filtering also enforced on Feed, Dashboard, and Node Detail views. Recursive human owner resolution for LLM node access control.
8. ✅ **Data migration scripts**:
   - `backend/scripts/migrate_privacy_settings.py` - Migrates existing nodes to privacy='private', ai_usage='train'
   - `backend/scripts/migrate_profile_privacy_settings.py` - Migrates existing profiles to privacy='private', ai_usage='chat'
9. ✅ **Frontend privacy UI** - Created PrivacySelector component with clear labels and descriptions
10. ✅ **Frontend integration**:
    - NodeForm: Privacy selector for text nodes, voice recordings, and file uploads
    - Dashboard: Privacy selector for user profile editing
    - NodeDetail: Conditionally hide LLM Response button when ai_usage='none' (#38)
11. ✅ **Tests** - Comprehensive unit tests for privacy validation, authorization, and AI usage checks (backend/tests/)
12. ✅ **Profile generation filtering** - Only include nodes with ai_usage='chat' or 'train' in AI-generated profiles, while user data exports include all nodes (#37)
13. ✅ **Account setting as the master switch** (#340, #346) - The AI-filtered export also blocks profile/todo/artifact rows and quotes marked 'none'; `filter_ai_usage` is a required argument and False is only for the user's own download. An account set to 'none' gets no background AI work: profile update/integration/batch steps, recent context, embeddings (nodes + references), references digest, poll drafts, admin pre-fill and intentions, each checked when the job runs. Every job also checks each row's own ai_usage (a 'none' profile is never an update base; the next build starts from the writing). A profile's ai_usage comes only from the account setting at creation. Admin pre-fill and intentions also refuse a declined tweet-seed consent. The UI-less `/export/generate_profile`, `/export/integrate_profile` and `/export/estimate_profile_tokens` routes are removed.

### Previously Blocking (Now Complete):

- ✅ Separate API keys for chat vs train (#39)
- ✅ GCP KMS encryption — envelope encryption for all content + audio at rest
- ✅ Rebrand to Loore (loore.org)

### NOT Implemented (Future Work):

- **Email infrastructure at scale** - Basic SMTP works for magic link auth; SendGrid/AWS SES needed for notifications and digests
- **Circles implementation** - Privacy level exists but circles functionality not yet built (moved to Phase 4)

**Deliverable:** Two-column privacy system for Nodes and UserProfiles with authorization enforcement and comprehensive tests

**Migration Strategy Implemented:**
- **Existing nodes:** Default to `privacy_level='private'` + `ai_usage='train'` (maintains training data value proposition)
- **Existing profiles:** Default to `privacy_level='private'` + `ai_usage='chat'` (AI can understand user for responses)
- **New nodes:** Default to `privacy_level='private'` + `ai_usage='none'` (fully private)
- **New profiles:** Default to `privacy_level='private'` + `ai_usage='chat'` (useful for AI responses)
- **User control:** Users can change privacy settings at any time via UI

**Privacy Level Options:**
- 🔒 **private** - Only owner can see
- 👥 **circles** - Shared with specific groups (coming soon)
- 🌐 **public** - Anyone can see

**AI Usage Options:**
- 🚫 **none** - No AI access
- 💬 **chat** - AI can use for responses (not training)
- 🎓 **train** - AI can use for training data

---

### Phase 0: Foundation (Weeks 4-6) 🏗️

**Priority: Enable fast, safe development of new features**

**Note:** CI/CD pipeline already exists! GitHub Actions run tests, linting, security scans, and auto-deploy on push to main. This phase focuses on adding actual tests and monitoring.

1. **Backend testing framework with pytest** - Set up pytest, conftest.py, test database fixtures (CI already configured to run pytest)
2. **Frontend testing framework with Jest/React Testing Library** - Write actual component tests (Jest already runs in CI with placeholder test)
3. **Test coverage reporting in CI** - Add coverage.py and jest coverage reports to existing CI pipeline
4. ✅ **Local development Docker Compose setup** - COMPLETED: Docker Compose with full local stack (PostgreSQL, Redis, Celery, backend, frontend), Makefile shortcuts, staging environment (#68, #69)
5. **Logging infrastructure with structured logs** - Add JSON logging with request IDs for debugging
6. **Error monitoring with Sentry** - Integrate Sentry for both backend and frontend error tracking
7. **Health check endpoints** - `/health` and `/ready` for monitoring and load balancer health checks
8. ✅ **Database backup and restore automation** - COMPLETED: nightly `pg_dump` + media → GCS, locked 28-day retention, restore runbook
9. **Dependabot configuration** - Enable GitHub Dependabot for automated dependency update PRs

**Deliverable:** Solid foundation with actual tests running in existing CI pipeline, monitoring, and development environment

**Why first:** You have the CI/CD infrastructure but no tests. This phase fills that gap. Without tests, building complex features is slow and risky. Every bug becomes a production fire. This investment pays off immediately.

---

### Phase 1: Download Foundation (Weeks 7-9) 📥

**Priority: Enable semantic search and RAG before building on top**

13. **Vector database with pgvector extension** - Install pgvector, add migration for embedding column — 🔧 #331 built without a migration: copies in a `vec` schema the autogenerate never sees; on once Peter installs the extension and runs `flask pgvector-enable --apply`
14. **Automatic embedding generation pipeline** - Celery task to embed nodes on create/edit (respects privacy: only embeds training/public nodes)
15. **Global embedding service** - Centralized embedding API with cost tracking
16. **Privacy-aware semantic search API** - Endpoint for cosine similarity search that filters by user's access permissions
17. **RAG (Retrieval Augmented Generation) system** - Chat with archive using top-K retrieval (only searches nodes user has access to)
18. **Test coverage for embedding and search** - Comprehensive tests with mocked OpenAI API and permission checks

**Deliverable:** Can search your journal semantically and chat with your archive

**Why now:** This is immediately valuable for personal use and is foundational for Features 2, 3, and 4 (all need semantic matching/search)

---

### Phase 2: Download Complete (Weeks 10-12) 📚

**Priority: Complete MemeOS integration for full "consume" feature**

19. **MemeOS API client or shared database layer** - Integration layer for querying bookmarks
20. **Context-aware recommendation engine** - Analyze writing context + profile to rank bookmarks
21. **Theme extraction service** - LLM-based topic extraction from current writing
22. **Spaced repetition integration** - Factor in review schedules for ranking
23. **Frontend: Recommendation sidebar widget** - UI component showing relevant bookmarks
24. **Caching layer with Redis** - Cache bookmark recommendations to reduce API calls
25. **Tests for recommendation logic** - Unit tests for ranking algorithm

**Deliverable:** MemeOS bookmarks surface in sidebar while you write

**Why now:** Completes Feature 2, provides immediate value, and demonstrates the "closed loop" between consumption and creation

---

### Phase 3: Development Velocity Upgrades (Weeks 13-14) 🚀

**Priority: Invest in speed before building complex features 3 and 4**

26. **API documentation with Swagger/OpenAPI** - Auto-generated API docs
27. **Component storybook for frontend** - Isolated component development
28. **Feature flags system** - Toggle features on/off without deployment
29. **Database query performance monitoring** - Identify and fix slow queries
30. **Performance monitoring and profiling** - Track endpoint latency and bottlenecks
31. **End-to-end testing with Playwright** - Critical flow testing (login → create → LLM)
32. **JWT authentication system** - Add token-based auth alongside session auth for future mobile/API access

**Deliverable:** Faster development workflow, better debugging, gradual rollout capability

**Why now:** Features 3 and 4 are complex with many moving parts. These tools will make building them 2-3x faster.

---

### Phase 4: Upload Foundation (Weeks 15-18) 📤

**Priority: Build sharing infrastructure without external platform complexity**

**Note:** This phase includes Circles implementation, which was deferred from Phase -1 as it's not blocking for basic privacy functionality.

33. **Circles data model and management** - Create Circle model with membership, privacy-aware share targeting (implements the 'circles' privacy level from Phase -1)
34. **Content analysis pipeline** - LLM-based shareability scoring and insight extraction (only analyzes training/public nodes unless user opts in)
35. **Privacy analysis system** - Detect sensitive information and flag risks before sharing
36. **Share approval workflow** - State machine for suggested/approved/published status with audit trail
37. **Content transformation engine** - Convert journal entries to polished shares while preserving attribution to encrypted source
38. **User preference learning system** - Track accept/reject patterns to personalize suggestions
39. **Audit logging for shares** - Log who shared what to whom and when for compliance
40. **Frontend: Circles management UI** - Create/edit circles, add/remove members
41. **Frontend: Share suggestion UI** - Preview, edit, approve/reject with clear privacy indicators
42. **Tests for analysis and transformation** - Mock LLM responses, test scoring logic, verify privacy enforcement

**Deliverable:** Get share suggestions, preview transformations, manage circles (sharing within app only, no external platforms yet)

**Note:** Shares respect privacy levels. Only training/public nodes can be suggested for sharing unless user explicitly opts in.

**Why now:** Proves core sharing value without external API complexity. Users can share to other Loore users.

---

### Phase 5: Upload External Platforms (Weeks 19-21) 🌐

**Priority: Add external publishing capabilities**

43. **OAuth integration framework** - Reusable OAuth client for multiple platforms with secure token storage
44. **Multi-channel publishing adapters** - API clients for Twitter, LinkedIn, Substack, GitHub Gists
45. **Draft generation for external platforms** - Format content for each platform's requirements and character limits
46. **Rate limiting per channel** - Prevent API quota exhaustion with per-platform rate limits
47. **Multi-entry synthesis** - Combine related entries into long-form essays using LLM
48. **Cloud file storage migration (S3/R2)** - Move audio files from local storage to cloud with encryption at rest
49. **Frontend: External platform connection UI** - OAuth flow, channel selection, publish preview
50. **Circuit breaker for external APIs** - Handle third-party API failures gracefully with retry logic

**Deliverable:** Publish shares to Twitter, LinkedIn, Substack with one click

**Why now:** Completes Feature 3, maximizes distribution of user's insights

---

### Phase 6: Intention Market Foundation (Weeks 22-25) 🤝

**Priority: Build marketplace infrastructure and matching algorithm**

51. **Intention data model with structured metadata** - DB schema for needs/offerings/opportunities with privacy controls
52. **Semantic matching algorithm** - Embed intentions and calculate compatibility scores using existing embedding service
53. **Reputation scoring engine** - Track completion rates, responsiveness, feedback with privacy-preserving aggregation
54. **Intention expiration and renewal system** - Auto-expire stale posts, prompt renewals based on activity
55. **Market discovery API with filtering** - Browse/search by type, topic, recency with permission checks
56. **Content moderation for marketplace** - OpenAI Moderation API + admin review queue for flagged intentions
57. **User blocking and reporting** - Allow users to block/report abusive users or spam
58. **Frontend: Intention posting UI** - Form to create intentions with metadata and privacy settings
59. **Frontend: Browse market UI** - Filterable list of active intentions with moderation indicators
60. **Tests for matching algorithm** - Unit tests for score calculation with mock embeddings and permission checks

**Deliverable:** Can post intentions, browse market, see match scores

**Why now:** Core marketplace functionality without real-time notifications complexity

---

### Phase 7: Intention Market Complete (Weeks 26-28) 💬

**Priority: Add connection features and notifications**

61. **WebSocket infrastructure for real-time updates** - Socket.IO server for live match notifications and messages
62. **Real-time match notification system** - Detect complementary intentions and push notifications via WebSocket
63. **Connection management infrastructure** - Accept/decline matches, private messaging with encryption
64. **Feedback and outcome tracking** - Post-collaboration ratings with privacy protections
65. **Group matchmaking logic** - Suggest forming groups for similar intentions (3+ users)
66. **Unified notification framework** - Consolidate all notifications (shares, matches, messages) with digest options
67. **Frontend: Match notification UI** - See matches, view profiles, connect with privacy-safe previews
68. **Frontend: Connection thread UI** - Private messaging between matched users with encryption indicators
69. **Tests for notification delivery** - Ensure matches are detected correctly and permissions enforced

**Deliverable:** Full matchmaking with notifications, connections, and messaging

**Why now:** Completes Feature 4, enables serendipitous collaboration

---

### Phase 8: Cross-Feature Integration (Weeks 29-31) 🔄

**Priority: Connect all four features into unified flywheel**

70. **Webhook system for inter-feature communication** - Event bus triggering actions across features (journal→recommend→share→match)
71. **Cost tracking and quota management** - Per-user LLM usage tracking per feature with tier enforcement
72. **Analytics and insight dashboard** - Track flywheel metrics (journal→download→upload→market→journal cycle time)
73. **Privacy consent management UI** - Centralized dashboard for all privacy settings across features
74. **Background job prioritization** - Optimize Celery queues with priority levels (urgent/normal/low) — 🔧 interactive/background split built (#379), off until switched on
75. **Spam detection across features** - ML-based spam detection for shares, intentions, messages
76. **Frontend: Unified dashboard** - Show activity across all four features with privacy indicators
77. **End-to-end flywheel tests** - Test complete cycle from journaling to connection with permission checks

**Deliverable:** Seamless integration where features amplify each other

**Why last:** Requires all four features to exist, demonstrates full vision

---

### Phase 9: Production Hardening (Weeks 32-33) 🛡️

**Priority: Make system bulletproof before scaling**

78. **Input validation and sanitization library** - Centralized validation for all user inputs (XSS, SQL injection, command injection prevention)
79. **CSRF protection for state-changing endpoints** - Prevent cross-site attacks on POST/PUT/DELETE
80. **Content Security Policy (CSP) headers** - Mitigate XSS attacks by restricting script sources
81. **Database migration testing automation** - Ensure migrations run cleanly forward/backward without data loss
82. **Load testing and capacity planning** - Stress test with realistic user loads to find bottlenecks
83. **Penetration testing on encryption** - Verify encryption implementation is secure (hire external auditor)
84. **Comprehensive end-to-end test suite** - Cover all critical user journeys including privacy edge cases

**Deliverable:** Production-ready system that can scale securely

**Why last:** Critical for launch but requires complete system to test properly

**Note:** Security scanning (Bandit, Safety) already runs in CI. This phase focuses on additional hardening and external audit.

---

## Success Metrics Per Phase

### Phase -1 (Privacy & Encryption)
- **Encryption coverage:** 100% of all nodes encrypted at rest with KMS
- **Zero data breaches:** No unauthorized access to encrypted content
- **Embedding isolation:** 100% of queries filtered by user_id
- **Permission enforcement:** 0 unauthorized node access incidents
- **User understanding:** 80%+ of users understand privacy levels and promises

### Phase 0 (Foundation)
- **Test coverage:** 60%+ backend, 50%+ frontend
- **CI pipeline:** All tests run in <5 minutes
- **Error monitoring:** 100% of errors captured in Sentry
- **Developer onboarding:** New dev can run full stack in <30 minutes

### Phase 1-2 (Download)
- **Embedding coverage:** 95%+ of nodes embedded
- **Search latency:** <500ms for semantic search
- **RAG quality:** User reports useful answers 70%+ of time
- **Recommendation relevance:** User clicks through 30%+ of suggestions

### Phase 4-5 (Upload)
- **Share suggestion acceptance:** 40%+ of suggestions approved
- **Privacy violations:** 0 accidental leaks of sensitive info
- **External publish success:** 95%+ publish attempts succeed
- **User satisfaction:** "Makes sharing effortless" - 70%+ agree

### Phase 6-7 (Intention Market)
- **Match quality:** 60%+ of suggested matches accepted
- **Connection engagement:** 40%+ of connections lead to conversation
- **Market activity:** 50%+ of users post at least one intention
- **Collaboration outcomes:** 30%+ of matches lead to reported value

### Phase 8 (Integration)
- **Flywheel completion:** 20%+ of users complete journal→download→upload→market cycle
- **Cross-feature usage:** 60%+ of users active in 2+ features
- **Cycle time:** Median time from journal entry to match <7 days

### Phase 9 (Hardening)
- **Uptime:** 99.5%+ availability
- **Security:** 0 critical vulnerabilities
- **Performance:** 95th percentile latency <2s for all endpoints
- **Data safety:** 0 data loss incidents

---

## Cost Projections

### Development Costs (Time Investment)
- **Phase -1 (Privacy & Encryption):** 3 weeks - BLOCKING change, must be done first (reduced from 4 weeks with KMS approach)
- **Phase 0 (Foundation):** 3 weeks - one-time investment, pays dividends forever
- **Phase 1-2 (Download):** 6 weeks - immediate personal value
- **Phase 3 (Dev Velocity):** 2 weeks - 2-3x speedup on subsequent phases
- **Phase 4-5 (Upload):** 7 weeks - high complexity due to external APIs and privacy
- **Phase 6-7 (Market):** 7 weeks - high complexity due to matching logic and moderation
- **Phase 8 (Integration):** 3 weeks - straightforward once features exist
- **Phase 9 (Hardening):** 2 weeks - essential for production, includes external security audit
- **Total:** ~33 weeks (8.25 months) for complete system

### Infrastructure Costs (Monthly, at scale)

**At 100 users:**
- **Compute:** $50 (DigitalOcean/AWS)
- **Database:** $25 (PostgreSQL + Redis)
- **Storage (S3):** $10 (encrypted audio files)
- **GCP KMS:** $5 (encryption/decryption operations)
- **LLM APIs:** $200 (embeddings, RAG, analysis, transformation)
- **External APIs:** $50 (Twitter, LinkedIn, etc.)
- **Email (SendGrid):** $15 (notifications)
- **Monitoring:** $25 (Sentry, logging)
- **Total:** ~$380/month = $3.80/user

**At 1,000 users:**
- **Compute:** $200
- **Database:** $100
- **Storage (S3):** $50 (encrypted audio with CDN)
- **GCP KMS:** $20 (scales with operations)
- **LLM APIs:** $1,500 (benefits from caching and optimization)
- **External APIs:** $300
- **Email (SendGrid):** $50
- **Monitoring:** $100
- **Total:** ~$2,320/month = $2.32/user (economies of scale)

**Revenue Model:** $10-25/month subscription → profitable at 100+ users

---

## Risk Mitigation

### Technical Risks

**Risk: LLM API costs spiral**
- **Mitigation:** Aggressive caching, model selection (use GPT-4o-mini where possible), quota management, cost tracking per feature

**Risk: External API rate limits hit**
- **Mitigation:** Circuit breakers, exponential backoff, queue management, batch operations where possible

**Risk: Vector search performance degrades**
- **Mitigation:** Start with pgvector (handles 1M+ vectors), monitor query times, plan migration to Pinecone if needed

**Risk: Privacy violation (accidental sharing)**
- **Mitigation:** Multi-layer protection, always require approval, obvious privacy warnings, easy unshare/delete

**Risk: System complexity becomes unmaintainable**
- **Mitigation:** Comprehensive tests (60%+ coverage), feature flags for gradual rollout, monitoring for early issue detection

### Product Risks

**Risk: Features don't integrate into flywheel**
- **Mitigation:** Build Phase 8 integration layer explicitly, measure cycle metrics, user research at each phase

**Risk: Matching algorithm doesn't work**
- **Mitigation:** Start simple (cosine similarity), iterate based on feedback data, track acceptance rates

**Risk: Users don't want to share**
- **Mitigation:** Make sharing optional, start with inner circle only, build trust gradually, emphasize privacy

---

## Open Questions

### Technical Decisions Needed

1. **✅ DECIDED: Encryption Strategy:** Application-level encryption with GCP KMS (pragmatic, no per-user key complexity)
2. **✅ DECIDED: Embedding Strategy:** Store embeddings unencrypted with strict user_id isolation (pragmatic, enables search/AI)
3. **MemeOS Integration Method:** Shared database or API? (Affects Phase 2 timeline)
4. **Vector Database Choice:** Start with pgvector or go straight to Pinecone? (Cost vs simplicity tradeoff)
6. **Notification Delivery:** Email, in-app, WebSocket push, mobile push, or all? (Affects Phase 7 infrastructure complexity)
7. **Testing Philosophy:** Unit tests only or also integration/E2E? (Affects Phase 0 scope)
8. **Deployment Strategy:** Monolith or microservices? (Affects Phase 9 architecture)

### Product Decisions Needed

9. **Privacy Level Marketing:** How to communicate 5 privacy levels without confusing users? (Affects onboarding UX)
10. **Default Privacy Evolution:** When to switch from training→private as default for new nodes? (Affects user trust timeline)
11. **Feature Launch Order:** Should we launch features to users incrementally or wait for all four? (Affects beta strategy)
12. **Monetization Timing:** Free tier for all features or paywall some? (Affects user growth vs revenue)
13. **Network Effects:** Can Feature 4 work with 100 users or need 1,000+? (Affects cold start strategy)

---

## Conclusion

This roadmap prioritizes **privacy & encryption first** (Phase -1), then **foundation** (testing, monitoring), then **personal value** (Feature 2: Download), then **sharing** (Feature 3: Upload), then **community** (Feature 4: Intention Market).

**Critical Insight:** Privacy infrastructure (Phase -1) is BLOCKING. Retrofitting encryption after building Features 3-4 would require rebuilding everything. This one-time architectural change must happen first.

**Key Infrastructure Gaps Identified:**
1. **Privacy & Encryption** - 5-level privacy system with encryption, key management, recovery
2. **Authentication & Authorization** - Fine-grained permissions, JWT tokens, OAuth framework
3. **Data Infrastructure** - Cloud storage (S3), email service, WebSockets for real-time
4. **Compliance & Safety** - Audit logging, GDPR enhancements, content moderation, spam detection
5. **Mobile & Offline** - Mobile-optimized APIs, offline sync, push notifications

**Total Timeline:** ~33 weeks (8.25 months) for complete four-feature ecosystem

**First Milestone:** After Phase -1 + 0 (6 weeks), you'll have KMS encryption + tests foundation
**Second Milestone:** After Phase 1-2 (12 weeks total), you'll have RAG + MemeOS integration for personal use

**Next Steps:**
1. ✅ **COMPLETED:** API key separation for chat vs train operations
2. ✅ **COMPLETED:** GCP KMS envelope encryption for content at rest (v2 format, REST transport, DEK caching, paginated Feed/Dashboard) and audio files at rest
3. ✅ **COMPLETED:** Rebrand to Loore (frontend, backend, domain loore.org, landing page, icons, email templates)
4. ✅ **COMPLETED:** Magic link email authentication (replaces Twitter-only login)
5. ✅ **COMPLETED:** Full UI redesign with warm literary design system
6. ✅ **COMPLETED:** User plan tier standardization with admin management
7. ✅ **COMPLETED:** Docker Compose local development + staging environment (#68, #69)
8. ✅ **COMPLETED:** Unified agentic Voice workflow (#81, supersedes Reflect/Orient #70)
9. ✅ **COMPLETED:** Iterative profile generation with automatic updates (#71)
10. ✅ **COMPLETED:** System prompt management (Prompts page) (#72, #73)
11. ✅ **COMPLETED:** Cmd+K keyword search with date filtering and infinite scroll
12. ✅ **COMPLETED:** Dollar-cost tracking via APICostLog (#61)
13. ✅ **COMPLETED:** Claude data export import (#64)
14. ✅ **COMPLETED:** Unified Voice workflow with tool use (#81), including GitHub issue creation from voice
15. ✅ **COMPLETED:** Frequent audio upload (15s) + batched transcription + draft recovery (#74)
16. ✅ **COMPLETED:** Context artifact tracking (#77) + URL-style export params (#79)
17. ✅ **COMPLETED:** Hierarchical context freshness (#80, #84, #86)
19. ✅ **COMPLETED:** Proposal tracking with explicit IDs and lifecycle statuses, confirmation nodes
19b. ✅ **COMPLETED:** Feature 2 Download as quote-as-response (#208) — substrate, nightly X sync, digest, labeled search + read_full, per-user easter-egg activation
19c. ✅ **COMPLETED:** Delete all my content / admin data purge (#268; #590: a fresh start, only the jobs that read the deleted content stop) — hidden at once, restorable for 30 days, then deleted forever (Peter, 2026-10-09); cost rows kept anonymised
19d. ✅ **COMPLETED:** Account deletion (#269) — deleted at once, restore by signing in within 30 days, then deleted forever (purge + identity layer); admin deletion restorable the same way, with an admin Undo (2026-10-13); the purge revokes the stored X tokens at X, and the request revokes the session's "Sign in with X" token (OAuth 1.0a `oauth/invalidate_token`, best effort); web terms updated. Open: iPhone in-app deletion and terms (#482, App Store 5.1.1(v))
19e. ✅ **COMPLETED:** Large profile builds as independent versions merged in fixed groups (#545, builds on #586) — a 14.1M-unit import becomes one Batch submission of windows plus a few merges instead of 157 sequential steps; before the first such import: the tester's spend limit and the provider spend headroom
19f. ✅ **COMPLETED:** Small profile builds as one window + integration (both providers on; Peter, 2026-11-03); profile batches polled by listing; a full provider queue never moves an account to the full-price path (on #589)
19g. ✅ **COMPLETED:** Delete an artifact (#603: backend, web and iPhone) — soft delete hidden from every reader including the AI's index, purged with its session pins after 30 days; web ⋮ menu, iPhone long press
19h. ✅ **COMPLETED (in review):** Later turns see the thread's own artifact writes; every write names its version (#604) — web "Show changes" in Actions taken; iPhone app follow-up
19i. ✅ **COMPLETED (in review):** Spend cap carries a month's overflow into the next month (#594) — admin "Carried" column with a clear action; iPhone toasts read the server's lift date in a later build
19j. ✅ **COMPLETED (in review, dark):** Payments, part 1 (#438) — Stripe Managed Payments subscriptions behind `PAYMENTS_ENABLED`, tested end to end in the Stripe sandbox (trial → active → cancel at period end → ended; tax on top; failed first payment → not let in; failed renewal → lapsed → paid → active; portal upgrade); next: Peter's Stripe live setup and a last check of prices and ceilings, legal pages (#440), referrals (#439), the alpha testers' free month
19k. ✅ **COMPLETED (in review, dark):** The alpha testers' free month (#438) — one free month for every tester from the Beta release date, then the discounted price without a code; lapsed, not shut out, after it
19l. ✅ **COMPLETED (in review, dark):** Referrals (#439) — invite link, signup keeps the inviter, a free week each as Stripe credit on the friend's first payment, given once
20. **NOW:** Implement text mode — last major feature before expanding alpha to ~10 users
21. **NOW:** Stabilize Voice/Todo workflow — open bugs: todo merge hangs (#87), checkbox UX (#93, #94), interactive proposal editing (#89), completed item deletion (#97)
22. **NOW:** Protected usernames (#91) + Anthropic API spend monitoring (#85)
23. **NOW:** Sentry integration + health check endpoints + security hardening (Phase 0)
24. **NEXT:** Onboarding flow + About page updates for alpha expansion
25. **NEXT:** Expand agentic tool use — intentions, feedback, memory/artifacts, long-running thread UX
26. **NEXT:** RAG / semantic search — embeddings, chat with archive, community archive (Phase 1)
26b. 🔧 **IN PROGRESS:** Native iPhone app (PR #384) — built and simulator-tested (M1–M5); next: Peter's device checklist and a staging pass, then review and merge
27. **NEXT:** Alchemical Mode (spec'd 2026-07-01, see FOUR-FEATURE-ECOSYSTEM.md) — LLM readiness/safety checker over recent user data → opt-in with strong disclaimers → gated Alchemy home card with hidden source prompt (Meditationbook.page PoC first; Chapman corpus + Petr's book as pluggable sources later)
28. **THEN:** Testing infrastructure (Phase 0) + legal compliance
29. Iterate based on learnings at each phase

This is ambitious but achievable. Each phase delivers value, and the foundation work (especially privacy) ensures sustainable velocity and user trust throughout.

**The KMS approach:** Simpler than per-user encryption (no password recovery complexity, no client-side crypto), still provides strong security with transparent encryption/decryption. Embeddings stored unencrypted but strictly isolated by user_id enables full search/AI functionality.
