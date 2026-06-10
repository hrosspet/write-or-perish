# Write-or-Perish (Loore) — Implementation & Testing Plan

## 1. Executive summary

**Triaged:** all 65 open issues. **Verdict breakdown (structured source of truth):** 20 GOOD, 30 BORDERLINE, 15 EXCLUDE.

**Selected for this plan (build now):** 18 GOOD with clean desktop-Chrome verification (Section 3) + 9 BORDERLINE/GOOD-with-caveats that carry tractable, scoped caveats (Section 4) = **~23 actionable issues** scheduled across the waves. The finalizer conservatively placed #101 (favicon — needs human brand sign-off) and #160 (mic-denied — only the denial path is Chrome-testable) into the caveats table even though their structured verdict was GOOD. The remaining BORDERLINE issues are deferred pending a product decision or because they are backend-only-no-UI-signal items that ride along with a GOOD sibling; the 15 EXCLUDE are out of scope (exploration, mobile-OS/Safari-only, needs-mic-audio, or prod-only ops).

**Core testing loop:** code on a feature branch off `main` → local verify (flake8 E9/F-codes, pytest, `npm run build`) → **force-push to `staging`** → `deploy-staging.yml` rebuilds + deploys to `staging.loore.org` (6–15 min, DB wiped) → drive the **already-logged-in desktop Chrome session** via the automation extension → first action: click "I Agree" on the terms modal → seed test data (nodes/todos) → run the UI assertions → if green, open PR and merge to `main` (auto-deploys prod).

**#1 enabler:** Auth is solved (persistent Flask-Login cookie in the live Chrome session; magic-link-via-Gmail-MCP fallback). That removes the historical top blocker.

**#1 risk / governing constraint:** **Staging is a single shared environment with a DB wiped on every deploy.** Branches can be *developed* in parallel, but **staging testing must be serialized or batched** — combine several green-locally branches into one staging integration push, test them together in one Chrome cycle, then merge the passing ones to `main` individually. **Secondary risk:** the Chrome extension cannot inject deterministic mic audio, so every voice feature is testable only at the UI/error-handling/state-machine level — never transcription correctness. This is why the voice cluster is largely deferred or scoped to its denial/lifecycle paths only.

**#2 risk / inherited from triage:** **Several verdicts carry stale root-cause analysis** that no longer matches HEAD. Confirmed-stale for **#93** (TodoPage renders via its own `TodoItem` component, NOT `MarkdownBody` — there is no `<label>` to widen), **#89/#105** (`ProposalInline.js` is already interactive — toggles/move/apply flows shipped; the "read-only proposal" premise is dead), **#94/#108** (checkbox toggle uses `PATCH /todo`, NOT `PUT` — PUT is the manual Save button only), and **#128** (the recon map's `to_iso()` chokepoint is **fictional** — the fix is a multi-file `.isoformat()` sweep). A **mandatory re-confirm-against-HEAD step** is baked into Section 8 for exactly these issues. Do not build from the inherited rationale for #93, #89, #94, #105, #108, #128 — re-derive first.

---

## 0. PROGRESS LOG (read this first)

**✅ Wave 1 — COMPLETE & merged to `main`/prod (2026-05-29).** All 7 issues shipped via separate PRs, each verified on staging:
- #142 nginx Server header (PR #162) — required a manual `server_tokens off` + reload on the **VM edge nginx** (the deploy only updates the container config); done on the VM, confirmed `Server: nginx`.
- #62 disabled-button tooltips (PR #163) — the working fix wraps the disabled button in a non-disabled `<span title=…>` (a `title` on a `disabled` element never shows).
- #63 remember privacy/AI choices (PR #164) — localStorage `loore_last_privacy_level` / `loore_last_ai_usage`.
- #134 text-mode auto-generate (PR #165) — WritePage passes `auto_generate` from `loore_auto_generate`.
- #98 landing scroll line (PR #167) — final design: content-sized hero, line ~40px below CTA on all viewports; narrative fade gated on `useHasScrolled()` (tall desktop fits the first section in-viewport, so a geometry threshold alone fired it pre-scroll).
- #66 stale TTS on edit (PR #166) — confirm dialog (Keep / Regenerate). Took several layers: `regenerate_tts` flag-gating; `has_tts` on BOTH node serializers (focal + `serialize_node_recursive`); `onTtsGenerated` callback fired from the SSE completion path so same-session edits see fresh `has_tts`; `SpeakerIcon` cache reset on `content` change; and **cache-busting the media URL** (`?v=<chunk id>`) because nginx serves `/media` with a 24h `Cache-Control` and the path is reused every regen.
- #28 mobile audio player (PR #168) — pops out of the NavBar into a bottom floating card < 640px; toasts offset above it via `--floating-player-offset`.
- Also filed during Wave 1: **#161** (Stop button should reset playback to 0, not hide the player) — ~~not yet done~~ **RESOLVED (confirmed fixed, 2026-06-10)**.

**Key lessons that bit us in Wave 1 (see memory + Section 8):**
- **Test like a human, end-to-end, in the real state.** Screenshots are primary evidence for layout; reproduce the user's *exact* repro (e.g. "generate TTS then edit in the *same session*", logged-out for the landing page). "200 OK"/"string is in the bundle"/passing unit tests are NOT proof a UI feature works.
- **`staging-batch-N` is deploy-only — never commit on it.** Verify `git rev-parse --abbrev-ref HEAD` is the `fix/<n>` branch before every commit (commits on the batch branch get destroyed when it's rebuilt; recover via `git reflog` + cherry-pick).
- **Verify the deployed bundle actually contains the change** (`curl` the live `main.<hash>.js`, grep a unique marker) before testing — a successful deploy of the wrong commit looks identical to success; browser cache can also serve a stale bundle.

**✅ Wave 2 — COMPLETE & merged to `main`/prod (2026-05-29).** A user-reprioritized batch of **6 issues** (NOT the original wave order — it cherry-picked across Section-7 Waves 2/3/4/5): **#108, #128, #129, #130, #135, #137**. Built in **one parallel multi-agent workflow** (3 isolated git-worktree streams), integrated into one `staging-batch`, verified on staging, then shipped as **3 per-stream PRs**:
- **PR #169 — fe-pages:** #108 quick-add (`+` on TodoPage → `PATCH /todo`, lands in `## Today`, no AI step); #129 Cmd+Return / Ctrl+Enter primary-submit via a shared `useSubmitShortcut` hook across NodeForm/Profile/PromptDetail/Todo-edit/Login/Account; #128-frontend (the 7 duplicated `formatDate` helpers consolidated into `utils/date.js`, defensive `Z` parsing + `today`/`yesterday`).
- **PR #170 — import:** #137 Claude import extracts `conversations.json` client-side with JSZip and POSTs only the blob (sidesteps the 200 MB nginx limit); backend analyze reads `conversations_file` JSON (no zip). #135 import progress — spinner + `Extracting…/Analyzing…/Importing…` stage labels across all import paths.
- **PR #171 — time-backend:** #128-backend `.isoformat()→Z` sweep + `User.timezone` column (`server_default='UTC'`); #130 LLM temporal grounding — every context message prefixed `[YYYY-MM-DD HH:MM TZ]` in the user's local zone, sourced from `node.updated_at` (fallback `created_at`); tz auto-captured by `UserContext` on any full app load + `X-Timezone` header.

**Post-merge follow-ups (also shipped):** removed the `⌘↵` hint icon next to Send (kept the shortcut); extended #108 quick-add to the **Voice/Text todo-update proposals** (`ProposalInline`, shared by both modes) with Cmd/Ctrl+Enter; added the **pre-dialog** import progress (closing the "frozen until the dialog appears" gap). Plus model-config maintenance — **added Claude 4.8 Opus** (`claude-opus-4-8`, $5/$25, 1M ctx) to available models (4.5 Opus was already deprecated).

**GitHub status (2026-05-29):** all 6 batch issues (#108/#128/#129/#130/#135/#137) now closed as completed. Todo-cluster siblings also resolved — **#94** (toggle ~1s delay) fixed & closed completed; **#93** (hit target too small) closed **won't-implement** (maintainer decision). So the whole todo cluster (Wave 2) is done. #172 (model attribution) stays open.

**Caveats / open items:**
- **#128** is read-time `Z` (no data backfill); existing users' `timezone` was backfilled to `UTC` by the migration's `server_default` and **self-heals to their real zone on the next full app load** (no logout/backfill needed).
- **#130** model behavior isn't deterministically Chrome-testable — the prefix format is unit-tested; verify assembled prefixes in the celery-worker logs. **Open decision filed as #172** — whether to attribute the *specific model* on assistant turns (multi-model provenance); needs more thought.
- **#137/#135** — the actual file-picker upload couldn't be driven (the Chrome extension blocks programmatic uploads); verified via a live analyze POST + unit tests + bundle markers, so the in-browser upload + visual spinner are the one unexercised path (a quick manual check is worthwhile). Real Claude export confirmed to carry a top-level `conversations.json`.

**Key lessons added this wave:**
- **The reused Chrome session serves a STALE bundle after a staging deploy** — verify the running `main.<hash>.js` matches the deployed hash and **cache-bust (`?cb=`)** before testing, or brand-new features look *falsely broken* (this burned a full pass on #108/#129/#130 before being caught).
- In multi-agent **worktree** runs, a subagent's `cd <repo>` resolves to the **main checkout, not its worktree** — pin commits to the worktree branch and reset any stray local `main` back to `origin/main` before pushing.

**▶ NEXT — remaining leftovers from the original Section-7 clusters** (the batch cherry-picked across them; see Section 7 for per-issue specs + updated status markers):
- ~~**Todo polish** (#94, #93)~~ — **RESOLVED 2026-05-29:** #94 fixed & closed completed; #93 closed won't-implement. Todo cluster fully closed out.
- **Import dedupe:** #136 (re-import / snapshot overlap) — now unblocked since #137/#135 shipped; model column + dedupe key, no manual migration. *Best next pick.*
- **Profile/UX:** #131 (app-wide toast on profile-generation completion), #101 (transparent favicon — needs brand sign-off).
- **Security:** #91 (reserved usernames — security-critical, final single-item staging pass) + #160 (mic-denied path).
- Then the keyword cluster (#139→#149, #105) and #138 (markdown rendering, isolated). Decision-gated items (Section 9) remain parked. ~~Also still open from Wave 1: **#161**~~ — #161 confirmed fixed (2026-06-10). **Update 2026-06-10:** #139/#110/#104 implemented (PR in flight, stacked on the caching batch); #105 dropped — likely no longer reproduces per maintainer; #149 dropped from the current plan per maintainer.

---

## 2. Prerequisite: staging testing harness

The loop given the live Chrome session and the wipe-on-deploy DB:

**After every staging deploy (DB is empty, terms reset):**
1. **Accept terms first.** The `TermsModal` (App.js, gated on `user.terms_up_to_date === false`) appears on first load post-wipe. The automation extension's first action must locate and click **"I Agree"** (`POST /terms/accept`). Nothing else works until this clears.
2. **Re-seed test data per test.** All prior nodes/todos/drafts/audio are gone. Seed exactly what the batch needs, e.g.:
   - **Todo tests** (#93, #108, #66-profile): enter edit mode on TodoPage; the default template auto-creates `## Today / ## Upcoming / ## Completed recently` sections — paste markdown with `- [ ] task A` / `- [ ] task B` under `## Today`, save. Note: the rendered list is `TodoPage.js`'s `TodoItem` component, not `MarkdownBody`.
   - **Node/markdown tests** (#138, #128, #66): use the Write dialog to create a node with the constructs under test (headings, bold, blockquote, table, checklist; or content destined for TTS).
   - **Import tests** (#135, #136, #137): keep a small ChatGPT/Claude export file ready to upload via the file picker each cycle.
   - **Username tests** (#91): the seeded admin account (`hrosspet`) exists post-init; use Account settings to attempt reserved names.
3. **Verify via DevTools, not just visuals.** Use the network panel (request shape/payload/**verb** for #137, #94, #108, #135 — assert `PATCH /todo` for toggles, `PUT /todo` only for manual Save; response headers for #142), DOM inspection (title attribute for #62, computed font-size for #106, `Server` header for #142), and timezone/device emulation (Sensors → timezone for #128; device toolbar for #28/#98).

**Session-lapse fallback (rare):** if the persistent cookie expires (SECRET_KEY rotation or 30-day window), drive the magic-link flow: enter `hrosspet@gmail.com` on LoginPage → `POST /auth/...` sends the link → read it via the **Gmail MCP** → open the link in Chrome → session re-established. Then resume at step 1.

**Batching cadence:** one **staging integration branch** per wave. Cut `staging-batch-N` from `main`, merge each green-locally feature branch into it, force-push to `staging`, run the whole batch's Chrome assertions in a single post-deploy cycle. Merge only the individually-green features to `main`. Budget ~6–15 min per deploy; do **one** seed+assert pass per deploy. Never interleave two deploys — the concurrency group serializes them anyway and the second wipe destroys the first's data.

**Isolation caveat (batch-tested, merged-alone):** items are tested *in combination* on staging but merged to `main` *individually*, so a prod deploy runs code only ever exercised alongside its batch siblings. Fine for low-coupling waves. For **Wave 6** (security + backend-correctness mix), run a **final single-item staging pass for #91** (the security-critical fix) before merging it alone to `main`.

---

## 3. Selected issues (GOOD)

### Theme: Tiny/quick UX & infra wins (XS/S, high confidence)
| # | Title | Surface | Effort | Deps | One-line UI test |
|---|-------|---------|--------|------|------------------|
| 142 | Strip `Server` header from nginx | infra | XS | — | DevTools Network → assert `Server` is version-stripped (also `curl -I`) |
| 62 | Tooltip on disabled upload/record buttons | frontend | XS | — | Set AI usage "No AI Access"; assert disabled buttons carry explanatory `title` |
| 63 | Remember AI-usage + privacy choices in Write dialog | frontend | S | — | Change selectors, reopen dialog, assert values restored (localStorage) |
| 134 | Text mode auto-generates first response when auto-gen is off | frontend | S | — | Auto-gen OFF → submit → assert no AI child node; ON → assert one is generated |
| 98 | Landing arrow off-screen on some viewports | frontend | S | — | `/landing` across emulated viewports → arrow always visible, margins sane |

### Theme: Todo list UX
| # | Title | Surface | Effort | Deps | One-line UI test |
|---|-------|---------|--------|------|------------------|
| 93 | Todo checkbox hit target too small | frontend | S | 94 | Click empty row area → checkbox toggles (full-row click target). **Re-derive root cause first — fix lives in `TodoPage.js` `TodoItem` (18px `<div onClick>`, lines ~104-167), NOT MarkdownBody label.** |
| 94 | Todo checkbox toggle ~1s delay | both | M | — | Click checkbox → instant flip; **`PATCH /todo`** fires in background (Network), not `PUT`. **Confirm which verb creates a version row before building.** |
| 108 | Quick-add task button on todo list | both | M | 94 | Click "+", type, Enter → new `- [ ] item` appears under `## Today` + persists (via `PATCH /todo`), no AI step. **NOT gated on #144** (see §6). |

### Theme: TTS & markdown rendering
| # | Title | Surface | Effort | Deps | One-line UI test |
|---|-------|---------|--------|------|------------------|
| 66 | Stale TTS audio persists after content edit | backend | S | — | Generate TTS → edit node → reopen → stale audio gone **and chunked replay does not resurface old audio** (clear `audio_tts_url` *and* TTSChunk rows in `update_node` + profile `set_content`) |
| 28 | Audio player not fully visible on mobile | frontend | S | — | Device emulation 320/375/414px → all player controls visible, no clip |
| 138 | Markdown rendering to match standard expectations | frontend | M | — | Node with all constructs → headings/blockquote/table/hr render styled, both themes |

### Theme: Timezone correctness
| # | Title | Surface | Effort | Deps | One-line UI test |
|---|-------|---------|--------|------|------------------|
| 128 | Timestamps show UTC not user local | both | **M (spans many files)** | 130 | DevTools Sensors → override tz → node/feed/version-history times match wall clock. **No single `to_iso()` chokepoint exists — sweep every `.isoformat()` on timestamp columns (feed.py, export_data.py, serialization.py tombstone path, other routes).** |

### Theme: Data import
| # | Title | Surface | Effort | Deps | One-line UI test |
|---|-------|---------|--------|------|------------------|
| 136 | Deduplicate imported data | both | M | 137,135 | Import same export twice → second import creates no new nodes |
| 135 | Progress indicator during imports | both | M | 137 | Start import → spinner/progress appears and persists until done |
| 137 | Claude import: handle oversized zip (port ChatGPT fix) | both | M | 136,135 | `/import` Claude → Network shows small JSON POST, not raw-zip multipart |

### Theme: Profile completion UX
| # | Title | Surface | Effort | Deps | One-line UI test |
|---|-------|---------|--------|------|------------------|
| 131 | Notify user when profile generation completes | frontend | M | — | Start regen, navigate away, stub status→completed, assert app-wide toast |

### Theme: Keyboard / submit ergonomics
| # | Title | Surface | Effort | Deps | One-line UI test |
|---|-------|---------|--------|------|------------------|
| 129 | Cmd+Return / Ctrl+Enter primary submit across app | frontend | M | — (quick-add input wiring waits on #108) | Cmd+Return in Write/Profile/Todo/Login/Account → submits; plain Enter = newline |

### Theme: Security / alpha-expansion blockers
| # | Title | Surface | Effort | Deps | One-line UI test |
|---|-------|---------|--------|------|------------------|
| 91 | Protected usernames denylist | both | S | — | Account → set username "admin"/"loore" → 400 + error shown, value unchanged. **Validation lives in `dashboard.py` `update_user` (lines ~188-205); no denylist today.** |

---

## 4. Borderline — include with caveats

These have a clean, Chrome-testable (or unit-test-anchored) slice. Build the in-scope slice; defer the open-decision part.

| # | Title | Surface | Effort | Deps | Caveat (what to scope in / out) |
|---|-------|---------|--------|------|---------------------------------|
| 160 | Voice fails silently when mic permission denied | frontend | S | 88 | Build the **denial path only** (NotAllowedError → toast, no stuck UI). Deny mic via DevTools is Chrome-reproducible. Skip the Android-specific copy beyond a static string. Success/transcription path NOT testable. |
| 139 | `{user_export}` keyword: first-occurrence only | backend | S | 149 | Backend-only, **verify by unit test** (extend `test_llm_placeholders.py`). No Chrome signal. CONFIRMED: `{user_export}` uses `.replace()` (all occurrences, ~line 925) while profile/recent use first-occurrence. Stale title ("chat-with-archive" → it's `{user_export}`). Decide if rule also applies to `{quote:ID}`. |
| 105 | Proposal tag/ID leaking into UI | both | S | 144 | Frontend fix authoritative. CONFIRMED gap: `parseOrientResponse` applies `stripProposalTag()` to note/issueTitle/description/category (lines ~43-49) but NOT to completed/newTasks/priority bodies — broaden it there. **The "read-only ProposalInline" framing is stale; the component is already interactive.** Reproducing a live proposal is non-deterministic (LLM); anchor on a unit test feeding a tagged response. |
| 149 | Keyword UX: explainer + insert buttons | both | L | 139 | **Ship only the insert-buttons + explainer slice** (clean GOOD-tier). **Defer** the `user_export→user_archive` rename and autocomplete/slash-command (open decisions; "user_archive" does not exist in repo). |
| 101 | Logo/favicon square background in Chrome tab | frontend | S | — | CC wires references + can produce a transparent PNG/ICO; **final brand-quality icon needs human approval**. Tab strip not screenshot-able by the extension — verify served asset transparency + dimensions in DevTools/direct URL. Don't touch apple-touch icon. |
| 29 | Progress indicator for TTS generation | both | S | 145 | A binary "generating" indicator already exists. **PM decision:** is granular "chunk X of Y" actually wanted, or close as done? If wanted, wire per-chunk progress over the existing TTS SSE. |
| 110 | Raw export excludes user's replies in others' threads | backend | M | — | Backend CTE fix, **verify by unit test** (multi-user precondition impractical on single-session staging). Watch token-budget blowup; lean on existing `accessible_nodes_filter`. **No Chrome signal — acceptance is local pytest; no staging slot needed (see §7 Wave 6).** |
| 104 | `_call_llm_with_retries` never passes max_tokens | backend | XS | — | One-line fix in `backend/tasks/exports.py` + test. **No Chrome signal** — verify by unit test asserting `max_tokens` forwarded. **Acceptance is local pytest; no staging slot needed.** |
| 130 | LLM has no temporal grounding | both | L | 128 | **Share the `User.timezone` plumbing with #128** — add the column once. Prefix every message with local timestamp in `_format_author_line`. Benefit is LLM behavior (not Chrome-assertable); verify prompt-render via unit test + tz-send via Network. Re-check token budget. |

---

## 5. Excluded

| # | Reason bucket |
|---|---------------|
| 107 | decision-needed / likely stale (unified Orient flow landed) |
| 103 | mobile-OS-only (rooted Android, needs device logs) |
| 22 | mobile-OS-only (locked-phone background audio constraint) |
| 113 | needs-mic-audio (transcription quality with background music) |
| 141 | needs-mic-audio + heavy-infra (local whisper fallback) |
| 102 | Safari-only (autoplay/queue gating) |
| 99 | decision-needed (privacy semantics of profile TTS) |
| 148 | exploration (security audit umbrella → spawns sub-issues) |
| 126 | prod-op (one-time backfill run on prod VM) |
| 159 | exploration (voice-driven dev pipeline, meta) |
| 157 | exploration (Claude Code integration umbrella) |
| 156 | exploration + decision-needed (corpus-to-OpenAI privacy) |
| 155 | exploration (RAG/vector infra, XL) |
| 154 | exploration (topic-page knowledge base) |
| 153 | exploration + needs-mic-audio (real-time voice mode) |
| 151 | exploration (overnight briefing, missing Follow model) |
| 18 | stale / not-found in current code (40% status bar) |

**Deferred BORDERLINE (decision- or sibling-gated; revisit after waves):** 89 (proposal interactivity — **largely already shipped** in the evolved `ProposalInline.js`; re-scope remaining work against HEAD before any effort), 97, 144 (todo agentic/merge cluster — prompt-engineering against non-deterministic LLM; decisions on priorities section); 106, 132 (iOS/WebKit-only — not Chrome-verifiable); 88, 127, 124, 23 (voice data-loss — lifecycle code implementable but core repro needs real audio/phone; #124 is the best-specified, ship with fixture unit tests if/when prioritized); 152, 32, 65 (profile-policy cluster — partly already built, weak Chrome signal, decisions on thresholds); 143, 146, 147 (positioning/onboarding/AI-prefs reconciliation — product/copy decisions); 78 (router migration — large regression surface, isolate later); 85 (spend monitoring — alert-channel decision); 145, 140 (TTS chunking/chapters — decisions + weak Chrome signal); 150, 158 (new feature surfaces — scope-split + decisions).

---

## 6. Dependency & shared-plumbing graph

Order these so shared code lands once and downstream items inherit it.

- **Timezone plumbing — #128 → #130.** Both stem from "no user-local time anywhere." Add `User.timezone` **once** in #128. **There is no `to_iso()` helper** — #128 must sweep every `.isoformat()` on a timestamp column across the route/serialization files (feed.py, export_data.py, serialization.py tombstone branch, and other surfaces) and emit an explicit `Z`/offset; treat as **M spanning many files**, not a one-liner. #130 then reuses the column to prefix LLM messages. **Order: #128 first, #130 second** (or same PR for the column, separate PRs for the two surfaces). Avoid two competing column adds.
- **Todo cluster — #94 → #93 → #108, with #129 wiring.** #94 (perf/save-path) and #93 (hit target) touch the todo toggle path. **Verb correction:** checkbox toggles call **`PATCH /todo`** (TodoPage.js:216); **`PUT /todo`** is the manual Save button only (line 227). Confirm which handler creates a version row before scheduling #94. #93's fix is in `TodoPage.js` `TodoItem` (widen the 18px `<div onClick>` to the row, guarding the collapse-toggle child) — **not** a MarkdownBody `<label>` (which doesn't exist). **Do #94 first or together** so #93 builds on a stabilized toggle path. #108 (quick-add) appends a `- [ ] item` to the auto-created `## Today` section and persists via the same `PATCH /todo` path; **#108 is NOT blocked by #144** — `## Today` already exists deterministically (TodoPage.js:240). #129 wires Cmd+Return into the quick-add input **after #108 lands**.
- **#129 ↔ #108 — resolve the circular dependency.** #129 ships Cmd+Return for **all existing surfaces independently** (Write/Profile/Login/Account/Todo-edit). **Only the quick-add input wiring depends on #108.** The dependency is one-directional: #129 does NOT depend on #108; the quick-add-specific keybinding does. (Earlier dep tables encoded #108→#129 and #129→#108 — that cycle is removed: #108 has no #129 dep.)
- **Keyword cluster — #139 → #149.** CONFIRMED: `{user_export}` currently uses `.replace()` (all occurrences); #139 routes it through the first-occurrence helper used by profile/recent. #149's insert-buttons must reference the *actual* placeholder strings. Land #139's substitution semantics first, then expose them in #149's UI. Both must avoid the stale `user_archive` rename (deferred).
- **Import cluster — #137 ↔ #136 ↔ #135.** Add the **dedupe key (#136) first** so the Claude port (#137) inherits it; #135's progress UI should wrap both the client-side extraction and the analyze/confirm steps for **all** import paths. CONFIRMED: `handleClaudeImportFile` uploads the raw `zip_file` while ChatGPT uses JSZip + `f.name.endsWith("conversations.json")` (ImportData.js:248) — #137 ports that client-side extraction; **explicitly define the fallback when JSZip extraction fails client-side** (the current Claude path has zero extraction). Recommended order: **#136 → #137 → #135** (or #136+#137 together since #137 mirrors ChatGPT shape). Per CLAUDE.md, #136's new indexed column is a **model change only — no manual migration**.
- **Profile-policy cluster — #152 ↔ #32 ↔ #65 (deferred).** Largely already built in `exports.py`. #65 may be "fixed-by-#32" — verify + close with a regression test. #152 needs a cadence/threshold product decision (codebase uses 80k, issue says 100k — reconcile). Treat as a single later backend-test-anchored effort; not in the early waves.
- **AI-prefs cluster — #143 ↔ #63 ↔ #99 (mostly deferred).** Only **#63** is GOOD and independent (localStorage persistence). #143 (canonical source decision) and #99 (TTS privacy semantics) are decision-gated. Keep #63's storage keys consistent with onboarding (#147) for later.
- **TTS chunking — #140 ↔ #145 ↔ #29.** All touch `adaptive_chunk_text` / chunk-SSE metadata. Decisions open (chapter depths, granular progress value). Sequence together later; not early.
- **Security family — #91, #142 are independent leaf fixes** spun out of the #148 umbrella; ship immediately, no cross-deps.

---

## 6b. Prompt-caching & context-pinning cluster (filed 2026-06-04, post-triage)

A tightly-interdependent family filed during a voice latency/cost design session — **not part of the original 65-issue triage**. Governing insight: **#191 (pin artifacts) is the linchpin** — independently valuable (logical consistency + reproducibility, for both live sessions *and* data exports) **and** a hard pre-condition for the provider-cache and backend-cache work. When picked up, these slot in as their own wave.

### Priority-relevance map

| # | Issue | Relevance | Depends on | Notes |
|---|-------|-----------|------------|-------|
| **#191** | Pin all context artifacts to a per-session snapshot | **Logical consistency + reproducibility** (sessions + exports) | — (standalone) | **Linchpin — do first.** Blocks #187 + #192. Today only the 10k-raw is pinned; profile / recent-context / todos / AI-prefs drift. |
| **#187** | Voice provider prompt caching (Anthropic `cache_control` + finalize pre-warm/3b) | **Both cost + latency** | #191 (hard); de-risked by #192 | Headline win. Gated on #191. |
| **#192** | `backend-cache`: assembled system prompt per session | **Reproducibility (byte-identity) + perf** | #191 (hard) | Guarantees the byte-identity #187 needs (assemble-once-reuse) + removes per-turn re-assembly. |
| **#188** | Stream Claude → progressive-TTS chunker | **Latency only** | — (independent) | No cost change. Composes with #187, blocks nothing. |
| **#189** | OpenAI provider caching (`prompt_cache_key` + cost-accounting fix) | **Cost only** | — (independent) | Also fixes a *current* over-count: we already get OpenAI's cache discount but log full price (`utils/cost.py:10`). |
| **#190** | Text-mode prompt caching | **TBD — placeholder, needs scoping** | would reuse #191 | No recording/finalize window → 3b doesn't map. Decide whether/when it's worth it. |
| #173 *(pre-triage)* | Batch API + prompt caching | **Cost only** | — | Part B (caching) is subsumed by #187/#189 for the agentic path — coordinate. Part A (Batch profile-gen) is separate cost work. |
| #143 *(triaged)* | Reconcile AI-prefs (profile vs artifact) | consistency (AI-prefs) | — | **Related to #191, NOT a blocker.** When it settles canonical storage, align which source #191 pins. |

### Implementation order

1. **#191 — first, independent of the caching work.** Standalone consistency/reproducibility win (sessions + exports) *and* the hard pre-condition for the rest. Decided: all artifacts frozen per-session, uniformly (no per-artifact tiers); the live `propose_todo` drafts still surface via the trailing-notes channel, so freezing the persistent list is acceptable.
2. **#192 — with or just before #187.** Assemble-once-reuse guarantees #187's byte-identity and kills per-turn re-assembly. Its within-turn (warm↔gen) sharing may fold into #187; the cross-turn cache is the separable part.
3. **#187 — the headline both-cost-and-latency win.** Gated on #191, de-risked by #192.
4. **#188 (latency) + #189 (cost) — independent, opportunistic.** Neither is blocked; pick up by whichever axis is the current priority. #189 has standalone value *today* (the over-count fix).
5. **#190 — deferred.** Revisit after #191/#187; reuses #191's pinning. Needs a whether/when decision first.

### How to prioritize by goal
- **Latency:** #191 → #187 (+ #192), with #188 in parallel.
- **Cost:** #189 now (standalone + fixes over-count); #173 Part A (Batch) separately; #187 also cuts multi-turn cost.
- **Correctness / consistency:** #191 stands alone as the highest-value single item — independent of any caching.

---

## 7. Implementation & testing plan — waves

Principle: front-load XS/S high-confidence wins, sequence shared-plumbing, isolate risky/regression-prone items into their own staging cycle. Each wave = one staging integration branch + one Chrome test cycle.

### Wave 1 — Quick wins & leaf fixes (XS/S, independent) — ✅ DONE (merged to main, 2026-05-29; see Section 0)
**Issues:** #142, #62, #63, #134, #98, #66, #28.
**Why grouped:** all small, no cross-dependencies, each independently Chrome-testable (or asset-verifiable). High confidence, low regression surface — perfect first batch to validate the harness.
**Parallel PRs:** all 7 are **separate parallel PRs** (different files/surfaces: nginx, NodeForm tooltip, NodeForm/PrivacySelector persistence, WritePage gating, LandingPage, backend nodes/profile TTS clearing, GlobalAudioPlayer responsive).
**Staging batch:** merge all into `staging-batch-1`, force-push, deploy.
**Chrome test cycle (post-deploy):** Accept terms. (1) Network → hard-reload → assert `Server` version-stripped (#142). (2) Write dialog → "No AI Access" → assert disabled upload/record buttons have `title` (#62). (3) Change privacy + AI-usage, reopen dialog → values restored (#63). (4) Auto-gen OFF → submit text → no AI child; ON → AI child generated (#134). (5) `/landing` across 375/Pixel/iPad/short-desktop → arrow visible (#98). (6) Seed node, generate TTS, edit content, reopen → stale audio gone **and** chunked replay does not resurface old audio (#66). (7) Device emulation 320/375/414 → all player controls visible (#28).
**Note on #66 scope:** touches `nodes.py` `update_node` AND profile.py `set_content`, plus TTSChunk row cleanup — verify chunk deletion doesn't balloon it past S.
**Merge:** each green PR → `main`.

### Wave 2 — Todo cluster (shared toggle path) — ✅ DONE / CLOSED OUT (2026-05-29)
**Issues:** #94, #93, then #108. **(#108 shipped in PR #169; #94 fixed & closed completed; #93 closed won't-implement. Whole cluster resolved — see Section 0.)**
**Why grouped:** #94 and #93 touch the same toggle path — landing them together avoids conflict and lets #93's full-row target sit on #94's stabilized save path. #108 reuses the same `PATCH /todo` path.
**Pre-build (mandatory):** re-confirm root cause against HEAD — #93's fix is in `TodoPage.js` `TodoItem` (not MarkdownBody label); #94/#108 use `PATCH /todo` (not PUT). Identify which verb/handler creates a version row.
**Parallel PRs:** **#94 and #93 developed together but as two PRs** (save-path vs. hit-target), reviewed as a pair. **#108 is a separate PR** sequenced after #94 merges. #129's quick-add wiring waits for #108.
**Staging batch:** `staging-batch-2` = #94 + #93 (test pair), then a follow-up push adding #108.
**Chrome test cycle:** Accept terms, seed a todo with several `- [ ] task` lines under `## Today`. (1) Click empty row area → toggles; full row is the target (#93). (2) Click checkbox → instant visual flip; Network shows **`PATCH /todo`** in background, not blocking (#94); toggle rapidly → each flips instantly. (3) Click "+", type "buy milk", Enter → new item appears under `## Today`, no AI step; reload → persisted; Network shows **`PATCH /todo`** (#108).
**Merge:** green PRs → `main`.

### Wave 3 — Import cluster (shared analyze/confirm plumbing) — ◐ PARTIAL: #137, #135 ✅ DONE (PR #170, 2026-05-29); #136 still pending
**Issues:** #136, #137, #135. **(#137 + #135 done — see Section 0; #136 dedupe is the remaining item and is now unblocked.)**
**Why grouped:** all share the import analyze→confirm pipeline; dedupe key must exist before the Claude port so it inherits it; progress UI wraps both.
**Parallel PRs:** **#136 first** (model column + dedupe — model change only, no manual migration). **#137** (Claude JSZip port) can be developed in parallel but merged after/with #136. **#135** (progress) is a separable frontend-led PR layered on top.
**Staging batch:** `staging-batch-3` with all three. Note: the new column auto-migrates on deploy.
**Chrome test cycle:** Accept terms. (1) Import small ChatGPT export, analyze→confirm, note node count; import same file again → no new nodes (#136). (2) `/import` Claude → upload small Claude `.zip` → Network shows small JSON POST to `/import/claude/analyze`, not raw-zip multipart (#137); complete → conversations appear as nodes. (3) During a non-trivial import → spinner/progress visible until done (#135).
**Caveat to confirm:** exact JSON entry name inside a real Claude export (#137) — use a flexible entry-name match (likely `conversations.json`, as ChatGPT) and define the client-side-extraction-failure fallback; may need a human-supplied sample to confirm.
**Merge:** green PRs → `main`.

### Wave 4 — Timezone plumbing (sequenced, heavier than it looks) — ✅ DONE (#128 + #130; PR #171, 2026-05-29; see Section 0)
**Issues:** #128, then #130.
**Why grouped:** shared `User.timezone` column. Add the column once.
**Effort reality:** #128 is **M spanning many files** (no `to_iso()` chokepoint — sweep every timestamp `.isoformat()`); #130 is L. This wave is heavier than "M + mostly-unit-test." Budget accordingly.
**Parallel PRs:** **#128 first** (backend `Z`/offset on every timestamp serialization + frontend display; add `User.timezone` model column — auto-migrates). **#130 second**, depends on the column (message-prefix render + tz capture/send). Develop #130 concurrently but merge after #128.
**Staging batch:** `staging-batch-4` = #128 (testable in Chrome); #130's verification is primarily unit-test + a Network check.
**Chrome test cycle:** Accept terms, seed a node. (1) DevTools Sensors → override tz to America/Los_Angeles → reload → NodeFooter/feed/version-history times match wall clock (#128); flip to a UTC+ zone → offset flips. (2) For #130: with tz override, trigger a text-mode chat → Network shows tz sent / column populated; assert message-prefix format via unit test (not Chrome).
**Merge:** green PRs → `main`.

### Wave 5 — Cross-app submit + profile notify + favicon — ◐ PARTIAL: #129 ✅ DONE (PR #169, 2026-05-29); #131, #101 still pending
**Issues:** #129, #131, #101. **(#129 done — see Section 0; it also now wires the #108 quick-add inputs on TodoPage and the Voice/Text proposals.)**
**Why grouped:** independent, medium, each with a clean (or stub-driven) Chrome check. #129 now picks up #108's quick-add input (landed in Wave 2) in addition to its independently-shipped existing surfaces.
**Parallel PRs:** all three **separate parallel PRs**.
**Staging batch:** `staging-batch-5`.
**Chrome test cycle:** Accept terms, seed nodes/todo. (1) Cmd+Return in Write / Profile-edit / Todo-edit / Todo quick-add / Login email / Account username → submits; plain Enter = newline; no-op when button disabled (#129). (2) Start profile regen, navigate away, stub `GET /export/profile-status/<id>`→`completed` via DevTools override → assert app-wide toast (#131). (3) DevTools Network / direct favicon URL → served asset is transparent, correct dims/mime; apple-touch unchanged (#101).
**Merge:** green PRs → `main`.

### Wave 6 — Security/keyword/voice-denial + backend-test-only items
**Issues:** #91, #160, #139, #105, #149 (insert-buttons slice), #110, #104.
**Why grouped:** mix of one Chrome-strong fix (#91), one Chrome-testable denial path (#160), one clean frontend slice (#149 insert-buttons), one frontend fix verified mostly by unit test (#105), and **backend/unit-test-anchored** items (#139, #110, #104). The Chrome cycle covers only #91, #160, #149, #105.
**Pre-build (mandatory):** re-confirm #105 against HEAD — `ProposalInline.js` is already interactive; the real gap is `parseOrientResponse` not calling `stripProposalTag` on completed/newTasks/priority bodies.
**Backend-test-only items — no staging slot:** **#139, #110, #104 have zero Chrome signal; their acceptance is local pytest.** Merge them via the per-PR local-test gate (Section 8) **without occupying a staging integration slot** — do not spend a Chrome cycle "confirming no regression" on them.
**Parallel PRs:** all **separate parallel PRs**. #139 before #149 (placeholder semantics before UI exposure).
**Staging batch:** `staging-batch-6` = #91, #160, #149, #105 only.
**Chrome test cycle:** Accept terms. (1) Account → set username "admin"/"loore"/"ADMIN" → 400 + error shown, value unchanged; valid name saves (#91). (2) DevTools → deny mic permission → click mic → clear error toast, no stuck recording UI, no orphaned draft; reset to ask → normal prompt (#160). (3) Write dialog → assert Profile/Archive/Quote insert buttons present; click → token inserted at cursor; explainer lists keywords (#149 slice). (4) Trigger a text-mode agentic proposal → confirm no raw `[todo-proposal:NNNN]` tag visible (best-effort; authoritative check is the unit test) (#105).
**Final single-item pass:** before merging **#91** to `main`, do a dedicated single-item staging push + Chrome re-check (security-critical; a batch sibling must not mask a regression).
**Merge:** green PRs → `main`.

### Wave 7 (optional, isolated) — high-regression / decision-gated
**Issues:** #138 (markdown rendering — touches a component consumed by Feed/NodeDetail/Profile; isolate so a regression is easy to spot), and any unblocked profile-policy verification (#65 verify-and-close-or-fix-by-#32).
**Why isolated:** #138 has broad consumer surface and theme interactions; give it its own staging cycle. Note: the todo list itself renders via `TodoItem`, not MarkdownBody, so #138's checkbox-construct changes do not affect TodoPage directly — but verify any shared markdown styling.
**Chrome test cycle:** Seed a node exercising all markdown constructs → assert styled headings/blockquote/table/hr/lists/code in NodeDetail, Feed, Profile; toggle light/dark and re-verify; confirm checkbox-construct rendering still works where MarkdownBody is used.
**Merge:** green → `main`.

> Items needing product decisions (Section 9) are **not scheduled** until decided: #149 rename, #152/#32/#65 thresholds, #143/#99 canonical-source, #29 granular-progress value, #144 priorities section, voice data-loss cluster (#88/#124/#127/#23), #78 router migration, #147/#146 onboarding/copy, #85 alert channel. (#108's section placement is **resolved** — lands in `## Today`; not gated on #144.)

---

## 8. Per-PR workflow checklist

Repeatable loop for every issue:

1. **Branch from `main`:** `git checkout main && git pull && git checkout -b fix/<issue#>-<slug>`. Never work on `main` directly.
2. **RE-CONFIRM ROOT CAUSE AGAINST HEAD (mandatory for #93, #89, #94, #105, #108, #128).** Several inherited verdict rationales are stale. Before implementing these, grep HEAD and confirm the actual file/markup/verb:
   - **#93:** fix in `TodoPage.js` `TodoItem` (18px `<div onClick>`), NOT a MarkdownBody `<label>` (which doesn't exist).
   - **#94 / #108:** checkbox toggles use `PATCH /todo`; `PUT /todo` is the manual Save button. Identify which creates a version row.
   - **#105 / #89:** `ProposalInline.js` is already interactive (toggles/move/apply shipped). #105's real gap is `parseOrientResponse` not stripping the tag on completed/newTasks/priority bodies. Re-scope #89's remaining work if/when revisited.
   - **#128:** no `to_iso()` exists — locate every `.isoformat()` on a timestamp column (feed.py, export_data.py, serialization.py tombstone path, others).
   For all other issues, a quick HEAD sanity-check is still recommended.
3. **Implement.** Honor CLAUDE.md:
   - **New Celery task?** Register it in `backend/celery_app.py` with `from backend.tasks import <module>  # noqa: F401` — or the worker never picks it up.
   - **Model/column change?** Edit `backend/models.py` only. **Do NOT hand-write Alembic migrations** — deploy auto-generates them (applies to #136, #128/#130's `User.timezone`).
   - Inline React styles using `var(--…)`; respect light/dark `data-theme`. Don't edit dead code (`Dashboard.js`, `StreamingAudioPlayer.js`).
4. **Local verify (CI-blocking + build):**
   - `flake8 backend --count --select=E9,F63,F7,F82 --show-source --statistics` → must be 0.
   - `cd backend && python -m pytest` (conda env `write-or-perish`).
   - Frontend: from `frontend/`, `npm run build 2>&1 | tail -30` → no errors. **Then `cd` back to repo root** before any `git add` (build changes cwd).
5. **Backend-test-only items (#139, #110, #104):** acceptance is the local pytest in step 4 — **skip the staging cycle**; open the PR straight to `main` once green. All other items proceed to step 6.
6. **Batch into the wave's staging branch:** `git checkout staging-batch-N && git merge fix/<issue#>-...` (or merge several). **Force-push to `staging`** (`git push -f origin staging-batch-N:staging`) — force-pushes to staging are routine; just execute.
7. **Wait for deploy:** `deploy-staging.yml` runs (6–15 min, single concurrency — don't trigger a second). Watch via `gh run watch` if needed.
8. **Chrome (reuse live session):** select the existing logged-in browser; navigate to `staging.loore.org`; **first action: click "I Agree"** on the terms modal; **seed the batch's test data**.
9. **Run the wave's Chrome assertions** (Section 7) — DOM, Network (assert correct **verb**), DevTools emulation/overrides as specified per issue.
10. **Security-critical items (#91): final single-item staging pass** before merge — push the item alone to `staging`, re-run its Chrome check, confirm no batch sibling masked a regression.
11. **If green → open PR to `main`** and merge (auto-deploys prod). One PR per issue so prod deploys are independently revertible. Use `gh api -X PATCH repos/<owner>/<repo>/pulls/<n> -F body=@file` for PR bodies (avoid `gh pr edit`'s Projects-classic error). End commit messages with the required `Co-Authored-By` trailer.
12. **Never amend, never force-push to `main`.** Always new commits for fixes.

---

## 9. Open questions / decisions needed before starting

Resolve these before scheduling the gated issues; the waves above proceed without them.

1. **#144 — todo "Today's priorities" section.** Does a pinned `## ` priorities section own the top slot, and how does it interact with the existing `## Today / ## Upcoming / ## Completed recently` template? Blocks #144 only. **#108 is NOT blocked** — it lands quick-added tasks in the existing `## Today` section now; revisit placement if #144's priorities section ships.
2. **#152 / #32 — profile cadence & threshold.** New-user auto-update trigger (every session? every N nodes? time-based?) and reconcile the codebase's `THRESHOLD_TOKENS = 80000` vs the issue's stated 100k. Blocks #152; #32 hardening waits on it.
3. **#143 — canonical AI-preferences source.** Artifact-canonical / profile-canonical / both-with-precedence. Blocks editing `profile_generation.txt` and the chat/voice prompt-assembly path. (#99 — whether profile TTS respects `ai_usage` — depends on this.)
4. **#149 — keyword rename.** Do `{user_export}` → `{user_archive}`? It's a cross-cutting rename touching placeholder constants, `placeholders.py`, export filenames, tests. **Recommended: ship insert-buttons + explainer now, defer the rename.**
5. **#29 — granular TTS progress value.** Is "chunk X of Y" wanted, or is the existing spinner+pulse sufficient (close the issue)? PM call.
6. **#137 — Claude export JSON entry name + extraction fallback.** Confirm the exact filename inside a genuine Claude archive (likely `conversations.json`); prefer a flexible entry-name match and define the fallback when client-side JSZip extraction fails (the current Claude path has zero client extraction). May need a human-supplied sample zip.
7. **#91 — denylist policy details.** Substring vs exact match (block `loore123`?), storage (hardcoded vs config vs DB), and grandfathering the existing `Lore` user. Validation hook is `dashboard.py` `update_user`. Minor; sensible defaults acceptable.
8. **#85 — alert channel + spend cap source.** Email/Slack/in-app, thresholds, and where the Anthropic cap value lives (must become a config/env var). Blocks scheduling.
9. **#94 — which todo verb creates a version row?** Confirm whether `PATCH /todo` (toggle) or `PUT /todo` (Save) inserts a new `UserTodo` version row, so the perf fix targets the right handler. Quick code check, not a product decision — but resolve before building #94.