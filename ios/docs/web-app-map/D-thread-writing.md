# Map D — Thread view, writing, LLM replies, proposals

Source: `.claude/worktrees/ios-app` at origin/main `2774ab2` (2026-09-30).
Frontend paths are relative to `frontend/src/`, backend paths to `backend/`.
All API paths below are under `/api` (axios `baseURL: "/api"`, cookie session, `withCredentials: true`, header `X-Timezone: <IANA tz>` on every request). SSE URLs are `${REACT_APP_BACKEND_URL}/api/sse/...` (EventSource, cookies).

---

## 0. Things the brief assumed that are not how the web app works

Read these first; they change what the Swift port has to build.

| Assumption | Actual behavior |
|---|---|
| "Siblings/branches with navigation between branches" | There is **no sibling switcher**. The thread page shows: all ancestors (root → parent), the focal node, and the focal node's **entire descendant subtree** as nested preview bubbles. Siblings of the focal node (and of any ancestor) are **not shown at all**. You reach another branch by tapping an ancestor, which then shows its subtree. |
| "Edit / version history" for nodes | Editing a node saves the previous text as a `NodeVersion` row (backend `PUT /nodes/<id>`), but **no endpoint or UI exposes node versions**. `VersionHistoryDrawer` + `utils/diff.js` are used only for artifacts (Profile, Todo, Prompts, `/artifacts/:kind`), not for thread nodes. Documented in §4.9 for completeness. |
| "Rename thread" in the thread view | `RenameThreadDialog` is used only from **Log cards** (`components/Log.js` kebab: "Rename thread"). The thread page has no rename. Endpoint: `PUT /nodes/<root_id>/thread-name`. |
| "YouTube embeds in markdown" | `MarkdownBody` has **no YouTube or tweet embed**. `YouTubeEmbed`/`TweetEmbed` are used only on `pages/ReferenceDetailPage.js` (saved references). A YouTube URL in a node renders as a plain external link. |
| "Attachments/quotes in NodeForm" | NodeForm has **no quote-insert UI and no image/file attachments**. The only attachment is an **audio file upload** (craft mode). Quotes are `{quote:<nodeId>}` markers typed by the user or written by the LLM. |
| "Regenerate" button | There is **no Regenerate button**. Regenerating = pressing "LLM Response" again on the parent (craft bar), which creates a new LLM **sibling**; or editing a user node, which (with auto-generate on) fires a new LLM child → a new branch. |
| "useLlmTaskWarnings in the thread" | Only `hooks/useVoiceSession.js` uses it. The thread page (NodeDetail) **does not toast `llm-status.warnings`**. |
| "Refusals" | No model-refusal handling exists. "Refused" in this area means: (a) `llm_error` in a 2xx response when an entry was saved but the reply was refused (e.g. uncapped `{user_export}` on a non-Pro plan), (b) 403 when requesting a reply on someone else's node, (c) 402 spend cap. |

---

## 1. Thread structure

### 1.1 Routes that show a thread

| Web route | Component | Notes |
|---|---|---|
| `/node/:id` | `NodeRoute` → `NodeDetailWrapper` (logged in) / `PublicThreadPage` (logged out) | `NodeDetailWrapper` renders `<NodeDetail key={id}>` — **remounts on every node navigation**, so all local state (model choice, streaming, form) resets per node. |
| `/@:username/:slug` (and legacy `/u/...`) | `PermalinkRoute` → `GET /commons/permalink/<username>/<slug>` → `{node_id, canonical?}` → `NodeDetailWrapper nodeIdOverride` | Only resolves **public** nodes. |
| After load | if `node.permalink` and URL is `/node/<id>`, the address bar is replaced (display only) with the permalink. | Swift: use for share links. |

### 1.2 Fetch: `GET /nodes/<id>` (backend `routes/nodes.py:get_node`)

404 for missing, soft-deleted, or not visible (never 403, so ids can't be probed). UI copy for 404/403: **"This node doesn't exist, was deleted, or isn't shared with you."**; any other error: **"Error fetching node details."**; while loading: **"Loading node..."**; null node: **"No node found."**

Response (focal node's own fields, from `_focal_own_fields` + `_system_prompt_fields`):

```
id, content (decrypted markdown), node_type ("user"|"llm"), created_at, updated_at (ISO UTC),
permalink (string|null; only for public nodes with a slug),
user: {id, username},
parent_user_id  // LLM node: human_owner_id; user node: parent's user_id
privacy_level ("private"|"circles"|"public"), ai_usage ("none"|"chat"|"train"),
pinned_at (ISO|null), llm_model (string|null), origin (string|null, e.g. import platform),
llm_task_status ("pending"|"processing"|"completed"|"failed"|"cancelled"|null),
has_original_audio (bool), has_tts (bool),
streaming_content (only while pending/processing and text exists — the reply so far),
tool_calls_meta (array, when present), feed_picks_count (read replies only),
is_system_prompt, prompt_title, prompt_key, user_prompt_id, prompt_version_number,
context_artifacts: {prompt?, profile?, todo?, recent?, recent_raw?, share_guidance?,
                    external_content_guidance?, memory?, scratchpad?, ai_preferences?, intentions?} | null,
read_reply (bool, read replies), read_window ({window_start, window_end, tweets, accounts, excluded})
// tree fields:
child_count, ancestors: [...], children: [...],
in_read_thread (bool), read_reply_above (bool),
reply_ai_usage  // what a new reply form should pre-select (#362)
```

**ancestors** (root first, nearest parent last). Each alive ancestor: `id, username, llm_model, content, preview (first 200 chars + "..."), node_type, child_count, created_at, user_id, parent_user_id, ai_usage, privacy_level` + system-prompt fields. A soft-deleted ancestor the viewer could see before deletion: `{id, deleted: true, deleted_at, username, node_type, created_at, child_count, ai_usage, privacy_level, ...prompt fields}`. Ancestors the viewer can't access are **omitted** (the chain can have gaps).

**children** (recursive, via `serialize_node_recursive`), sorted by **descendant count, descending** (the busiest branch first). Each: `id, content, node_type, child_count, created_at, updated_at, username, llm_model, origin, descendant_count, user_id, parent_user_id, children[], has_tts, privacy_level, ai_usage` + prompt fields. Tombstones appear only if they have visible living descendants (`{id, deleted:true, ..., child_count, descendant_count, children}`); inaccessible nodes appear as `{id, inaccessible: true}` and are not recursed.

Not included on ancestors/children: `human_owner_username`, `pinned_at`, `has_original_audio`, `tool_calls_meta`. So on non-focal bubbles the "Pinned" and "Voice Note" tags never show and LLM attribution is model-only.

### 1.3 Layout (top to bottom), `components/NodeDetail.js`

1. Header row: `<h2>Thread</h2>` (serif, 1.8rem) on the left; **top-right controls** column on the right (see §1.6).
2. `SemanticNeighbors` — admin-only fixed right rail (≥1200 px wide): `GET /search/neighbors?node_id=&limit=5` → "Semantically near [admin]" cards with `username · date` and `NN%` score, 3-line preview. Skip or admin-only for iOS.
3. **Ancestors**: vertical list of `Bubble`s (collapsed previews, left-aligned), each tappable → navigate to that node.
4. `<hr>` then the **focal card** ("highlighted node"): card with 3 px accent left border, padding 1.8rem 2rem, full content rendered (see §2.2). Scrolled into view (smooth, block start) once per focal id after load.
5. Below the card: **focal footer** (`NodeFooter` + pin/speaker/download), then the **action row** (craft bar and/or Read row), then an in-progress line.
6. **Inline reply form** (`NodeForm compact`) for any logged-in viewer.
7. `<hr>` then **children subtree** (`RenderChildTree`).

`RenderChildTree` rule: for a list of sibling nodes, if there is **more than one** sibling each sibling block is indented `marginLeft: 20px; paddingLeft: 10px; borderLeft: 2px solid var(--border)`, with an `<hr>` between siblings. A single child is not indented (a linear continuation). Recurse into each child's `children`. Every bubble is tappable (Cmd/Ctrl-click opens a new tab on web).

### 1.4 Ownership rules (client)

- Focal `isOwner = node.user.id === me.id || (node.node_type === "llm" && node.parent_user_id === me.id)`.
- Non-focal `ownedByMe(n) = n.user_id === me.id || (n.node_type === "llm" && n.parent_user_id === me.id)`.
- An LLM reply is "owned" by the human who requested it (can edit/delete it).

### 1.5 Navigation

- Tap ancestor/child bubble → `/node/<id>` (push).
- Placeholders (tombstones, inaccessible) are not tappable.
- In-text node links (§3.4) navigate in-app.
- History handling around pending replies uses `state: { fromParent: true }` so that a failed reply can `navigate(-1)` back to its parent instead of pushing a duplicate entry (see §5.6).
- Tab title: first line of content (leading `#`, `>`, whitespace stripped, max 120 chars) + " — Loore"; while an LLM node is pending: **"Thinking… — Loore"** or **"Processing… — Loore"** (batch). Swift equivalent: navigation title.

### 1.6 Craft mode: floor vs ceiling

`craftMode = !!currentUser.craft_mode` (server field; also mirrored to `localStorage.loore_craft_mode`). Toggled from NavBar ⋮ menu ("Craft mode" switch) or Account page; turning **on** first shows `CraftModeDialog`; turning off is immediate with toast **"Craft mode off."**; confirming shows toast **"Craft mode on. Its controls carry the sliders icon."** (5 s). Persist: `PUT /dashboard/user {craft_mode: bool}` → `{user}`.

`CraftModeDialog` copy: title **"Turn on craft mode?"** (with sliders `CraftIcon`), body "It shows extra controls for steering the details:" • "privacy and AI usage on each entry" • "a switch for auto-generating responses, model picker and voice upload on threads" • "prompt editing and data export in the ⋮ menu" then "Everything it adds carries the sliders icon. Turn it off any time in the ⋮ menu or under Account." Buttons: **"Turn on"** (accent) / **"Not now"** with sub-line "Keep Loore's current simple UI." Esc / backdrop = Not now.

`CraftIcon`: 24×24 viewBox sliders glyph, stroke currentColor 1.6, paths `M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3` and `M1 14h6M9 8h6M17 16h6`.

| Surface | Floor (craft off) | Ceiling (craft on) |
|---|---|---|
| Thread top-right | "Voice Mode" button (owner, ai_usage≠none, not public) | + **Auto-generate** toggle |
| Action row under focal | Nothing, unless thread is public (owner then sees craft bar) or it's a read thread | **LLM Response + model picker** — shown only when auto-generate is **off** (`showCraftBar = isOwner && (craftMode || isPublicThread) && !autoGenerateActive && node.ai_usage !== 'none' && !isLlmPending`) |
| Inline reply form | textarea, Send, Discard draft, Record (mic) | + Privacy/AI-usage selectors, + audio **Upload** button |
| Reply modal (kebab → Reply) | textarea + **Record + Upload** (upload is not hidden here) | + Privacy/AI-usage selectors |
| Edit modal | Privacy/AI-usage selectors **always** shown (hidePowerFeatures not passed) | same |
| NavBar ⋮ | — | "Write new entry" (opens modal with Agentic Reply + Auto-generate toggles and selectors), "Export data", "Prompts" |
| Text page `/textmode` | textarea, Send, Record | + selectors, + Upload |

The auto-generate preference (`localStorage.loore_auto_generate`, default **true**) governs behavior in **both** modes; only its toggle is craft-only. Public threads force auto-generate off (`autoGenerateActive = isPublicThread ? false : autoGenerate`) and show the explicit LLM Response bar to the owner.

---

## 2. Bubble anatomy

### 2.1 Non-focal `Bubble` (`components/Bubble.js`) — ancestors, children, Log cards

Card: bg `--bg-card`, 1 px `--border`, radius 10, padding 1.6rem 1.8rem, max width 1000, width `calc(100% - 30px)` (room for the kebab outside the right edge). Hover: border `--border-hover`, shadow, lift 1 px (web only).

Content, in order:

1. **Kebab** (`BubbleKebabMenu`) — absolutely positioned **outside** the card's right edge, vertically centered. Visible on hover (kept 3 s after mouse-leave) or always on touch devices (`(hover: none)`). Icon `FaEllipsisV` 14 px, aria-label **"More actions"**. Menu (min width 160, flips left if it would overflow). Items for thread bubbles (`buildActions`):
   - **Reply** (always; `kind: 'reply'`) → opens **Reply modal** (NodeFormModal title "Reply") targeting that node.
   - **Edit** (if ownedByMe) → Edit flow (§4.7).
   - **Delete** (if ownedByMe, accent color) → Delete flow (§4.8).
   Log cards instead get **Rename thread** (unless `can_rename === false`) and **Delete thread**.
   Only one of reply/edit/delete targets is active at a time (`setExclusiveTarget`).
2. **Body**:
   - Tombstone: italic muted **"[Node deleted]"**; inaccessible: **"[Node inaccessible]"** (and no footer at all).
   - Collapsed (default): **title** = first line of `content || preview` with a leading `# ` stripped, truncated to 120 chars + "..."; **body** = the rest, trimmed, truncated to 250 chars + "...", clamped to 2 lines, plain text (not markdown). If `thread_name` is set (Log cards): the name is the title; if the first line was a `# ` heading it is dropped, otherwise all text becomes the body.
   - Expanded: full `content` rendered by `MarkdownBody` (not `QuotedContent`, so `{quote:N}` markers show raw here).
   - Expand chevron shown only if `content` exists and (title > 120 chars, body > 250 chars, or body has > 2 lines). aria "Expand preview"/"Collapse preview".
3. **Footer row** (taps inside don't navigate): `NodeFooter` on the left, tags on the right.
4. **Tags** (uppercase 0.65rem, accent-dim on accent-subtle): **"Pinned"** (if `pinned_at`), prompt label (if `prompt_key`: `read`/`read_thread` → "Read", else key capitalized with `_`→space, e.g. "Textmode", "Voice"), else **"Voice Note"** (if `has_original_audio`). Then the expand chevron.

Tapping the card navigates (`/node/<id>`).

### 2.2 `NodeFooter` (`components/NodeFooter.js`)

Row, 0.75rem muted, items separated by `·` in `--border` color:

1. **Author link**: user node → `username`; LLM node → `llm_model` (e.g. "claude-opus-4.6" — it is the model **id**, not display name); on public nodes with a known human owner: `"<model> · via <human>"`. Link target: public node → `/@<human or username>`; otherwise own → `/dashboard` (→ `/profile`), other → `/dashboard/<user>` (→ `/@user`).
2. `via <origin>` (tooltip "Imported from <origin>") when `origin` is set.
3. Date-time **`yyyy/mm/dd HH:MM`** local 24 h (`utils/date.js formatDateTime`; marker-less ISO strings are treated as UTC).
4. Reply icon `FaRegCommentDots` + child count (count hidden when 0). On non-focal bubbles it's a button (title "Reply") that opens the Reply modal.
5. Extra children (focal only): pin, speaker, download.

### 2.3 Focal card (in `NodeDetail`)

Inside the card:
- **Kebab** (owner only), always visible: **Edit**, **Delete** (no Reply — the inline form is right below).
- **System-prompt header** (if `is_system_prompt && prompt_title`): `"<prompt_title> v<prompt_version_number>"`, then `· Profile v<N>` / `· TODO v<N>` when those artifacts are pinned. 0.8rem muted.
- **Read window line** (read replies): "Tweets from {from} to {to} (your time): {N} by {M} accounts." + " {K} you had already read were left out."
- **Content**:
  - Pending LLM with streamed text → partial text (§5.4) + three pulsing dots (aria "Still writing").
  - Pending LLM without text → italic **"Thinking"** (or **"Processing"** for batch) + three pulsing dots (5 px circles, 1.2 s cycle, 0.15 s stagger, opacity 0.3→1 and −2 px lift) + admin rerun controls.
  - Otherwise `QuotedContent` (full custom markdown, §3) with owner-only interactive checkboxes and "+" add-item. If a proposal is present, only the lead-in text here; the proposal card (§6) and trailing commentary follow.
- Legacy `FeedPicks` (read replies from before 2026-09-16), `ReadReplyTail` ("N unread" / "N of M unread" + **"Mark all as read"**, or **"All read."**) — read feature, admin-only today.
- **"Actions taken (N)"** disclosure (`▸`/`▾`) listing visible tool calls (`tool_calls_meta` entries whose name doesn't start with `_`). Each row: `✓` or `✗` + a label:

| tool name | label |
|---|---|
| propose_todo | "Todo update proposed" + " (applied)" / " (applying...)" / " (failed)" |
| propose_github_issue | "Issue proposed" + " (created)" / " (failed)" |
| propose_feedback | "Feedback proposed" + " (sent)" / " (failed)" |
| propose_share | "Share proposed" + " (saved as draft)" / " (failed)" |
| apply_todo_changes | "Todo apply failed" / "Todo changes applied" / "Todo apply failed: <err>" / "Todo apply in progress..." |
| apply_github_issue | "Issue creation confirmed" / "Issue creation failed" |
| apply_feedback | "Feedback sent" / "Feedback send failed" |
| apply_share | "Share saved as a draft — [Share page]" / "Share save failed" |
| update_ai_preferences | "Preferences updated" |
| update_artifact | "Created"/"Updated" + " artifact `<kind>`" (link `/artifacts/<kind>`) |
| read_artifact | "Read artifact `<kind>`" |
| read_todo | "Read [todo list]" (link `/todo`) |
| semantic_search | "Searched archive & references — “<query>”" |
| read_full | external: "Read in full — [@handle's post]" (url) or "Read a saved reference in full"; node: "Read in full — [entry #<ref_id>]"; failed: "Read in full (failed)" |
| other | the raw tool name |

  These labels are the reply's owner's (the user who asked for it). Anyone else gets only each entry's `name` and `status` from the server (`/nodes/<id>`, `/llm-status`, `/textmode/from-node`), and each row names the action without details or links: "Searched archive & references", "Read in full", "Wrote an artifact", "Read an artifact", "Read the todo list", "Todo changes confirmed", "Share saved as a draft", and the proposal / apply labels without their apply state (`sharedToolLabel` on the web, `ToolCallMeta.sharedLabel` on iOS).
| any with `error` | " — <error>" in accent color |

Below the card:
- **Footer**: `NodeFooter` + **Pin** button (`FaThumbtack`; accent when pinned; disabled/35 % opacity when not allowed). Titles: "Only the owner can pin" / "Cannot pin a private node" / "Unpin from your public page" / "Pin to the top of your public page". `POST /nodes/<id>/pin` → `{pinned_at}`; `DELETE /nodes/<id>/pin`. Errors set the page error (`err.response.data.error || "Error toggling pin."`) — note this replaces the whole page.
  + **SpeakerIcon** (TTS play; hidden unless `user.voice_mode_enabled` or node is public; disabled with title "TTS disabled — No AI access" when `ai_usage === 'none'`; otherwise "Play audio"/"Generating audio..."). Flow: `GET /nodes/<id>/audio` → if none, `GET /nodes/<id>/audio-chunks`, else `POST /nodes/<id>/tts` then SSE `…/api/sse/nodes/<id>/tts-stream` `chunk_ready` events; chapters `GET /nodes/<id>/tts-chapters`. (Audio playback details belong to the voice/audio map.)
  + **DownloadAudioIcon** (same gating; "Download audio"/"Download disabled — No AI access"; `GET /nodes/<id>/audio`).
- **Action row** (§5.1).
- **In-progress line** (only when a request is tracked but the craft bar isn't shown): spinner + "Waiting for AI…" (status pending) / "Generating…".
- **Inline form** (§4).

### 2.4 Privacy / AI-usage indicators

There are **no privacy or ai_usage badges on bubbles**. The only visible consequences: public nodes change footer attribution/link; `ai_usage: 'none'` disables speaker/download, hides top-right controls and the craft bar; pin is disabled on private nodes. A Swift design may want to add indicators, but that would be new UI.

---

## 3. Markdown rendering

### 3.1 Pipeline

```
node.content
  └─ QuotedContent (components/QuotedContent.js)       ← custom syntax, pre-markdown
       1. replace {share_guidance}\n? and {external_content_guidance}\n?
          with context_artifacts.<same>.content (+ "\n") or "" 
       2. if no quotes, no external quotes, no context_artifacts and no {user_*} marker:
             render whole string with MarkdownBody
          else split on COMBINED_PATTERN and render segments in order:
             text → MarkdownBody (each text segment is a SEPARATE markdown document)
             {quote:N} → InlineQuoteBubble
             {quote_ext:N} → ExternalQuoteBubble (inside div.ext-quote-slot)
             {user_<kind>} → InlineArtifactSection
  └─ MarkdownBody (components/MarkdownBody.js)
       react-markdown ^10.1.0, remarkPlugins [remark-gfm ^4.0.1, remarkHtmlAsCode]
       no rehype plugins, no raw HTML, no syntax highlighting, no math
```

Regexes (exact):
```
COMBINED_PATTERN = /(\{quote_ext:\d+\}|\{quote:\d+\}|\{user_(?:profile|todo|recent_raw|recent|ai_preferences|memory|scratchpad|intentions)\})/g
ARTIFACT_PATTERN = /\{user_(profile|todo|recent_raw|recent|ai_preferences|memory|scratchpad|intentions)\}/g
quote markers in NodeDetail: /\{quote(?:_ext)?:\d+\}/g
```
Important consequence: because content is split at markers, **markdown constructs cannot span a quote marker** (a list interrupted by `{quote:5}` becomes two lists). Swift must split identically.

Quote data comes from `GET /nodes/<id>/resolve-quotes` (fetched only when content contains a marker; refetched when the marker set changes or another bubble is edited/deleted):
```
{ has_quotes: bool,
  quotes: { "<id>": {id, content, username, user_id, created_at, node_type, ai_usage}
                    | {id, deleted: true, content: null, username, ...} | null },
  external_quotes: { "<id>": {id, content, source, author_handle, title, url, posted_at,
                               user_id, read_at, feedback,
                               // owner viewing a reply that recommended it (#352):
                               feedback_shared, rated_before: {feedback, at} | null, recommendation_id}
                     | null } }
```
Until quotes arrive (or if the fetch fails) the plain path renders, so **`{quote:123}` shows as literal text** in the meantime. A Swift port should show a placeholder instead.

### 3.2 Standard markdown (remark-gfm) and styling

| Element | Rendering |
|---|---|
| Paragraph | `white-space: pre-wrap` (**single newlines are preserved as line breaks** — not CommonMark soft-wrap). `overflow-wrap: break-word`, margin `0.5em 0` (quote/artifact cards pass `0` / `0.3em 0`). `flowText` prop (soft wrap) is used only for authored docs like the changelog, never for nodes. |
| h1–h6 | Serif (Cormorant Garamond). Sizes 2.2/1.8/1.5/1.25/1.1/0.95 em; weights 700/700/600/600/600/600; line-height 1.2–1.35; top margins 1.2/1.1/1/1/0.9/0.9 em, bottom 0.4/0.4/0.35/0.35/0.3/0.3 em. First/last child margins trimmed to 0 (`.loore-md > *:first-child/last-child`). |
| Blockquote `>` | 3 px `--accent-dim` left border, `--bg-card` background, padding 0.5em 1em, italic, `--text-secondary`. |
| Bold / italic / ~~strike~~ | 700 / italic / line-through in `--text-muted`. |
| Links | See §3.4. |
| Images `![]()` | `max-width: 100%`, auto height (remote URL loaded as-is). |
| Lists | ul/ol margin 4px 0, padding-left 24 px. Task lists: see §3.5. |
| `---` | 1 px `--border` top rule, margin 20px 0. |
| Code | Fenced → `<pre><code class="language-x">` with `--bg-card` bg, 1 px border, radius 6, padding 0.75em 1em, horizontal scroll, 0.9em; monospace font. **No syntax highlighting.** Inline code: in react-markdown v10 the `inline` prop no longer exists, so the "inline" branch in MarkdownBody is dead code; inline code is just a monospace `<code>` with no extra style. |
| Tables (GFM) | Wrapped in a horizontal-scroll container; `border-collapse`, 100 % width, 0.9em; `thead` 2 px bottom border; `th` padding 6px 10px, left-aligned, 600, nowrap; `td` 6px 10px with 1 px top border. |
| Autolinks (GFM) | Bare URLs become links. |
| Footnotes (GFM) | Parsed by remark-gfm (no custom style). |
| Raw HTML | Never rendered. `remarkHtmlAsCode`: any `html` mdast node becomes a **code block** (block position) or **inline code** (inside paragraph/heading/emphasis/strong/delete/link/linkReference/tableCell), preserving the literal text and its newlines; pure comments `<!-- ... -->` are **dropped**. |

### 3.3 Custom syntax catalogue

| Syntax | Where handled | Rendering |
|---|---|---|
| `{quote:<nodeId>}` | QuotedContent → `InlineQuoteBubble` | Card: padding 12, 3 px **accent** left border, radius 6. Header italic 0.85em muted **"Quoted from @<username>"**; body = quote content **truncated to 250 chars + "..."**, rendered with MarkdownBody (paragraph margin 0); then a `NodeFooter` (username, date, 0 replies). Tap → navigate to the quoted node. Null → inline chip **"[Quoted node inaccessible]"**; `deleted` → **"[Quoted node deleted]"**. |
| `{quote_ext:<externalItemId>}` | QuotedContent → `ExternalQuoteBubble` | Card with 3 px **`--info`** left border. Header **"Saved from @<author_handle or 'unknown'> · <source label>"** (source labels: community_archive/read_pick → "Community Archive", twitter_bookmark → "X bookmark", twitter_like → "X like", else raw). Body truncated to **500** chars. Footer: posted date ("Mon D, YYYY") left; for the **owner** (`quote.user_id === me.id`): optional italic "You rated this <good/bad> on <date>" (when `rated_before` and no current feedback), **Good quote / Bad quote** toggle glyphs (FiPlusCircle / FiMinusCircle, 40 px hit target), and **"Mark as read"/"Mark as unread"** text button (title "You read it <date>"). Tapping the card opens `url` externally and, for the owner, `POST /external/items/<id>/read {node_id?, via:'open'}`. Toggle read: `POST` / `DELETE /external/items/<id>/read` body `{node_id?}` → `{read_at}`. Verdict: `POST /external/items/<id>/feedback {feedback: 'good'|'bad'|null, node_id?}` → `{feedback, read_at?}`; choosing the selected verdict again clears it; a verdict also marks read. Shared verdict title: "Good quote (your rating from another reply)". Null → **"[Quoted reference inaccessible]"**. Errors toast "Could not update the read mark." / "Could not save your feedback.". |
| `{user_profile}`, `{user_todo}`, `{user_recent}`, `{user_recent_raw}`, `{user_ai_preferences}`, `{user_memory}`, `{user_scratchpad}`, `{user_intentions}` | QuotedContent → `InlineArtifactSection` (appear in system-prompt nodes) | Collapsed card (3 px accent left border) with header `▶ <Label>` / `▼`, tap toggles full artifact content (MarkdownBody). Labels: User Profile, User TODO, Recent Context Summary, Recent Context Raw, AI Preferences, Memory, Scratchpad, Intentions. Header text: profile → "User Profile v<N>"; recent → "Recent Context Summary"; others "<Label> v<N>" when versioned. `recent_raw`: non-expandable `■ Recent Context Raw — Covers <start> to <end> (<N>K tokens)` or "Date range unavailable". Missing artifact → chip **"[<Label> not available]"**. Token format: ≥1M "1.2M tokens", ≥1K "10K tokens". |
| `{share_guidance}`, `{external_content_guidance}` | QuotedContent string replace | Replaced by server-provided text in `context_artifacts` (or removed). |
| In-text node link (bare URL) | MarkdownLink → NodeLink | §3.4 |
| `- [ ]` / `- [x]` task items | MarkdownListItem | §3.5 |
| `:::share <type>` … `:::` fences and `### Completed/New Tasks/Priority/Note/Issue Title/Description/Category/Feedback/Feedback category/Share/Share type` sections | ProposalInline (not markdown) | §6 |
| `{quote:N}` / `{quote_ext:N}` while a reply streams | NodeDetail `partialReplyText` | Stripped; `:::share` fence lines also removed until the reply completes. |

### 3.4 Links (`MarkdownLink`, `utils/nodeLinks.js`)

- **Node link**: href parsed by `parseNodeLink`: an app-relative `/node/<digits>` or `/node/<digits>/`, or an absolute `http(s)` URL on `loore.org`, `www.loore.org`, `staging.loore.org` (or the current origin) with path exactly `/node/<digits>/?`. Anything else (e.g. `/node/5/edit`) is not a node link.
- Only when the link's visible text equals its URL (a bare pasted URL or `[url](url)`, trailing `/` ignored) is it replaced by the node's **title**. Links with author-given text keep their text but still navigate in-app.
- Titles: `GET /nodes/titles?ids=1,2,3` (max 50 ids, deduped) → `{titles: {"1": {id, title}, "2": null, "3": {id, deleted: true, title: null}}}`. Title = thread name if the node is a named thread root, else first line stripped of markdown markers. Requests made in the same tick are coalesced into one; results cached per app session (module-level map); failures not cached (raw URL stays).
- Rendering: accent + underline; while loading shows the raw URL; `null` → muted italic **"[Node inaccessible]"**; deleted → **"[Node deleted]"** (still a link; the URL goes into the tooltip).
- Other `/…` app-relative links navigate in-app only when a host passes `onInternalLinkClick` (the updates modal); in the thread they are normal links.
- External links open in a new tab (`target=_blank`, `noopener`). All link taps stop propagation (don't navigate the enclosing bubble).

### 3.5 Checklists (interactive, owner only)

- GFM task items render with a custom **circle** (18 px, 1.5 px border `--border-hover`; checked = filled `--accent-dim` with a `✓` in `--bg-deep`), `list-style: none`, nested task lists indented 1.5em.
- Tapping the circle (owner, focal node only — `onCheckboxToggle` is passed only when `isOwner`) calls `toggleCheckbox(content, itemText, checked)`: finds the first line matching `^(\s*)- \[([ xX])\]\s+(.+)` whose `stripInlineMarkdown(label).trim() === itemText` and flips `[ ]`↔`[x]` (only `-` bullets are toggleable). Optimistic update, then `PUT /nodes/<id> {content}`; on failure revert and toast **"Couldn't save change — reverted (<reason>)"**.
- `itemText` is the item's plain text excluding nested lists. Matching is by text, so **duplicate item labels toggle the first match**.
- Per-row **"+"** (accent, title/aria "Add an item below"), visible on row hover (web). Opens an inline input (placeholder **"New item…"**, dashed circle). Enter inserts, Esc or blur-when-empty cancels. `insertItemAfter(content, afterText, newText)` inserts after the matched item and its deeper-indented subtree, copying indent, bullet char (`-`/`*`) and checkbox style (`- [ ] `). Save via `PUT /nodes/<id> {content}`; failure toast **"Couldn't add task — reverted (<reason>)"**.
- `stripInlineMarkdown` (utils/markdown.js) removes `![alt](u)`→alt, `[t](u)`→t, `**b**`, `__b__`, `~~s~~`, `` `c` ``, `*i*`, `_i_` — the Swift parser must produce the same plain text or toggles silently no-op.
- #321 (pinned by `MarkdownBody.stable.test.js`): re-rendering after a toggle must **not** remount list items (focus stays on the tapped checkbox, the page doesn't jump, a half-typed "+" input survives parent re-renders). Swift: use stable identities for rows.

---

## 4. Writing

### 4.1 NodeForm instances

| Where | Props | Submit path |
|---|---|---|
| Thread inline form | `parentId=<focal id>`, `compact`, `hidePowerFeatures=!craft`, `hideAudioUpload=!craft`, placeholder "Type what's on your mind…" (read reply: "Ask about these picks, or say what you make of them…"), read reply: `onSubmitOverride=submitReadReplyMessage` | §4.5 |
| Reply modal (kebab Reply / comment icon) | NodeFormModal title **"Reply"**, `parentId=<target>`, `hidePowerFeatures=!craft` | Plain `POST /nodes/`, then navigate to the new node. **No auto-generate** in this path. |
| Edit modal | NodeFormModal title **"Edit Text"**, `editMode`, `nodeId`, `initialContent/PrivacyLevel/AiUsage`, `detachPrompt` (node has a pinned prompt), `hasGeneratedTts`, `hasChildren` | §4.7 |
| Write New Entry modal (craft ⋮ menu, Welcome page) | NodeFormModal title **"Write New Entry"**, `parentId=null`, `allowAgenticPrompt` | Agentic/auto-generate toggles, §4.5 |
| `/textmode` (WritePage, Home "Text" card) | `parentId=null`, `hidePowerFeatures=!craft`, `hideAudioUpload=!craft`, `aiUsageFromGlobalDefault`, placeholder "Type what's on your mind…", `onSubmitOverride` | §4.6 |

`NodeFormModal`: full-screen dim (rgba 0,0,0,.7 + 8 px blur) backdrop; card width 1170 max 90vw/90vh, scrolls; `×` close (top-right); Esc or backdrop tap closes. Unsaved text survives via the server draft.

### 4.2 Fields and controls

1. **Textarea** — rows 3 (compact, min height 90) or 6 (height clamp(120px, 40vh, 400px)); font 1.05rem weight 300; resizable. Default placeholder **"What's present for you right now..."**. Disabled while an uploaded audio file is selected (new entries).
2. **PrivacySelector** (unless `hidePowerFeatures`):
   - "PRIVACY LEVEL" select: `🔒 Private · Only I can see this` (private), `👥 Circles · Shared with specific groups (coming soon)` (circles — selectable; backend accepts it), `🌐 Public · Anyone can see this` (public). Descriptions: "This note is private and only visible to you." / "This note will be shared with your selected circles (feature coming soon)." / "This note will be visible to all users."
   - When replying under a public parent (and not editing): select replaced by static text **"Public — inherited from the thread."**
   - "AI USAGE" select: `🚫 None · No AI access` (none), `💬 Chat · AI can use for responses` (chat), `🧠 Train · AI can use for training` (train). Descriptions: "AI will not access this note." / "AI can read this note to generate responses, but won't use it for training." / "AI can use this note for training data to improve the model."
3. **Agentic Reply** toggle and **Auto-generate** toggle — only when `allowAgenticPrompt && !editMode && !parentId && !hidePowerFeatures && aiUsage !== 'none'` (i.e. Write New Entry modal). Labels "AGENTIC REPLY" / "AUTO-GENERATE" (0.7rem uppercase, 0.14em tracking) with 32×18 pill switch. Descriptions: Agentic on "AI will include your profile, recent entries, todos and preferences as context." / off "No agentic context. AI replies only see this entry."; Auto-generate on "AI will reply automatically after you submit." / off "No AI reply. You can click LLM Response on the created node later." Persisted in `localStorage.loore_agentic_reply` / `loore_auto_generate` (both default true; not read or written by forms where the toggles aren't offered).
4. Status lines: "Transcribing recovered audio..." (draft audio recovery), "You're offline", error text (accent), draft status (§4.3).
5. Uploaded file row: `"<name> (<size> MB)"` + **"Remove"**.
6. Button row: **Send** (accent outline; disabled while loading / offline / recording, 35 % opacity when offline or recording). Label states: "Uploading... N%" → "Transcribing... N%" / "Waiting to transcribe..." → "Sending..." → "Send". **"Discard draft"** when a draft exists (deletes server draft and resets text to `initialContent || ""`). For new entries: **Record** (`StreamingMicButton`) and, unless `hideAudioUpload`, **Upload** (file picker).

### 4.3 Drafts (`hooks/useDraft.js`, `routes/drafts.py`)

- Stored **server-side** in the `Draft` table (encrypted), one per `(user, node_id)` for edits or `(user, parent_id)` for new entries (`parent_id` null = top-level). Proposal drafts (labels `todo_pending` etc.) are excluded from these lookups.
- Load on mount: `GET /drafts/?node_id=<id>` (edit) or `GET /drafts/?parent_id=<id>` / `GET /drafts/` (new) → `{id, content, node_id, parent_id, created_at, updated_at, session_id?, has_stored_chunks?, parent_deleted?, warning?}`; 404 = none. If found with content, the textarea is **replaced** by the draft (also in edit mode) and "Discard draft" appears.
- Save: on every keystroke with non-empty trimmed text, `saveDraft(text)` → debounced **1000 ms** → `POST /drafts/ {content, node_id, parent_id}` → `{..., updated_at}`. A **3000 ms** interval also flushes pending content. Only one save in flight. Errors are retried on the next tick. Clearing the textarea to empty **does not** save an empty draft (the old draft remains on the server).
- Delete: `DELETE /drafts/?node_id=` / `?parent_id=` after a successful send, on transcription completion, or "Discard draft".
- UI: while saving **"Saving..."** (accent); after **"Draft saved just now"** / **"Draft saved Nm ago"** / **"Draft saved Nh ago"** (computed at render; not a live clock). Shown only while the textarea has text.
- 410 from POST when the edit target/parent was deleted.
- Audio recovery: if the loaded draft has `session_id` and `has_stored_chunks`, `POST /drafts/streaming/<sid>/transcribe-remaining`, then poll `GET /drafts/streaming/<sid>/status` every **3 s** (max 5 min) → `{streaming_status, content}`; content replaces the textarea; on completed/failed the draft is re-saved.

### 4.4 Defaults for privacy / ai_usage

Order of precedence:
1. Edit: the node's own values.
2. Reply (`parentId`): fetch `GET /nodes/<parentId>`; privacy = parent's `privacy_level`; ai_usage = parent's **`reply_ai_usage`** (falls back to `ai_usage`, then "none"). If the parent is public, privacy is forced to "public" and locked.
3. Fresh top-level entry: `localStorage.loore_last_privacy_level` / `loore_last_ai_usage` (remembered on every change), else `user.default_privacy_level` / `user.default_ai_usage`, else "private"/"none". WritePage passes `aiUsageFromGlobalDefault`: ai_usage always starts from `user.default_ai_usage` and is not remembered.

### 4.5 Submit decision tree (`NodeForm.handleSubmit`)

Pre-checks, in order (each opens a dialog; confirming resumes the same submit with the choice):
1. No text and no uploaded file → error **"Content is required."**
2. Edit + node has generated TTS + text changed → **RegenerateTtsDialog**.
3. Edit + node has children + privacy or ai_usage changed → **ApplyToRepliesDialog**.
4. Text > **100,000** chars (`NODE_CHAR_CAP`): edits and typed custom-submit pages → error "This entry is N characters — above the 100,000-character limit. Please move part of it into separate entries."; otherwise → **SplitContentDialog** (once per form). Paste that would exceed the cap: edit/custom-submit → error "Pasting this would make the entry N characters — above the 100,000-character limit."; else the split dialog with the pasted result pending.
5. Reply under a public parent and `localStorage.loore_public_reply_ack !== "true"` → **PublicReplyDialog**.

Then:
- **Custom submit** (`onSubmitOverride`, new entry, no uploaded file): call it with `{content, parent_id, privacy_level, ai_usage, streaming_session_id?}`; delete the draft; if `data.spend_capped` fire the spend-cap banner; if `data.llm_error` toast it (10 s); `onSuccess(data)`.
- **Agentic top-level** (`useAgenticPrompt && !parentId && aiUsage !== 'none' && ≤ cap && no audio`): `POST /textmode/start {content, privacy_level, ai_usage, auto_generate}` → `{conversation_id, user_node_id, llm_node_id?, task_id?, spend_capped?, llm_error?}`; `onSuccess({id: user_node_id, awaitLlm: llm_node_id?, ...})`.
- **Recorded transcript** (`streamingSessionId`): `POST /drafts/streaming/<sid>/save-as-node {content, agentic?: true, auto_generate?: true}` (the flags only for top-level entries with AI allowed and the matching toggles) → `{id, user_node_id, tip_id, content, parent_id, privacy_level, ai_usage, created_at, conversation_id?, llm_node_id?, task_id?, spend_capped?, llm_error?}`.
- **Edit**: `PUT /nodes/<id> {content, privacy_level, ai_usage, detach_prompt?: true (only if detachPrompt && text changed), regenerate_tts?: true, apply_to_descendants?: true}` → `{message, node: <focal own fields>, descendants_updated: N}`. 422 if > cap; 403 if not editable; 400 if `train` on a read node ("A Community Archive read cannot be used for training: it quotes other people's tweets.").
- **Audio upload** (new entry): > 10 MB → chunked upload (`/nodes/upload/init|chunk|finalize`, `utils/chunkedUpload.js`), else `POST /nodes/` multipart `{audio_file, parent_id?, privacy_level, ai_usage}` → `{id, transcription_status: 'pending', ...}`; then poll `GET /nodes/<id>/transcription-status` every 2 s until completed (→ `onSuccess({...data, id: node_id})`) or failed (error "Transcription failed"). Validation: ≤ 200 MB ("File size must be under 200 MB"), types webm/wav/m4a/mp3/mp4/mpeg/mpga/ogg/oga/flac/aac ("Invalid file type. Please upload an audio file (mp3, wav, m4a, webm, ogg, flac, aac)"). 402 → toast "You've reached your monthly usage limit, so you can't upload audio until it resets on <Month D>." Selecting a file clears the textarea; typing clears the file.
- **Plain text**: `POST /nodes/ {content, parent_id, privacy_level, ai_usage}` → `{id, content, node_type, parent_id, linked_node_id, created_at, username, privacy_level, permalink, ai_usage, split_into, tip_id}` (201). Backend auto-splits > 100k content into a serial chain (head keeps the id; `tip_id` is the last part). 400 `{code: "public_reply_required"}` if a reply under a public node isn't public; 410 if the parent was deleted. Then, only for the Write New Entry modal with Auto-generate on and Agentic off (top-level, AI allowed): `POST /nodes/<id>/llm {source_mode:'textmode'}` and `onSuccess({..., id: llm_node_id, awaitLlm: llm_node_id})`.
- Errors: `err.response.data.error || err.message || "Error submitting form."` shown under the form.

**After success, by host:**
- Thread inline form (`handleInlineSuccess`): chain = `[focal, ...ancestors]`. If auto-generate is active: if any node in the chain has ai_usage outside {chat, train} → set auto-generate **off** and toast **"Turning off auto-generate. AI usage on some nodes is turned off."** (8 s), no reply; else `POST /nodes/<newId>/llm {model: selectedModel, source_mode: 'textmode'}` and navigate `/node/<newId>?awaitLlm=<llmId>`. Otherwise navigate `/node/<newId>`. If the LLM request fails the user still lands on the new node, then the error is handled (§5.7). Note: the check covers the parent chain, not the new reply's own ai_usage; and it uses `data.id` (the head), not `tip_id`, when an entry was split.
- Read-reply form (`submitReadReplyMessage`): recorded → `save-as-node {content, agentic: true, auto_generate, model}`; ai_usage none → plain `POST /nodes/`; otherwise `POST /textmode/from-node/<id> {content, ai_usage, model, auto_generate}` → `{prompt_node_id, user_node_id, llm_node_id?, ...}`; navigate `/node/<user_node_id>[?awaitLlm=<llm_node_id>]`.
- Write New Entry modal: navigate `/node/<data.id>[?awaitLlm=<data.awaitLlm>]`.

Keyboard: **Cmd+Return / Ctrl+Enter** submits (`useSubmitShortcut`) when `canSubmit` (text or file, not loading, online, not recording). Plain Return inserts a newline. `submitShortcutHint()` returns "⌘↵" or "Ctrl+↵" (not rendered in NodeForm). Swift: support hardware-keyboard ⌘↩ and a Send button.

### 4.6 Text page `/textmode` (WritePage) and Home

- WritePage heading **"What's on your mind?"**; custom submit:
  - ai_usage not chat/train → toast "Turning off auto-generate. AI usage on some nodes is turned off." and save a plain entry (`save-as-node {content}` or `POST /nodes/ {content, privacy_level, ai_usage}`), land on it.
  - otherwise auto-generate = `localStorage.loore_auto_generate` (default true): recorded → `save-as-node {content, agentic: true, auto_generate}`; typed → `POST /textmode/start {content, privacy_level, ai_usage, auto_generate}`.
  - Navigation: `llm_node_id` + `user_node_id` → `/node/<user>?awaitLlm=<llm>`; only llm → `/node/<llm>?awaitLlm=<llm>`; only user → `/node/<user>`; else `/node/<id>`.
- HomePage: greeting "Good morning" (<12) / "Good afternoon" (<18) / "Good evening", heading "What's on your mind?", cards: **Voice** ("Speak what's present.", → `/voice`), **Text** ("Type what's on your mind.", → `/textmode`), **Share** ("Give something outward.", only if `user.share_v1_enabled`, → `/share`), **Read** ("What's worth your time today.", admin only; `POST /read/start {auto_generate}` → navigate `/node/<llm_node_id || prompt_node_id>`; description "Starting…" while busy; errors toast or "Could not start the read."). Rows: ≤3 cards one row; 4+ split into two rows (odd: Voice+Text alone first).

### 4.7 Edit

- Entry: kebab "Edit" on any owned bubble (focal or not).
- If the target has `context_artifacts.prompt` (a prompt-rooted system node), first a confirm dialog: title **"Edit prompt for this thread only?"**, body "This changes the system prompt in this conversation only. Future threads won't be affected, and updates to your prompt template won't reach this thread anymore." + "To edit the template for all future threads, go to [Prompts]." Buttons **"Cancel"** / **"Edit for this thread"**. Then the edit modal with `detachPrompt`.
- Edit modal (title **"Edit Text"**) seeded with the node's content/privacy/ai_usage; draft keyed by `node_id`; no mic/upload.
- RegenerateTtsDialog: **"Regenerate audio?"** — "This entry has generated audio that won't match your edits. Keep the existing audio, or regenerate it (it'll be created fresh the next time you play it)." Buttons: **"Regenerate audio"** ("Removes the outdated audio; new audio is generated on next play."), **"Keep existing audio"** ("Saves your edits; the current audio stays as-is."), **"Cancel"**.
- ApplyToRepliesDialog: **"Apply to replies too?"** — "You changed the {privacy | AI usage | privacy and AI usage} of a node that has replies." Buttons: **"This node only"** ("Replies keep their current settings."), **"This node and all my replies"** ("Every reply of yours below it, including AI responses you requested. Other users' replies are kept (they own them)."), **"Cancel"**.
- After save (`handleEditSuccess`): close modal; if `descendants_updated > 0` toast **"Applied to N reply/replies too"**. Non-focal edit → refetch focal and quotes; no generation. Focal edit → merge `data.node` into the held node (refetch if cascaded); if the edited node is a user node and auto-generate is active and the chain allows AI → `POST /nodes/<id>/llm` and navigate to `/node/<llmId>?awaitLlm=<llmId>` (state fromParent) — i.e. **editing a user node spawns a new reply branch**. Editing an LLM node never generates.
- Backend: a `NodeVersion` with the old text is saved when the text changed; context artifacts re-synced to placeholders; making a node private auto-unpins it.

### 4.8 Delete (`DeleteConfirmDialog`, `DELETE /nodes/<id>`)

Dialog variants:
- `single`, no children: **"Delete node?"** — "Content and edit history will be removed." → **"Delete"** (accent) / **"Cancel"**.
- `single`, has children (`child_count > 0` or children present): **"Delete node?"** — "This node has replies. Choose how to handle them:" → **"Delete this node only"** ("Replies stay; this node becomes a placeholder."), **"Delete this node and all my replies"** ("Other users' replies are kept (they own them).", accent), **"Cancel"**.
- `thread` (Log): **"Delete entire thread?"** — "Your content in this thread will be removed. Your nodes remain as placeholders so other users' replies stay reachable (they own them)." → **"Delete thread"** / **"Cancel"**.
- `prompt` follow-up: **"Delete the system prompt too?"** — "After this, nothing is left in the session but its system prompt, and {your Log | your public page} would list the session by the prompt's text." → **"Also delete the system prompt"** ("The whole session leaves {…}."), **"Keep the system prompt"** ("You can continue the session from it."), **"Cancel"**.

Flow:
1. If the thread root (first visible ancestor, else the node) `is_system_prompt` and the target isn't the root: `GET /nodes/<target>/delete-impact?delete_descendants=<bool>` → `{orphaned_system_prompt_id}`; non-null → show the `prompt` dialog. (Failure = proceed.)
2. `DELETE /nodes/<id>?delete_descendants=<bool>&delete_orphaned_prompt=<bool>` → `{scheduled, grace_days, orphaned_prompt_deleted, deleted_pinned_ids}`. Soft delete; purge after the grace period.
3. Toast **"Deleted N node(s)"** (3 s).
4. Landing: if `orphaned_prompt_deleted` → leave thread (public root with `share_v1_enabled` → `/commons`, else `/log`). Non-focal target that didn't cascade over the focal → refetch focal (the deleted node shows as a tombstone). Focal target, or an ancestor deleted with descendants → navigate to the nearest **alive** ancestor above it; none → leave thread.
5. Error → page error "Error deleting node." (replaces the view).

### 4.9 Rename thread (Log only) and version history (artifacts only)

- **RenameThreadDialog**: title **"Rename thread"**, text field (max 120 chars, placeholder = card's fallback title or "Thread name", aria "Thread name", selected on open), hint "Shown on this thread's Log card. Leave it empty to show the entry's own title." Buttons **"Cancel"** / **"Save"** ("Saving…"); Save disabled when unchanged. Return saves, Esc closes. `PUT /nodes/<thread_root_id>/thread-name {thread_name}` → `{thread_name|null}`; empty clears. Errors: 403 not owner, 400 "Only a thread root can be named", 400 length. Toast on failure: server error or "Error renaming thread.".
- **VersionHistoryDrawer** (artifacts): right-side drawer; list rows `v<N>` (+ "current" on the first), `"<date> · <generated-by label>"` (+ "· ~N source tokens"). Labels: user/manual → "Manual edit", revert → "Reverted", orient_session → "Orient session", voice_session → "Voice", import → "Imported", generation_type update/iterative/integration → "Auto-updated (g)" / "Iterative build (g)" / "Integrated profile (g)", else "Auto-generated (g)". Null date → "File default". Preview: **"Changes (N)"** / **"Full text"** toggle (defaults to Full when > 50 % of lines and > 40 lines changed), "Initial version" for the oldest, **"Revert to this version"** for non-current. Diff (`utils/diff.js`): common prefix/suffix trim, then LCS on lines (fallback to del-block+add-block above 1500 lines), word-level refinement of paired del/add lines when ≥ 40 % similar (jsdiff `diffWordsWithSpace`), unchanged runs folded with 2 lines of context into "⋯ N unchanged line(s) ⋯". "No changes from the previous version." when nothing changed.

---

## 5. LLM replies

### 5.1 The action row under the focal node

- **LLM Response** group (shown when `showLlmResponse`: craft bar condition and not "a read thread before its first picks"): button + attached `ModelSelector` (joined borders). Button states: **"LLM Response"** / spinner + **"Requesting…"** (during the POST) / "Waiting for AI…" / "Generating…". Disabled while requesting or tracking a task, and directly under a completed read reply (title "To chat about the recommendations, send your reply first. To read further, use the button on the right."; in a read thread otherwise title "Chat about the picks").
- **Read / Read further** group (owner, read thread, AI allowed, not pending) — admin read feature; see §5.9.
- On narrow screens the two groups become a 2-column grid (buttons | pickers).

### 5.2 Requesting: `POST /nodes/<parentId>/llm`

Body `{model: <id|null>, source_mode: 'textmode'}` (`source_mode` ∈ voice|textmode). Response **202** `{message, task_id, status: 'pending', node_id}` — `node_id` is the new placeholder LLM node (content `"[LLM response generation pending...]"`, inherits parent privacy/ai_usage).

Errors: **403** "You can only request an AI response on your own nodes. Reply first, then generate on your reply." (non-owner); **402** `{error: "monthly_spend_limit_reached", message}` (decorator + placeholder chokepoint); **400** unsupported model (`{error, supported_models}`) or `{user_export}` validation; **410** parent deleted.

After success from the button: navigate `/node/<newId>?awaitLlm=<newId>` with `state: {fromParent: true}` — the user watches the reply on its own page from the start (#367).

### 5.3 Model choice

- `ModelSelector` (`components/ModelSelector.js`): `GET /nodes/models` → `{models: [{id, name, provider: 'anthropic'|'openai', featured, read}]}` (active only, newest first per provider). Default: `GET /nodes/<nodeId>/suggested-model[?purpose=read]` (or `/nodes/default-model` with no node) → `{suggested_model, source: 'predecessor'|'user_preference'|'default'}`. Applied once per (node, purpose): a `predecessor` suggestion always wins; otherwise it only replaces an empty/unofferable selection.
- Options: chat collapsed = featured models (Anthropic first, then OpenAI) with the selected non-featured model prepended, then **"More models…"** (expands in place, list stays open); expanded = all models grouped under provider headings "Anthropic"/"OpenAI" (serif, accent). Read = only `read: true` models, no "More". Trigger shows selected `name` + chevron, "…" while loading; aria-label "Model: <name>" / "Model for Read: <name>". Opens upward if < 320 px below. Keyboard: arrows/Home/End/Enter/Space/Esc/Tab.
- NodeDetail's `selectedModel` initial value is `currentUser.preferred_model` and is reset on every node (remount). The picker only mounts with the craft bar, so **when auto-generate fires from the inline form without a visible picker, `model` is the user's `preferred_model`** (or null → server walk). Server resolution when `model` is absent (`utils/llm_nodes.resolve_chat_model`): nearest ancestor LLM reply's model that is still active (skipping reads) → `user.preferred_model` → `DEFAULT_LLM_MODEL`.

### 5.4 Progress while generating

Two channels run together on the pending node's page:

1. **Polling** `GET /nodes/<id>/llm-status` (`useAsyncTaskPolling`): immediately, then every **2 s** (every **15 s** in the batch stage), request timeout 10 s, header `Cache-Control: no-cache`, overall cap **30 min** (25 h for batch) → error "Polling timeout - task took too long". Also polls on returning to foreground. Stops at completed/failed/cancelled. Network errors are ignored and retried. Response:
   ```
   {node_id, status, progress, error, task_info, continuation_node_id,
    tts_task_status, tts_streaming,
    content (completed|cancelled), tool_calls_meta?, stage?: 'batch', batch_submitted_at?,
    feed_picks_count?, warnings: [string], node?: {id, content, node_type, llm_model, created_at}}
   ```
   Polling starts when `?awaitLlm=<this id>` is present, or when the opened node is itself a pending LLM node.
2. **Streaming text** (#367), SSE `GET /api/sse/nodes/<id>/llm-stream` (only for pending, non-batch LLM nodes): events `snapshot {text}` (whole text so far; on connect and after a restarted model call), `delta {text}` (append), `done {status, continuation_node_id, error}` (stop listening), `heartbeat` (every 15 s). Server polls the DB every 0.5 s; connection closes after 30 min. Initial text = `node.streaming_content` from the fetch. The client reconnects 3 s after a CLOSED error until `done`.

Pending UI: streamed text rendered through QuotedContent with `{quote:N}`/`{quote_ext:N}` markers removed and `:::share` fence lines (`/^:::(?:share(?:[ \t]+[A-Za-z]+)?)?[ \t]*\r?$/i`) removed, followed by pulsing dots; with no text yet: "Thinking" + dots ("Processing" for batch).

### 5.5 Completion

- `completed` + `continuation_node_id` (within-turn retrieval, #158): navigate to `/node/<cont>?awaitLlm=<cont>` (fromParent) and keep going. Each retrieval round is its own bubble.
- `completed` on the node being viewed: patch in place `content`, `tool_calls_meta`, `feed_picks_count`, `llm_task_status: 'completed'` (no navigation, no refetch — ancestors/children stay as they were).
- `completed` for another node id: navigate there.
- `stage === 'batch'` while tracking from another page: hand off → navigate to the pending node, which then polls slowly.

### 5.6 Failure / cancel

- `failed`: toast `llmError || "LLM response generation failed"` (8 s). If viewing the dead pending node: go back to its parent (`navigate(-1)` if we came from the parent, else replace with `/node/<parentId>`), unless the parent is a tombstone. The failed node remains in the thread (content is still the placeholder text).
- `cancelled` (a queued read withdrawn by the spend cap): toast `llmData.error || "Read cancelled"`, patch content.
- Backend sets `llm_task_status='failed'` with `llm_task_error` (exception string) on errors; a spend-capped user at task start is failed silently (no error text).

### 5.7 Request errors (`handleLlmRequestError`)

- 402 → silent (the global **SpendCapBanner** appears: bottom-center, "LIMIT REACHED" label + server `message` or default "You've reached your monthly usage limit for the free alpha. It resets at the start of next month.", dismiss "×"; driven by the axios interceptor event `loore:spend-capped`).
- Otherwise toast `response.data.error || err.message || "Error requesting LLM response."` (8 s). Never replaces the page.
- `spend_capped: true` in textmode/save-as-node 2xx responses → the banner (entry kept, no reply). `llm_error` → toast (10 s).
- Client-side cap flag (`utils/spendCap.js`): set from `user.spend_blocked` on load and any 402; blocks **Record** and **Upload** before they start with toast "You've reached your monthly usage limit, so you can't start a new recording / upload audio until it resets on <Month D>." (reset = first of next **UTC** month).

### 5.8 Auto-generate

- Preference `localStorage.loore_auto_generate` (default true). Toggle in thread top-right (craft only): "Auto-generate" + pill; title "Auto-generate is on — click to turn off" / "Auto-generate is off — click to turn on".
- Applies to: inline reply submit, focal edits of user nodes, WritePage, Write New Entry modal (its own toggle, same key), `/read/start`, `/read/from-node`.
- Not applied: Reply modal, public threads (forced off).
- Auto-off rule: any node in `[parent, ...ancestors]` with ai_usage outside {chat, train} → preference set to **false** permanently + toast.

### 5.9 Other entry points on the thread page

- **Voice Mode** (top-right, owner, ai_usage≠none, not public; title "Continue this conversation by voice"; "Starting…"): `POST /voice/from-node/<id> {model}` → `{mode, llm_node_id, parent_id, fresh}`; `mode === 'processing'` → `/voice?resume=<llm>&parent=<p>[&fresh=1]`, else `/voice?parent=<p>`. 402 silent; other error → page error `"Error starting voice session."`. (Voice page is another map.)
- **Read / Relevant tweets / Read further** (admin-only Community Archive feature): `POST /read/from-node/<id> {model?, auto_generate}` → `{llm_node_id?, prompt_node_id?}` → navigate to whichever. Admin rerun: **"Rerun live"** / **"Resubmit batch"** (`POST /read/<id>/rerun {live}`), toasts "Running the read live." / "Read resubmitted as a batch." / "Could not rerun the read.". Tooltips: READ_ENTRY "Loore reads the last day of Community Archive tweets and shows you the ones relevant to this thread"; READ_FURTHER "Another pass over the day's tweets, against everything in this thread so far — your marks on these picks included." Candidate for deferral in iOS v1.

---

## 6. Proposals (`components/ProposalInline.js`)

### 6.1 What they are

In agentic threads (textmode/voice prompt) the model may end a reply with structured sections the user can act on. After generation the backend (`tasks/llm_completion._auto_create_drafts`, agentic only, not on cut-off replies) detects them and creates a **pending Draft** (label `todo_pending` / `github_issue_pending` / `feedback_pending` / `share_pending`, `parent_id = llm node id`) plus a `tool_calls_meta` entry `{name: 'propose_todo'|…, status: 'success', draft_id, apply_status: 'pending_approval'}`. A newer proposal of the same kind deletes the older pending draft and marks older entries `apply_status: 'superseded'` (the UI doesn't show "superseded"; applying an old one then returns 404 "No pending … found", which the card shows as its error).

Detection (backend and frontend agree):
- Todo: any `### ` heading (outside share fences) containing "completed", "new task", "new tasks" or "priority". A lone `### Note` is not enough.
- GitHub issue: heading containing "issue title" (or exactly "title") **and** one containing "description".
- Feedback: heading exactly "feedback".
- Share: a `:::share` fence line, or legacy heading exactly "share".

Where the card shows (`NodeDetail.showProposal`): node has content, not pending, and (LLM node with `hasProposalSections`) **or** (a user node, owner, `share_v1_enabled`, with `:::share` fences → share-only mode). Also used by VoicePage with `size="roomy"`. The frontend shows the card (and its Apply button) for any LLM reply whose text matches, even in a non-agentic thread where the backend created no pending draft; pressing Apply there returns 404 and the card shows the server's error text.

### 6.2 Parsing

- `parseOrientResponse(text)`: strip share fences, split on `^###\s+` (the text before the first heading is never a section). Heading (lowercased, trimmed) → section:
  - includes "completed" → `completed`; includes "new task" → `newTasks`; includes "priority" → `priority`; includes "note" → `note` (proposal tags `[xxx-proposal:...]` stripped); includes "issue title" or == "title" → `issueTitle`; == "description" → `issueDescription`; == "category" → `issueCategory` (**first line only**, lowercased); == "feedback" → `feedback`; == "feedback category" → `feedbackCategory` (first line); == "share" → `share`; == "share type" → `shareType` (first line). First match wins in that order.
- Share fences: open `/^:::share(?:[ \t]+([A-Za-z]+))?[ \t]*\r?$/i`, close `/^:::[ \t]*\r?$/`. `parseShareBlocks` → `[{content (trimmed), type (lowercase word or '')}]`; an unclosed fence runs to the end; `###` headings inside a fence belong to the share body; with no fences, fall back to legacy `### Share` / `### Share type`.
- Todo items (`parseTodoItems`): each non-empty line with leading `- `/`* ` and `[ ]`/`[x]` removed, `**`/`__` stripped.
- Priority items (`parsePriorityItems`): strip `1.`/`1)`, bullets, checkboxes; split "text — hint" / "text – hint" or "text (hint)".
- `splitProposalText(text, {shareOnly})` → `{before, after}`: lines inside share fences and inside proposal sections are removed; lines before the first proposal section form `before` (shown above the card), lines after form `after` (shown below, with 20 px top margin). Proposal headings (substring): completed, new task(s), priority (order), issue title, title, description, category, feedback; plus "note" only when a task heading exists; exact: "share", "share type". Value sections (category, feedback category, share type) consume only their one value line (blank lines around it dropped); the next non-empty line starts trailing commentary. A non-proposal `###` heading ends a section and stays visible. `shareOnly`: only fences are removed; `###` headings are the user's own.

### 6.3 Card rendering (compact variant in the thread)

Section labels: 0.68rem uppercase, 0.18em tracking, accent at 60 % opacity.

- **Todo** (`hasTodo` = completed/newTasks/priority parsed):
  - Label **"Updated from your sharing"** then rows: Completed items (filled `--accent-dim` 16 px circle with ✓, text struck through at 40 % opacity) and New Tasks items (empty circle `--border-hover`).
  - **"Suggested priority order"**: numbered cards (serif number, text, muted hint).
  - Note: "A note: <text>".
  - Apply area (shown whenever there's a todo proposal or a `propose_todo` meta): **"Apply changes to my Todo"** → "Todo update started…" → **"Todo updated"** (success color) / error text (default "Todo update failed").
- **GitHub issue** (needs `issueTitle`): label **"GitHub Issue"**, card with title (serif), description (markdown), category badge (uppercase pill); button **"Create issue"** → "Creating issue…" → **"Issue created — #<n>"** (link to issue_url) / error ("Issue creation failed").
- **Feedback**: label **"Feedback"**, card with markdown + category badge; **"Send feedback"** → "Sending…" → **"Feedback sent — thank you"** / error ("Feedback send failed").
- **Shares**: label **"Share"** / **"Shares"** (roomy: "Proposed Share(s)"); one card per block with markdown body, type badge, a copy button (top-right; copies block text; check icon for 1.5 s; title "Copy share text"); per-block **"Save to shares"** → "Saving…" → **"Saved as a private draft — publish from your [Share page]"** / error ("Saving the share failed").
- **Preferences**: "Preferences updated" when `update_ai_preferences` succeeded.
- Roomy (Voice) variant labels: "GitHub Issue Proposal", "Create GitHub Issue", "Feedback for the Team", "Send feedback to the team", "Save to your shares", plus pulsing AI dot on labels.

State from `tool_calls_meta` on load: `propose_todo.apply_status` completed/failed(+apply_error)/started, or `apply_todo_changes` success → completed; issue completed (with `issue_url`, `issue_number`); feedback completed; share completed (all) or `saved_indexes` (per block). The accept buttons and their status show to the reply's owner only; anyone else sees the proposal's text and the share copy button.

### 6.4 Accept flows (no explicit "reject")

There is no reject/dismiss button. Not applying leaves the proposal pending until superseded. In agentic threads the user can also confirm by replying ("yes, apply it"); the model then calls `apply_todo_changes` / `apply_github_issue` / `apply_feedback` / `apply_share` tools (shown under "Actions taken").

| Action | Request | Response / follow-up |
|---|---|---|
| Apply todo | `POST /todo/apply-draft {llm_node_id}` | 202 `{status:'started', task_id, llm_node_id}`; 404 "No pending todo changes found". Then poll `GET /nodes/<id>/llm-status` every **2 s** until `tool_calls_meta[propose_todo].apply_status` is `completed` or `failed` (`apply_error`). The merge task reads the **LLM node's current content** (so the user's ticks/edits count), merges into the todo with the `orient_apply_todo` prompt, saves a new UserTodo version. |
| Create issue | `POST /github/create-issue {llm_node_id}` | `{issue_url, issue_number, ...}`; errors 404 "No pending GitHub issue found", 400 "Could not parse issue from proposal", 500. |
| Send feedback | `POST /feedback/submit {llm_node_id}` | `{feedback_id, ...}`; 404 "No pending feedback found". |
| Save share | `POST /share/save-proposal {node_id, share_index}` | `{status: 'completed'|'partial', share, shares, saved_indexes, total}`; 400 "Already saved"; 404 "No pending share found" / "Not found" (sharing disabled). Works on the user's own nodes without a pending draft. |

After each success the page's copy of `tool_calls_meta` is updated (`onApplied`).

### 6.5 Editing the proposal before applying (owner only; disabled once apply started/completed)

- **Tick / untick** a row: tap the circle ("Mark as done" / "Unmark as done"). `moveProposalItem(content, itemText, from, to, {prepend})`:
  1. Find `###` sections; locate the source section whose lowercase heading **includes** `from` ('new task' or 'completed').
  2. Find the first line in it whose text (bullet, checkbox, `1.` stripped; `**`/`__` stripped; trimmed) equals `itemText`; remove it. Not found → content unchanged.
  3. If a target section exists: insert the raw line right after its heading (`prepend`, used when unticking) or after the last consecutive non-blank line following the heading (append, used when ticking).
  4. **If the target section doesn't exist (#377, fixed in fee95d2 / #378)**: create it next to the source section in prompt order (Completed before New Tasks): target ranks earlier → insert `### Completed`, the line, and a blank line right above the source heading; target ranks later → insert a blank line, `### New Tasks`, the line after the source section's last non-blank line. So the item is never dropped.
  Then optimistic `onContentChange(new)` + `PUT /nodes/<id> {content}`; failure reverts and reports "Couldn't save change — reverted (<reason>)".
- **"+" on a row** → inline input "New item…" (Enter adds, Esc/blur-empty cancels, only one open at a time) → `insertItemAfter` (same as §3.5) + PUT; failure "Couldn't add task — reverted (<reason>)".

---

## 7. UI copy catalogue (thread + writing)

Buttons / labels: "Thread", "Voice Mode", "Starting…", "Relevant tweets", "Read", "Read further", "Auto-generate", "LLM Response", "Requesting…", "Waiting for AI…", "Generating…", "Thinking", "Processing", "Rerun live", "Rerunning…", "Resubmit batch", "Actions taken (N)", "Reply", "Edit", "Delete", "More actions", "Send", "Sending...", "Uploading... N%", "Transcribing... N%", "Waiting to transcribe...", "Discard draft", "Record", "Offline", "Resume", "Finalizing...", "Error - Retry", "Stop & save", "Save audio", "Upload", "Remove", "Agentic Reply", "Privacy Level", "AI Usage", "More models…", "Mark as read", "Mark as unread", "Good quote", "Bad quote", "Mark all as read", "All read.", "New item…" (placeholder), "Add an item below".

Placeholders: "What's present for you right now..." (default), "Type what's on your mind…" (thread inline, /textmode), "Ask about these picks, or say what you make of them…" (read reply).

Inline text: "Loading node...", "No node found.", "This node doesn't exist, was deleted, or isn't shared with you.", "Error fetching node details.", "Error toggling pin.", "Error deleting node.", "Error starting voice session.", "[Node deleted]", "[Node inaccessible]", "[Quoted node inaccessible]", "[Quoted node deleted]", "[Quoted reference inaccessible]", "[<Label> not available]", "Quoted from @user", "Saved from @handle · Source", "You rated this good/bad on <date>", "Draft saved just now/Nm ago/Nh ago", "Saving...", "You're offline", "Transcribing recovered audio...", "Content is required.", "Public — inherited from the thread.", "Offline — recording continues, uploads will retry when connection returns", "Recording paused — another app took the microphone. Audio up to the interruption is saved.".

Toasts: "Turning off auto-generate. AI usage on some nodes is turned off." (8 s), "Applied to N reply/replies too", "Deleted N node(s)", "LLM response generation failed", "Error requesting LLM response.", "Read cancelled", "Couldn't save change — reverted (…)", "Couldn't add task — reverted (…)", "Could not start the read.", "Could not update the read mark.", "Could not save your feedback.", "Could not mark the list as read.", "You've been recording for 59 minutes — consider stopping soon and continuing in a new recording." (10 s, with chime), spend-cap messages, "Craft mode on. Its controls carry the sliders icon.", "Craft mode off.". Default toast duration 3 s.

Dialogs: see §1.6 (craft), §4.5 (split, public reply), §4.7 (prompt edit, TTS, apply to replies), §4.8 (delete variants), §4.9 (rename).
- SplitContentDialog: **"Long entry"** — "This text is N characters — above the 100,000-character limit for a single entry. It can be saved as ~P connected entries: they read as one continuous text and stay together in the thread." → **"Split into ~P connected entries"** ("Split happens on line breaks — no line is cut in half.") / **"Cancel"**. P = max(2, ceil(N / 100000)).
- PublicReplyDialog: **"This reply will be public"** — "You're responding in a public thread, so your reply will be visible to everyone — including people who aren't signed in. To think about this privately instead, quote it into one of your own threads." Checkbox "Don't show this again" (→ `localStorage.loore_public_reply_ack = "true"`). Buttons **"Cancel"** / **"Publish reply"** (filled accent).

Audio-disabled tooltips: "Set AI Usage to Chat or Train to record or upload audio — transcription needs AI access.", "You're offline — reconnect to record or upload audio.", "Finish the current recording first.", "You're offline — reconnect to record audio."

---

## 8. Jest tests and what they pin

| Test file | Pins |
|---|---|
| `components/ProposalInline.test.js` | Category badges take the first line only (issue + feedback). `stripProposalSections`/`splitProposalText` keep lead-in and trailing commentary after a single-line value section, exclude structured parts. Multiple `:::share` blocks with inner `###` headings; unclosed fence runs to end; legacy `### Share` fallback; `hasShareBlocks` matches fences only (not prose mentioning `:::share`, not legacy headings); `hasProposalSections` true for a fence. Lead-in "Noted both — …" is not a note section. `###` headings inside a share body don't trigger other sections. A standalone "### A note on process" stays in the prose; "### Note" belongs to the card only with task headings. `shareOnly` split keeps own headings. **`moveProposalItem` (#377/#378)**: ticking with no Completed creates `### Completed` above New Tasks (exact output asserted); unticking moves back (prepend); unticking with no New Tasks creates it below Completed (before Priority Order); unticking the only item of a trailing Completed appends `### New Tasks` (exact string `'Intro.\n\n### Completed\n\n### New Tasks\n- ship it\n'`); both sections present → appended to the end of the existing Completed list; missing item → unchanged. |
| `components/MarkdownBody.test.js` | `remarkHtmlAsCode`: block html → `code` (raw value kept), inline html → `inlineCode`, pure comments dropped, recursion into blockquotes, non-html untouched. |
| `components/MarkdownBody.stable.test.js` | #321: toggling a checkbox calls `onCheckboxToggle('two', false)`; after re-render with new callbacks the checkbox elements are the same instances, aria-checked updates, focus stays; a half-typed "+" input survives a parent re-render. |
| `components/Bubble.test.js` | `splitPreview` (heading marker stripped, body split, `isHeading`); thread name replaces a `#` heading; on plain text keeps every line as body; one-line plain entry keeps the line; heading-only entry shows just the name; blank name falls back. |
| `components/ExternalQuoteBubble.test.js` | Owner-only read toggle + verdict glyphs; toggle doesn't open the post; POST/DELETE bodies (with `node_id` when in a reply); body tap opens `url` with `_blank`, marks read `{via:'open'}` and reports `onReadChange`; opening an already-read post still logs; non-owner logs nothing and sees no buttons; a verdict marks read; `feedback_shared` title suffix; `rated_before` line with empty control; `data-read` attribute states. |
| `components/ModelSelector.test.js` | `pickerOptions`: featured Anthropic-first + More; non-featured selection first once; expanded grouping; read → only read models, no More. Read picker asks `/nodes/7/suggested-model {purpose:'read'}` and replaces a chat model; a deprecated preference is replaced; an offered selection is kept over a non-predecessor default; More expands in place without choosing; keyboard End+Enter expands and lands on the first added model; outside click closes; disabled stays shut. |
| `utils/nodeLinks.test.js` | Accepted/rejected hrefs (listed in §3.4); same-tick lookups coalesce into one `GET /nodes/titles?ids=1,2`; cache; failures resolve `undefined` and are retried. |
| `utils/markdown.test.js` | `stripInlineMarkdown` reduces links to text; toggling/inserting keyed by stripped label works on a link-bearing item; raw label does not match. |
| `utils/diff.test.js` | Line diff basics, folding with context, word refinement only for similar pairs, positional pairing. |
| `utils/spendCap.test.js` | Reset date = first of next UTC month; toast names the action; only 402 + `monthly_spend_limit_reached` counts. |
| `utils/intentions.test.js` | Intentions artifact parser (sections, entries, status line, notes). Not used by the thread; `parseIntentions`/`statusState` feed `IntentionsView` on `/artifacts/intentions`. |
| `components/SpeakerIcon.test.js` | #376: TTS chunks arriving at once all reach the play queue, in order. |
| `components/ReadReply.test.js`, `FeedPicks.test.js`, `ReferenceFeedback.test.js` | Read-feature widgets (admin). |

No tests exist for NodeDetail, NodeForm, useDraft, useAsyncTaskPolling, QuotedContent, InlineQuoteBubble, the dialogs, or WritePage.

---

## 9. Endpoint reference (this area)

| Method & path | Purpose |
|---|---|
| GET `/nodes/<id>` | Thread payload (§1.2) |
| POST `/nodes/` | Create text node (JSON) or audio node (multipart) |
| PUT `/nodes/<id>` | Edit content/privacy/ai_usage (+ detach_prompt, regenerate_tts, apply_to_descendants); also used for checkbox toggles, "+" inserts and proposal ticks (`{content}` only) |
| DELETE `/nodes/<id>?delete_descendants&delete_orphaned_prompt` | Soft delete |
| GET `/nodes/<id>/delete-impact?delete_descendants` | Orphaned system prompt check |
| POST/DELETE `/nodes/<id>/pin` | Pin to public page |
| PUT `/nodes/<id>/thread-name` | Name a thread root |
| GET `/nodes/<id>/resolve-quotes` | Quote data |
| GET `/nodes/titles?ids=` | Node-link titles |
| GET `/nodes/models`, `/nodes/default-model[?purpose]`, `/nodes/<id>/suggested-model[?purpose]` | Model picker |
| POST `/nodes/<id>/llm` | Request reply |
| GET `/nodes/<id>/llm-status` | Poll reply |
| SSE `/api/sse/nodes/<id>/llm-stream` | Reply text stream |
| GET `/nodes/<id>/transcription-status` | Audio upload transcription |
| `/nodes/upload/init|chunk|finalize|cleanup` | Chunked audio upload (>10 MB) |
| GET/POST/DELETE `/drafts/?node_id|parent_id` | Text drafts |
| `/drafts/streaming/init`, `/<sid>/audio-chunk`, `/<sid>/finalize`, `/<sid>/status`, `/<sid>/transcribe-remaining`, `/<sid>/discard`, `/<sid>/save-as-node`; SSE `/api/sse/drafts/<sid>/transcription-stream` | Recorded input in forms (15 s chunks) |
| POST `/textmode/start` | New agentic thread (+ optional reply) |
| POST `/textmode/from-node/<id>` | Agentic message under a node (read replies) |
| POST `/voice/from-node/<id>` | Hand-off to Voice |
| POST `/read/start`, `/read/from-node/<id>`, `/read/<id>/rerun` | Read feature (admin) |
| POST `/todo/apply-draft`, `/github/create-issue`, `/feedback/submit`, `/share/save-proposal` | Proposal accepts |
| POST/DELETE `/external/items/<id>/read`, POST `/external/items/<id>/feedback` | External quote controls |
| POST `/nodes/<id>/feed-picks/read`, GET `/nodes/<id>/feed-picks` | Read picks (admin) |
| GET `/nodes/<id>/audio`, `/audio-chunks`, `/tts-chapters`; POST `/nodes/<id>/tts`; SSE `/api/sse/nodes/<id>/tts-stream` | Speaker/download |
| PUT `/dashboard/user {craft_mode}` | Craft mode |
| GET `/commons/permalink/<user>/<slug>` | Permalink resolution |
| GET `/search/neighbors?node_id&limit` | Admin neighbors rail |

Client-side persisted keys (use `UserDefaults`): `loore_auto_generate` (default true), `loore_agentic_reply` (default true), `loore_last_privacy_level`, `loore_last_ai_usage`, `loore_public_reply_ack`, `loore_craft_mode` (mirror of server field).

---

## 10. Porting notes: hard parts and risks

1. **Markdown fidelity.** Swift's `AttributedString(markdown:)` does not do GFM tables, task lists, footnotes, or block layout. Use a real CommonMark+GFM parser (e.g. swift-markdown / cmark-gfm) and a custom SwiftUI renderer. Requirements: pre-wrap paragraphs (single newlines are hard breaks), HTML nodes shown as code / comments dropped, custom task circles with toggle + "+", link interception for node links, tables in horizontal scroll, no syntax highlighting needed.
2. **Pre-markdown segmentation** (`{quote:N}`, `{quote_ext:N}`, `{user_*}`) must split the string exactly like `COMBINED_PATTERN` and render each text segment as its own markdown document.
3. **Checkbox/insert matching by plain text** depends on the renderer's text extraction equalling `stripInlineMarkdown(rawLabel).trim()`. Port both sides together and test with the web's fixtures; duplicate labels hit the first match.
4. **Proposal parsing** is regex/heading-based with many edge cases (tests in §8 are the spec). Port `parseOrientResponse`, `parseShareBlocks`, `splitProposalText`, `moveProposalItem`, `insertItemAfter` as pure functions and port the jest cases as XCTest.
5. **Reply lifecycle** = navigation + polling + SSE together. On iOS: URLSession-based SSE (no EventSource), reconnect with 3 s delay until `done`, poll `llm-status` every 2 s (15 s batch), refresh on foreground (the web does this for iOS Safari throttling), handle continuation chains and the "fail → back to parent" rule. Backgrounded apps will drop SSE; rely on `streaming_content` + polling on resume.
6. **Hover-only affordances** (kebab reveal, "+" add-item, proposal "+") need touch equivalents; the web already shows the kebab always on touch devices.
7. **Full-subtree render**: `GET /nodes/<id>` returns and renders the whole descendant tree. Large threads mean big payloads and deep nesting; a lazy list with indentation is needed.
8. **Craft-mode matrix** (§1.6) and the auto-generate rules (§5.8) are easy to get subtly wrong; e.g. the Reply modal never auto-generates, public threads force it off, the auto-off rule is sticky.
9. **Quirks to decide on explicitly** (keep for parity or fix): raw `{quote:N}` visible until quotes load; expanded non-focal bubbles don't resolve quotes; `llm-status.warnings` never shown in the thread; auto-generate sends `preferred_model` explicitly when no picker is visible (overriding the thread-predecessor default); auto-gen check ignores the new reply's own ai_usage; split entries get their reply under the head, not the tip; empty textarea doesn't clear the server draft; pin errors replace the whole page; `ARTIFACT_PATTERN` is a global regex used with `.test()` (stateful `lastIndex`).
10. **Dialog chains** in submit (TTS → apply-to-replies → split → public reply) resume the same submit with accumulated choices; model this as a small state machine.
11. **Auth/SSE**: cookie session must be shared by URLSession requests and the SSE connection; send `X-Timezone`.
