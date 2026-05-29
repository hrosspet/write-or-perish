# Triage verdicts (sorted)


## GOOD

- **#28** Audio player not fully visible on mobile
  - type=bug | well_defined=medium | cc=yes | chrome=yes | effort=S | surface=frontend
  - files: frontend/src/components/GlobalAudioPlayer.js
  - test: On staging desktop Chrome with DevTools device emulation (e.g. iPhone SE 375px and a 320px viewport): generate TTS on a text node so the global player mounts (it only renders when currentAudio is set - GlobalAudioPlayer returns null otherwise). Assert all controls (play/pause, st
  - why: Confirmed the layout is non-responsive and prone to mobile clipping. GlobalAudioPlayer.js (239 lines) uses a single horizontal flex row with hardcoded widths: title maxWidth 200px (lines 63), progress bar width 150px (line 216), plus a 7-button control cluster and a 70px time rea
  - risk: The player is mounted globally (imported in 2 files) and shares the navbar/header area - verify the responsive change does not break the desktop header layout. Real-device touch behavior (tap targets) is a nice-to-have h

- **#62** Add tooltip to disabled upload and record buttons
  - type=enhancement | well_defined=high | cc=yes | chrome=yes | effort=XS | surface=frontend
  - files: frontend/src/components/NodeForm.js, frontend/src/components/StreamingMicButton.js
  - test: On staging in desktop Chrome: open the Write dialog and set AI usage to 'No AI Access' (the mode that disables the upload + record buttons). Assert the upload button and the record (StreamingMicButton) button are disabled AND now carry an explanatory tooltip (title attribute) suc
  - why: Tiny, crisp UX fix with an unambiguous acceptance criterion: add a title/tooltip explaining why the upload and record buttons are disabled in No-AI mode. The disabling logic already exists in NodeForm.js (audio upload hidden/disabled when not AI-allowed) and StreamingMicButton.js
  - risk: Minor a11y nuance: native browser title tooltips don't show on a fully disabled <button> in some browsers — may need a wrapping span with the title (or aria-disabled + a custom tooltip) so the hint actually surfaces. Con

- **#63** [Idea] Remember AI Usage and Privacy Choices in Write dialog
  - type=enhancement | well_defined=high | cc=yes | chrome=yes | effort=S | surface=frontend
  - files: frontend/src/components/NodeForm.js, frontend/src/components/PrivacySelector.js, frontend/src/contexts/UserContext.js
  - test: On staging in desktop Chrome: open the Write dialog, change the Privacy level and the AI-usage choice away from their defaults, submit (or just close). Re-open the Write dialog and assert both selectors restore the last-used values (persisted via localStorage, mirroring the exist
  - why: Small, well-defined frontend enhancement with a clear acceptance test and an existing in-repo pattern to copy: NodeForm.js already persists the auto-generate toggle to localStorage (loore_auto_generate) with a useState initializer reading from storage. Applying the identical patt
  - risk: Decide persistence scope: localStorage (per-device, matches existing auto-generate pattern, simplest) vs a server-side user default column (cross-device, larger). Recommend localStorage to match precedent. Touches the sa

- **#66** Stale TTS audio persists after content edits (audio_tts_url not cleared)
  - type=bug | well_defined=high | cc=yes | chrome=yes | effort=S | surface=backend
  - files: backend/routes/nodes.py, backend/routes/profile.py, backend/models.py, backend/tasks/tts.py, backend/tests/
  - test: On staging desktop Chrome: (1) open a text node, click the SpeakerIcon to generate TTS (text->audio, no mic needed), wait for audio to be ready; (2) edit the node content and save; (3) re-open the node and assert the stale audio is gone - the speaker control should require regene
  - why: Confirmed root cause matches the issue exactly: backend/models.py has audio_tts_url (2 refs) and a TTSChunk model (1), nodes.py is the node-edit route with heavy TTS logic (tts=44 refs) and the recon map explicitly flags 'audio_tts_url is NOT cleared when node content is edited (
  - risk: Must clear BOTH the audio_tts_url column AND the per-chunk TTSChunk records (chunked playback reads those), or stale chunked audio will still replay - clearing only the scalar URL is insufficient. The issue also asks for

- **#91** [Critical] Implement protected usernames list
  - type=feature | well_defined=high | cc=yes | chrome=yes | effort=S | surface=both
  - deps: [148, 146]
  - files: backend/routes/dashboard.py, backend/routes/auth.py, frontend/src/pages/AccountPage.js, backend/utils/auth_helpers.py
  - test: Read verbatim (lines 1235-1274): add a reserved/protected-username denylist (loore/lore/admin/api/root/hrosspet/etc., plus route-collision names) enforced server-side. The exact slot is CONFIRMED: backend/routes/dashboard.py PUT /dashboard/user (lines 188-205) already validates u
  - why: Read verbatim. Bounded, well-understood denylist pattern, and the repo makes it concrete: the username-write validation already lives in one place (dashboard.py:188-205) with case-insensitive comparison plumbing, and the frontend has a usernameValid() hook (AccountPage.js:22) for
  - risk: Signup paths also need coverage: Twitter OAuth (auth.py:62) takes the Twitter screen_name verbatim and magic-link signup uses generate_unique_username(email) — a denylisted screen_name could slip in at signup, so enforce

- **#93** Todo checkbox hit target too small — clicking anywhere on the row should toggle
  - type=enhancement | well_defined=high | cc=yes | chrome=yes | effort=S | surface=frontend
  - deps: [94]
  - files: frontend/src/components/MarkdownBody.js, frontend/src/pages/TodoPage.js
  - test: On staging in desktop Chrome: open the Todo page (seed a todo with a couple of '- [ ] task' items after the deploy DB reset, e.g. via the Edit textarea, or via the existing list). Click in the empty area of a task ROW (not directly on the small checkbox input) and assert the chec
  - why: Verified the exact cause in frontend/src/components/MarkdownBody.js: task items render as `<li style={{listStyle:'none', marginLeft:'-1.5em'}}><label style={{display:'flex',...}}><input.../><span>{children}</span></label></li>`. The label (clickable area) only spans input+text, s
  - risk: Make the whole row a single click target without breaking text selection or nested links inside the task text, and without double-firing the onChange. Mind light-mode/data-theme styling. The 'feels painful' framing pairs

- **#94** Todo checkbox toggle has ~1 second delay — needs performance optimization
  - type=bug | well_defined=high | cc=yes | chrome=yes | effort=M | surface=both
  - files: frontend/src/utils/markdown.js, frontend/src/pages/TodoPage.js, frontend/src/components/MarkdownBody.js, backend/routes/todo.py
  - test: On staging in desktop Chrome: open the Todo page with several task items, click a checkbox and observe whether the visual state flips immediately or lags. Use DevTools Performance/console timestamps to measure click->repaint, and the Network tab to confirm the PUT /todo happens i
  - why: Read the code: useCheckboxToggle in frontend/src/utils/markdown.js ALREADY does an optimistic setContent(newContent) before awaiting api.put, so the 'waiting for server' theory is partly wrong — the real cost is likely the re-render path: TodoPage re-runs parseSections over the F
  - risk: Root cause needs profiling to confirm (re-render vs. server round-trip vs. layout thrash) — the optimistic update already exists, so a naive 'add optimistic UI' fix would be wrong. Per-toggle backend versioning could be 

- **#98** Landing page arrow barely visible and off-screen on some viewports
  - type=bug | well_defined=medium | cc=yes | chrome=yes | effort=S | surface=frontend
  - files: frontend/src/components/LandingPage.js
  - test: On staging in desktop Chrome using DevTools device emulation: open /landing and cycle through several viewport sizes (e.g. iPhone SE 375x667, Pixel 5, iPad, and a short/wide desktop). Assert the blinking scroll-down arrow is fully on-screen and visible (not clipped off the bottom
  - why: Responsive CSS bug confined to a single file (frontend/src/components/LandingPage.js, the landing hero with a blinking scroll arrow). Acceptance is concrete: arrow always visible when text fits, fix the excessive mobile top margin and missing bottom margin. Per the testing constr
  - risk: Fix is empirical (margins, vh math, safe-area insets) and must be checked across multiple emulated viewports; risk of fixing one size and breaking another. Real iOS Safari dynamic-toolbar height (100vh quirk) can differ 

- **#101** Logo/favicon has square background in Chrome tab
  - type=bug | well_defined=high | cc=partial | chrome=partial | effort=S | surface=frontend
  - files: frontend/public/index.html, frontend/public/favicon.ico, frontend/public/manifest.json
  - test: Partial. The visible effect is the browser TAB icon (favicon), which is chrome-painted and not part of the page DOM, so the automation extension can't reliably screenshot the tab strip. Best available checks: (1) confirm in desktop Chrome DevTools Network that the served favicon 
  - why: Crisp, well-specified bug: make the tab favicon transparent (round/transparent outside), leave the apple-touch icon's filled background untouched. Scope is clear (swap/regenerate the favicon asset + ensure index.html/manifest point at the right files). It is GOOD on definition bu
  - risk: Don't regress the apple-touch icon (issue is explicit it should keep its filled background). Favicon caching is aggressive in Chrome — hard-reload / cache-bust needed to verify. The actual 'looks right in the tab' judgme

- **#108** Add quick-add task button to todo list view
  - type=feature | well_defined=high | cc=yes | chrome=yes | effort=M | surface=both
  - deps: [129, 144, 94]
  - files: frontend/src/pages/TodoPage.js, frontend/src/utils/markdown.js, backend/routes/todo.py
  - test: On staging in desktop Chrome: open the Todo page, click the new '+' quick-add control, type 'buy milk', press Enter, and assert a new '- [ ] buy milk' item appears immediately in the list with NO AI proposal/confirmation step. Reload the page and confirm it persisted (new UserTod
  - why: Well-defined: '+' button -> input -> Enter -> append item -> save, explicitly bypassing the AI flow. The plumbing already exists: TodoPage parses todo.content markdown sections, useCheckboxToggle/parseTodoItems manipulate the markdown, and backend/routes/todo.py PUT /todo saves a
  - risk: Small product call: which section new tasks land in (top, an Inbox, or end) — coordinate with #144's 'Today's priorities' pinned-top section so they don't fight over the top slot. Reuse the existing PUT /todo versioning 

- **#128** Timestamps display in UTC instead of user's local timezone
  - type=bug | well_defined=high | cc=yes | chrome=yes | effort=M | surface=both
  - deps: [130]
  - files: backend/utils/serialization.py, backend/routes/feed.py, backend/routes/ai_preferences.py, backend/routes/export_data.py, frontend/src/components/NodeFooter.js, frontend/src/utils/date.js
  - test: On staging, open DevTools > Sensors and override the timezone to America/Los_Angeles (UTC-7/8). Reload, create a new node, and read the creation time in NodeFooter (Feed + node detail). Assert the displayed time matches the wall-clock time in the overridden timezone (currently it
  - why: Root cause confirmed: backend/utils/serialization.py to_iso() returns dt.isoformat() with no 'Z' suffix on naive UTC datetimes, so JS new Date() parses them as local time -> systematic offset, exactly as the issue states. Fix is a clear backend change (append Z / emit explicit UT
  - risk: STALE PATH: the issue cites frontend/src/components/Dashboard.js:349, but Dashboard.js is DEAD CODE per project memory - the live profile timestamp is in ProfilePage.js; do not edit Dashboard.js. Prefer the backend Z-suf

- **#129** Cmd+Return / Ctrl+Enter keyboard shortcut for primary submit across the app
  - type=enhancement | well_defined=high | cc=yes | chrome=yes | effort=M | surface=frontend
  - deps: [108]
  - files: frontend/src/hooks/useSubmitShortcut.js, frontend/src/components/NodeForm.js, frontend/src/pages/ProfilePage.js, frontend/src/pages/PromptDetailPage.js, frontend/src/pages/TodoPage.js, frontend/src/components/LoginPage.js
  - test: On staging in desktop Chrome (macOS session, so use Cmd+Return): (1) Open the Write/new-entry form, type content, focus the textarea, press Cmd+Return, assert the node is created (same as clicking Send). (2) Verify plain Enter still inserts a newline in the textarea. (3) ProfileP
  - why: Excellent sweet-spot issue with an exhaustive spec (exact files + lines, a ready-to-use shared useSubmitShortcut hook, explicit excluded surface SearchModal, plain-Enter-still-newline rule). Verified against the repo: all six target files exist; useSubmitShortcut.js does NOT exis
  - risk: Minor inaccuracy in the issue: metaKey/ctrlKey IS used elsewhere (Cmd+click open-in-new, Cmd+K), just not for Enter-submit — confirm the new keydown handler doesn't collide. Broad-but-shallow change across many surfaces;

- **#131** Notify user when profile generation completes
  - type=enhancement | well_defined=high | cc=yes | chrome=partial | effort=M | surface=frontend
  - files: frontend/src/components/NavBar.js, frontend/src/pages/ProfilePage.js, frontend/src/contexts/ToastContext.js, frontend/src/hooks/useAsyncTaskPolling.js, frontend/src/App.js
  - test: On staging.loore.org in desktop Chrome: from any page click the NavBar 'Generate/Regenerate profile' action (NavBar.js already POSTs /export/update_profile and stores loore_profile_task_id), then NAVIGATE AWAY to Home/Feed and assert a completion toast/banner appears app-wide whe
  - why: Self-contained UI gap with clear acceptance. Today the polling + completion handling lives only in ProfilePage (useAsyncTaskPolling against /export/profile-status/<task_id>) and shows an inline pulsing <span>, not a toast; NavBar already owns the dispatch + loore_profile_task_id 
  - risk: Must dedupe so a user already on ProfilePage isn't notified twice (ProfilePage polls AND a new global host would poll) -- consolidate to one poller. The existing 'loore_profile_done' window event is the natural single so

- **#134** Text mode auto-generates first response even when auto-generate is off
  - type=bug | well_defined=high | cc=yes | chrome=yes | effort=S | surface=frontend
  - files: frontend/src/pages/WritePage.js, frontend/src/components/NodeForm.js, frontend/src/components/NodeDetail.js
  - test: On staging in desktop Chrome: in the Write entry form set Auto-generate OFF (toggle persists to localStorage key loore_auto_generate). Submit a text entry. Assert NO AI child response is auto-created on the first turn (no pending/'Generating' LLM node appears; you land on a plain
  - why: Tightly scoped bug with a code-verified root cause. WritePage.js handleSubmit unconditionally POSTs to /textmode/start whenever ai_usage is AI-allowed (it checks only isAiAllowed, NOT loore_auto_generate — grep confirms WritePage has zero auto_generate references), whereas NodeFo
  - risk: Cleanest fix lets NodeForm own the gating: call /textmode/start only when auto-generate is on, else POST /nodes/ (the craft-mode fallback path already exists in WritePage.handleSubmit). Verify no regression where an auto

- **#135** Show progress indicator during data imports
  - type=enhancement | well_defined=medium | cc=yes | chrome=yes | effort=M | surface=both
  - deps: [137]
  - files: frontend/src/components/ImportData.js, backend/routes/import_data.py
  - test: On staging.loore.org /import, start a ChatGPT/Claude import of a non-trivial export and assert a spinner or progress bar appears immediately and stays visible until completion (rather than the UI appearing frozen). If a coarse 'processing X of Y' counter or stage label (parsing -
  - why: Clear user-facing problem with an observable UI outcome in desktop Chrome. The surface (frontend/src/components/ImportData.js) is confirmed to own all import dialogs and the analyze/confirm calls, and already carries an 'importing' boolean. A basic in-flight spinner/loading state
  - risk: Scope ambiguity drives effort: a spinner is XS and frontend-only and fully Chrome-testable; a real progress counter requires reworking the import endpoints to emit progress (SSE or polling), making it backend+frontend an

- **#136** Deduplicate imported data (re-imports + snapshot overlap)
  - type=bug | well_defined=medium | cc=yes | chrome=yes | effort=M | surface=both
  - deps: [137, 135]
  - files: backend/models.py, backend/routes/import_data.py, frontend/src/components/ImportData.js, backend/tests/test_chatgpt_import.py
  - test: On staging.loore.org /import, import a ChatGPT (or Claude) export, run analyze->confirm, and note the created node count in the Log/Feed. Then import the SAME export again and run analyze->confirm: assert NO new nodes are created (count unchanged) and ideally the UI reports dupli
  - why: Real, verified gap: the import confirm endpoints (/import/claude/confirm import_data.py:627, /import/chatgpt/confirm:1248) consume request.get_json() and rebuild Node rows from the analyzed payload with no uniqueness check; the Node model has no import-source-id column. The dedup
  - risk: Per CLAUDE.md, do NOT write a manual Alembic migration — change the model and let deploy.sh auto-generate it; a hand-written migration is a process violation and can conflict. Uniqueness must be scoped per user, not glob

- **#137** Claude import: handle oversized zip files (port the ChatGPT-import fix)
  - type=bug | well_defined=high | cc=yes | chrome=partial | effort=M | surface=both
  - deps: [136, 135]
  - files: frontend/src/components/ImportData.js, backend/routes/import_data.py, backend/tests/test_chatgpt_import.py
  - test: On staging.loore.org open /import and click the Claude import option. Use the file picker to upload a real Claude export .zip (a small one is fine). With the fix, the browser extracts the conversations JSON client-side via JSZip and POSTs only the JSON blob to /import/claude/anal
  - why: Extremely well-specified and verified against the code. The fix is a port of an already-merged ChatGPT fix (commit f6d329e): handleChatGPTImportFile (ImportData.js:236) already does JSZip.loadAsync (line 246), finds the conversations.json entry (line 248), and POSTs only the blob
  - risk: One real verification gap: the exact JSON entry name inside a Claude export must be confirmed against a genuine Claude archive (the issue flags this; likely conversations.json, same as ChatGPT, but should use a flexible 

- **#138** Improve markdown rendering to match standard markdown expectations
  - type=enhancement | well_defined=medium | cc=yes | chrome=yes | effort=M | surface=frontend
  - files: frontend/src/components/MarkdownBody.js, frontend/src/index.css
  - test: On staging, create a node whose content exercises all markdown constructs: # h1, ## h2, ### h3, **bold**, *italic*, a blockquote (> ...), a fenced code block, an inline `code` span, an ordered + unordered list, a GFM table, and a horizontal rule. Save and open the node in NodeDet
  - why: MarkdownBody.js (react-markdown ^9 + remark-gfm ^4) currently defines custom components ONLY for p/ul/li/code/pre/a - headings, blockquotes, tables, hr, em/strong fall back to unstyled browser defaults, which is exactly the 'is this rendering correctly?' feeling the issue describ
  - risk: Must respect existing inline-style + CSS-var design system (--serif/--sans, accent tokens) and the light/dark theme override - test both themes. Keep the clickable-checkbox behavior (onCheckboxToggle / toggleCheckbox) in

- **#142** Strip Server header from nginx responses (info leak)
  - type=infra | well_defined=high | cc=yes | chrome=yes | effort=XS | surface=infra
  - deps: [148]
  - files: frontend/nginx.conf
  - test: Add `server_tokens off;` to frontend/nginx.conf (verified: 31-line file, currently NO server_tokens directive). Force-push to staging; the nginx:alpine staging container bind-mounts this conf read-only (docker-compose.staging.yml), so it applies on next deploy. Verify in desktop 
  - why: Read verbatim (lines 534-551) and confirmed against the repo: frontend/nginx.conf exists, is 31 lines, and contains zero `server_tokens` occurrences (grep exit 1 / count 0) — so this is a genuine one-line change. Explicit, observable acceptance criterion (Server header absent/ver
  - risk: Issue asks for BOTH the prod VM nginx and the staging nginx-alpine container; prod nginx is a VM/systemd config that may live outside the repo and require a manual deploy step you cannot drive from Chrome, but the stagin

- **#160** Voice recording silently fails when browser mic permission isn't granted (Android, DuckDuckGo)
  - type=bug | well_defined=high | cc=yes | chrome=partial | effort=S | surface=frontend
  - deps: [88]
  - files: frontend/src/hooks/useStreamingMediaRecorder.js, frontend/src/components/StreamingMicButton.js, frontend/src/hooks/useStreamingTranscription.js, frontend/src/contexts/ToastContext.js
  - test: Desktop Chrome on staging (auth solved): open the voice/Write entry that mounts StreamingMicButton. (1) In DevTools, deny microphone permission for staging.loore.org, click the mic button, assert a clear error toast/modal appears AND the UI does NOT enter the recording visual sta
  - why: Precise, well-scoped: check navigator.permissions.query({name:microphone}) and catch NotAllowedError/PermissionDeniedError at MediaRecorder start, show an actionable toast, ensure no orphaned draft / stuck recording UI. Pure-frontend, CC end-to-end. Crucially the failure path (pe
  - risk: Success/transcription path needs real audio (not testable), but the FIX is the denial path which IS Chrome-testable; partial only due to the Android-instructions copy. Shares the silent voice data-loss surface and toast 


## BORDERLINE

- **#18** Status bar shows unclear 40% initially
  - type=enhancement | well_defined=low | cc=partial | chrome=partial | effort=S | surface=frontend
  - files: frontend/src/components/NavBar.js
  - test: Partial/blocked-on-discovery. I could NOT locate the '40% status bar' in the current frontend via grep (no statusBar/StatusBar/progress/'40%' match in NavBar/Feed or elsewhere) — it appears to predate the redesign and may already be gone or renamed. First step is to confirm wheth
  - why: Old (Dec 2025), vaguely-worded enhancement: 'status bar shows 40% with no context.' It is under-specified (what the bar measures is unknown) and, critically, I could not find the corresponding code in the current codebase — the design system was overhauled since, so the element m
  - risk: Likely stale: no matching code found via grep; may already be resolved by the redesign — verify on staging before scheduling work. If it exists, deciding what the percentage represents (profile completeness? onboarding p

- **#23** Voice note re-recording is broken (second take fails)
  - type=bug | well_defined=medium | cc=partial | chrome=partial | effort=M | surface=frontend
  - deps: [160]
  - files: frontend/src/hooks/useStreamingMediaRecorder.js, frontend/src/hooks/useVoiceSession.js, frontend/src/components/StreamingMicButton.js
  - test: Desktop Chrome (auth solved) CAN observe the recorder LIFECYCLE/state machine without transcription quality: start a take (mic button -> recording state), stop/discard, start a SECOND take, assert via UI state + console/Network that a fresh MediaRecorder + mic stream is created, 
  - why: Issue hypothesizes MediaRecorder/mic-stream lifecycle not fully reset between takes - a deterministic state-machine bug, not audio quality. CC can implement a clean reset (stop tracks, null recorder, clear chunks/draft) and the broken-vs-fixed lifecycle (does take 2 start a valid
  - risk: May be partially stale/already addressed by later streaming-recorder work (useStreamingMediaRecorder family). Lifecycle/reset failure is Chrome-observable (state + network); transcript correctness is not. Confirm current

- **#29** No progress indicator for audio generation from text (TTS)
  - type=enhancement | well_defined=medium | cc=yes | chrome=yes | effort=S | surface=both
  - deps: [145]
  - files: frontend/src/components/SpeakerIcon.js, frontend/src/contexts/AudioContext.js, frontend/src/components/GlobalAudioPlayer.js, frontend/src/hooks/useSSE.js, backend/tasks/tts.py
  - test: On staging desktop Chrome: open a long text node, click the speaker icon to start TTS generation (text->audio, no mic). Assert a generating indicator appears - SpeakerIcon already animates (generat=12, spinner=3 refs) and GlobalAudioPlayer shows a pulsing 'Generating more audio..
  - why: Already PARTIALLY shipped, which lowers value/clarity. Confirmed in code: GlobalAudioPlayer.js renders a generatingTTS pulse indicator (lines 198-209), AudioContext exposes generatingTTS/setGeneratingTTS, and SpeakerIcon.js (302 lines) has spinner/animation logic (generat=12, spi
  - risk: Risk of low marginal value: a reviewer/PM should decide whether the existing spinner+pulse already satisfies the issue (then close it) or whether granular chunk-count progress is genuinely wanted. The granular path needs

- **#32** Feature: Incremental user profile generation with context window management
  - type=feature | well_defined=medium | cc=yes | chrome=partial | effort=L | surface=backend
  - deps: [65, 152]
  - files: backend/tasks/exports.py, backend/utils/tokens.py, backend/routes/export_data.py, backend/tests/test_exports_incremental.py
  - test: Primarily verify by backend unit test -- test_exports_incremental.py already exists as the pattern. Assert: token counting (approximate_token_count), chunk budgeting (CHUNK_BUDGET=90000 / MIN_CHUNK_TOKENS=80000), the chunked loop (_chunked_profile_loop, _do_iterative_incremental_
  - why: Large parts of this feature are ALREADY implemented in backend/tasks/exports.py: CHUNK_BUDGET/MIN_CHUNK_TOKENS constants, generation_type field on UserProfile (initial/iterative/update/integration/revert), _do_incremental_update / _do_iterative_incremental_update / _chunked_profi
  - risk: Iterative-merge quality (does the merged profile stay coherent across chunks?) is hard to unit-test and needs human review of output. The PromptTooLongError retry/reduce_export_tokens path overlaps with #65. Real large-c

- **#65** [Bug] Generating Profile: Prompt is too long
  - type=bug | well_defined=medium | cc=partial | chrome=no | effort=M | surface=both
  - deps: [32, 131]
  - files: backend/tasks/exports.py, backend/llm_providers.py, backend/utils/tokens.py, frontend/src/pages/ProfilePage.js, backend/tests/test_exports_incremental.py
  - test: Not cleanly testable on desktop Chrome: reproducing the bug needs a real ~700k-token import / >200k-token prompt, which cannot be deterministically created in a browser session and is wiped on every staging deploy. Best available check is a backend unit test feeding an oversized 
  - why: The core fix (don't send one giant request) appears largely present: generate_user_profile wraps the LLM call in a MAX_RETRIES loop that catches PromptTooLongError and shrinks the export via reduce_export_tokens (exports.py:151-162), and the broader chunked/incremental machinery 
  - risk: Heavy overlap with #32 -- should be worked together (or #65 verified-and-closed as fixed-by-#32 with a regression test). reduce_export_tokens after 3 attempts re-raises, and ProfileGenerationTask.on_failure (exports.py:7

- **#78** Block SPA navigation during active recording (requires createBrowserRouter migration)
  - type=enhancement | well_defined=high | cc=yes | chrome=partial | effort=L | surface=frontend
  - deps: [23, 160]
  - files: frontend/src/index.js, frontend/src/App.js, frontend/src/components/NavBar.js, frontend/src/hooks/useVoiceSession.js, frontend/src/components/StreamingMicButton.js
  - test: Desktop Chrome (auth solved) CAN verify most of this WITHOUT real audio, since the block is gated on recording-active STATE not captured audio: (1) enter recording state (mic button -> recording UI; Chrome grants permission so state flips). (2) Click a NavBar link -> assert a con
  - why: Very well-specified: migrate BrowserRouter (per issue body in index.js/App.js) to createBrowserRouter, add useBlocker gated on active recording, keep beforeunload. CC can implement the whole thing with repo access. The blocking confirm-dialog is gated on recording-active STATE (n
  - risk: The router migration is the real risk: createBrowserRouter changes the whole routing tree and can break ProtectedRoute, modals (NodeFormModal/SearchModal), and deep links - needs a full staging-Chrome regression pass. Th

- **#85** Add Anthropic API spend monitoring and alert before spending limit is reached
  - type=feature | well_defined=medium | cc=partial | chrome=partial | effort=M | surface=backend
  - deps: [148]
  - files: backend/models.py, backend/routes/admin.py, backend/tasks/llm_completion.py, backend/config.py
  - test: Read verbatim (lines 1322-1333). Mostly backend/ops with a weak Chrome surface. CONFIRMED substrate: backend/models.py:536 defines APICostLog (request_type, input_tokens, output_tokens, cost_microdollars, user_id, model_id, created_at), and an existing backend/scripts/cost_breakd
  - why: Read verbatim. The data layer exists (APICostLog + cost_breakdown.py), so the local-tracking approach is fully CC-implementable with tests. Marked BORDERLINE not GOOD because: (a) it's fundamentally a backend/ops monitoring feature whose primary effect (an alert before hitting th
  - risk: Read verbatim. Decide data source: local APICostLog token-cost sum (recommended, self-contained, CC-doable) vs polling Anthropic's usage/billing API (org-level, needs an admin API key = a secret-management/ops concern, t

- **#88** Voice recording silently drops last audio chunk from lock screen after phone-call interruption
  - type=bug | well_defined=medium | cc=partial | chrome=partial | effort=M | surface=both
  - deps: [160, 127]
  - files: frontend/src/hooks/useStreamingMediaRecorder.js, frontend/src/hooks/useStreamingTranscription.js, frontend/src/components/StreamingMicButton.js, backend/routes/drafts.py
  - test: Desktop Chrome can test the FLUSH plumbing but not the real interruption: (1) start a recording, dispatch visibilitychange/pagehide via DevTools (or switch tabs) and assert the code calls MediaRecorder.requestData() and the final chunk upload POST fires (Network tab). (2) Assert 
  - why: Concrete direction (requestData() flush on pause/visibilitychange/pagehide, possibly sendBeacon for the last chunk, recovery picks up uploaded chunks). The visibility/pagehide flush logic IS implementable and partially Chrome-testable (lifecycle events dispatchable for the upload
  - risk: Real-world event (incoming call) + mobile lock-screen = not reproducible on staging-Chrome; only the lifecycle-flush mechanism is. Shares the silent-drop data-loss surface with #127/#160 and the recorder/upload plumbing.

- **#89** Make proposed todo changes interactive / editable before applying
  - type=feature | well_defined=high | cc=yes | chrome=partial | effort=M | surface=frontend
  - deps: [97]
  - files: frontend/src/components/ProposalInline.js, frontend/src/components/MarkdownBody.js, frontend/src/pages/ConversePage.js, frontend/src/utils/markdown.js
  - test: Partial. The editable-proposal UI is rendered by ProposalInline (imported in frontend/src/pages/ConversePage.js) and is desktop-Chrome-observable, but SURFACING a proposal requires the LLM to emit a propose_todo tool call during a session. In text mode this needs no mic (drive a 
  - why: Confirmed the current state in frontend/src/components/ProposalInline.js: the proposal renders as read-only MarkdownBody blocks (no onCheckboxToggle) with only Apply/Dismiss. The requested change (uncheck/edit/remove items before applying) is a well-scoped frontend rework using t
  - risk: Need to define how the edited proposal is applied: currently Apply triggers the backend LLM merge (orient_apply_todo.txt via voice_todo_merge.py) using the LLM's free-text summary, not a structured diff — making the prop

- **#97** Completed todo items are not removed when proposed for deletion
  - type=bug | well_defined=medium | cc=partial | chrome=partial | effort=S | surface=backend
  - deps: [89, 144, 107]
  - files: backend/prompts/orient_apply_todo.txt, backend/tasks/voice_todo_merge.py
  - test: Partial. End result (completed items removed after a removal proposal is applied) is visible on the Todo page in desktop Chrome, but reproducing requires an LLM proposal that marks [x] items for removal AND the LLM merge honoring it — both non-deterministic. Deterministic verific
  - why: Confirmed the root cause by reading backend/prompts/orient_apply_todo.txt: it has rules for completing/adding/keeping tasks and an explicit 'Do not remove tasks unless explicitly told they should be removed' (line 11), but NO instruction describing how to actually delete items wh
  - risk: It's prompt engineering against an LLM, so a prompt tweak may not be 100% reliable across models (ties into #107's model-sensitivity). Safer long-term is a structured/parsed removal step rather than relying on the merge 

- **#102** [Safari] TTS chunks after first 4s not added to playback queue
  - type=bug | well_defined=medium | cc=yes | chrome=no | effort=M | surface=frontend
  - deps: [29, 66]
  - files: frontend/src/contexts/AudioContext.js, frontend/src/components/GlobalAudioPlayer.js, frontend/src/components/SpeakerIcon.js, frontend/src/hooks/useSSE.js
  - test: NOT desktop-Chrome verifiable: the bug is Safari-only (the issue explicitly says playback works in Chrome). The whole point is Safari's autoplay/HTMLMediaElement gating, which desktop Chrome does not reproduce. Best available checks: (1) on staging desktop Chrome, generate TTS on
  - why: Re-read of the issue corrects my earlier framing: this is NOT a streaming/range bug, it is a Safari-specific failure where only the first ~4s TTS chunk plays and later chunks never enter the playback queue. The relevant code is heavily Safari-aware already: AudioContext.js has ex
  - risk: Critical caveat: Safari-only, so the automation extension (desktop Chrome) cannot reproduce or confirm the fix - this is the main reason it is BORDERLINE rather than GOOD. The autoplay/queue code is subtle and already pa

- **#104** _call_llm_with_retries never passes max_tokens to LLM provider
  - type=bug | well_defined=high | cc=yes | chrome=no | effort=XS | surface=backend
  - files: backend/tasks/exports.py, backend/tests/test_export_data.py
  - test: Not desktop-Chrome observable: this only changes the max_tokens cap on profile-generation LLM calls, which has no distinct rendered UI surface and the difference (a slightly different output-token ceiling) is non-deterministic in the model's response. VERIFY BY UNIT TEST: in back
  - why: Confirmed real and trivial: _call_llm_with_retries (backend/tasks/exports.py:178) accepts max_output_tokens but its provider.get_completion(...) call omits max_tokens, so the provider falls back to its own default. get_completion (llm_providers.py:44) does accept a max_tokens kwa
  - risk: PATH CLARIFICATION: the function lives in backend/tasks/exports.py, NOT llm_completion.py - the recon map's max_tokens discussion conflated profile-gen budgeting with completion. Passing max_tokens could in rare cases ca

- **#105** Proposal tag / note ID leaking into UI display
  - type=bug | well_defined=medium | cc=yes | chrome=partial | effort=S | surface=both
  - deps: [144]
  - files: frontend/src/components/ProposalInline.js, backend/prompts/agentic.txt
  - test: Partial. The leak is the [todo-proposal:NNNN] / [issue-proposal:NNNN] tag the agentic prompt tells the model to emit (agentic.txt:36). Frontend already has stripProposalTag() in ProposalInline.js (regex /\s*\[\w+-proposal:[^\]]*\]/g) but applies it ONLY to the Note/Issue-Title/De
  - why: After reading the code the leaking artifact is identified: the [todo-proposal:NNNN]/[issue-proposal:NNNN] tag defined in agentic.txt:36, with an existing stripProposalTag() helper that is applied inconsistently (only some parsed sections, not the main prose or the task/priority l
  - risk: Two possible layers: frontend (stripProposalTag not applied to main prose / Completed-New Tasks-Priority lines) and/or prompt (model emitting the tag where the user can see it). Prefer the frontend fix as authoritative s

- **#106** [iPhone] Todo form zooms in unnaturally on focus
  - type=bug | well_defined=high | cc=yes | chrome=no | effort=XS | surface=frontend
  - deps: [108]
  - files: frontend/src/pages/TodoPage.js, frontend/src/index.css
  - test: The actual symptom (iOS Safari auto-zoom on inputs with font-size < 16px) does NOT reproduce in desktop Chrome and cannot be confirmed via the automation extension. Best available checks: (a) DevTools — inspect the todo Edit textarea computed style and assert font-size >= 16px af
  - why: Root cause and fix are crisp and confirmed: the todo Edit textarea in frontend/src/pages/TodoPage.js uses `fontSize: 14`, and iOS Safari auto-zooms any focused input/textarea under 16px. Bump to >=16px (or add the standard global rule). Trivial CSS, cc_implementable yes, well_def
  - risk: Apply the >=16px rule to ALL todo input surfaces (the Edit textarea now, plus any #108 quick-add input later) without regressing intended desktop sizing. Related to #132 (iPhone Chrome todo save failures) — same iOS todo

- **#110** Raw context export excludes user's replies in other users' threads
  - type=bug | well_defined=medium | cc=yes | chrome=partial | effort=M | surface=backend
  - files: backend/routes/export_data.py, backend/tests/test_user_export_placeholder.py
  - test: This is a backend correctness fix to _preselect_node_ids (the recursive CTE in backend/routes/export_data.py) so the export/raw-context selection also seeds from nodes where user_id == target_user with parent_id NOT NULL (the user's replies inside other users' threads). Hard to e
  - why: Tightly scoped backend bug with a concrete, named fix location: _preselect_node_ids in backend/routes/export_data.py uses a recursive CTE whose base case is the user's top-level nodes only (user_id == target AND parent_id IS NULL), excluding the user's replies inside other users'
  - risk: Token-budget blowup is the real risk: seeding from all of the user's nodes (including deep replies in large foreign threads) can substantially increase export size; the fix must respect the existing token budget and deci

- **#124** Recovery flow: resumed recordings drop pre-resume audio at finalize merge
  - type=bug | well_defined=high | cc=yes | chrome=no | effort=M | surface=backend
  - deps: [127, 88]
  - files: backend/routes/drafts.py, backend/utils/webm_utils.py, backend/tasks/streaming_transcription.py, backend/utils/audio_storage.py
  - test: Not verifiable via desktop Chrome end-to-end: the documented repro (record 40s so chunks 0/1 upload, refresh, click Continue on recovery banner, record more, stop, assert transcript contains BOTH pre- and post-resume audio) requires real mic audio across two MediaRecorder subsess
  - why: Exceptionally well-specified bug with precise root-cause analysis and a ~50-line backend fix plan naming functions (upload_streaming_chunk init-detection, persist_init_segment index suffix, transcribe_chunk_batch sub-batch partitioning, audio_storage init filter). CC can implemen
  - risk: Backend-only fix with airtight spec but zero deterministic staging-Chrome signal (mic-audio dependency). All four files are confirmed in the issue body with function-level precision. Ship with fixture-based unit tests (r

- **#127** Voice recording silently drops content after 60-minute mark
  - type=bug | well_defined=medium | cc=partial | chrome=no | effort=M | surface=both
  - deps: [88, 124]
  - files: frontend/src/hooks/useStreamingMediaRecorder.js, frontend/src/hooks/useStreamingTranscription.js, backend/routes/drafts.py, backend/tasks/streaming_transcription.py, frontend/src/components/StreamingMicButton.js
  - test: Not testable end-to-end via desktop Chrome: reproducing the 60-minute drop requires an actual ~60+ minute recording (recon lists duration-limit as NOT testable - cannot inject deterministic long mic audio nor wait 60 real minutes). A pre-end WARNING UI (warn at ~55 min, graceful 
  - why: Concrete expected behavior (no hard limit OR warn-before + graceful stop + recover unprocessed chunks). CC can implement a duration warning and harden boundary/finalize handling, but verification is intrinsically a 60-minute real-audio event the staging-Chrome automation cannot p
  - risk: Long-duration real-world/time dependency + real mic audio = not staging-Chrome-testable for the actual drop. Shares the silent-data-loss surface with #88/#160 and finalize/recovery plumbing with #124. The warning-UI sub-

- **#130** LLM has no temporal grounding — confabulates relative times
  - type=bug | well_defined=high | cc=partial | chrome=partial | effort=L | surface=both
  - deps: [128]
  - files: backend/tasks/llm_completion.py, backend/models.py, backend/routes/auth.py, frontend/src/contexts/UserContext.js, frontend/src/api.js
  - test: The core fix (LLM correctly reasoning about elapsed time) is NOT deterministically Chrome-testable - it depends on model output and, for voice, on mic audio that the automation extension cannot feed. PARTIAL/HTTP-observable checks: (1) After adding User.timezone capture, set the 
  - why: Root cause confirmed: _format_author_line (llm_completion.py ~882) emits 'author <label>: <content>' with no created_at; there is NO User.timezone field (grep of User class = 0 matches), matching the issue's audit precisely. The fix is well-specified (prefix every message with an
  - risk: Depends on / overlaps #128: both stem from 'we don't track user-local time anywhere'; the User.timezone column should be added once and shared. Timestamps are naive datetime.utcnow throughout models.py - formatting to a 

- **#132** [iPhone Chrome] Todo edits sometimes cannot be saved
  - type=bug | well_defined=low | cc=partial | chrome=partial | effort=M | surface=frontend
  - files: frontend/src/pages/TodoPage.js, frontend/src/utils/markdown.js, frontend/src/api.js
  - test: Desktop Chrome can verify the GENERAL save path on staging: open TodoPage, edit a todo textarea, trigger save, assert the PUT/POST fires (Network tab) AND the UI refreshes with new content. DevTools iPhone-viewport emulation checks layout but does NOT reproduce real iOS Safari/We
  - why: Explicitly iPhone Chrome (WebKit/iOS underneath), intermittent, with three competing hypotheses (request not firing / response not refreshing UI / iOS blur interference). The likely real fix (blur/keyboard timing) is iOS-WebKit behavior desktop Chrome cannot reproduce. CC can imp
  - risk: iOS/WebKit-only intermittent race; desktop Chrome (even emulated) will not reproduce keyboard-blur timing. Related to #106 (iOS zoom-on-focus, same input flow). Risk of shipping a fix unverifiable on staging-Chrome. Todo

- **#139** chat-with-archive keyword: replace only the first occurrence per message
  - type=bug | well_defined=high | cc=yes | chrome=partial | effort=S | surface=backend
  - deps: [149]
  - files: backend/tasks/llm_completion.py, backend/tests/test_llm_placeholders.py
  - test: No direct Chrome signal: the substitution happens server-side in _substitute_placeholders and the expanded text is only sent to the LLM, never displayed and there is no token/cost UI in the frontend (grep: no cost/token display in frontend/src). VERIFY BY UNIT TEST: extend backen
  - why: Tightly scoped and well-understood. The fix pattern already exists in the same file: _replace_first_and_mark() (line ~237) already does first-occurrence-only for {user_profile} and {user_recent}. The bug is that _substitute_export() (line ~194) replaces ALL occurrences and _resol
  - risk: STALE TITLE: the keyword is {user_export}, not 'chat-with-archive' (0 hits in repo). Confirm whether the desired behavior should also apply to {quote:ID} (issue says 'apply same rule consistently') - quote currently reso

- **#140** Sentence-aware TTS chunking - fall back to word-aware for tiny first chunks
  - type=enhancement | well_defined=high | cc=yes | chrome=partial | effort=S | surface=backend
  - deps: [145]
  - files: backend/tasks/tts.py, backend/tests/
  - test: Primary verification is a backend unit test on adaptive_chunk_text in backend/tasks/tts.py: feed a string whose first sentence is very short followed by a long bullet list and assert the first emitted chunk meets the minimum size (merged with following content or split word-aware
  - why: Tightly scoped extension of existing logic: tts.py centers on adaptive_chunk_text (adaptive=2 refs) which already does sentence-boundary chunking (sentence=1). The fix is a minimum-chunk-size rule: when the first sentence chunk is below threshold, merge forward or fall back to wo
  - risk: Backend-only with a weak UI signal: the user-facing effect is audio-chunk timing, which the automation extension cannot assert deterministically (no audio analysis), so confidence comes from the unit test, not Chrome. Th

- **#143** Reconcile AI interaction preferences (user profile vs. dedicated artifact)
  - type=enhancement | well_defined=low | cc=partial | chrome=partial | effort=M | surface=both
  - deps: [32]
  - files: backend/prompts/profile_generation.txt, backend/tasks/llm_completion.py, backend/routes/export_data.py, frontend/src/pages/AiPreferencesPage.js, frontend/src/pages/ProfilePage.js, backend/routes/ai_preferences.py
  - test: Partial on desktop Chrome. The duplication is demonstrable: edit the standalone artifact at /ai-preferences (AiPreferencesPage GET/PUT /ai-preferences confirmed) and separately regenerate the profile, then inspect whether preference-like text appears in BOTH the Profile page cont
  - why: Confirmed real duplication: backend/prompts/profile_generation.txt has an entire 'AI Interaction Patterns' section (lines 28-32, plus shadow/light AI subsections) so profile-gen captures preferences, AND there is a separate standalone artifact with full CRUD+versions (routes/ai_p
  - risk: Editing the profile-generation prompt to drop the preferences section affects every future regen; combined with #32's incremental merge, stale preference text could linger in already-generated profiles unless a backfill/

- **#144** Add 'Today's priorities' section to todo list + update agentic prompt
  - type=feature | well_defined=medium | cc=partial | chrome=partial | effort=L | surface=both
  - deps: [108, 97, 107]
  - files: frontend/src/pages/TodoPage.js, frontend/src/utils/markdown.js, backend/prompts/agentic.txt, backend/prompts/orient_apply_todo.txt, backend/tasks/voice_todo_merge.py
  - test: Partial. Static-render half: on staging in desktop Chrome, seed a todo whose markdown has a '## Today's priorities' section (via the Edit textarea) and confirm TodoPage (parseSections in frontend/src/pages/TodoPage.js splits on ## headings) renders it pinned at top with the same 
  - why: Two halves. Frontend 'pinned Today's priorities section that parses/renders/preserves a ## block' is well-defined and Chrome-testable — TodoPage already parses ## sections, so pinning one is straightforward. The agentic half requires extending agentic.txt's propose_todo behavior 
  - risk: Merge fragility: orient_apply_todo.txt merges may strip/reorder the priority section unless explicitly instructed to preserve it — the LLM merge is the unreliable part and is hard to unit-pin. Needs product decisions on 

- **#145** Section-aware TTS playlist (use headings as playlist items)
  - type=feature | well_defined=medium | cc=yes | chrome=yes | effort=L | surface=both
  - deps: [140, 29]
  - files: backend/tasks/tts.py, backend/models.py, backend/routes/nodes.py, frontend/src/components/GlobalAudioPlayer.js, frontend/src/contexts/AudioContext.js, backend/tests/
  - test: On staging desktop Chrome: create a text node with multiple markdown headings (## A, ## B, ## C) each with body text, click the speaker icon to generate TTS (no mic). Assert the player renders a chapter/section list showing heading titles, that clicking a chapter jumps playback t
  - why: Backend already understands headings: tts.py has _strip_heading_sections (strip=2) and heading logic (heading=6 refs), with TTSChunk records (models.py=1) as the natural per-section unit. But chunking is currently sentence/adaptive-based (sentence=1, adaptive=2; note word-aware p
  - risk: Two open product questions in the issue (read the heading aloud vs use it only as a label; which heading depths are chapters - h1/h2 vs also h3) should be decided before coding. Larger effort: it changes the chunking str

- **#146** Update About pages to reflect current Loore capabilities (pre-alpha-expansion)
  - type=chore | well_defined=medium | cc=partial | chrome=yes | effort=M | surface=content
  - deps: [147]
  - files: frontend/src/components/LandingPage.js, frontend/src/pages/WelcomePage.js, frontend/src/pages/WhyLoorePage.js, frontend/src/pages/HowToPage.js, frontend/src/pages/VisionPage.js
  - test: On staging in desktop Chrome: navigate to /landing (public) and the about-style pages (/welcome, plus WhyLoore/HowTo/Vision routes) and assert copy mentions current features (voice mode, profile generation, todos, keyword system) and references no removed/renamed features; click 
  - why: Marketing surface is concrete and verified, but the issue's implied path is partly stale: the landing component is frontend/src/components/LandingPage.js (under components/, NOT pages/). There are MORE about-style pages than the issue implies — WhyLoorePage.js, HowToPage.js, Visi
  - risk: STALE/INCOMPLETE PATHS: LandingPage.js is in components/ not pages/; and the about surface spans several pages (WhyLoore/HowTo/Vision/Welcome) the issue does not enumerate — audit all before editing. Copy is a brand/posi

- **#147** First-login onboarding flow
  - type=feature | well_defined=low | cc=partial | chrome=yes | effort=XL | surface=frontend
  - deps: [146, 129, 63]
  - files: frontend/src/App.js, frontend/src/components/TermsModal.js, frontend/src/pages/WelcomePage.js, frontend/src/contexts/UserContext.js, frontend/src/components/NodeForm.js, frontend/src/pages/AccountPage.js
  - test: On staging (DB resets each deploy, giving a natural fresh-account state): log in via the persisted Chrome session, click 'I Agree' on the TermsModal (App.js gates it; onboarding should chain right after). Assert the onboarding flow launches: step bubbles appear in sequence, the p
  - why: Valuable pre-alpha-expansion feature, fully observable in desktop Chrome (it gates right after the desktop-testable TermsModal in App.js, confirmed). But it is a large, design-heavy build: no tour/onboarding library exists (verified package.json has no joyride/shepherd/intro.js, 
  - risk: Scope is broad and partly a product-design exercise; should be split into sub-issues (walkthrough engine, privacy/AI explainer step, defaults audit) before implementation. TTS-narrated onboarding adds a TTS-pipeline depe

- **#149** Improve keyword UX: explainer + editor exposure + NodeForm insert buttons
  - type=enhancement | well_defined=medium | cc=partial | chrome=yes | effort=L | surface=both
  - deps: [139]
  - files: frontend/src/components/NodeForm.js, frontend/src/components/MarkdownBody.js, backend/tasks/llm_completion.py, backend/utils/placeholders.py, backend/routes/export_data.py
  - test: On staging (after clicking 'I Agree' on terms), open the Write New Entry dialog. Assert new keyword insert buttons (Profile / Archive / Quote) are present. Click the 'Profile' button and assert the corresponding token (e.g. {user_profile}) is inserted into the textarea at the cur
  - why: NodeForm.js confirmed to have ZERO keyword-related lines today, so keywords ({user_profile}, {user_export}, {quote:ID}) are genuinely undiscoverable as the issue claims. The three improvements (explainer, editor exposure, insert buttons) are all frontend UI that is fully observab
  - risk: STALE NAMING: the issue refers to 'user_archive' but that keyword does NOT exist anywhere in the codebase (grep: 0 hits in backend); the real keyword is {user_export} (8 backend files). 'chat-with-archive' also has 0 hit

- **#150** Intentions feature — track and clarify aspirations (parallel workflow to todos)
  - type=feature | well_defined=medium | cc=partial | chrome=partial | effort=L | surface=both
  - deps: [144, 143]
  - files: backend/models.py, backend/routes/todo.py, backend/tasks/voice_todo_merge.py, backend/prompts/agentic.txt, backend/prompts/orient_apply_todo.txt, frontend/src/pages/TodoPage.js
  - test: Partial. The CRUD/UI surface is directly Chrome-testable by mirroring the existing todo workflow (TodoPage.js + UserTodo model + version history drawer + the agentic propose/merge pipeline are the template): (1) accept terms (DB resets each deploy); (2) navigate to a new Intentio
  - why: Concrete 'feature'-labeled workflow with a strong existing template (the todo pipeline: model, route, page, agentic prompt, orient_apply merge, version history). CC can build a parallel Intentions model + page + agentic prompt extension + tests, and the CRUD/UI is staging-testabl
  - risk: Risk of duplicating the todo plumbing rather than generalizing it — consider whether intentions and todos should share a generic 'tracked-list artifact' abstraction (also touches #143's preferences-artifact reconciliatio

- **#152** Profile generation policy for new users — more frequent + recalculate from scratch
  - type=enhancement | well_defined=low | cc=partial | chrome=partial | effort=M | surface=backend
  - deps: [32, 65]
  - files: backend/tasks/exports.py, backend/routes/export_data.py, backend/routes/nodes.py, backend/tests/test_exports_incremental.py
  - test: Mostly verify by backend unit test (test_exports_incremental.py is the existing pattern): assert that for a small-corpus user the regenerate path forces from_scratch (not incremental) and that the auto-trigger fires on the chosen cadence (e.g. on node create / via maybe_trigger_i
  - why: The issue's own 'Open questions' section flags multiple undecided product choices: the exact new-user auto-update trigger (every session? every N nodes? time-based?) and whether 100k is the right boundary. The 'mature users' incremental path is explicitly delegated to #32, which 
  - risk: Auto-triggering on every node creation risks LLM cost spikes and concurrent runs; the existing profile_generation_task_id concurrency guard + _is_task_stale (exports.py:30) help but cadence still needs throttling. The co

- **#158** Add tool use to the LLM — artifacts, GitHub issues, feedback submission
  - type=feature | well_defined=medium | cc=partial | chrome=partial | effort=L | surface=both
  - deps: [157, 159, 150]
  - files: backend/llm_providers.py, backend/tasks/llm_completion.py, backend/prompts/agentic.txt, backend/tasks/voice_todo_merge.py, backend/models.py, backend/routes/todo.py
  - test: Partial. There is an existing tool-use foundation: the agentic propose_todo tool already flows Anthropic SDK tool calls through backend/tasks/llm_completion.py + llm_providers.py and is applied via voice_todo_merge.py, so the wiring pattern is proven. To verify on staging via Chr
  - why: Concrete feature with proven plumbing precedent (propose_todo tool use already exists), so CC can write FE prompt/UX + BE handlers + tests. But it spans three tool families, the 'artifact' concept (memory/scratchpad/user artifacts) needs a product decision on what counts as an ar
  - risk: Scope creep risk — split into per-tool issues (artifacts vs feedback vs GitHub). GitHub tool requires a bot token/account decision and acting-on-user-behalf safeguards; do not point it at the live repo during staging tes


## EXCLUDE

- **#22** Voice mode does not work on locked phone in app
  - type=bug | well_defined=low | cc=no | chrome=no | effort=M | surface=frontend
  - deps: [88]
  - files: frontend/src/hooks/useStreamingMediaRecorder.js, frontend/src/components/StreamingMicButton.js
  - test: Not testable via desktop Chrome: the issue is that locking a PHONE stops background mic capture - a mobile-OS-level restriction. There is no lock-screen concept in desktop Chrome and no way to emulate the mobile OS suspending audio capture. The issue body itself states it is a mo
  - why: Explicitly a mobile-OS platform constraint (no background audio capture while screen locked). Not fixable in code beyond a UI warning, which is essentially the #88 lifecycle-handling work. No desktop-Chrome-observable surface and no repo-only solution. Hardware/OS-dependent with 
  - risk: Mobile-OS restriction; cannot be solved or verified from repo + desktop Chrome. Best handled as graceful degradation under #88 (detect interruption, warn, preserve partial). Do not schedule as an independent CC build.

- **#99** Profile TTS speaker icon — unclear privacy/ai-usage handling
  - type=exploration | well_defined=low | cc=no | chrome=partial | effort=S | surface=both
  - deps: [143]
  - files: frontend/src/components/SpeakerIcon.js, frontend/src/pages/ProfilePage.js, backend/routes/profile.py, backend/tasks/tts.py
  - test: Only surface mechanics are Chrome-testable: on staging Profile page (voice-mode user), click the speaker icon and confirm POST /profile/<id>/tts fires (network tab) and audio plays via GlobalAudioPlayer. But the issue is a clarification 'question' -- there is no defined correct p
  - why: Imported from the old tracker and effectively a 'question': the body literally asks 'How is this managed now? Need to clarify the privacy model.' Code reality confirms the ambiguity is real: SpeakerIcon ALREADY receives aiUsage + isPublic props from ProfilePage (profile.ai_usage 
  - risk: Low code effort once a decision exists, but with no acceptance criteria it is not a buildable ticket today. Coupled to #143 (where ai-usage/preference semantics canonically live). If it became a defined task (e.g. 'profi

- **#103** Voice recording not working on Android (Chrome 140, Sony Xperia, rooted)
  - type=bug | well_defined=low | cc=no | chrome=no | effort=M | surface=frontend
  - deps: [160, 22]
  - files: frontend/src/hooks/useStreamingMediaRecorder.js, frontend/src/components/StreamingMicButton.js
  - test: Not testable via desktop Chrome: report is specific to Chrome 140 on Android 15 on a ROOTED Sony Xperia 5 II. Repro needs that exact rooted-Android hardware (the issue flags rooting as a likely factor). Desktop Chrome cannot emulate a rooted mobile OS audio stack and no real mic 
  - why: Single-user report on a rooted device with no diagnosed root cause and no captured error. Exploration/triage needing special hardware (rooted Android) and on-device debugging before code. Fails cc_implementable (no repro from repo) and staging_chrome_testable (mobile-OS/hardware-
  - risk: Hardware + real-world (rooted phone, real mic) dependency. No acceptance criteria, no captured error. Likely superseded by #160 once it lands. Do not schedule for CC end-to-end.

- **#107** Todo empty update ignored on smaller models (Sonnet)
  - type=bug | well_defined=low | cc=partial | chrome=partial | effort=M | surface=backend
  - deps: [97, 144]
  - files: backend/prompts/orient_apply_todo.txt, backend/tasks/voice_todo_merge.py, backend/prompts/agentic.txt
  - test: Hard to test deterministically in desktop Chrome: failure is LLM/model-specific (Sonnet refusing to populate an empty todo) and normally originates from a voice/text agentic session feeding voice_todo_merge.py. One could drive a text-mode session in Chrome with an empty todo and 
  - why: The issue is explicitly investigatory and possibly obsolete in its own text: 'Needs testing on production with larger models to confirm if this is model-specific' and 'may also be obsolete after merging the Orient branch that combines update and updated todo in one LLM response.'
  - risk: Likely already fixed or stale; verify current empty-todo behavior first. Any real fix is prompt-engineering in orient_apply_todo.txt (handle the empty-CURRENT base case) plus possibly guarding the empty-merge-result bran

- **#113** Background music interferes with voice input
  - type=bug | well_defined=low | cc=no | chrome=no | effort=M | surface=frontend
  - files: frontend/src/hooks/useStreamingMediaRecorder.js
  - test: Not testable via desktop Chrome: background music on the device bleeds into the mic and degrades transcription. Validating any mitigation (audio ducking, echoCancellation/noiseSuppression constraints, or transcription quality with/without music) requires real audio input with mus
  - why: Vague (suppress/duck background audio OR provide guidance) with no acceptance criteria and no chosen approach - needs a product/audio-engineering decision. Any fix (getUserMedia audio constraints, server-side suppression, or guidance copy) can only be judged by real-audio transcr
  - risk: Real-audio/transcription-quality dependency; needs a design decision before coding. If narrowed to add echoCancellation/noiseSuppression/autoGainControl getUserMedia constraints it becomes a small CC change, but effectiv

- **#126** Run legacy-chunk backfill on production (~183 nodes, ~2 GB)
  - type=infra | well_defined=high | cc=no | chrome=no | effort=S | surface=infra
  - files: backend/scripts/backfill_legacy_chunks.py
  - test: Not implementable or testable by CC via staging Chrome. Read verbatim (lines 936-983): this is a one-time PRODUCTION ops run — execute `python -m backend.scripts.backfill_legacy_chunks` (dry-run, then --commit --node 5168, then --commit --verify-decode) ON THE PROD VM with prod e
  - why: Read verbatim (lines 936-983). Title and body are explicit: 'Run legacy-chunk backfill on production.' The code already landed via PR #119 and is validated on staging; the entire deliverable is running the migration on the prod VM against real user data. Per selection criteria, p
  - risk: No new code to write — backend/scripts/backfill_legacy_chunks.py already exists (confirmed). The work is a careful, idempotent, crash-safe prod migration with a per-node rollback recipe in the script docstring; touches ~

- **#141** Voice transcription drops chunks on highly repetitive content (e.g. singing)
  - type=bug | well_defined=medium | cc=no | chrome=no | effort=XL | surface=backend
  - files: backend/tasks/streaming_transcription.py, backend/tasks/transcription.py, backend/requirements.txt, backend/routes/drafts.py
  - test: Not desktop-Chrome testable. The trigger requires feeding real, highly repetitive audio (singing/mantra) into the recorder, but the automation extension cannot inject deterministic mic audio (no Chrome launch flags), and the streaming-transcription chunk pipeline is driven by upl
  - why: Excluded on multiple grounds confirmed by grep. (1) No local-model fallback exists anywhere: whisper appears 0 times across all of backend (config has the gpt-4o-transcribe name; transcription.py=4 and streaming_transcription.py=12 are gpt-4o-transcribe references), and fallback=
  - risk: Standing up a local transcription model on the staging/prod VMs has real ops weight (memory/CPU, image size, model download in deploy) and staging containers are resource-limited (512M backend). The 'empty relative to au

- **#148** Perform a security and privacy audit
  - type=exploration | well_defined=low | cc=no | chrome=no | effort=L | surface=both
  - deps: [142, 91, 110, 85]
  - files: backend/routes/auth.py, backend/routes/dashboard.py, backend/routes/export_data.py, backend/routes/import_data.py, backend/utils/encryption.py, backend/config.py
  - test: Not a directly testable change. #148 (verbatim, issues file lines 376-401) is an AUDIT umbrella whose deliverable is explicitly 'a finding-by-finding writeup that becomes its own batch of focused follow-up issues,' severity-ranked. There is no single code change to verify in desk
  - why: Read verbatim (lines 376-401). Framed as a structured audit producing a punch list of findings + remediations across auth/sessions, authz, input handling/XSS, secrets, data-at-rest, TLS, privacy, logging, third-party keys, and server hardening. That breadth + 'produces its own ba
  - risk: Explicitly names #142 (server header) and #110 (cross-user export access) as members; #146 names #91 as a sibling pre-scale blocker. Scope is far too broad for one CC PR. CC CAN run /security-review, but turning the outp

- **#151** [Exploration] Overnight personalized morning briefing from Community Archive
  - type=exploration | well_defined=low | cc=partial | chrome=partial | effort=XL | surface=both
  - deps: [145, 155]
  - files: backend/models.py, backend/tasks/exports.py, backend/tasks/tts.py, backend/celery_app.py, frontend/src/pages/HomePage.js
  - test: Partial / mostly not as one unit. Has an explicit pre-validation gate ('confirm enough overlap between CA opt-in users and the current user's followed accounts') that must pass before building, and recon confirms there is no Follow/following model in backend/models.py today (it '
  - why: Despite the 'feature' label, it is gated by data pre-validation and missing infrastructure: no Follow model, dependence on CA opt-in content, a scheduled overnight job, and dependence on #145's chapter playlist. Multiple open questions (length, push notification, where followed u
  - risk: Blocked on a missing data model (follows) and on #145. Pre-validation step is a real gate — building before confirming data overlap risks shipping an empty briefing. New Celery task must be registered in backend/celery_a

- **#153** [Exploration] Real-time voice conversation mode (record while playing response)
  - type=exploration | well_defined=low | cc=no | chrome=no | effort=XL | surface=both
  - files: frontend/src/hooks/useVoiceSession.js, frontend/src/hooks/useStreamingMediaRecorder.js, frontend/src/pages/VoicePage.js, backend/tasks/streaming_transcription.py, backend/tasks/tts.py
  - test: Not testable on staging via Chrome. The whole feature is mic-audio-and-timing dependent: echo cancellation (TTS bleeding into mic), VAD/energy-threshold interruption detection while TTS plays, and mid-response regeneration based on actual heard playback position. The automation e
  - why: Explicitly 'a large rewrite of the voice/transcription/TTS pipeline' and exploratory. Core mechanics (AEC, VAD interruption, sub-second voice-to-voice latency, tracking how much of the response the user actually heard) depend on real-time mic input and audio hardware behavior tha
  - risk: Mic-audio injection limitation is decisive here. Even a phased plan's earliest phases (interruption detection) require real audio. Treat as a research/scoping track, not a buildable issue.

- **#154** [Exploration] Topic-page LLM knowledge base (Karpathy-style personal wiki)
  - type=exploration | well_defined=low | cc=no | chrome=partial | effort=XL | surface=both
  - deps: [155, 152]
  - files: backend/tasks/exports.py, backend/tasks/llm_completion.py, backend/models.py, frontend/src/pages/ProfilePage.js
  - test: Mostly not testable as a unit. The explicit next step is a Plan-mode design session, not implementation. The new pieces (arbitrary-depth, on-demand-generated topic pages + agentic retrieval that pulls relevant topic pages into session context) are net-new and undesigned. If topic
  - why: Self-labeled exploration whose stated next step is 'walk through the implementation... likely a Plan-mode session.' The core differentiator (arbitrary-depth topic pages with dynamic agentic retrieval) needs a design decision on data model, generation triggers, and how it differs 
  - risk: Heavily overlaps RAG (#155) and profile-generation policy (#152). Needs an explicit decision on relationship to RAG before building, or the two approaches will duplicate retrieval/context plumbing.

- **#155** [Exploration] RAG / semantic search across user + community archives
  - type=exploration | well_defined=low | cc=partial | chrome=partial | effort=XL | surface=both
  - deps: [154, 151, 149]
  - files: backend/tasks/llm_completion.py, backend/models.py, backend/routes/export_data.py
  - test: Largely not testable as one unit. Recon confirms no embeddings/vector infrastructure exists in the backend today, so this is net-new infra (embedding pipeline, vector store, retrieval, agentic multi-query, plus Community-Archive access control). If a thin slice eventually shipped
  - why: Self-described 'next big initiative' and umbrella with four sub-components (user-archive embeddings, CA extension, agentic search, combined access-controlled retrieval). Requires choosing/standing up a vector DB and an embeddings pipeline, plus CA opt-in access control — large ar
  - risk: Cross-user/CA retrieval has serious privacy/authorization implications (overlaps #148 and #110-family access-control work). Pick the vector store and define access control before any build. Strongly recommend splitting i

- **#156** [Exploration] Chat-with-archive as one-time corpus submission to OpenAI
  - type=exploration | well_defined=low | cc=no | chrome=no | effort=M | surface=both
  - deps: [155, 148]
  - files: backend/tasks/llm_completion.py, backend/utils/placeholders.py
  - test: Not meaningfully testable on staging via Chrome. The behavior (user corpus included in the OpenAI request payload during chat-with-archive) already happens as a side effect; the issue is about deliberately reframing it as an opt-in 'submit my corpus for training' action for users
  - why: Explicitly 'Exploratory framing. Not a near-term build.' It is primarily a privacy/product-positioning decision (how to surface opt-in, consent copy, irreversibility messaging), not a clear engineering task. The actual training-data outcome is unobservable, so there is no verific
  - risk: Significant privacy implications ('once submitted, it's submitted'). Should be gated behind the security/privacy audit (#148) and a clear consent flow. Not appropriate as a CC-driven build without a human privacy decisio

- **#157** [Exploration] Claude Code integration — skill + harness + persistent context
  - type=exploration | well_defined=low | cc=no | chrome=no | effort=XL | surface=infra
  - deps: [158, 159]
  - files: 
  - test: Not testable on staging via Chrome. The issue is an umbrella with explicit unresolved 'Decisions to make' (client- vs server-side integration, auth model for CC acting as a specific user, CC-direct vs Loore-native agent). The harness path requires running a CC instance on the pro
  - why: Self-labeled exploratory umbrella. Requires foundational product/architecture decisions before any code (auth model, integration topology), and the most concrete sub-path (CC running on prod server as a harness) is a prod-only ops change, not CC-implementable end-to-end. The 'Loo
  - risk: Decompose: the 'Loore skill for CC' sub-task could become a small standalone issue once #158's tool/API surface and an auth model exist. As written, it is a strategy umbrella, not a buildable unit.

- **#159** [Exploration] Voice-driven development pipeline
  - type=exploration | well_defined=low | cc=no | chrome=no | effort=XL | surface=infra
  - deps: [158, 157]
  - files: 
  - test: Not testable on staging via Chrome. This is a meta development-process pipeline (voice issue capture outdoors, auto-prioritization of GitHub issues, auto PR drafting, voice PR review, dual-CC plan review, CC implementation, automated UI testing). It is orchestration of external C
  - why: Self-described as exploratory; explicitly says to scope piece by piece rather than commit to the whole. It is a process/workflow concept (steps 3, 6, 9 are net-new, others are 'manually orchestrated'), not a shippable code change. Requires prod-server CC instances, dual-CC iterat
  - risk: Umbrella/meta issue. Its only concrete sub-capability (voice -> GitHub issue) is really #158's GitHub tool. Should be decomposed; do not treat as a single buildable issue. Mic-audio injection limitation and prod-CC ops p

