# E — Logged-in feature pages (everything except the thread view and voice mode)

Source: `.claude/worktrees/ios-app` at `2774ab2` (origin/main, 2026-09-30). Frontend paths are relative to `frontend/src/`, backend to `backend/`.

Pages covered: Documents workspace (ArtifactsNav, Profile, Todo, Artifacts incl. Intentions, shared VersionHistoryDrawer), Log, SearchModal, References list and detail (with ReferenceEditForm, ReferenceFeedback, ReferenceFooter, ReferenceReadToggle, and FeedPicks/ReadReply as they appear inside the thread), Prompts list and detail, Account, Import (ImportData, ExternalImport, NewTokenDialog), Share, Commons, UpdatesModal, ProfileGenerationWatcher, PrefillConsentCard, WelcomePage, ConfirmEmailPage, AdminPanel (summary only).

---

## 0. Cross-cutting facts the Swift client needs for every page below

### 0.1 Transport
- Axios base URL is `/api` (`api.js`), `withCredentials: true`, **60 s timeout**. Auth is the Flask-Login session cookie on every endpoint here (`@login_required`). The one exception is `POST /api/external/clip` and `GET /api/external/clip/status`, which also accept `Authorization: Bearer loore_…` (personal API token, scope `external:write`); a present-but-invalid bearer is a 401 even when a session exists.
- Every request carries header `X-Timezone: <IANA tz>` (`api.js` interceptor). Also `PATCH /api/dashboard/timezone {timezone}` runs on session start when the device tz differs from `user.timezone` (`contexts/UserContext.js`). Send both from iOS (`TimeZone.current.identifier`).
- **HTTP 402** with `{"error":"monthly_spend_limit_reached","message":…}` anywhere → the global spend-cap banner (event `loore:spend-capped`); the caller's own error path still runs.
- **HTTP 403** `{"error":"Your account is not approved…"}` for any JSON call from an unapproved user, except `GET /api/dashboard*`, `PUT /api/dashboard/user`, `POST|DELETE /api/dashboard/email*`, `/api/terms*` (`backend/__init__.py` `block_unapproved_users`).
- nginx: `/api/` has `client_max_body_size 200M` and `proxy_read_timeout 60s` (`configs/nginx.txt`). Any synchronous request longer than 60 s dies with a 504. This matters for the import confirm calls (section 7).
- **Trailing slashes.** Flask registers several list routes as `"/"`, so the canonical path ends in `/`; the web calls them without the slash and follows a 308 (ProxyFix keeps https). Call the canonical form from Swift to save a round trip:
  - `/api/todo/` (GET, PATCH, PUT), `/api/artifacts/` (GET), `/api/profile/` (POST), `/api/prompts/` (GET), `/api/dashboard/` (GET).
  - Registered with `""` (no slash, a trailing slash 404s): `/api/share` (GET, POST), `/api/updates` (GET).
- Timestamps come from `iso_utc()` (UTC). `utils/date.js` `parseTimestamp` appends `Z` if a timestamp has a time part and no zone marker; do the same defensively.
  - `formatDate(iso, {fallback, relative=true})`: `"today"`, `"yesterday"`, else `"Sep 12, 2026"` (`month:'short', day:'numeric', year:'numeric'`). `relative:false` skips the words.
  - `formatDateTime(iso)`: `"yyyy/mm/dd HH:MM"` local time, no seconds (reference footers).
- Toasts: `useToast().addToast(text, ms)` (default duration when omitted). iOS: a transient banner/HUD.

### 0.2 Cross-component events (web `window` events → iOS shared observable state)
| Web event | Fired by | Listened by | iOS equivalent |
|---|---|---|---|
| `loore_artifacts_changed` | ArtifactsPage after save/revert | ArtifactsNav (refetch bubble list) | an `@Observable ArtifactsStore` that the nav row reads |
| `loore_profile_started` | **nothing dispatches it today** (dead listener) | ProfileGenerationWatcher, ProfilePage | drop, or fire it after an import returns `profile_update_task_id` |
| `loore_profile_progress` `{running, status, progress, message, source, latestProfileId}` | ProfileGenerationWatcher | ProfilePage | `ProfileGenerationStore` |
| `loore_profile_done` | ProfileGenerationWatcher | ProfilePage | same store |
| `loore:spend-capped` | api interceptor | SpendCapBanner | global store |

### 0.3 Keyboard shortcuts in these pages (iPad / hardware keyboard only)
- ⌘/Ctrl+Enter = primary submit in every edit form (`hooks/useSubmitShortcut`). Esc = cancel edit (`hooks/useEscapeKey`). ⌘K toggles SearchModal (scope `external` when the path starts with `/references`, else `archive`). iOS: `.keyboardShortcut(.return, modifiers: .command)`, `.keyboardShortcut(.cancelAction)`, `.keyboardShortcut("k")`.

### 0.4 User payload fields these pages read (from `GET /api/dashboard/` → `user`, and echoed by `PUT /api/dashboard/user`)
`id, username, description, email, pending_email, pending_email_expired, approved, terms_up_to_date, accepted_terms_at, is_admin, plan, voice_mode_enabled, craft_mode, preferred_model, profile_generation_task_id, profile_batch_pending, default_privacy_level, default_ai_usage, twitter_login, twitter_handle, prefill_consent, prefilled_handle, timezone, spend_blocked, share_v1_enabled, share_v1_available, public_sharing_enabled, external_content_available, external_content_enabled`.

Feature gates used below:
- `share_v1_enabled` (env `SHARE_V1` AND the user's opt-in): Share, Commons, "My public page".
- `share_v1_available` (env only): whether Account shows the Public sharing row.
- `external_content_available` (env `SEMANTIC_SEARCH_AGENTIC`, default true): whether Account shows the External references row.
- `external_content_enabled`: the Chrome clipper card on Import, and token minting.
- `craft_mode`: Prompts link and "Export data" in the menu; bookmarks-JSON import when X sync is configured.
- `is_admin`: Admin link, the Keyword/Semantic toggle in search, admin-only prompts.
- `voice_mode_enabled`: SpeakerIcon may start TTS generation.

### 0.5 Navigation entry points (NavBar, for context)
Top nav: Reflect (`/`), **Artifacts** (→ `/profile`; highlighted on `/profile`, `/todo`, `/artifacts*`), **Log**, **Commons** (only `share_v1_enabled`). Overflow ⋮ menu: Import data, Admin (admins), Account, My public page (`/@username`, share flag), Light mode toggle, Craft mode toggle, then craft-only: Write new entry, **Export data**, **Prompts**; About links; Log out (`/auth/logout`).
- Export data: `GET /api/export/threads` → `text/plain` attachment, saved as `loore-export-YYYY-MM-DD.txt` via a blob `<a download>`. iOS: download to a temp file and present `ShareLink`/`UIActivityViewController` (or `.fileExporter`).

### 0.6 Shared building blocks used by several pages
- `MarkdownBody` (react-markdown + GFM; `inline` prop for single-line items). Needs a native Markdown renderer with GFM task lists, links, headings, lists, code, blockquote. Probably specified by the thread-view mapper.
- `Bubble` card (Log, References): see section 2.3.
- `VersionHistoryDrawer` (Todo, Artifacts, Profile, PromptDetail): section 1.5.
- `DeleteConfirmDialog` modes `thread` and `reference`; `RenameThreadDialog`; `RegenerateTtsDialog`: copy quoted where used.
- `SpeakerIcon` for profiles (`profileId`) and references (`itemId`): endpoints in 1.2 and 4.2. Only users with `voice_mode_enabled` can start TTS generation; others can only play existing audio.

---

## 1. Documents workspace: ArtifactsNav · Profile · Todo · Artifacts (+ Intentions)

Three routes that present as one workspace: `/profile`, `/todo`, `/artifacts` and `/artifacts/:kind`. All three render `ArtifactsNav` at the top and share one header pattern: serif H1 (2rem, weight 300); a clickable "● v{N} · {date}" chip (green dot) that enters edit mode; an underlined "history" link that opens the version drawer; a muted meta line; then a 1 px accent divider. Layout max width 800 px, padding 60px 24px.

`/ai-preferences` redirects to `/artifacts/ai_preferences`. `/dashboard` redirects to `/profile`.

### 1.1 ArtifactsNav (`components/ArtifactsNav.js`)
- **Purpose:** a wrapped row of pill "bubbles" linking the documents.
- **Order:** `Profile` (→`/profile`), `Intentions` (if present in the artifact list), `Todo` (→`/todo`), then built-in kinds in `BUILTIN_KIND_ORDER` minus intentions (`predictions, memory, scratchpad, ai_preferences`), then custom kinds sorted by `(title||kind).localeCompare`, then `+` (→`/artifacts?create=1`, tooltip "Create a new artifact").
- **Data:** `GET /api/artifacts/` → `{artifacts:[…]}` (see 1.4). Cached in a module-level variable so the row renders instantly on remount and refreshes in the background; refetches on `loore_artifacts_changed`. On error, Profile and Todo still render.
- **Active state:** accent border and card background when `path === to`, or when the host passes `activeKind` equal to the bubble's kind. While creating, only `+` is active.
- **Guard:** the host can pass `onNavigate(to)`; ArtifactsPage uses it to block navigation with unsaved edits.
- Bubble tooltip = artifact `description`.
- Modified clicks (⌘/Ctrl/Shift/middle) open a new tab. Not applicable on iOS.
- **iOS:** a horizontal `ScrollView` of capsule buttons (or a segmented menu) at the top of a "Documents" tab. The bubble list comes from `ArtifactsStore`.

### 1.2 ProfilePage (`pages/ProfilePage.js`)
**Purpose:** the AI-written living profile of the user, versioned, editable, playable as TTS.

**Load:**
- `GET /api/dashboard/` → `latest_profile` = `{id, content, generated_by, tokens_used, created_at, source_tokens_used, source_origin_stats, source_data_cutoff, generation_type, has_tts}` or `null`. The dashboard call is heavy: it also serializes pinned nodes and 20 nodes. A lighter iOS alternative is `GET /api/profile/versions` followed by `GET /api/profile/versions/<first id>`, but that response lacks `has_tts` and `source_origin_stats`, so the web page's source of truth is the dashboard.
- Then, whenever `profile` changes: `GET /api/profile/versions` → `versions[]` (newest first) `{id, generated_by, tokens_used, created_at, version_number, source_tokens_used, source_origin_stats, source_data_cutoff, generation_type}`. Pipeline intermediates are hidden server-side. `versionNumber = versions.length`.
- Loading: only ArtifactsNav shows. Errors are only logged; the page then shows the empty state.

**Header:** H1 "Profile"; SpeakerIcon (profile TTS); version chip `v{versionNumber} · {formatDate(created_at)}` (a click enters edit mode, or saves when already editing); "history"; a generation indicator (below).

**Meta line:**
- `source_tokens_used` truthy → `Built from ~{source_tokens_used.toLocaleString()} tokens of writing`, else `Generated from {tokens_used} tokens`.
- Then the source mix: `formatSourceMix(source_origin_stats)`. `stats` is `{origin: {tokens}}`. Take each origin other than `loore` as a share of total tokens, rounded; keep those > 0; sort descending; label `twitter`→"public tweets", `chatgpt`→"ChatGPT imports", `claude`→"Claude imports", `markdown`→"markdown imports". Result: ` (96% public tweets, 3% ChatGPT imports)`, or nothing.
- Then ` · {generated_by}`, then, if `source_data_cutoff`, ` · Data through {formatDate(cutoff, {relative:false})}`.

**Empty state (no profile, not editing):** three paragraphs, verbatim:
1. "Your profile is a living document the AI writes about you as you use Loore — what you're working on, what you care about, how you've changed."
2. "It's the first of your artifacts — the row above: documents you and the AI keep together. Todo and Intentions hold what you mean to do; Memory holds what the AI has learned. They start empty and fill in as you write and talk."
3. "There's nothing to set up. Start writing, and this page will follow — or write the first version yourself."

Then the button **"Write Profile"**, which opens an empty editor.

**Display:** `MarkdownBody` of `content` (the `.loore-profile` style: sans, 0.9rem, weight 300, line-height 1.7).

**Edit:** a 400 px textarea, **Save** ("Saving..." while in flight, disabled) and **Cancel**. ⌘Enter saves; Esc cancels. Save does nothing when the content is blank.
- Existing profile with `has_tts` and changed text → first show **RegenerateTtsDialog**:
  - Title "Regenerate audio?"
  - Body "This entry has generated audio that won't match your edits. Keep the existing audio, or regenerate it (it'll be created fresh the next time you play it)."
  - Options: "Regenerate audio" (subtext "Removes the outdated audio; new audio is generated on next play."), "Keep existing audio" (subtext "Saves your edits; the current audio stays as-is."), "Cancel".
- Existing profile → `PUT /api/profile/<id> {content, regenerate_tts?:true}`. This **edits the current version in place**, with no new version row, except in one case: when the account's `default_ai_usage` is `none` and the profile's `ai_usage` is chat/train, the backend saves a new version instead (#346). 400 errors: "Content is required", "Content cannot be empty".
- No profile → `POST /api/profile/ {content}` → 201.
- Afterwards: leave edit mode and refetch the dashboard.

**History drawer:** see 1.5. Endpoints: `GET /api/profile/versions/<id>` → `{profile:{id, content, generated_by, tokens_used, created_at}}` (the previous version is fetched too, for the diff). Revert: `POST /api/profile/revert/<id>` creates a new row with `generation_type:'revert'`; 400 "Already the current version". Then refetch and close.

**Generation indicator (background):** shown while `user.profile_generation_task_id || user.profile_batch_pending`, or after a `loore_profile_progress` event with `running`. Pulsing accent text:
- No message yet: `Starting generation...`
- Batch source: the message as given (e.g. "Generating profile: Chunk 3 of ~7").
- Sync source: `{message} · {progress}%`.
- Terminal `failed` → "Generation failed"; `stalled` → "Generation stopped before finishing". Shown for 5 s in muted colour.
- While running, when `latestProfileId` differs from the shown profile id, the page refetches so each chunk version appears as it lands.
- On `loore_profile_done`: clear the indicator and refetch.

**Profile TTS (SpeakerIcon with `profileId`):**
- `GET /api/profile/<id>/audio` → `{tts_url}` (200); `{status:'generating', progress, task_id}` (202); or 404.
- Else, if `voice_mode_enabled`: `POST /api/profile/<id>/tts` (spend-gated; 202 `{task_id}`, or 200 `{tts_url}` if the audio already exists), then SSE `GET /api/sse/profiles/<id>/tts-stream` for streamed chunks, with polling fallback `GET /api/profile/<id>/tts-status` → `{status, progress, task_id, profile:{id, audio_tts_url?}}`.
- Profiles have no chapters route. Audio URLs are relative (`/media/...`); prefix the backend origin.
- Mark `has_tts=true` locally after generation.

### 1.3 TodoPage (`pages/TodoPage.js`)
**Purpose:** the concrete, finishable task list, stored as one markdown document with `## Section` headings and `- [ ]` / `- [x]` items. Versioned.

**Endpoints (`backend/routes/todo.py`):**
- `GET /api/todo/` → `{todo: null}` or `{todo:{id, content, generated_by, tokens_used, created_at, privacy_level, ai_usage, version_number}}`. `version_number` = count of the user's versions.
- `PATCH /api/todo/ {content}` → **edits the latest version in place** (checkbox toggle, quick-add, per-row add). 400 when blank; 404 "No todo exists to update". Returns `{todo:{…}}`.
- `PUT /api/todo/ {content, generated_by:'user'}` → **creates a new version** (the Save button in edit mode, and the first create). `ai_usage` = the account default.
- `GET /api/todo/versions` → `{versions:[{id, generated_by, tokens_used, created_at, version_number}]}`, newest first.
- `GET /api/todo/versions/<id>` → `{todo:{id, content, …}}`.
- `POST /api/todo/revert/<id>` → a new version `generated_by:'revert'`; returns `{todo}`.
- (`POST /api/todo/apply-draft {llm_node_id}` belongs to the thread/voice proposal flow, not this page.)

**Header:**
- H1 "Todo".
- Version chip `v{version_number} · {formatDate(created_at)}`. A click enters edit mode, or saves if already editing.
- "history".
- A round **+** button (24 px, aria "Quick-add task", title "Quick-add task to Today"). It toggles the quick-add row and turns into **×** when open. Hidden while editing.
- Meta line: `Last updated by {label} · {formatDate(created_at)}`. Labels: `user`/`manual`→"edited manually", `orient_session`→"Orient session", `voice_session`→"Voice", `revert`→"reverted", `import`→"imported", else the raw value.

**Quick-add row (#108):**
- Input placeholder "Add a task to Today and press Enter".
- Enter (no modifier) or ⌘Enter → `appendItemToSection(content, 'Today', task, {createAtStart:true})`, applied optimistically, then `PATCH /api/todo/`.
- The input stays open and focused for rapid entry. On failure: revert the content and restore the typed text. Esc closes the row.

**Rendering (`parseTodoSections`), port exactly:**
- Split on `\n`. A line matching `^##\s+(.+)` starts a section `{title, items:[]}`.
- Items before any heading go into a section with title `''`.
- Checkbox line `^(\s*)- \[([ xX])\]\s+(.+)` → `{checked: mark!==' ', text, depth: floor(indent/2)}`.
- Plain line `^(\s*)- (.+)` → `{checked: null}`: a category header with no checkbox, rendered in the primary colour at weight 400.
- Nesting: an item at depth d>0 attaches to the last item at depth d−1, found by walking the last child chain; otherwise it becomes top-level.
- Section header: accent uppercase 0.68rem, letter-spacing 0.18em, plus a count of all items including nested ones. Empty section → italic "No items".
- **Item row:**
  - Checkbox: an 18 px circle. Checked = filled `--accent-dim` with ✓; the text gets line-through at 40 % opacity.
  - Text rendered with inline Markdown.
  - Items with children show a "▶ {count}" disclosure (rotates 90° when open). **Children are collapsed by default.**
  - Checkbox items get a hover "+" (title "Add an item below"). It opens an inline input (placeholder "New item…"); Enter submits, Esc or blur-with-empty closes. Only one inline input is open at a time.
  - Indent 24 px per depth.
  - iOS: hover does not exist. Show "+" as a swipe action or context-menu item ("Add item below").

**Toggle (`utils/markdown.js` `useCheckboxToggle`):**
- Key the item by `stripInlineMarkdown(item.text).trim()`. This strips `![alt](u)`→alt, `[t](u)`→t, `**b**`, `__b__`, `~~s~~`, `` `c` ``, `*i*`, `_i_`.
- `toggleCheckbox` rewrites **every** line whose stripped label equals the key: `- [ ]` ↔ `- [x]`, indent and raw label kept.
- Optimistic, then `PATCH`. On failure, revert and toast `Couldn't save change — reverted ({reason})`.

**Per-row add (`useTaskInsert` → `insertItemAfter`):**
- Find the first line matching `^(\s*)([-*])\s+(\[[ xX]\]\s+)?(.*)$` whose stripped label equals the key.
- Insert `{indent}{bullet} {"[ ] " if the matched line had a checkbox}{text}` after that line's deeper-indented subtree.
- Optimistic `PATCH`. On failure, revert and toast `Couldn't add task — reverted ({reason})`.

**`appendItemToSection` (quick-add):**
- Case-insensitive exact match of a `##` heading. The section ends at the next `#` or `##` heading.
- Insert after the last non-blank line of the section, which keeps the blank lines before the next heading.
- Missing section with `createAtStart` → prepend `## Today\n\n- [ ] task\n` + `\n` + the trimmed body.

**Empty state (no todo):**
- Explainer: "Your todo list is the concrete counterpart to intentions — specific, finishable tasks; as you write and talk, the AI notices completions, new items, and priorities, and proposes updates that apply only when you confirm."
- Line: "No todo list yet. Create one to track your tasks."
- Button **"Create Todo"** opens the editor pre-filled with `## Today\n\n- [ ] \n\n## Upcoming\n\n- [ ] \n\n## Completed recently\n`. Save → `PUT`.

**Edit mode:** a 400 px textarea with the raw markdown, **Save**/"Saving..." and **Cancel**. ⌘Enter saves; Esc cancels. Save is ignored when the content is blank.

**Loading:** only ArtifactsNav. Errors: logged only.

**Race to be aware of:** `PATCH` overwrites the latest row with whatever the client holds, and there is no version check. An AI todo merge (voice/text "apply to todo" writes a **new** version via Celery) that lands between load and toggle is overwritten with stale content plus the toggle. This happens on the web too. iOS should at least refetch on foreground and after a proposal is applied.

### 1.4 ArtifactsPage (`pages/ArtifactsPage.js`) + IntentionsView
**Purpose:** generic named, versioned documents that the AI and the user maintain together. Built-in kinds come first; users can create custom kinds.

**Endpoints (`backend/routes/artifacts.py`):**
- `GET /api/artifacts/` → `{artifacts:[{id, kind, title, description, content, generated_by, created_at, privacy_level, ai_usage}]}`.
  - Built-in default kinds that have no row yet are included as placeholders (`id:null, created_at:null, content:''`), each with its default description, where `{name}` is replaced by the username.
  - Default kinds and titles: `memory` Memory, `scratchpad` Scratchpad, `predictions` Predictions, `ai_preferences` AI Interaction Preferences, `intentions` Intentions.
  - Order: placeholders first, then existing kinds alphabetically. The client re-sorts with `compareArtifacts`: built-in order `intentions, predictions, memory, scratchpad, ai_preferences`, then custom by title.
- `GET /api/artifacts/<kind>` → `{artifact}` with `version_number`; 404 for an unknown custom kind. The page does not use it; it uses the list.
- `POST /api/artifacts/<kind>/viewed` → 204. Fire-and-forget, once per kind per page visit (admin activity metric).
- `PUT /api/artifacts/<kind> {content, description, generated_by:'user'[, title]}` → a new version; creates the kind if new.
  - `kind` must match `^[a-z0-9][a-z0-9_-]{0,47}$`, else 400 "Invalid kind: use a short lowercase slug (letters, digits, dashes)."
  - `content` is required; the empty string is allowed.
  - `title` when omitted: the previous title → the default title → `kind.replace('-', ' ').title()`.
  - `description`: the explicit value wins (blank → null); otherwise it is carried forward. Truncated to 255 characters; title to 128.
- `GET /api/artifacts/<kind>/versions` → `{versions:[…same fields without content, plus version_number]}`, newest first.
- `GET /api/artifacts/versions/<id>` → `{artifact:{…, content}}`.
- `POST /api/artifacts/<kind>/revert/<id>` → a new version `generated_by:'revert'`; 400 if the version is already current or belongs to another kind.
- **There is no delete endpoint for artifacts.**

**Routing state:**
- `/artifacts` with no kind → the active kind is `memory`.
- `/artifacts/:kind` selects that kind and leaves edit/create mode.
- `?create=1` opens the create form (and is removed from the URL).
- A kind without a row (a deep link to an unknown custom kind) is shown as a synthetic empty artifact titled `titleFromKind(kind)`, which replaces `-`/`_` with spaces and title-cases the words. It can be edited, and saving creates it.
- After loading the list, if the active artifact has `created_at`, `GET /api/artifacts/<kind>/versions` is called only to count versions for the chip.

**Header:**
- H1 = the artifact title, or "New artifact" while creating.
- If the artifact exists (`created_at`): a chip "● v{versionNumber||1}" that enters edit mode (accent while editing; tooltip "Edit"), then `formatDate(created_at)`, then "history".

**Subtitle:** `description` → else a per-kind blurb → else "A persistent document shared between you and the AI."
- Blurbs: memory "Durable facts the AI remembers about you across sessions. It updates this on its own as you talk."; scratchpad "The AI's working notes for ongoing threads — where it left off, open questions."; intentions "Your longer-running aspirations — noticed, clarified, and tracked together with the AI. Fulfilled or consciously released, both count."
- Plus ` · last updated by {label}` when it exists. Labels: `user`/`manual`→"edited manually", `agentic_session`→"AI session", else raw.

**Display:**
- `content` non-empty: `intentions` → IntentionsView; anything else → MarkdownBody.
- Empty:
  - An optional kind intro. intentions: "Intentions are your longer-running aspirations — the communication layer between your conscious goals and your subconscious orientation: directional rather than specific, quietly shaping which opportunities you notice and reach for." ai_preferences: "AI Interaction Preferences are your standing notes on how the AI should work with you — tone, style, boundaries, topics to leave alone — written by you or noted by the AI when you express one, and honored in every conversation." external_digest: "The Saved References Digest is a compact topic map of the tweets and bookmarks you saved elsewhere and imported into Loore — the AI reads it to judge whether your references hold something relevant before searching them, and it rebuilds itself after each import."
  - Then "Nothing here yet. The AI fills this in during Voice and Text sessions,⏎or write your own."
  - Then the button **"Write {title}"**.

**Edit / create form:**
- Create only: a name input, placeholder "artifact-name (lowercase, dashes)". It sanitizes on every keystroke: lowercase, then replaces `[^a-z0-9_-]` with `-`. Nothing stops a leading dash or more than 48 characters; the backend 400s, and the page only logs the error. iOS should validate against the backend regex.
- Description input, placeholder "One-line description — what this artifact is for". **Required:** Save stays disabled while it is blank.
- Textarea (300 px), placeholder "Facts the AI should remember about you..." for memory, else "Artifact content (markdown)...".
- **Save** ("Saving...") is enabled when `!saving && (!creating || name) && description.trim()`. **Cancel**. ⌘Enter from any field; Esc cancels.
- Save → `PUT /api/artifacts/<kind>`, refetch the list, fire `loore_artifacts_changed`, leave edit mode, navigate to `/artifacts/<kind>`. Errors are only logged.

**Unsaved-changes guard:**
- Dirty = creating with any of name/content/description filled, or editing where content or description differs from the loaded values.
- Bubble clicks while dirty show a modal: title "Unsaved changes", body "You have unsaved edits to this artifact. Leave without saving?", buttons **Cancel** / **Leave**. The web also sets `beforeunload`.
- iOS: `.interactiveDismissDisabled` plus a confirmation dialog on navigation.

**IntentionsView (`components/IntentionsView.js`, parser `utils/intentions.js`):**
- **Parser:** `# Heading` starts a section. `## Name` starts an entry `{name, status:'', body:[], notes:[]}`; entries before any `#` go into an untitled section.
- Within an entry:
  - A bullet line `^\s*[-*]\s+(.+)` → a note.
  - Blank lines are skipped.
  - A line wholly wrapped in `*…*` or `_…_` that comes before any status or body → `status`.
  - Anything else → a body line. Body lines are joined with spaces and rendered as plain text, not markdown.
- If no section has entries, fall back to plain MarkdownBody.
- **State from the status text** (`statusState`): contains "fulfilled" → fulfilled (solid accent dot with ✓); "released" → released (hollow dot at 50 %, name at 60 % opacity); "inferred" or "unconfirmed" → inferred (dashed hollow dot); else active (solid `--accent-dim` dot).
- **Row:** 15 px dot; the name in the serif font, 1.16rem, weight 600; the status line (0.72rem muted); the body (0.9rem); notes as a left-bordered list (0.78rem muted).
- **Section header:** accent uppercase 0.86rem, weight 600, letter-spacing 0.16em, plus the entry count.
- The artifact is **read-only in this view**; editing is the raw-markdown editor.

### 1.5 VersionHistoryDrawer (`components/VersionHistoryDrawer.js`), shared
- **Presentation:** a right-side drawer, 420 px wide (max 90vw), with a dim backdrop; Esc or a backdrop tap closes it. Title: "Todo History", "Profile History", "{title} History", or "{prompt title} History". iOS: a sheet (`.presentationDetents([.large])`) with a `NavigationStack` list → detail.
- **List rows:** `v{version_number}` with a "current" badge on the first row. Second line: `{formatDate(created_at, fallback 'File default')} · {label}`, plus ` · ~{source_tokens_used} source tokens` when present.
  - Labels: user/manual "Manual edit", revert "Reverted", orient_session "Orient session", voice_session "Voice", import "Imported".
  - Otherwise by `generation_type`: `update` "Auto-updated ({g})", `iterative` "Iterative build ({g})", `integration` "Integrated profile ({g})"; else "Auto-generated ({g})".
- **Selecting a version:** load its content and the next-older version's content (to diff against).
- **Diff/Full toggle** (only when both sides are loaded and the version is not the oldest):
  - Diff tab label "Changes ({changedCount})"; the other tab is "Full text".
  - The default is Diff, unless it is a heavy rewrite (changed ops > 50 % of all ops AND > 40), which defaults to Full.
  - The oldest version shows "Initial version".
  - "Revert to this version" appears when the selected version is not the first row.
  - While loading: "Loading...". No changes: "No changes from the previous version."
- **Diff algorithm (`utils/diff.js`), port exactly:**
  - Split on `\n`; trim the common prefix and suffix. LCS DP on the middle; if either side of the middle exceeds 1500 lines, emit one del block then one add block. Backtrack prefers `del` when `table[i+1][j] >= table[i][j+1]`.
  - `refineWordDiffs`: pair the i-th del with the i-th add inside each del-run→add-run block. Word-diff the pair (jsdiff `diffWordsWithSpace`); keep the segments only when similarity (common chars / max length) ≥ 0.4.
  - `collapseUnchanged(ops, 2)`: fold runs of unchanged lines, keeping 2 lines of context around each change and none at the document start or end, into `⋯ {n} unchanged line(s) ⋯`.
  - Colours: add = success tint; del = error tint with line-through. Word segments get a stronger tint.
  - iOS has no jsdiff. Port a word tokenizer that splits on word/whitespace boundaries (Myers or LCS over tokens), or use Swift's `CollectionDifference` on token arrays.
- **Prompts:** version id `'default'` is a synthetic v0 (the file default). Its content comes from `/prompts/<key>/default`, and revert calls `revert-to-default`.

---

## 2. Log (`components/Log.js`) — route `/log`

**Purpose:** the private diary. It lists the user's thread roots and pinned nodes, newest first; public roots are excluded.

**Endpoint:** `GET /api/log?per_page=20[&cursor=…]` (`backend/routes/log.py`) → `{nodes:[card], has_more, next_cursor, page}`.
- **Cursor pagination:** pass `next_cursor` verbatim, URL-encoded, format `"<iso ts>|<id>"`. The client also dedupes ids already held. A malformed cursor → 400.
- Sort: `coalesce(pinned_at, created_at) desc, id desc`.
- Deleted roots with alive descendants still appear.
- Card fields: `id` (the display node; for a system-prompt root this is its first child), `thread_root_id`, `newest_node_id`, `thread_name`, `can_rename`, `preview` (≈200 chars), `node_type`, `child_count`, `created_at`, `pinned_at`, `username`, `human_owner_username`, `llm_model`, `origin`, `has_original_audio`, `prompt_key`.

**Layout:**
- Header: H2 "Log" (serif 2rem), a 40 px accent rule, and a search icon button (aria "Search your entries", title "Search your entries (⌘K)", 8 px padding / −8 px margin for a larger tap target). It opens SearchModal with scope `archive`.
- A column of Bubble cards, gap 1rem, max width 720 px.

**Pagination:**
- Auto-load when within 300 px of the bottom. Also a clickable "Load more..." and a "Loading more...".
- A failed later page shows "Couldn't load more entries. **Retry**" and pauses auto-load; the cards already loaded stay.

**States:** first load "Loading log..."; error "Error loading log."; empty "Your private entries will appear here as you share thoughts with Loore."

**Card tap:** opens `/node/{newest_node_id || id}`, which jumps to the newest node in the thread.

**Card actions (kebab):**
- **"Rename thread"** (omitted when `can_rename === false`) → RenameThreadDialog:
  - Title "Rename thread"; input maxLength 120; placeholder = the card's fallback title (`splitPreview(preview).title`) or "Thread name".
  - Helper "Shown on this thread's Log card. Leave it empty to show the entry's own title."
  - Buttons Cancel / Save ("Saving…").
  - `PUT /api/nodes/<thread_root_id||id>/thread-name {thread_name}` → `{thread_name|null}`. Empty clears the name. Update every card whose thread root matches. Error toast = the server `error` or "Error renaming thread.".
- **"Delete thread"** → DeleteConfirmDialog mode `thread`:
  - Title "Delete entire thread?"; body "Your content in this thread will be removed. Your nodes remain as placeholders so other users' replies stay reachable (they own them)."; buttons **Delete thread** / **Cancel**.
  - `DELETE /api/nodes/<thread_root_id||id>?delete_descendants=true` → `{scheduled, grace_days, orphaned_prompt_deleted, deleted_pinned_ids}`.
  - Toast "Deleted {n} node(s)" (3 s).
  - Remove every card of that thread, plus cards whose id or thread root is in `deleted_pinned_ids`.
  - Error toast = the server error or "Error deleting thread.".

### 2.3 Bubble card (`components/Bubble.js`), shared by Log and References
- **`splitPreview(text)`:** `isHeading = /^#\s+/`; strip that prefix; `title` = the first line; `body` = the rest, trimmed.
- **Thread name:** `thread_name` (trimmed) replaces the title. The body is the whole text when the entry was not a heading, else the body without the heading line.
- **Truncation:** title at 120 characters + "..."; body at 250 characters + "...", clamped to 2 lines.
- **Expand chevron** only when full `content` is present and something is hidden (title > 120, body > 250, or body > 2 lines). The Log sends only `preview`, so Log cards never expand.
- **Placeholders:** `deleted` → italic "[Node deleted]"; `inaccessible` → "[Node inaccessible]". Neither is tappable, and inaccessible cards have no footer.
- **Footer:** `NodeFooter` (username, date-time, reply count, "model · via human" for LLM nodes). References pass their own footer.
- **Tags:** the passed `tag`; "Pinned"; the prompt label (`read`/`read_thread` → "Read", else the key capitalised with `_`→space); or "Voice Note" when `has_original_audio`.
- **Kebab:** visible on hover, and always on touch devices (`matchMedia('(hover: none)')`). iOS: a trailing "…" `Menu` on each card, plus swipe actions.
- Tests (`Bubble.test.js`) pin the `splitPreview` and thread-name rules (7 cases; see §14).

---

## 3. SearchModal (`components/SearchModal.js`), opened by ⌘K or the search icons on Log and References

**Scopes:** `archive` (the default: own entries; semantic results also merge in saved references) and `external` (the References page: saved references only).

**UI:**
- An input with ⌕ glyph, placeholder "Search your entries..." or "Search your references...".
- An admin-only **Semantic/Keyword** toggle; everyone else always uses semantic.
- A **Dates** toggle revealing From/To `<input type="date">` fields (YYYY-MM-DD) and a "Clear" button.
- Results list.

**Search:**
- Debounced 300 ms on every change of query or dates.
- An empty query with no dates clears the results.
- Semantic: `GET /api/search/semantic?q=&from=&to=[&scope=external]` (the web also sends `per_page=20&page=1`, which the server ignores; the server uses `limit`, default 20, max 50, and `min_score` 0.2). Response `{results, total, mode:'semantic', scope}`: one ranked set with no paging. 400 if `q` is empty; 503 if not configured; 502 if the embedding failed.
  - **Each call embeds the query with OpenAI and is billed per call.** Keep the debounce on iOS (≥300 ms) and do not search per keystroke.
  - Dates only, with no `q`, → 400, which the modal silently shows as "No results found".
- Keyword (admin): `GET /api/search?q=&from=&to=&page=&per_page=20[&scope=external]` → `{results, page, per_page, total, has_more, search_type:'keyword'}`. Needs `q` or a date. Paged: a "Show more ({n} of {total})" button that also auto-triggers when scrolled into view.

**Result item:**
- Node: `{id, preview, snippet, node_type, created_at, username, child_count, parent_id, score}`.
- Reference: `{kind:'external', id, source, author_handle, title, external_url, preview, snippet, created_at, score}`.
- Row: `node_type` or `sourceLabel(source)`; author (`authorLabel`); `formatDate(created_at)`; "{n} reply/replies"; `{round(score*100)}%` in semantic mode. Then the reference title, if any. Then the **snippet as HTML**: the keyword snippet contains `<mark>…</mark>` and is **not HTML-escaped** by the server. iOS: render it as plain text and turn only `<mark>` spans into highlighted `AttributedString` runs.
- Tap: node → `/node/<id>`; reference → `/references/<id>`. The modal closes.

**States:** "Searching...", "No results found", "Type to search your entries" / "Type to search your references". Esc or a backdrop tap closes.

**iOS:** `.searchable` on the Log and References lists, or a dedicated search sheet, with a date-range filter sheet.

---

## 4. References (`pages/ReferencesPage.js`, `pages/ReferenceDetailPage.js`)

Saved external content: web clips (Chrome clipper), X bookmarks, Community Archive tweets. Read picks are a non-saved source. A reference is not a node.

**Shared helpers (`utils/references.js`):**
- `SOURCE_LABEL`: web_clip "Page", twitter_bookmark "Tweet", community_archive "Archive tweet", read_pick "Archive tweet". A YouTube web clip is labelled "Video".
- `authorLabel`: web_clip → the handle as-is (a byline); tweets → `@handle`.
- `asCardNode(item)`: `{id, preview, thread_name: title, created_at: posted_at||fetched_at, child_count:0}`.
- `bodyWithoutTitle`: drop a leading `#…` heading line whose text equals the title (case-insensitive).
- `tweetId`: `external_id` for tweet sources.
- `youtubeVideo`: a web_clip on one of the YouTube hosts (`youtube.com`, `www.`, `m.`, `music.`, `youtu.be`, `www.youtube-nocookie.com`) with an 11-character id from `youtu.be/<id>`, `/watch?v=`, `/embed|shorts|live|v/<id>`, plus a start time from `t=` or `start=` (`90`, `1h2m3s`, …).
- Tests pin these (§14).

**Serialized item (`backend/routes/external.py` `_serialize_item`):** `{id, source, external_id, author_handle, title, preview (280 chars + …), url, posted_at, fetched_at, read_at, feedback ('good'|'bad'|null), feedback_at, edited_at, has_tts, surfaced_count, last_surfaced_at}`. The detail response adds `content`.

### 4.1 References list — `/references`
- `GET /api/external/items?sort=saved&page=N&per_page=20` → `{items, total, has_more, counts:{source:n}}`. `sort=saved` = most recently saved first. Only saved items; read picks are excluded.
- **Header:** H2 "References" with an accent rule the width of the title, and a search icon (aria "Search your references", title "Search your references (⌘K)"). The search opens SearchModal with scope `external`.
- **Cards:** Bubble with `asCardNode(item)`; footer = ReferenceFooter; tag = `Read · {source label}` if `read_at`, else the source label.
- **Kebab:** "Open source" (if `url`; opens a new tab → iOS `SFSafariViewController`/`openURL`) and "Delete".
- **Pagination:** the same as Log, but page-number based. "Load more..." / "Loading more...".
- **States:** "Loading references..."; "Error loading references."; empty "Pages and tweets you save from elsewhere will appear here."
- **Delete:** DeleteConfirmDialog mode `reference`:
  - Title "Delete reference?"; body "It leaves your saved references and search. The original stays where it is."; buttons **Delete** / **Cancel**.
  - `DELETE /api/external/items/<id>` → `{deleted:true, id[, kept_as_pick:true]}`.
  - Toast "Reference deleted"; remove the card.
  - Error toast = the server error or "Error deleting reference.".

### 4.2 Reference detail — `/references/:id`
- **Load:** `GET /api/external/items/<id>` → item + `content`. 404 → "This reference does not exist or was deleted."; other errors → "Error loading reference."; while loading, "Loading...".
- **Layout:**
  - Heading row: H2 "Reference" and a link "All references".
  - A focal card: accent left border, kebab in the corner.
  - Title row: H1 serif 1.5rem (if a title exists) and SpeakerIcon (`itemId`, content `# {title}\n{content}`).
  - Embed:
    - **Tweets:** `TweetEmbed` loads `https://platform.twitter.com/widgets.js` and reports `shown`/failure.
    - **YouTube:** an iframe `https://www.youtube-nocookie.com/embed/<id>?start=…`.
  - If an embed is shown: a toggle "Show stored text"/"Hide stored text" reveals the stored markdown. Otherwise the stored markdown is the body (`bodyWithoutTitle`).
  - Footer row: ReferenceFooter plus tags "Read" and the source label.
  - Bottom rule, left: `Shown by Loore {surfaced_count}× · last {formatDate(last_surfaced_at)}` or "Not yet shown by Loore in a conversation", then ` · you read it {date}` and ` · edited {date}`.
  - Bottom rule, right: ReferenceFeedback (18 px) and the button **"Mark as read"** (accent fill) or **"Mark as unread"** (outline).
- **Actions (kebab):** "Open source" (if url), "Edit", "Delete".
- **Read toggle:**
  - `POST /api/external/items/<id>/read` marks it read. It is idempotent and keeps the original `read_at`. Optional body `{node_id, via:'open'}` for logging.
  - `DELETE` the same path to unmark.
  - Response `{id, read_at|null}`. Error toast "Could not update the read mark.". Opening the page does **not** mark the item read.
- **Edit:**
  - In a NodeFormModal titled "Edit Reference" with **ReferenceEditForm**:
    - Title input (placeholder "Title", maxLength 512, serif 1.3rem).
    - Autofocused textarea whose rows = clamp(lines+2, 8, 30).
    - Hint "⌘↵ to save · Esc to cancel", or, when over the cap, `{n} characters — the limit is 100,000.` (`NODE_CHAR_CAP` = 100000, same on the backend)
    - Buttons Cancel / Save ("Saving…"). Save is enabled when not saving, not over the cap, content non-blank, and something changed.
  - If `has_tts` and the text changed → RegenerateTtsDialog first.
  - `PUT /api/external/items/<id> {title, content[, regenerate_tts:true]}` → the item with `content`. Toast "Reference updated". Errors: 400 empty content; **422** `{error, char_cap}` over the cap. Error toast = the server error or "Error saving reference.".
- **Delete:** the same dialog as 4.1, then navigate to `/references`.
- **Reference TTS (SpeakerIcon `itemId`):**
  - `GET /api/external/items/<id>/audio` → 200 `{tts_url}`, 202 `{status:'generating', progress, task_id}`, or 404.
  - `POST /api/external/items/<id>/tts` → 202 `{task_id, status:'pending'}` or 200 `{tts_url}`.
  - SSE `GET /api/sse/items/<id>/tts-stream`; polling fallback `GET /api/external/items/<id>/tts-status` → `{status, progress, task_id, item:{id, audio_tts_url?}}`.
  - Chapters: `GET /api/external/items/<id>/tts-chapters` → `{chapters}`.
- **iOS embeds:**
  - Tweets: widgets.js in a `WKWebView`, or skip the embed and show the stored text plus an "Open on X" button. The stored text is complete for bookmarks and clips.
  - YouTube: a `WKWebView` with the nocookie embed URL, or open the YouTube app via `openURL`.

### 4.3 ReferenceFeedback (`components/ReferenceFeedback.js`)
- Two outline glyphs, `FiPlusCircle` (aria "Good quote") and `FiMinusCircle` (aria "Bad quote"). The chosen one is accent; the others are muted. Tapping the chosen one again clears it.
- `POST /api/external/items/<id>/feedback {feedback:'good'|'bad'|null[, node_id]}` → `{id, feedback, feedback_at, read_at}`.
- **A verdict also marks the item read.** Callers copy `read_at` from the response.
- `shared` prop (FeedPicks): the verdict came from another reply. Its tooltip reads "{label} (your rating from another reply)" until the user taps.
- Tap target: 40 px tall and icon+gap wide (#351, phone-friendly).
- Error toast "Could not save your feedback.".

### 4.4 ReferenceFooter
`{author (tweets link to https://x.com/<handle>)} · {formatDateTime(posted_at||fetched_at)} · {hostname without www., links to url}`.

### 4.5 FeedPicks + ReadReply (inside the thread view; owner-only)
These render inside NodeDetail for a Community Archive "Read" reply. They are documented here because they are reference UI; the thread-view mapper owns placement.
- **FeedPicks:**
  - Shown for legacy replies only (`feed_picks_count > 0` and the content has no `{quote_ext:N}` markers).
  - `GET /api/nodes/<node_id>/feed-picks` → `{node_id, picks:[{rank, relevance (0–100), recommended, picked_by ('random'→"random sample"), why, item:{…serialized item, content, feedback_shared?}}]}`.
  - Ordered list. Left margin: `relevance%`, plus a filled mark if recommended. Head: `@handle` (link) and "recommended". Then the tweet markdown, the italic `why`, and a foot: posted date, "Open on X", ReferenceFeedback (`nodeId`), ReferenceReadToggle (`nodeId`).
  - Read picks fade to 55 % opacity.
  - **"Open on X"** also does `POST /external/items/<id>/read {node_id, via:'open'}`, which marks the pick read.
  - Bottom: "Mark all as read" (`POST /api/nodes/<id>/feed-picks/read` → `{read_at:{item_id: iso}}`) or "All read.".
  - Error line "Could not load the picks."; toasts "Could not update the read mark." and "Could not mark the list as read.".
- **ReadWindowLine:** `Tweets from {formatDateTime(window_start)} to {formatDateTime(window_end)} (your time): {tweets} by {accounts} accounts.`, plus ` {excluded} you had already read were left out.` when excluded > 0. Numbers use en-US grouping. Nothing renders without `window_end`.
- **ReadReplyTail:** `{total} unread` / `{unread} of {total} unread` plus a "Mark all as read" button, or "All read.". Silent until the quotes have loaded or when total is 0.
- **ReferenceReadToggle:** the same read endpoints with an optional `{node_id}`. The DELETE sends a JSON body; URLSession allows a body on DELETE.

---

## 5. Prompts (`pages/PromptsPage.js`, `pages/PromptDetailPage.js`) — craft mode only in the menu

**Purpose:** view, edit, version, and revert the user's copy of each system prompt.

**List `/prompts`:**
- `GET /api/prompts/` → `{prompts:[{prompt_key, title, preview (150 chars), version_number (count of the user's edit rows; 0 = default), generated_by ('default'|'user'|'revert'), created_at|null, default_updated}]}`.
- Hidden keys: `reflect`, `orient`. Admin-only: `read`, `read_thread`.
- Visible to everyone: `profile_generation`, `profile_update`, `profile_integration`, `narrative_detection`, `letter_from_the_future`, `orient_apply_todo` ("Apply to Todo"), `voice` ("Voice Mode"), `textmode` ("Text Mode").
- Page: H1 "Prompts"; subtitle "System prompts that power Loore's AI features"; divider.
- Rows: title (plus a 6 px accent dot when `default_updated`, tooltip "Default prompt has been updated"), `v{n} · {label} · {date}` (labels: default/"edited manually"/"reverted"; date fallback "default"), and a one-line preview with ellipsis.
- Loading: "Loading...".

**Detail `/prompts/:promptKey`:**
- `GET /api/prompts/<key>` → `{prompt:{id|null, prompt_key, title, content, generated_by, created_at, version_number, default_updated}}`; 404 "Unknown prompt key" → "Prompt not found.".
- Back link "← All prompts".
- Header: H1 title, chip `v{n} · {date}` (a click enters edit mode or saves), "history". Meta: the label, plus ` · date`.
- **"Default updated" banner** (when `default_updated`):
  - Text "The default prompt has been updated." with actions:
    - **View new default**: `GET /api/prompts/<key>/default` → shows the content in a 300 px scroll box.
    - **Accept new default**: `POST /api/prompts/<key>/revert-to-default`.
    - **Dismiss**: `POST /api/prompts/<key>/acknowledge-default`.
- **Edit:**
  - A monospace 500 px textarea, Save/Cancel, ⌘Enter. There is no Esc binding here.
  - `PUT /api/prompts/<key> {content}` → `{prompt}`.
  - 400 errors carry user-facing text about `{user_export}` placeholders or the plan gate. Show it as a toast for 10 s; the fallback is "Failed to save prompt.".
- **Display:** MarkdownBody of the prompt text. Prompts contain `{placeholders}`; render them literally.
- **History:**
  - `GET /api/prompts/<key>/versions` → the user's rows plus a final `{id:'default', generated_by:'default', created_at:null, version_number:0}`.
  - Content: `GET /api/prompts/<key>/versions/<id>`, or `/default` for `'default'`.
  - Revert: `POST /api/prompts/<key>/revert/<id>`, or `/revert-to-default`.
- **iOS:** low-traffic power-user surface. It can be a plain list plus a monospace `TextEditor`, or stay on the web (§15).

---

## 6. Account (`pages/AccountPage.js`) — `/account`

Max width 600 px. H2 "Account", a section "Settings" (h3). Deep-link anchors `#email`, `#x`, `#model`, `#references`, `#craft` scroll the row into view; the changelog links to them. iOS: a `Form` with `ScrollViewReader`, plus deep-link handling.

**Account info:**
1. **Username.**
   - Input and a **Save** button ("Saving..."), disabled when the value is unchanged.
   - Client validation: non-empty ("Username cannot be empty."); ≤ 64 characters ("Username must be 64 characters or fewer."); `^[a-zA-Z0-9_]+$` ("Only letters, numbers, and underscores allowed.").
   - Enter or ⌘Enter saves.
   - `PUT /api/dashboard/user {username}` → `{user}`. Success message "Username updated.". Errors: the server `error` (reserved name, taken, or a former handle of another account) or "Failed to update username.". 409 on a race: "Your profile changed in another request. Reload and try again.".
   - Helper "Letters, numbers, and underscores only."
2. **Email** (#260; verification flow).
   - Input type email, placeholder = the current email or "Add an email to sign in with". Button **"Send confirmation link"** ("Sending...").
   - **Exactly one request in flight** (a ref guard): a double tap would mail two links and void the first. Plain Enter and ⌘Enter each send once.
   - `POST /api/dashboard/email {email}` → `{message, email, pending_email, pending_email_expired}`. Errors 400: "Please enter a valid email address." / "That is already your email address."; 502 "Could not send the confirmation email. Please try again.".
   - Merge `email`, `pending_email`, and `pending_email_expired` into the user.
   - **Pending notice:**
     - Normal: "Confirmation link sent to {pending}. Open it to make it your sign-in address. Nothing after a few minutes? Check the spelling; an address that already signs in to Loore can't be added here."
     - Expired: "The confirmation link sent to {pending} has expired."
     - Inline actions: **Resend** (or **Send a new link** when expired), which POSTs the same address; the success message is "New link sent. The earlier one no longer works.". And **Cancel** → `DELETE /api/dashboard/email/pending`; failure "Could not cancel the pending change.".
   - Helper: with an email, "The new address becomes yours once you confirm it from the link we send to it."; without, "You currently sign in with X only.".
   - **Remove email** is shown only when the user has both an email and X login → `DELETE /api/dashboard/email`. Success "Email removed. You sign in with X."; the server refuses with "This email is your only way to sign in. Add another address first.".
   - The server takes an already-registered address silently: the same pending UI appears, and the mail tells the inbox owner the address is in use. This avoids leaking which addresses have accounts.
3. **X** (#311).
   - Connected: a disabled field "Connected as @{handle}" (or "Connected"). Helper "Sign in with X opens this account.", plus **Disconnect X** (only if an email exists) → `DELETE /api/dashboard/x` → `{twitter_login:false, twitter_handle:null}`. Messages "X disconnected. You sign in with email." / the server error "X is your only way to sign in. Add an email first." / "Could not disconnect X.".
   - Not connected: the link **"Connect X"** → a full-page navigation to `GET /auth/x/connect`. That redirects to X OAuth, and the flow comes back to `{FRONTEND_URL}/account?x_login=<outcome>#x`.
   - Outcome messages:
     - `linked` "X connected. Sign in with X now opens this account."
     - `cancelled` "X was not connected."
     - `failed` "Could not read your X account. Please try again."
     - `other_x` "This account is already connected to a different X account."
     - `taken` "That X account already signs in to another Loore account, so it can't be connected here. If that account was made by accident, write to info@loore.org and we'll remove it so you can connect X here."
     - `taken_placeholder` "An account was already set up for that X account, and no one has signed in to it yet. Write to info@loore.org and we'll join it with this one."
     - Unknown values show nothing. The query parameter is then removed from the URL.
   - Helper when not connected: "Lets you sign in with X as well. X will ask you to allow Loore."
   - **iOS:** this OAuth flow depends on the Flask session cookie (`session['x_connect']`, then flask-dance). `ASWebAuthenticationSession` does not share the app's `URLSession` cookies. Options:
     - (a) Copy the session cookie into the auth session. Not possible with ASWebAuthenticationSession.
     - (b) Run it in a `WKWebView` whose `WKHTTPCookieStore` is seeded with the session cookie, and intercept the final `/account?x_login=` redirect.
     - (c) Keep Connect X on the web.
   - The same applies to X bookmarks connect (§7.2).
4. **Plan:** read-only field showing `plan` capitalised (default "free").

**Settings.** Every change saves immediately via `PUT /api/dashboard/user {field: value}` → `{user}`. Errors are silent; the select is disabled while saving.
- **Default model:** ModelSelector (`GET /api/nodes/models` → `{models:[{id, name, provider, featured, read}]}`; `GET /api/nodes/default-model` → `{suggested_model, source}`). Collapsed list = featured models (Anthropic first) plus the selected model if not featured, then "More models…", which expands to all models grouped Anthropic/OpenAI. Saves `{preferred_model}`; 400 "Model not offered: …". Helper "Used for profile generation and LLM responses." (The component is covered by the thread-view map; ModelSelector.test.js pins it.)
- **Default privacy:** Private / Circles (coming soon, disabled) / Public → `default_privacy_level`. Helper "Default visibility for new entries."
- **External references** (only if `external_content_available`): Off / "On (experimental)" → `external_content_enabled`. Helper: "Let Loore also search your saved external references (imported tweets, bookmarks and clipped web pages) during conversations, and quote what it finds. Your own archive is always searchable; this switch only adds references. Import bookmarks or set up the Chrome clipper on the Import page. Experimental." ("Import page" is a link.)
- **Public sharing** (only if `share_v1_available`): Off / "On (experimental)" → `public_sharing_enabled`. Turning it off unpublishes everything from the open web immediately. Helper "The public side of Loore: publish shares to your public page and the Commons, and respond in public threads. Experimental."
- **Default AI usage:** None / Chat / Train → `default_ai_usage`. Helper "Controls how AI can use your new entries by default."
- **Craft mode** (with CraftIcon): Off / On → `craft_mode`. Helper: "Shows extra controls for people who want to steer the details: privacy and AI usage on each entry, the auto-generate switch and model picker on threads, audio upload, prompt editing and data export. Off is the simpler Loore. Menu items and controls added by craft mode carry the sliders icon."
- **AI Preferences:** a row button "How AI interacts with you … View" → `/artifacts/ai_preferences`. Helper "Tone, style, boundaries. Updated automatically during Voice sessions."
- `prefill_consent` (yes/no) is also accepted by `PUT /dashboard/user`. PrefillConsentCard says "change your mind later in Settings", but **Account has no prefill row today**.
- `PUT /dashboard/user` rejects `email` with a 400 pointing to the verification route. Description is capped at 128 characters (not shown on this page).

---

## 7. Import (`pages/ImportPage.js` = `ImportData inline` + `ExternalImport`) — `/import`

H2 "Import Data". Max width 600 px. **This is the most browser-dependent page.**

### 7.1 ImportData (your own writing → nodes)
There are four entry points. `ImportPage` renders them inline as labelled file pickers; `WelcomePage` renders a button that opens a picker modal titled "Import Data". Every one accepts **`.zip`** only.

| Label | Client work | Analyze call | Confirm call |
|---|---|---|---|
| **Import Claude** | Unzip in the browser (JSZip); take the entry ending `conversations.json`, else the largest `.json` whose parsed top level is an array. Only that JSON is uploaded. | `POST /api/import/claude/analyze` multipart `conversations_file` (filename `conversations.json`) → `{conversations:[{name, created_at, messages:[{text, sender, created_at, uuid}], message_count, token_count}], total_conversations, total_messages, total_tokens, total_size}` | `POST /api/import/claude/confirm {conversations, privacy_level, ai_usage[, on_deleted]}` (synchronous) |
| **Import ChatGPT** | Same client-side extraction | `POST /api/import/chatgpt/analyze` multipart `conversations_file` → same shape | `POST /api/import/chatgpt/confirm {conversations, privacy_level, ai_usage[, on_deleted]}` (synchronous) |
| **Import Markdown (e.g. Obsidian)** | Upload the whole zip | `POST /api/import/analyze` multipart `zip_file` → `{files:[{name, filename_without_ext, content, size, modified_at, token_count}], total_files, total_tokens, total_size}`. Only `.md`, UTF-8, `__MACOSX/` skipped; timestamps from zip metadata. | `POST /api/import/confirm {files, import_type:'separate_nodes'|'single_thread', date_ordering:'modified'|'created', privacy_level, ai_usage[, on_deleted]}` (synchronous; it always sorts by `modified_at`, whatever `date_ordering` says) |
| **Import Tweets** | Upload the whole zip (an X data export) | `POST /api/import/twitter/analyze` multipart `zip_file` → `{import_token, total_tweets, original_count, reply_count, skipped_retweets, total_tokens, original_tokens, total_size}`. The tweets stay stashed server-side. | `POST /api/import/twitter/confirm {import_token, import_type, include_replies, privacy_level, ai_usage[, on_deleted]}` → **202** `{task_id, status:'queued', total}`; then poll `GET /api/import/status/<task_id>` every **1.5 s** → `{status:'queued'|'running'|'completed'|'failed', done, total, result, error}` |

**Analyze phase:**
- While working, the options are replaced by a spinner with a stage label: "Extracting…" (client unzip), "Analyzing…" (upload), "Importing…" (confirm).
- Errors show above the options in accent colour: the server `error` or "Error analyzing … Please try again.".
- ChatGPT has richer messages:
  - `"{error}: {details}"` when both are present.
  - 413 → "conversations.json is too large to upload. Please contact support."
  - Other HTTP status → "Error analyzing ChatGPT export (HTTP {status}). Please try again."
  - No response → "Error analyzing ChatGPT export. The request did not reach the server — check your connection."
- Zip errors:
  - "Could not read the zip file. Please make sure it's a valid data export."
  - "Could not find conversations.json in the zip archive. Please upload the original data export."

**Confirm dialogs.** Each is a modal card (max 520 px) with a serif title and a short accent rule.
- **"Confirm Import"** (Markdown):
  - "Found **{total_files}** .md file(s) ({total_size} bytes)" and "Estimated tokens: **{n}**".
  - Import Type radios: "Import as separate top-level nodes (one thread per file)" / "Import as a single thread (all files connected sequentially)".
  - Date Ordering radios: "Order by modification date" / "Order by creation date".
- **"Confirm Claude Import"** / **"Confirm ChatGPT Import"**: "Found **{n}** conversation(s) with **{m}** messages", "Estimated tokens: **{t}**", "Each conversation will be imported as a separate thread."
- **"Confirm Twitter Import"**:
  - "Found **{total_tweets}** tweets ({original_count} original, {reply_count} replies)", plus "Skipped {n} retweets" when > 0.
  - "Estimated tokens: **{…}** ({count} tweets)". The numbers depend on the checkbox "Include replies ({reply_count})".
  - Import Type radios: "… (one node per tweet)" / "… (all tweets connected sequentially)".
- All four include **PrivacySelector**:
  - "PRIVACY LEVEL" select: "🔒 Private · Only I can see this" / "👥 Circles · Shared with specific groups (coming soon)" / "🌐 Public · Anyone can see this", with descriptions "This note is private and only visible to you." / "…shared with your selected circles (feature coming soon)." / "This note will be visible to all users.".
  - "AI USAGE" select: "🚫 None · No AI access" / "💬 Chat · AI can use for responses" / "🧠 Train · AI can use for training", with descriptions "AI will not access this note." / "AI can read this note to generate responses, but won't use it for training." / "AI can use this note for training data to improve the model.".
  - **Import defaults: privacy `private`, AI usage `none`.** These are not the account defaults.
- Buttons **Confirm Import** (the spinner shows `{done} / {total}` during a Twitter import) / **Cancel**. A backdrop tap or Esc cancels, except while importing or while a nested prompt is open.

**409 deleted-content conflict** (all four confirms):
- Response `{error:'deleted_content_matches', deleted_matches:N}` → modal **"Previously Deleted Content"**: "**{N}** message(s) in this import match(es) content you previously deleted. Restore them/it, or keep them/it deleted?"
- Buttons **Restore deleted content** (retry with `on_deleted:'restore'`), **Keep it deleted** (retry with `'skip'`), **Cancel import**.

**Result:**
- Response (201, or the Twitter task `result`): `{message, nodes_created, thread_count, profile_update_task_id, created, skipped, restored, updated[, empty]}`.
- Modal **"Import Finished"**: big serif numbers for Imported (always shown; accent if > 0) and, when > 0, Restored, Updated, Skipped, and "No text".
- Notes:
  - Nothing new: "Everything in this archive was already imported — nothing new was added."
  - Updated > 0: "Updated items were already imported; their privacy and AI-usage settings now match this import."
  - Skipped > 0: "Skipped items were already imported and left untouched."
  - Empty > 0: "Posts with no text — usually media-only tweets — were not imported. There was nothing to write."
- **OK** (or a backdrop tap) runs `window.location.reload()`. That reload is how the app picks up `profile_update_task_id` / `profile_batch_pending` from the fresh `/dashboard` and starts the watcher. iOS: refetch the user and start ProfileGenerationWatcher explicitly instead.
- Twitter polling failure: "Lost track of the import — reload the page to check whether it finished." Task failure: the task error, or "Error importing Twitter data. Please try again.".

**iOS equivalents and risks:**
- Picking the zip: `.fileImporter(allowedContentTypes: [.zip])` / `UIDocumentPickerViewController`. Exports usually sit in Files/iCloud Drive or arrive via the share sheet. Also register the app as an "Open in…" target for `.zip` (and `.json` for bookmarks) so users can share an export from Mail or Files.
- Client-side unzip: Foundation has no zip reader. Use **ZIPFoundation** (SwiftPM) to stream-extract `conversations.json` without loading the whole archive. ChatGPT exports with media can be GBs; do not read them into memory.
- Upload: `URLSession.uploadTask(fromFile:)` with a multipart body written to a temp file. Use a background session for large files. **200 MB hard limit** (nginx); no chunking exists for imports. `utils/chunkedUpload.js` is for audio (thread/voice mapper).
- Round trip: the Claude, ChatGPT, and Markdown analyze responses **echo back all the content**, and confirm **posts it all again** as JSON. Large archives mean two very large bodies, and the confirm runs synchronously inside nginx's **60 s** window. A long confirm may 504 while the server keeps working. On a timeout, iOS should say so and suggest checking the Log rather than retrying blindly; dedup keys make a retry mostly harmless (skipped).
- A Twitter archive is uploaded whole, including media, within the 200 MB limit.

### 7.2 ExternalImport — "Import References" (third-party content → references, never profile)
Header "Import References". Help text: "Content you've saved elsewhere, made searchable next to your own writing (Cmd+K → Semantic)." When counts > 0 it adds " Imported so far: {n} archive tweets · {n} bookmarks · {n} clipped pages." and a **View references** button → `/references`.

**Load:** `GET /api/external/items?per_page=1` (only for `counts`) and `GET /api/external/twitter/status` → `{configured, connected, revoked, handle, last_synced_at, last_sync_created}`. `GET /api/external/tokens` → `{tokens:[{id, name, prefix, scope, created_at, last_used_at}]}`.

**Card "Community Archive":**
- Help "Fetch tweets from the open Community Archive — any account that donated its archive. Try your own handle or someone you follow."
- Input placeholder "@username", button **Fetch tweets**.
- `POST /api/external/community-archive/fetch {username[, max_items≤10000, default 2000]}` → 202 `{task_id}`.
- Status "Fetching in the background — counts update below." Then counts are polled every 5 s, 12 times. Error: the server error or "Fetch failed.".

**Card "X Bookmarks"** (anchor `#x-bookmarks`; the "X disconnected" notice links here):
- Revoked: "X disconnected (@h) — access was revoked or expired. Reconnect to resume nightly bookmark sync." plus **Reconnect X**.
- Connected: "Connected as @{handle} · last synced {YYYY-MM-DD}", button **Sync bookmarks** ("Syncing…"), and "New bookmarks sync automatically once a night — the button is just for syncing right now."
  - `POST /api/external/twitter/sync` → 202; 400 "X account not connected".
  - Then poll `twitter/status` plus the counts every 3 s, up to 20 times, until `last_synced_at` changes.
  - Results: "Synced — {n} new bookmark(s)." / "Synced — no new bookmarks."; revoked → "X access was revoked — reconnect below."; timeout → "Still syncing in the background — check back in a minute."
- Configured but not connected: "Connect your X account to pull in your bookmarks. X's API serves roughly the 100 most recent; after that, new bookmarks sync in nightly." plus **Connect X**.
  - Connect is a full-page navigation to `GET /api/external/twitter/connect` (PKCE; session-stored verifier). The callback redirects to `/import?x_connect=ok|failed`; **the page shows nothing for that parameter**.
  - The cookie problem is the same as Account/Connect X (§6).
- Not configured: "Direct sync isn't configured yet. You can still import a bookmarks JSON export:"
- **Import bookmarks JSON** ("Importing…"), shown when X sync is not configured, or in craft mode (then with the note "Craft: import a bookmarks JSON export (browser-exporter format) — covers bookmarks beyond the API's recent window.").
  - Accepts `application/json`. The file is read and parsed on the client, then posted as JSON: `POST /api/external/bookmarks/import` with `{bookmarks:[…]}` (or the object as-is).
  - Returns `{created, skipped, unrecognized}` → "Imported {n} bookmark(s) ({skipped} already known)."; error "Import failed — is it valid JSON?". The server also accepts multipart `file`.
  - iOS: `.fileImporter([.json])`, then post the Data as the JSON body.

**Card "Chrome clipper"** (only if `external_content_enabled`):
- Explainer: "Save any open tab into your references with one key press. The extension reads the page in your browser, sends the text to Loore, and closes the tab. Clips are references, not your writing: they are searchable and quotable, and never enter your profile."
- Install text: "open `chrome://extensions` (tap to copy; "Copied" tip for 1.5 s), turn on Developer mode, choose "Load unpacked" and pick the `extension/` folder of the Loore repository. Then paste a token below into the extension's options. A token can only add references; it cannot read anything."
- Token rows: name, `loore_{prefix}…`, "created YYYY-MM-DD", "last used YYYY-MM-DD" or "never used", and **Revoke** → `DELETE /api/external/tokens/<id>`.
- **Create token** → `POST /api/external/tokens {name:'Chrome clipper'}` → 201 `{…, token}`. The plaintext is returned once. Errors: 403 "Turn on external content under Account first."; 400 "You already have 10 active tokens; revoke one first.".
- Page messages: "Could not create a token." / "Could not revoke the token.".
- **NewTokenDialog:**
  - Title "Your clipper token".
  - Body "Paste it into the extension's options now. It is shown only this once: Loore keeps just a fingerprint of it, so after you close this there is no way to see it again. If it gets lost, revoke it and create another."
  - The token box copies on tap (icon becomes a green check for 1.5 s); hint "Click the token to copy it."
  - A single button **"Done, I've saved it"** with subtext "Closes this for good."
  - **No backdrop or Esc dismiss, on purpose.** iOS: `.interactiveDismissDisabled(true)`; copy with `UIPasteboard`.
- **iOS equivalent of the clipper: a Share Extension.** This is the natural mobile replacement and a strong addition.
  - Safari: an `NSExtensionJavaScriptPreprocessingFile` bundling the same `extension/vendor/Readability.js` and `turndown.js` plus logic like `extension/capture.js` returns `{url, title, content (markdown), author, posted_at, author_protected}`.
  - Other apps sharing a bare URL: extract in a hidden `WKWebView`, or send the URL with minimal content (`content` is required and must be non-empty).
  - Post to `POST /api/external/clip`. Response 201 `{created:true, id, source, title, truncated}`; 200 on a re-clip `{created:false, updated, …}`. The server truncates at `NODE_CHAR_CAP` and classifies tweets vs pages by URL.
  - Auth from the extension: either the shared session cookie (App Group `HTTPCookieStorage(forSharedContainerWithIdentifier:)`), since session auth works on this route without the external-content toggle, or a minted bearer token stored in a shared Keychain group. A token requires `external_content_enabled`.

---

## 8. Share and Commons (dark behind `SHARE_V1` + user opt-in; `user.share_v1_enabled`)
Both pages render "Not available." when `share_v1_enabled` is false. Every backend route 404s when the flag is off for this user.

### 8.1 SharePage — `/share`
**Purpose:** drafts of writing to give outward. Nothing is public until **Publish**, and publishing can be revoked.

**Endpoints (`backend/routes/share.py`, no trailing slash):**
- `GET /api/share` → `{shares:[{id, content, share_type, status ('draft'|'published'|'revoked'), source_node_id, public_node_id, permalink ('/@user/slug' or null), created_at, updated_at, published_at, revoked_at}]}`, newest first.
- `POST /api/share {content, share_type}` → 201 share. An invalid type is coerced to `other`.
- `PATCH /api/share/<id> {content?, share_type?}`. 409 "Revoke before editing a published share".
- `DELETE /api/share/<id>` → `{status:'deleted'}`. Deleting a published share soft-deletes its public node.
- `POST /api/share/<id>/publish` → the share. It creates a public root node with a slug, or undeletes the prior node when the content is unchanged. `ai_usage` = the account default.
- `POST /api/share/<id>/revoke` → the share (soft-deletes the public node; 409 if not published).
- `POST /api/share/save-proposal` belongs to the thread view (AI-proposed shares).

**UI:**
- Header: H1 "Share", a round **+** (title "Write a new share"), and a right-aligned link "Commons →".
- Intro: "Pieces of your writing worth giving outward — nothing is visible to anyone until you publish it, and you can take anything back."
- Groups in order **Published**, **Drafts**, **Revoked** (uppercase headers).
- Card:
  - A type badge (`need | offering | insight | exploration | intention | other`).
  - Date label: "published {date}" / "revoked {date}" / the created date.
  - The content as markdown.
  - A published card is tappable → the permalink or `/node/<public_node_id>`. iOS: open the thread view on `public_node_id`.
- Card actions:
  - Draft/revoked: **Edit**, **Publish**, **Delete**.
  - Published: **Revoke**, **Delete**.
- **Publish confirm** (inline): "Where should this go? Publishing to Loore puts it in the Commons and on your public page." ("your public page" links to `/@username`). Button **Publish to Loore**; disabled chips "Twitter / X · coming soon" and "Substack · coming soon"; **Cancel**.
- **Delete confirm** (inline): "Delete this share?" with **Delete** (error colour) / **Cancel**.
- **Edit/new form:** an autofocused textarea (placeholder "What would you like to give outward? (markdown)"), a type select, **Save** ("Saving...", enabled when content is non-blank), **Cancel**. ⌘Enter saves; Esc cancels. After any mutation, refetch the list.
- **Empty:** "Shares the AI proposes in conversation land here as private drafts — or start one yourself." plus "+ new share".
- Errors are logged only.

### 8.2 CommonsPage — `/commons`
**Purpose:** the public forum feed: public root nodes by everyone who has sharing on, newest first. No vanity metrics.

- `GET /api/commons/feed?page=N` (per_page 20, max 100) → `{items:[{id, username, permalink, content (600 chars + …), created_at, reply_count}], has_more, page}`.
- **Header:** H2 "Commons", a link "Share →", subtitle "What people here have chosen to make public.", rule.
- **Card:** username · `formatDate(created_at)`, markdown content, and a muted "{n} response(s)" when > 0.
- Tap → the permalink (`/@user/slug`) or `/node/<id>`. **iOS: open the thread view on `item.id` directly.** To resolve a permalink from a universal link: `GET /api/commons/permalink/<username>/<slug>` → `{node_id[, canonical]}` (no auth needed).
- **Pagination:** "Load more..." click only; there is no auto-scroll here.
- **States:** "Loading...", "Error loading the commons.", "Nothing public yet.".

---

## 9. UpdatesModal (`components/UpdatesModal.js`) — the dev-update channel, shown at launch

**Trigger (App.js):** once per session, after the user is loaded, approved, and `terms_up_to_date`: `GET /api/updates` → `{changelog:[{id, title, date, body}], notifications:[{id, type, title, body, link, created_at, meta}], polls:[{id, question, created_at, draft_terms:{model, data_source}, response}]}`. The modal opens only when the combined count is > 0, and it is suppressed while the Terms modal shows.

**Card:**
- Header "While you were away"; sub "What's new in Loore since your last visit."
- Items in order: notifications, then polls, then changelog.
- The modal closes itself when no items remain. A backdrop tap or Esc closes it; remaining items come back next visit.
- Mobile (≤640 px): full-width card, max-height = innerHeight − 72 px, so the dimmed app stays visible (a fix for iOS Safari `vh`). iOS: a sheet with detents.

**Changelog item:**
- Date eyebrow, serif title, markdown body.
- **Later** → `POST /api/updates/changelog/<id>/skip`. **Got it** → `POST /api/updates/changelog/<id>/read`.
- Tapping an internal link in the body: record a skip, close the whole modal, then navigate in-app.
- The iOS markdown renderer needs a link handler that maps in-app paths (`/account#model`, `/artifacts/…`, `/import#x-bookmarks`) to native routes. This is the main reason the app needs a URL→screen router.

**Notification item:**
- Eyebrow by type: `fix_ready` "Your issue has been fixed", `issue_declined` "Your issue — closed without a fix", `x_disconnected` "Action needed".
- For `profile_ready`: a stamp `v{version} · {date}` from `meta`.
- Title and body text.
- **Later** → `…/notifications/<id>/skip`. **Take a look** (if `link`) records a skip, then: an external URL opens a browser and the modal stays open; an in-app path closes the modal and navigates. **Got it** → `…/read`.

**Poll item** (two-phase opt-in):
- Eyebrow "A QUESTION FROM THE DEVELOPER", then the question.
- Note: "Answering is optional. If you ask for a draft, {model} will read {source} to write one for you to edit — and nothing is sent until you press Send." Source labels: `derived` → "your profile, recent summary and intentions"; `recent_window` → "your recent writing (as much as fits its context window)".
- Buttons: **No thanks** (`POST /api/updates/polls/<id>/decline`, permanent), **Later** (local only), **Write my own** (shows the textarea), **Draft with AI**.
  - Draft with AI: `POST /api/updates/polls/<id>/draft` → 202 `{response:{status:'drafting'}}`. 403-style error: "Your AI-usage setting doesn't allow this. You can …"; 409 closed/sent.
  - While drafting: "{model} is drafting an answer in the background — this can take a few minutes. You can close this and come back later." Poll `GET /api/updates/polls/<id>` every **5 s** until the status leaves `drafting`.
  - Status `draft` → fill the textarea. `draft_failed` → "Drafting didn't work this time — you can still write your own answer."
- With a draft: "Drafted by {model} from {source} — please review and edit before sending." Textarea placeholder "Your answer…".
- **Send to developer**: `PUT /api/updates/polls/<id>/response {content}`, then `POST …/send`. Errors: the server error or "Sending failed — try again?".

**iOS:** show at launch the same way. Consider also a persistent "What's new" entry, since dismissing the sheet hides items until the next launch.

---

## 10. ProfileGenerationWatcher (`components/ProfileGenerationWatcher.js`), app-wide, renders nothing

- **Starts when:** `user.profile_generation_task_id || user.profile_batch_pending` (from `/dashboard`). (`loore_profile_started` is also listened for, but nothing fires it.)
- **Polls** `GET /api/export/profile-progress[?task_id=<last sync task id>]`:
  - Every **60 s** for the batch pipeline, every **5 s** for sync. 10 s request timeout, `Cache-Control: no-cache`.
  - No duration cap. Stops after **10 consecutive errors**.
- **Response:** `{running, source:'batch'|'sync'|null, status, progress, message, task_id, error, latest_profile:{id, generation_type, created_at}|null[, batch_step_failed]}`.
  - Batch messages: "Generating profile", "Generating profile: Chunk n of ~N", "Integrating profile versions" (95 %).
  - Sync: status from Celery (`pending`/`processing`/`progress`/…); progress and message from the task meta.
- **While running:** remember the first `latest_profile.id` seen; remember the sync `task_id`; broadcast progress.
- **Terminal outcome:**
  - `failed` → failed.
  - `stalled`, or `idle` + `batch_step_failed` after running → stalled.
  - `completed`, or `idle` with a new `latest_profile.id` → completed.
  - `idle` after running with no new version → stalled.
  - `idle` without ever having seen it running → no outcome (a stale flag); stop quietly.
- **Toasts:** "Your profile has been updated ✓" (6 s) / "Profile generation failed" / "Profile generation stopped before finishing — it will be retried in the background".
- **Cleanup:** clear `profile_generation_task_id` and `profile_batch_pending` in the cached user; fire `loore_profile_done`. If polling gave up through errors: stop with no toast (pinned by a test).
- **iOS:** a singleton service with a `Task` loop while the app is foregrounded. When backgrounded, stop and re-check on `scenePhase == .active`. A local notification on completion is optional; the backend has no push support.

---

## 11. Onboarding-adjacent pages

### 11.1 PrefillConsentCard (`components/PrefillConsentCard.js`) — hosted by AlphaThankYouPage and WelcomePage
- **Shown only if** `user.twitter_login && !user.prefill_consent`. After the user answers, the card stays to show the confirmation line.
- Eyebrow: the `delayHint` prop, default "While you wait" ("Already on X?" on Welcome).
- Question "Start from what you've already written?"
- Body: "With your okay, we'll bring in your **public tweets** as @{username}, keep them private in your Loore, and write first drafts of your artifacts from them — like your profile and your intentions — so you don't begin from a blank page. Only your own public posts; nothing is published, and every draft is yours to edit or delete."
- Two equal-weight buttons, nothing preselected: **"Yes, seed from my tweets"** / **"Not now"** ("Saving…" on the tapped one). → `PUT /api/dashboard/user {prefill_consent:'yes'|'no'}` → `{user}`.
- Footnote "You can change your mind later in Settings." (There is no such setting yet; see §6.)
- Answered yes: "Noted. We'll seed your Loore from your public tweets and have first drafts of your artifacts — like your profile and intentions — waiting when you come in."
- Answered no: "Noted. You'll start from a blank page — you can always seed from your tweets later, from Settings."
- Error "Couldn't save your answer. Please try again."

### 11.2 WelcomePage — `/welcome` (protected)
- Reached from the admin "Activate & Welcome" magic link (`next_url=/welcome`); no in-app link. iOS: handle it as a universal-link landing, or show it once after first approval.
- **Hero:** logo; "Welcome to *Loore*."; "You're one of the first people here. This is an alpha — things are raw, evolving, alive. Your experience and your feedback shape what Loore becomes."
- **Card "Your first entry":** "What brought you to Loore — and what are you hoping to find here?" and the CTA **"Start writing"**, which opens the global "Write New Entry" modal (NodeFormModal with `allowAgenticPrompt`; on success it navigates to `/node/<id>[?awaitLlm=…]`). Note: "You can type or record a voice note — whatever feels natural."
- PrefillConsentCard.
- **Card "Already have a journal?":** "Import your Obsidian journals, markdown files, or exported tweets. Your lore doesn't start from zero." and an **"Import data →"** button that opens the import picker modal (the four options from §7.1).
- Link "See practical tips & workflows →" → `/how-to`.
- Closing lines: "There's no wrong way to do this." / "Write about today. Talk about a dream. Process something that's been sitting in you. Loore will meet you wherever you are." / "If something's broken or feels wrong, tell us. This is ours to shape together."
- Fade-in animations (`utils/Fade`).

### 11.3 ConfirmEmailPage — `/confirm-email?token=…` (not protected; unapproved users use it too)
- **On load:** once the user is known and a token is present, call once per visit (guarded against double effects) `POST /api/dashboard/email/confirm {token}` → `{message:'Email confirmed.', email, pending_email, pending_email_expired}`. Merge the email state into the user.
- **Errors:**
  - 400 `{reason:'invalid_or_expired'}` "That confirmation link is invalid or has expired. Request a new one."
  - 403 `{reason:'other_account'}` "This confirmation link was requested from a different Loore account. Sign in to that account to use it."
  - 409 `{reason:'taken'}` "That email was claimed by another account before you confirmed it."
  - Network or 5xx → reason `failed`.
- **States (heading / body / action):**
  - loading: "Confirming your email" / "One moment."
  - No token: "This link is incomplete" / "Open the link from the confirmation email again, or request a new one." / back link.
  - Signed out: "Sign in to confirm" / "A new sign-in email can only be confirmed from inside the account that asked for it. Sign in to that account the way you usually do, not with the address you are confirming, and you will come straight back here." / "Sign in →" (`/login?returnUrl=…`).
  - OK: "Email confirmed" / "**{email}** is now your sign-in address." / back link.
  - Error: "Not confirmed" / the server text (for other_account, plus " You are signed in as @{username}.").
    - `failed` → a **Try again** button (safe; confirming is idempotent).
    - `other_account` → "Sign out and use the other account →" (`/auth/logout?next=<this page>`).
    - Otherwise → back link.
  - Back link: approved → "Back to your account" (`/account#email`); unapproved → "Continue" (`/alpha-thank-you`).
- **iOS:**
  - The mailed link is `https://loore.org/confirm-email?token=…`. Because the token only counts **inside the session of the requesting account**, the link must open **in the app** (a Universal Link; needs `apple-app-site-association` for `/confirm-email`) so the app's cookie session posts it.
  - Opened in Safari, it only works if Safari has the same account signed in.
  - Without Universal Links, the fallback is a "paste the link" field, or telling users to confirm on the web.

---

## 12. AdminPanel (`components/AdminPanel.js`) — `/admin`, admins only. Recommend it stays on the web.
Tabs: **Users**, **Activity**, **Feedback**, **Polls**.
- **Users:**
  - "Whitelist a New User" form: `POST /api/admin/whitelist {handle, x_lookup}`, with an X-lookup confirm dialog.
  - Users table: `GET /api/admin/users`.
  - Per-user actions:
    - Approve toggle: `POST /users/<id>/toggle`.
    - Spam toggle: `POST /users/<id>/toggle_spam`.
    - `PUT /users/<id>/update_email`, `update_plan`, `update_spend_limit`.
    - **Activate & Welcome**: `POST /users/<id>/activate_and_welcome`, which mails the `/welcome` magic link.
    - Build profile: `POST /users/<id>/build_profile`.
    - Infer / cancel intentions: `POST /users/<id>/infer_intentions`, `cancel_intentions`.
    - Prefill from the Community Archive or X: `GET /admin/prefill/check`, `GET /admin/prefill/x/check`, `POST /users/<id>/prefill`, `POST /users/<id>/prefill-x`, then poll `GET /admin/prefill/status/<task_id>`.
- **Activity:** `GET /api/admin/activity?days=N`.
- **Feedback:** `GET /api/admin/feedback`, `PUT /api/admin/feedback/<id> {status}`.
- **Polls:** `GET/POST /api/admin/polls` (question, data source "Profile + recent + intentions" / "Recent writing (context window)", model from `/nodes/models`), `GET /admin/polls/<id>/responses`, `POST /admin/polls/<id>/close`.
- Backend-only admin routes, not used by this panel: `/api/admin/spend`, `/api/admin/voice-timing`.

---

## 13. Desktop-only or browser-only dependencies and iOS equivalents

| Web dependency | Where | iOS equivalent |
|---|---|---|
| `<input type=file accept=.zip>` + JSZip client-side unzip | ImportData (Claude, ChatGPT) | `.fileImporter([.zip])` + **ZIPFoundation** streaming extract of `conversations.json`; also accept "Open in Loore" from Files/Mail via `CFBundleDocumentTypes` |
| `<input type=file>` whole-zip multipart upload | ImportData (Markdown, Twitter) | `.fileImporter` + `URLSession.uploadTask(fromFile:)` (background session for big files); 200 MB cap |
| `file.text()` + `JSON.parse` | ExternalImport bookmarks JSON | `.fileImporter([.json])` → `Data` → POST as the JSON body |
| Chrome extension + personal API token | ExternalImport clipper card | **Share Extension** (Safari JS preprocessing with the bundled Readability/Turndown) → `POST /api/external/clip`; auth by the shared cookie or a Keychain token. Keep the token-management card on the web, or show it read-only. |
| `navigator.clipboard.writeText` | NewTokenDialog, `chrome://extensions` copy | `UIPasteboard.general.string` |
| Blob download (`<a download>`) | NavBar "Export data" (`/api/export/threads`, .txt) | download to a temp file → `ShareLink` / `UIActivityViewController` / `.fileExporter` |
| Full-page redirects to OAuth (`/auth/x/connect`, `/api/external/twitter/connect`) relying on the Flask session cookie | Account (Connect X), ExternalImport (Connect X bookmarks) | a `WKWebView` seeded with the session cookie that intercepts the final redirect (`/account?x_login=…`, `/import?x_connect=…`), or keep on the web. `ASWebAuthenticationSession` does not share app cookies. |
| `window.open(url, '_blank')` | "Open source", "Open on X", author links, external notification links | `SFSafariViewController` or `openURL` (to the X/YouTube apps) |
| Twitter `widgets.js` embed, YouTube nocookie iframe | ReferenceDetail | a `WKWebView` embed, or the stored text plus an open-in-app button |
| `beforeunload` unsaved-edit guard | ArtifactsPage | a confirmation dialog on navigation; `.interactiveDismissDisabled` |
| Hover affordances (row "+", kebab on hover) | Todo, Bubble | always-visible controls, swipe actions, context menus. The web already shows the kebab always on `(hover: none)`. |
| ⌘K, ⌘Enter, Esc | global, forms | `.keyboardShortcut` for hardware keyboards; the on-screen equivalents are buttons |
| `document.title`, `window.location.reload()` after an import | References, ImportData | not needed; refetch the user/state instead of reloading |
| Deep-link anchors `/account#model` etc. in changelog markdown | UpdatesModal → Account | an in-app URL router plus `ScrollViewReader` |
| Email links `/confirm-email?token=`, `/welcome` magic link | ConfirmEmail, Welcome | Universal Links (AASA) so the app session handles them |

---

## 14. Jest tests and what each pins down

- **`utils/intentions.test.js`**
  - Splits Endorsed/Inferred sections with entry counts.
  - Parses name, status (the italic line), body, and dated `- ` notes.
  - A status-only entry has an empty body and no notes.
  - Content with no `## ` entries yields no entries, which triggers the markdown fallback.
- **`utils/markdown.test.js`**
  - `stripInlineMarkdown` reduces `[t](u)` to `t`.
  - A link-bearing todo item toggles when keyed by its stripped label.
  - `insertItemAfter` places the new item below a link-bearing item.
- **`utils/references.test.js`**
  - `youtubeVideo` reads watch URLs, youtu.be, shorts, live, and embed paths, carries `t=`/`start=` over, and is null for channels, playlists, other sites, tweets, and bad ids.
  - `sourceLabel` returns "Video" for a YouTube clip and "Page" for other clips.
  - `bodyWithoutTitle` drops a leading heading equal to the title and keeps a heading that differs (and everything when there is no title).
- **`components/Bubble.test.js`** (7 tests on `splitPreview` and thread-name display)
  - The heading marker is stripped and the body separated.
  - Without a thread name, the first line is the title.
  - A thread name replaces a markdown heading and the rest stays as body.
  - With a thread name on plain text, every line stays as body; the same for a one-line plain entry.
  - A heading-only entry with a thread name shows just the name.
  - A blank thread name falls back to the title.
- **`components/FeedPicks.test.js`**
  - "Open on X" posts `{node_id, via:'open'}` and marks the pick read; "Mark as unread" remains to undo it.
  - Opening an already-read pick still logs the open and keeps the mark.
  - Rating a pick marks it read (it takes `read_at` from the feedback response).
  - A verdict given here clears `feedback_shared`, so it is no longer labelled as another reply's.
- **`components/ReadReply.test.js`**
  - The window line states the local date range, the counts, and "already read were left out".
  - Nothing is said when nothing was left out; no line renders without a window.
  - The tail's "Mark all as read" posts and reports back the `read_at` map.
  - An all-read list says "All read." with no other action.
  - The state is silent before the quotes load.
- **`components/ReferenceFeedback.test.js`**: the icon defaults to 16 px and passes its size to the CSS var; a caller-set size reaches both the icon and the CSS.
- **`components/ProfileGenerationWatcher.test.js`**
  - When polling gives up (consecutive errors) while stale `running` data is held, it still fires `loore_profile_done` once, shows **no** toast, and clears `profile_batch_pending` in the cached user.
  - A `failed` answer that also sets `error` is handled once, by the data path: one toast, one done event.
- **`pages/AccountPage.test.js`**
  - ⌘/Ctrl+Enter sends exactly one `POST /dashboard/email`.
  - Plain Enter sends one; a request in flight blocks further Enter or clicks.
  - A pending change shows Resend and Cancel; Cancel calls `DELETE /dashboard/email/pending` and adopts the server's `email`, which may have been confirmed elsewhere.
  - An expired link shows the "has expired" text and "Send a new link", which re-posts the same address and shows "New link sent".
  - "Remove email" is offered only with `twitter_login` and calls `DELETE /dashboard/email`.
  - Without X there is a "Connect X" link to `/auth/x/connect` and no Disconnect.
  - Connected shows "Connected as @handle" and Disconnect only when an email exists; an X-only account cannot disconnect.
  - `?x_login=taken#x` shows the "already signs in to another Loore account" text; an unknown outcome shows nothing.
  - "Disconnect X" calls `DELETE /dashboard/x`.
- **`pages/ConfirmEmailPage.test.js`**
  - Signed out: asks for sign-in with a return URL and posts nothing.
  - Signed in: posts the token exactly once, even under StrictMode, and updates the user.
  - A waitlisted account's "Continue" goes to `/alpha-thank-you`.
  - `other_account` says whose session this is (@username).
  - Signed out, it says which sign-in will not work.
  - No server answer offers "Try again", which posts again.
  - Without a token nothing is posted.
- **`pages/AlphaThankYouPage.test.js`** (hosts PrefillConsentCard and the waitlist email flow; auth mapper area)
  - The no-address form posts to the verified flow.
  - Pending state: says where the link went, with resend, and hints at the two reasons a link never arrives.
  - Expired state: offers a new link.
  - A mistyped pending address can be replaced or kept; "Use a different address" shows the form.
  - A confirmed address needs nothing more.
- **`utils/diff.test.js`** (VersionHistoryDrawer)
  - `computeLineDiff`: identical texts, an appended line, a changed line (del+add), a removed line, a mid insertion in a long text, empty old text (all adds).
  - `collapseUnchanged`: folds long runs with context; keeps short runs.
  - `refineWordDiffs`: similar pairs gain segments; dissimilar pairs stay unrefined; unpaired lines stay unrefined; multi-line blocks pair positionally.
- **`utils/date.test.js`**: `formatYmd` zero-pads; `formatDate` gives "Mon D, YYYY" and "today"; `formatDateTime` gives "yyyy/mm/dd HH:MM"; empty input gives the fallback.
- **`components/ModelSelector.test.js`** (Account default model): featured-first plus "More"; a non-featured selection listed first once; expanded grouped by provider; read picker; replacing a deprecated preference; keyboard and outside-click behaviour.
- `utils/spendCap.test.js` (402 handling, global) is relevant to every cost action.
- **No jest tests** for TodoPage, ArtifactsPage, ProfilePage, Prompts, References pages, Log, SearchModal, ImportData, ExternalImport, SharePage, CommonsPage, UpdatesModal, PrefillConsentCard, or WelcomePage. The parity spec for those is this document plus the backend pytest suite (`backend/tests/`).

---

## 15. Port sizing, what can stay on the web, risks

| Page / piece | Native size | Notes |
|---|---|---|
| ArtifactsNav + Documents shell | S | a capsule row backed by `ArtifactsStore` |
| Profile | M | markdown render, edit, TTS button, generation indicator, history |
| Todo | M–L | a faithful port of the markdown parser, toggle, and insert helpers; nested collapsible checklist; optimistic PATCH |
| Artifacts (+ Intentions view, create kind, dirty guard) | M | |
| VersionHistoryDrawer + diff (LCS + word refine + collapse) | M | pure logic, unit-testable in Swift against the jest cases |
| Log (+ rename/delete dialogs, Bubble card) | M | cursor paging |
| SearchModal | S–M | debounce; `<mark>` parsing; date filter |
| References list + detail (+ edit, feedback, read, TTS, embeds) | M–L | embeds need WKWebView or a fallback |
| FeedPicks / ReadReply bits | S | inside the thread view |
| Prompts list + detail | S–M | could stay on the web |
| Account | M | most rows simple; Connect X is the hard part |
| Import: ImportData | L | file picking, zip streaming, big uploads, 3 confirm dialogs, 409 flow, Twitter task polling |
| Import: ExternalImport (CA fetch, X sync, bookmarks JSON, tokens) | M | OAuth connect is hard |
| iOS Share Extension clipper (new) | M–L | the replacement for the Chrome clipper; high value on mobile |
| Share | S–M | |
| Commons | S | |
| UpdatesModal (changelog, notifications, polls with AI draft) | M | needs the in-app link router |
| ProfileGenerationWatcher | S | |
| PrefillConsentCard / Welcome | S / S | |
| ConfirmEmail | S (+ Universal Links setup) | |
| AdminPanel | L | **stay on the web** |

**Reasonable to keep on the web at first:** AdminPanel; Prompts (craft-only, desktop-editing workload); the Chrome clipper token card (the extension is desktop-only anyway); Connect X and Connect X bookmarks (OAuth plus session-cookie coupling); possibly the large archive imports (Claude/ChatGPT/Twitter/Markdown zips are usually produced and handled on a desktop). A native import with a "best on desktop" note is still worth doing for Markdown and bookmarks JSON. Link out with `SFSafariViewController` to `https://loore.org/<path>`. That only works if the web session exists in Safari; otherwise the user must sign in on the web too.

**Main risks:**
1. **Cookie-bound flows:** Connect X, X-bookmarks OAuth, and email confirmation all assume the browser holds the Flask session. The native app needs Universal Links, a cookie-seeded WKWebView, or web fallbacks.
2. **Import payload sizes and timeouts:** analyze echoes the full content and confirm re-posts it, synchronously, under nginx's 60 s / 200 MB limits. Client-side zip extraction of multi-GB exports needs streaming (ZIPFoundation) and must not load the file into memory.
3. **Last-write-wins todo PATCH** with text-keyed toggles: a duplicate item text toggles every matching line; a stale client can overwrite an AI-merged version. Keep the exact text-keyed semantics for parity, and refetch aggressively.
4. **Semantic search costs money per request:** keep the debounce and avoid search-as-you-type without a delay.
5. **Markdown fidelity:** Todo, Intentions, Profile, and Prompts all depend on react-markdown GFM behaviour (inline items, task lists, links to in-app routes). One native renderer must match it.
6. **Quirks found while mapping** (keep them in mind; do not "fix" them silently in the port):
   - `loore_profile_started` is never dispatched, and ImportPage passes no `onProfileUpdateStarted`. On the web, profile progress after an import appears only because OK reloads the page.
   - `/import?x_connect=ok|failed` is never read, so there is no feedback after X-bookmarks OAuth.
   - Prefill consent says "change it in Settings", but Account has no such row.
   - An artifact kind name is not validated client-side against the backend regex.
   - The markdown import's `date_ordering` is ignored server-side (it always sorts by `modified_at`).
   - The semantic search ignores `per_page`; dates-only semantic search 400s silently.
   - The search snippet HTML is unescaped.
   - `latest_profile` from `/dashboard` lacks `privacy_level`/`ai_usage` (SpeakerIcon receives undefined).
   - Profile edits via `PUT /profile/<id>` modify the current version in place (no history entry), unlike Todo and Artifacts saves.
