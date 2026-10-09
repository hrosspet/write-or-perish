# Map A — Screens, navigation and global UI (Loore web → SwiftUI)

Source: `.claude/worktrees/ios-app/frontend/src` at origin/main `2774ab2` (2026-09-30).
Scope: breadth inventory. Deep behaviour of NodeDetail/NodeForm/Bubble/MarkdownBody/ProposalInline, the voice/audio pipeline, and the secondary feature pages belongs to the other mappers; here they are at medium depth.

API paths below are relative to `/api` unless they start with `/auth` or `/media` (axios `baseURL` = `REACT_APP_API_URL` = `https://loore.org/api` in prod). `{x}` = path param.

---

## 0. App shell and bootstrap

### 0.1 Provider tree (`index.js`, `App.js`)

```
React.StrictMode
└ BrowserRouter
  └ ThemeProvider            (contexts/ThemeContext)
    └ UserProvider           (contexts/UserContext)  — GET /dashboard on mount
      └ ToastProvider        (contexts/ToastContext)
        └ App
          └ AudioProvider    (contexts/AudioContext) — global player state, survives route changes
            ├ NavBar                      (fixed, 56px, every route incl. landing/login/public pages)
            ├ SpendCapBanner              (bottom-center, listens to window 'loore:spend-capped')
            └ <div paddingTop 60px>
               ├ NodeFormModal "Write New Entry"  (when showNewEntry)
               ├ SearchModal                       (when showSearch)
               ├ TermsModal                        (when user && !user.terms_up_to_date)
               ├ UpdatesModal                      (when /updates has unread && !showTerms)
               ├ ProfileGenerationWatcher          (renders nothing; polls)
               └ <Routes> …
```

Sentry initialises only when `REACT_APP_SENTRY_DSN` is set at build time (`sendDefaultPii: false`).

### 0.2 What App.js does globally

| Behaviour | Detail |
|---|---|
| Terms gate | Whenever `user` loads with `terms_up_to_date === false`, `TermsModal` opens over everything (z 10000, no close). On accept: `setUser({...user, terms_up_to_date: true})`; if `!user.approved` → navigate `/alpha-thank-you`. |
| Dev updates | Once per session (ref guard), when `user.approved && user.terms_up_to_date`: `GET /updates`. If `changelog.length + notifications.length + polls.length > 0` → `UpdatesModal`. Errors ignored. |
| Search | `⌘K` / `Ctrl+K` anywhere toggles `SearchModal`. Scope = `'external'` when pathname starts with `/references`, else `'archive'`. Also opened by the magnifier buttons on Log (`archive`) and References (`external`). |
| New entry modal | `NodeFormModal` titled **"Write New Entry"**, `NodeForm` with `parentId=null, allowAgenticPrompt=true`. On success → `/node/{id}` plus `?awaitLlm={llmId}` when the entry went through `/textmode/start`. Opened by: ⋮ menu "Write new entry" (craft mode only) and WelcomePage "Start writing". |

### 0.3 Auth mechanics that shape the screens (backend unchanged)

- **Session cookie only.** Flask-Login `session` + `remember_token` cookie (30 days), `Secure`, `HttpOnly`, `SameSite=Lax`. Axios sends `withCredentials: true`. There is no bearer auth for the app; personal API tokens (`loore_…`) are scoped to `external:write` (Chrome clipper) and cannot read.
- Unauthenticated `/api/*` → `401 {"error":"Unauthorized"}`. `UserContext` treats any failure of `GET /dashboard` as "logged out" (`user = null`).
- Logged-in but **unapproved** users get `403 {"error":"Your account is not approved…"}` on every API except `GET /api/dashboard*`, `PUT /api/dashboard/user`, `POST|DELETE /api/dashboard/email*`, `/api/terms*`, `/auth/*`.
- **Sign in with X:** full-page navigation to `{BACKEND}/auth/login?next={returnUrl}` → X OAuth → backend redirects to `{FRONTEND_URL}{next}` (default `/dashboard`). Errors come back as `/login?error=x_try_again`.
- **Magic link:** `POST {BACKEND}/auth/magic-link/send` JSON `{email, next_url}` (plain `fetch`, not axios) → email → `GET /auth/magic-link/verify?token=…` (backend) → redirect `{FRONTEND_URL}{next}` or `/login?error=invalid_or_expired|link_already_used|confirm_needs_account`. Link expires in 15 min.
- **Connect X to an email account:** full-page link `{BACKEND}/auth/x/connect` → back to `/account?x_login={linked|cancelled|failed|other_x|taken|taken_placeholder}#x`.
- **X bookmarks OAuth:** link `{BACKEND}/api/external/twitter/connect` → back to `/import?x_connect=ok|failed` (the frontend ignores the param).
- **Logout:** plain `<a href="{BACKEND}/auth/logout">` (optional `?next=`), full page load.
- Every axios request carries header `X-Timezone: <IANA tz>`.
- Every `402 {"error":"monthly_spend_limit_reached","message"}` response dispatches `window` event `loore:spend-capped` → SpendCapBanner. The call site still sees the rejection.

Native consequence: the app must hold the Flask session cookie (shared `HTTPCookieStorage`) and must capture it from flows that currently end in a browser redirect to `https://loore.org/...`. See Risks §8.

---

## 1. Route inventory

Guards: **Public** = no session needed. **Session** = any logged-in user, approved or not. **Approved** = `ProtectedRoute` (logged out → `/login?returnUrl=<path+search>`; unapproved → `/alpha-thank-you`; while `loading` renders "Loading..."). **Admin** = Approved on the client; the backend returns 403 to non-admins (no client-side `is_admin` check in `AdminPanel`). **Dual** = renders a different screen for members and visitors. The Terms modal overlays every route for a logged-in user whose terms are out of date ("pre-terms" is a modal state, not a route).

| # | Path | Component | Guard | Class | Purpose |
|---|---|---|---|---|---|
| 1 | `/` | `RootRoute` → `HomePage` | logged out → `/landing`; unapproved → `/alpha-thank-you` | CORE | Home: greeting + mode cards (Voice, Text, Share*, Read*) |
| 2 | `/landing` | `LandingPage` | Public | PUBLIC/MARKETING | Marketing landing, "Join the Alpha" |
| 3 | `/login` | `LoginPage` | Public (logged-in → `returnUrl`) | PUBLIC/MARKETING | Sign in with X or email magic link |
| 4 | `/vision` | `VisionPage` | Public | PUBLIC/MARKETING | Vision essay |
| 5 | `/why-loore` | `WhyLoorePage` | Public | PUBLIC/MARKETING | Why Loore essay |
| 6 | `/how-to` | `HowToPage` | Public | PUBLIC/MARKETING | Tips and workflows |
| 7 | `/alpha-thank-you` | `AlphaThankYouPage` | Session (logged out → `/landing`) | PUBLIC/MARKETING | Waitlist page for unapproved signups: leave/confirm email, tweet-seed consent |
| 8 | `/confirm-email` | `ConfirmEmailPage` | Public; asks for sign-in itself | SECONDARY | Confirms a new sign-in email (`?token=`) |
| 9 | `/welcome` | `WelcomePage` | Approved | SECONDARY (onboarding, once) | First-visit welcome after approval (Activate & Welcome email link lands here) |
| 10 | `/voice` | `VoicePage` | Approved | CORE | Voice mode: record → transcribe → LLM → TTS playback loop |
| 11 | `/textmode` | `WritePage` | Approved | CORE | Text mode: one textarea, agentic first entry |
| 12 | `/profile` | `ProfilePage` | Approved | SECONDARY | AI-written profile artifact (view/edit/history) |
| 13 | `/todo` | `TodoPage` | Approved | SECONDARY | Todo artifact as a checklist |
| 14 | `/log` | `Log` | Approved | CORE | Private log of threads (cards, infinite scroll) |
| 15 | `/feed` | `Navigate → /log` | — | redirect | legacy |
| 16 | `/dashboard` | `Navigate → /profile` | — | redirect | legacy; **also the backend's default post-login target** |
| 17 | `/dashboard/:username` | `DashboardRedirect → /@:username` | — | redirect | legacy public dashboard |
| 18 | `/prompts` | `PromptsPage` | Approved | SECONDARY (craft) | List of the user's system prompts |
| 19 | `/prompts/:promptKey` | `PromptDetailPage` | Approved | SECONDARY (craft) | View/edit/version one prompt |
| 20 | `/import` | `ImportPage` | Approved | SECONDARY | Import archives (zip) + external references (CA, X bookmarks, clipper tokens) |
| 21 | `/references` | `ReferencesPage` | Approved | SECONDARY | Saved external references (tweets, bookmarks, clips) |
| 22 | `/references/:id` | `ReferenceDetailPage` | Approved | SECONDARY | One reference, full text, embeds |
| 23 | `/ai-preferences` | `Navigate → /artifacts/ai_preferences` | — | redirect | legacy |
| 24 | `/artifacts` | `ArtifactsPage` (kind defaults to `memory`) | Approved | SECONDARY | Artifact workspace; `?create=1` opens the new-artifact form |
| 25 | `/artifacts/:kind` | `ArtifactsPage` | Approved | SECONDARY | One artifact (intentions, memory, scratchpad, predictions, ai_preferences, custom) |
| 26 | `/share` | `SharePage` | Approved; shows "Not available." unless `share_v1_enabled` | SECONDARY | Drafts/published/revoked shares |
| 27 | `/commons` | `CommonsPage` | Approved; "Not available." unless `share_v1_enabled` | SECONDARY | Public feed of everyone's public roots |
| 28 | `/account` | `AccountPage` | Approved | SECONDARY | Username, email, X, plan, settings |
| 29 | `/node/:id` | `NodeRoute` | Dual | CORE (member) / PUBLIC (visitor) | Member: `NodeDetail` thread UI. Visitor: `PublicThreadPage` (404/private → `/login?returnUrl=`) |
| 30 | `/admin` | `AdminPanel` | Admin | ADMIN | Users / Activity / Feedback / Polls |
| 31 | `/:atName` | `AtRoute` → `PublicSharePage` | Public | PUBLIC/MARKETING | `/@username` public page. Anything not starting with `@` → `/` |
| 32 | `/:atName/:slug` | `AtRoute` → `PermalinkRoute` | Dual | PUBLIC/MARKETING | `/@username/slug` permalink: resolves via `GET /commons/permalink/{username}/{slug}` → member `NodeDetailWrapper(nodeId)` / visitor `PublicThreadPage(nodeId)`; failure → `PublicThreadPage("missing")` |
| 33 | `*` | `Navigate → /` | — | redirect | catch-all |

**Counts:** 33 route entries = 28 screen routes + 4 legacy redirects + 1 catch-all.
By class (screen routes): **CORE 5** (`/`, `/voice`, `/textmode`, `/log`, `/node/:id`), **SECONDARY 14**, **PUBLIC/MARKETING 8**, **ADMIN 1**. Two routes are dual-mode (`/node/:id`, `/@user/:slug`).

Server-side rendering note: the backend (`public_pages.py`) also serves `/`, `/landing`, `/why-loore`, `/vision`, `/how-to`, `/login`, `/alpha-thank-you`, `/@{u}`, `/@{u}/{slug}`, `/node/{id}`, `/@{u}/feed.xml`, `/sitemap.xml`, OG images, as HTML shells with meta tags for crawlers; the SPA then takes over. Not relevant to native beyond deep-link paths.

### 1.1 Query parameters and hash anchors the screens read

| Route | Param | Effect |
|---|---|---|
| `/login` | `returnUrl` (default **`/dashboard`** → `/profile`), `error` | error codes: `invalid_or_expired`, `link_already_used`, `confirm_needs_account`, `x_try_again`. `returnUrl` starting with `/confirm-email` switches the subtitle and hides the X-account note |
| `/node/:id` | `awaitLlm={nodeId}` | If equal to `:id`, start polling that LLM node; otherwise replace-navigate to `/node/{awaitLlm}?awaitLlm=…` (history state `{fromParent:true}`). Param is stripped after handling |
| `/voice` | `resume={llmNodeId}`, `parent={nodeId}` (`fresh=1` is sent by NodeDetail but unused) | resume = open in `processing` phase for that LLM node; parent = continue under a node |
| `/artifacts` | `create=1` | opens the create form; param stripped |
| `/account` | `x_login={outcome}`, hash `#email` `#x` `#model` `#craft` `#references` | outcome message then param stripped; hash scrolls to row |
| `/import` | hash `#x-bookmarks`, `x_connect` (ignored) | scroll to X card |
| `/confirm-email` | `token` | POSTed once |
| `/landing` | `alpha=1` (set by backend redirect for unapproved HTML requests; unused by the client) | — |

Web-only URL cosmetics: NodeDetail replaces `/node/{id}` with the node's `permalink` via `history.replaceState`; PermalinkRoute/PublicSharePage replace a former handle with `canonical`. `document.title` is set per node/reference.

---

## 2. Navigation

### 2.1 NavBar (`components/NavBar.js`)

Fixed top bar on every route: height 56px, `bg-surface`, 1px bottom `border`, z 1000, horizontal padding `clamp(14px, 4vw, 24px)`. Page content sits under a 60px top padding.

**NavBar does not use `useIsMobile`.** Items are the same on phone and desktop; only CSS changes:
- ≤412px: wordmark 1rem, letter-spacing .12em, logo gap 6px; link gap .45rem, left margin .6rem.
- ≤340px: logo image hidden.
- Link gap otherwise `clamp(.55rem, 2vw, 1.8rem)`.

Left: **brand** link to `/` = logo `/loore-logo-transparent.svg` (22px, opacity .7) + "LOORE" (serif 300, 1.15rem, uppercase, letter-spacing .3em, `text-secondary`).

Right group, in order:

| Item | Shown when | Target / action | Active colour when |
|---|---|---|---|
| `GlobalAudioPlayer` | audio loaded and path ≠ `/voice` | desktop (>640px): inline in the bar; mobile (≤640px): floating card at bottom | — |
| **About ▾** dropdown | logged out and user not loading | Why Loore `/why-loore`, Vision `/vision`, How To `/how-to` (centered dropdown) | path is one of the three |
| **Reflect** | logged out, or approved | `/` | path `/` or `/landing` |
| **Artifacts** | logged out, or approved | `/profile` (logged-out → login redirect) | `/profile`, `/todo`, `/artifacts*` |
| **Log** | logged out, or approved | `/log` | `/log` |
| **Commons** | approved and `share_v1_enabled` | `/commons` | `/commons` |
| **Login** | logged out | `/login?returnUrl=%2F` | `/login` |
| **⋮** overflow | logged in (approved or not) | menu below | — |

Link style: sans 300, .85rem, letter-spacing .02em, `text-muted`; active = `accent`; colour transition .3s.

**⋮ overflow menu** (dropdown: `bg-card`, 1px border, radius 8, padding 6px 0, min-width 220, shadow `0 8px 32px rgba(0,0,0,.4)`, z 1001; items 10px 16px, sans 300 .85rem `text-muted`). Closes on outside mousedown and on route change.

Approved users only:
1. **Import data** → `/import`
2. divider
3. **Admin** → `/admin` (only `is_admin`)
4. **Account** → `/account`
5. **My public page** → `/@{username}` (only `share_v1_enabled`)
6. **Light mode** toggle row: sun icon (light) / moon icon (dark) + label "Light mode" (same label in both states) + pill switch (32×18, radius 9, knob 14px `text-primary`; on = `accent`, off = `border`; .2s). Calls `toggleTheme()`.
7. (divider when craft on)
8. **Craft mode** toggle row with sliders icon (`CraftIcon`), title tooltip "Shows extra controls: privacy & AI usage per entry, auto-generate toggle, model picker, prompt editing, export." Turning ON opens `CraftModeDialog`; turning OFF flips immediately + toast "Craft mode off."
9. Craft-only block (each with sliders icon; one-shot `.craft-glow` 2.4s after turning on):
   - **Write new entry** → opens the global "Write New Entry" modal
   - **Export data** → `GET /export/threads` (blob) → downloads `loore-export-YYYY-MM-DD.txt`
   - **Prompts** → `/prompts`

Everyone logged in:
10. divider, "ABOUT" label (.7rem uppercase, letter-spacing .08em, opacity .6), Why Loore / Vision / How To
11. divider, **Logout** → `{BACKEND}/auth/logout`

Unapproved users therefore see only ⋮ with the About links and Logout. Logged-out visitors cannot change the theme (it follows the OS unless stored).

Craft mode persistence: `PUT /dashboard/user {craft_mode}` → `setUser(res.data.user)`; mirrored to `localStorage.loore_craft_mode` (fallback when the user object lacks it). After confirming: toast "Craft mode on. Its controls carry the sliders icon." (5s) and the menu re-opens.

### 2.2 Reachability (how a user gets to each screen)

| Screen | Entry points |
|---|---|
| Home `/` | brand logo, Reflect, `returnUrl=/` after login, catch-all |
| Voice | Home "Voice" card; NodeDetail "Voice Mode" (→ `/voice?parent=` or `?resume=`) |
| Text | Home "Text" card; VoicePage "Text Mode" button before any reply exists |
| Log | NavBar Log; NodeDetail after deleting a private thread |
| Thread `/node/:id` | Log cards, search results, Home Read card, Text/Voice submit, Commons/Share/public-page cards, in-markdown node links (render as the target's title via `GET /nodes/titles?ids=`), notifications |
| Profile | NavBar "Artifacts"; `/dashboard`; own-username links in node footers (`/dashboard` → `/profile`); default post-login target |
| Todo / Artifacts | `ArtifactsNav` bubble row (on Profile, Todo, Artifacts); "Actions taken" links in threads; Account "AI Preferences → View" |
| Prompts | ⋮ (craft) Prompts; NodeDetail prompt-edit confirm dialog link |
| Import | ⋮ Import data; Account "External references" helper link; WelcomePage import card (opens the picker in place) |
| References | **no nav item**: Import page "View references" button (only when something is imported), ReferenceDetail "All references", external search results |
| Share | Home "Share" card (`share_v1_enabled`); Commons "Share →"; "Share page" link in thread actions |
| Commons | NavBar Commons; Share "Commons →"; NodeDetail after deleting a public thread |
| Account | ⋮ Account; ConfirmEmail "Back to your account"; X connect round trip |
| Public page `/@u` | ⋮ My public page; author links on public threads; Share publish-confirm copy |
| Search | **no nav item**: ⌘K/Ctrl+K, magnifier on Log and References headers |
| Admin | ⋮ Admin |
| Welcome | Activate & Welcome email magic link (`next_url=/welcome`) only |
| Alpha thank-you | redirect for unapproved users; TermsModal accept (unapproved) |

### 2.3 Keyboard and pointer conventions (web-only; decide native equivalents)

- `⌘K`/`Ctrl+K` search; `⌘↩`/`Ctrl+Enter` submits every form (`useSubmitShortcut`); `Esc` closes modals/drawers and cancels edits (`useEscapeKey`).
- `⌘`/`Ctrl`-click on cards opens a new tab.
- Hover states everywhere (border lift, card translateY -1/-3px, glow). Card kebabs appear on hover with a 3s hide delay; on `(hover: none)` devices they are always visible. The kebab sits *outside* the card's right edge (card width `calc(100% - 30px)`).
- `beforeunload` guards unsaved artifact edits and active voice recordings.

---

## 3. Screens (medium depth)

Common page chrome for app pages: centered column, `max-width` 720px (Log, References, Commons) or 800px (Profile, Todo, Artifacts, Share, Prompts); page title serif 300 2rem `text-primary`; under it a short accent rule (40px × 1px `accent` at .5 opacity) or a full-width `accent-dim` hairline at .3 opacity. Loading states are plain muted text ("Loading...", "Loading log...", "Loading node..."); errors are a line of `accent` text replacing the page.

### 3.1 CORE

#### Home `/` — `pages/HomePage.js`
- Layout: vertically centered column, min-height `calc(100vh - 120px)`, padding 40px 24px, radial glow `rgba(196,149,106,0.06)` (hard-coded dark accent, used in both themes).
- Copy: greeting by local hour — "Good morning" (<12) / "Good afternoon" (<18) / "Good evening" (serif 300, `text-muted`); H1 **"What's on your mind?"** (serif 300, `clamp(1.8rem,4.5vw,2.8rem)`).
- Cards (`WorkflowCard`: `bg-card`, 1px border, radius 12, padding 2.2rem 1.8rem 2rem, icon 42px, title serif 1.5rem, description sans 300 .88rem muted; hover lift -3px + glow + accent top hairline):
  - **Voice** — "Speak what's present." → `/voice` (icon = Loore logo)
  - **Text** — "Type what's on your mind." → `/textmode`
  - **Share** — "Give something outward." → `/share` (only `share_v1_enabled`)
  - **Read** — "What's worth your time today." (admin only). Click → `POST /read/start {auto_generate}` (from `localStorage.loore_auto_generate`, default true) → `/node/{llm_node_id || prompt_node_id}`. Busy text "Starting…"; error toast (`error` or "Could not start the read.", 6s); 402 silent (banner handles it).
- Row layout: ≤3 cards one row; 4+ split into two equal rows (odd count: Voice+Text alone on row 1). Staggered entrance: 400ms + 120ms × index.
- No API calls except Read.

#### Text mode `/textmode` — `pages/WritePage.js`
- H1 "What's on your mind?" (serif 300), `NodeForm` 1170px / max 90vw, placeholder "Type what's on your mind…". Craft mode off hides power features and audio upload.
- Submit override:
  - AI not allowed for the chosen `ai_usage` → toast "Turning off auto-generate. AI usage on some nodes is turned off." (8s) → `POST /nodes/ {content, privacy_level, ai_usage}` or, for a recording, `POST /drafts/streaming/{sid}/save-as-node {content}`.
  - Else `autoGenerate = localStorage.loore_auto_generate ?? true`; recording → `POST /drafts/streaming/{sid}/save-as-node {content, agentic:true, auto_generate}`; typed → `POST /textmode/start {content, privacy_level, ai_usage, auto_generate}`.
- Success routing: `user_node_id` + `llm_node_id` → `/node/{user_node_id}?awaitLlm={llm}`; only llm → `/node/{llm}?awaitLlm={llm}`; only user node → `/node/{user_node_id}`; fallback `/node/{id}`.
- NodeForm itself (other mapper): textarea (`bg-input`, radius 8, padding 14px 16px, sans 300 1.05rem, height `clamp(120px,40vh,400px)`), mic (`StreamingMicButton`), "Send" button (accent outline; labels "Sending...", "Uploading... n%", "Transcribing... n%", "Waiting to transcribe..."), server drafts (`/drafts/`), privacy/AI selectors (craft), char cap → `SplitContentDialog`.

#### Voice `/voice` — `pages/VoicePage.js` (deep dive in the voice map)
- Driven by `useVoiceSession({apiEndpoint:'/voice', model: user.preferred_model, aiUsage: user.default_ai_usage})`. Phases: `ready` → `recording` → `processing` → `playback`. Global NavBar audio player is hidden on this route.
- Always: top-right **"Text Mode"** button (keyboard icon) → `/node/{lastLlmNodeId}` if a reply exists, else `/textmode`.
- Before recovery check resolves: empty page. If `GET /drafts/interrupted` returns a draft (and not recording): `RecoveryBanner` "Unfinished {label} recording" with **Continue recording** / **Discard** (`DELETE /drafts/streaming/{sid}/discard`).
- Ready/recording: italic serif "What's on your mind?", ECG SVG animation (280×168), waveform bars, timer `mm:ss` (+ " · paused"), interruption note "Recording paused — another app took the microphone. Everything up to the interruption is saved.", `OfflineBanner` ("You're offline"), red pulsing dot on error, round record button (accent circle; disabled offline). Spend-capped → toast "You've reached your monthly usage limit, so you can't start a new recording until it resets on {Month D}." (8s) + banner, no recording. Recording: stop (square) or resume (play, when paused).
- Processing: ECG + pulsing dot + "Thinking..." + ✕ cancel.
- Playback: ECG, waveform, controls (back 10s, play/pause, forward 10s), progress bar with chapter ticks, time, "●" pulse while TTS still generating, chapter list (Roman numerals + first words, tap to seek), `ProposalInline size="roomy"` for the reply's proposals (`GET /nodes/{id}/llm-status` for `tool_calls_meta`), `OfflineBanner`, round **Continue** record button.
- API (medium): `POST /voice`, `POST /nodes/{llm}/tts`, `GET /nodes/{llm}/tts-status`, SSE `/api/sse/nodes/{id}/tts-stream`, streaming transcription (`/drafts/streaming/init|audio-chunk|finalize|status|save-as-node|discard`, SSE `/api/sse/drafts/{sid}/transcription-stream`), `POST /voice/timing`, `GET /voice/timing/clock`.

#### Log `/log` — `components/Log.js`
- Header: "Log" (serif 300 2rem) + magnifier button (aria "Search your entries", opens archive search) + 40px accent rule.
- Data: `GET /log?per_page=20[&cursor=…]` → `{nodes, has_more, next_cursor}`. Cursor paging; dedupes ids. Auto-loads when within 300px of the bottom; "Load more..." fallback; page-2+ failure shows "Couldn't load more entries. Retry" without clearing cards.
- Card = `Bubble`: title = `thread_name` or first line of `preview` (leading `# ` stripped, 120-char cut), body = rest (2-line clamp, 250-char cut), expand chevron when long; footer `NodeFooter` (username or model, "via {origin}", `yyyy/mm/dd HH:MM`, reply icon + count); tags (uppercase .65rem pills, `accent-dim` on `accent-subtle`): prompt label ("Read", "Textmode"…), "Pinned", "Voice Note".
- Tap → `/node/{newest_node_id || id}`.
- Kebab actions: **Rename thread** (hidden when `can_rename === false`) → `RenameThreadDialog` → `PUT /nodes/{thread_root_id||id}/thread-name {thread_name}` (empty clears); **Delete thread** → `DeleteConfirmDialog mode="thread"` → `DELETE /nodes/{root}?delete_descendants=true` → toast "Deleted n node(s)", removes every card of that thread plus `deleted_pinned_ids`.
- Empty: "Your private entries will appear here as you share thoughts with Loore." Error: "Error loading log."
- Log card fields: `id, preview, node_type, child_count, created_at, pinned_at, username, human_owner_username, llm_model, origin, has_original_audio, prompt_key, thread_name, thread_root_id, newest_node_id, can_rename`.

#### Thread `/node/:id` (member) — `components/NodeDetail.js` via `NodeDetailWrapper` (keyed by id → full remount per node)
- Fetch: `GET /nodes/{id}`; 404/403 → "This node doesn't exist, was deleted, or isn't shared with you."; other → "Error fetching node details."; `GET /nodes/{id}/resolve-quotes` when content has `{quote:ID}`/`{quote_ext:ID}`. Scrolls to the focal node once.
- Layout top→bottom:
  1. Row: H2 **"Thread"** + top-right controls column (owner, `ai_usage ≠ none`, not a public thread): **Voice Mode** (mic icon; `POST /voice/from-node/{id} {model}` → `/voice?resume=…&parent=…` or `/voice?parent=…`), **Relevant tweets / Read / Read further** (admin; `POST /read/from-node/{id}`), **Auto-generate** pill (craft only; `localStorage.loore_auto_generate`).
  2. `SemanticNeighbors` (admin-only, fixed right panel on ≥1200px).
  3. Ancestors as `Bubble`s (left-aligned).
  4. Focal node card: `bg-card`, 1px border + **3px `accent` left border**, radius 10, padding 1.8rem 2rem; kebab (owner): Edit / Delete; system-prompt nodes show "{prompt_title} v{n} · Profile v{n} · TODO v{n}"; content via `MarkdownBody`/`QuotedContent`; `ProposalInline` for proposals; read-reply extras (`ReadWindowLine`, `FeedPicks`, `ReadReplyTail`); "▸ Actions taken (n)" expander listing tool calls with links (`/todo`, `/artifacts/{kind}`, `/share`, `/node/{ref}`).
  5. Footer: `NodeFooter` + pin (`POST|DELETE /nodes/{id}/pin`; tooltips "Pin to the top of your public page" etc.), `SpeakerIcon` (TTS), `DownloadAudioIcon`.
  6. Craft bar (owner & (craft or public thread) & auto-generate off): **LLM Response** + joined `ModelSelector` (`POST /nodes/{id}/llm`; labels "Requesting…", "Waiting for AI…", "Generating…"); "Read further" + read `ModelSelector` in read threads.
  7. Inline reply `NodeForm` (compact, any logged-in viewer); placeholder "Type what's on your mind…" or, under a read reply, "Ask about these picks, or say what you make of them…".
  8. Children as a recursive tree (branches indented 20px with a 2px left border when siblings > 1).
- Streaming: SSE `/api/sse/nodes/{id}/llm-stream` while a reply is pending; polling `GET /nodes/{llmId}/llm-status` every 2s (15s for batch waits).
- Dialogs: prompt-edit confirm ("Edit prompt for this thread only?" / "Cancel" / "Edit for this thread"), `NodeFormModal "Edit Text"`, `NodeFormModal "Reply"`, `DeleteConfirmDialog` single + "prompt" follow-up (`DELETE /nodes/{id}?delete_descendants&delete_orphaned_prompt`); after deleting the whole session → `/commons` (public) or `/log`.
- Toasts on LLM failures (never replaces the thread view).

### 3.2 SECONDARY

#### Profile `/profile` — `pages/ProfilePage.js`
- `ArtifactsNav` bubble row, then H1 "Profile" + `SpeakerIcon` + version button "● v{n} · {date}" (click = edit; in edit = save) + "history".
- Data: `GET /dashboard` → `latest_profile {id, content, generated_by, tokens_used, created_at, source_tokens_used, source_origin_stats, source_data_cutoff, generation_type, has_tts}`; `GET /profile/versions` for the count.
- Meta line: "Built from ~{n} tokens of writing ({pct}% public tweets, …) · {generated_by} · Data through {date}".
- Generation indicator from `ProfileGenerationWatcher` events: "{message} · {pct}%", batch "Chunk n of ~N", "Starting generation...", "Generation failed" / "Generation stopped before finishing" (5s).
- Empty state (also the first-visit explainer of the artifacts workspace): three paragraphs ("Your profile is a living document the AI writes about you…", "It's the first of your artifacts — the row above…", "There's nothing to set up…") + **Write Profile**.
- Edit: textarea + Save ("Saving...") / Cancel. Save: `PUT /profile/{id} {content, regenerate_tts?}` or `POST /profile {content}`. Editing text that has TTS → `RegenerateTtsDialog`.
- History: `VersionHistoryDrawer` (`GET /profile/versions`, `GET /profile/versions/{id}` + previous for diff, `POST /profile/revert/{id}`).
- Content: `MarkdownBody` in sans 300 .9rem `text-secondary`, line-height 1.7.

#### Todo `/todo` — `pages/TodoPage.js`
- `ArtifactsNav`, H1 "Todo", "● v{n} · {date}", "history", quick-add "+"/"×" (title "Quick-add task to Today").
- Data: `GET /todo` → `todo {content, version_number, created_at, generated_by}`. Parses `## Section` headings and `- [ ]`/`- [x]`/`- plain` items (2-space nesting) into sections with counts; items collapse children.
- Actions: toggle checkbox / insert after (via `utils/markdown` helpers) → `PATCH /todo {content}`; quick-add appends to "## Today" (optimistic, revert on failure) → `PATCH /todo`; full edit → `PUT /todo {content, generated_by:'user'}`; history `GET /todo/versions`, `GET /todo/versions/{id}`, `POST /todo/revert/{id}`.
- Meta "Last updated by {edited manually|Orient session|Voice|reverted|imported|…} · {date}".
- Empty: explainer + "No todo list yet. Create one to track your tasks." + **Create Todo** (template `## Today / ## Upcoming / ## Completed recently`). Section with no items: "No items". Quick-add placeholder "Add a task to Today and press Enter".

#### Artifacts `/artifacts`, `/artifacts/:kind` — `pages/ArtifactsPage.js`
- `ArtifactsNav` (Profile · Intentions · Todo · predictions · memory · scratchpad · ai_preferences · custom A–Z · "+"), pills radius 16, padding 6px 14px, .8rem; active = `bg-card` + `accent` border. Module-level cache; refetches on `loore_artifacts_changed`.
- Data: `GET /artifacts` → `artifacts[] {kind, title, description, content, generated_by, created_at, privacy_level, ai_usage}`; version count `GET /artifacts/{kind}/versions`; `POST /artifacts/{kind}/viewed` once per kind per mount.
- Header: title (or "New artifact"), "● v{n}" (click = edit), date, "history". Subtitle = description or built-in blurb or "A persistent document shared between you and the AI." + " · last updated by {…}".
- Edit/create: name input (create only; forced to `[a-z0-9_-]`, placeholder "artifact-name (lowercase, dashes)"), description input (**required** to save; "One-line description — what this artifact is for"), textarea; Save → `PUT /artifacts/{kind} {content, description, generated_by:'user'}` → navigate `/artifacts/{kind}`.
- Render: `intentions` via `IntentionsView`, others via `MarkdownBody`. Empty: kind intro + "Nothing here yet. The AI fills this in during Voice and Text sessions, or write your own." + **Write {title}**.
- Unsaved-changes guard on bubble navigation: modal "Unsaved changes" / "You have unsaved edits to this artifact. Leave without saving?" / Cancel / Leave.
- History: `GET /artifacts/versions/{id}`, `POST /artifacts/{kind}/revert/{id}`.

#### Prompts `/prompts`, `/prompts/:promptKey` (craft) — `pages/PromptsPage.js`, `PromptDetailPage.js`
- List: `GET /prompts/` → cards (title, dot if `default_updated`, "v{n} · {default|edited manually|reverted} · {date or 'default'}", preview). Subtitle "System prompts that power Loore's AI features".
- Detail: "← All prompts", title, "● v{n} · {date}", history, banner "The default prompt has been updated." with **View new default** (`GET /prompts/{key}/default`), **Accept new default** (`POST /prompts/{key}/revert-to-default`), **Dismiss** (`POST /prompts/{key}/acknowledge-default`). Edit `PUT /prompts/{key} {content}` (error toast 10s). Versions `GET /prompts/{key}/versions[/{id}]`, revert `POST /prompts/{key}/revert/{id}`. "Prompt not found."

#### Import `/import` — `pages/ImportPage.js` = `ImportData inline` + `ExternalImport`
- "Import Data" (serif 1.4rem). Four file pickers (`.zip`): **Import Claude**, **Import ChatGPT** (both unzip client-side with JSZip and upload only `conversations.json`), **Import Markdown (e.g. Obsidian)**, **Import Tweets** (upload the whole zip). Flow per source: `POST /import/{…/}analyze` (multipart) → confirm modal (counts, tokens, import type, privacy/AI selectors) → `POST /import/{…/}confirm` → poll `GET /import/status/{taskId}` → "Import Finished" modal; "Previously Deleted Content" prompt. Stage labels "Extracting…", "Analyzing…", "Importing…".
- "Import References": counts line; **View references** → `/references`; cards:
  - Community Archive: "@username" + **Fetch tweets** → `POST /external/community-archive/fetch {username}`, then polls counts every 5s ×12.
  - X Bookmarks: states revoked / connected / configured / unconfigured; **Connect X** / **Reconnect X** (full-page `{BACKEND}/api/external/twitter/connect`), **Sync bookmarks** (`POST /external/twitter/sync`, polls `GET /external/twitter/status` + `GET /external/items?per_page=1` every 3s ×20). JSON import (`POST /external/bookmarks/import`) when unconfigured or craft.
  - Chrome clipper (only `external_content_enabled`): tokens list (`GET /external/tokens`), **Create token** (`POST /external/tokens {name:'Chrome clipper'}` → `NewTokenDialog`), Revoke (`DELETE /external/tokens/{id}`).

#### References `/references`, `/references/:id` — `pages/ReferencesPage.js`, `ReferenceDetailPage.js`
- List: `GET /external/items?sort=saved&page=&per_page=20` → `{items, has_more}`; cards reuse `Bubble` with `ReferenceFooter` and tag "{Read · }{source}"; kebab Open source / Delete (`DELETE /external/items/{id}`, toast "Reference deleted"). Magnifier → external search. Empty "Pages and tweets you save from elsewhere will appear here."
- Detail: H2 "Reference" + "All references"; card with kebab (Open source, Edit → `NodeFormModal "Edit Reference"` + `ReferenceEditForm` → `PUT /external/items/{id} {title, content, regenerate_tts?}`, Delete); title (serif) + `SpeakerIcon`; `TweetEmbed` (X widget) or `YouTubeEmbed` (youtube-nocookie iframe) with "Show stored text"/"Hide stored text", else `MarkdownBody`; stats line "Shown by Loore n× · last {date} · you read it {date} · edited {date}" or "Not yet shown by Loore in a conversation"; ⊕/⊖ `ReferenceFeedback` (`POST /external/items/{id}/feedback`); **Mark as read / Mark as unread** (`POST|DELETE /external/items/{id}/read`). Errors "This reference does not exist or was deleted." / "Error loading reference."

#### Share `/share` — `pages/SharePage.js` (only `share_v1_enabled`)
- H1 "Share" + "+" (new) + "Commons →". Intro "Pieces of your writing worth giving outward — nothing is visible to anyone until you publish it, and you can take anything back."
- `GET /share` → `shares[] {id, content, share_type, status, created_at, published_at, revoked_at, public_node_id, permalink}`; groups **Published / Drafts / Revoked**; type badge (need, offering, insight, exploration, intention, other).
- Actions: Edit (`PATCH /share/{id} {content, share_type}`), new (`POST /share`), Publish → inline confirm "Where should this go? Publishing to Loore puts it in the Commons and on your public page." + **Publish to Loore** (`POST /share/{id}/publish`) + "Twitter / X · coming soon", "Substack · coming soon"; Revoke (`POST /share/{id}/revoke`); Delete with inline confirm (`DELETE /share/{id}`). Published cards open the thread.
- Empty: "Shares the AI proposes in conversation land here as private drafts — or start one yourself." + "+ new share".

#### Commons `/commons` — `pages/CommonsPage.js` (only `share_v1_enabled`)
- "Commons" + "Share →"; subtitle "What people here have chosen to make public."
- `GET /commons/feed?page=n` → `{items[] {id, username, created_at, content, reply_count, permalink}, has_more}`; cards (username, date, markdown, "n responses" muted text, never a badge); tap → permalink or `/node/{id}`. Empty "Nothing public yet." Error "Error loading the commons."

#### Account `/account` — `pages/AccountPage.js`
- "Account". Rows (label uppercase small, helper text muted):
  - **Username**: input + Save (`PUT /dashboard/user {username}`); client validation (non-empty, ≤64, `[A-Za-z0-9_]`); "Username updated."
  - **Email** (`#email`): input + **Send confirmation link** (`POST /dashboard/email {email}`); pending state with Resend / Cancel (`DELETE /dashboard/email/pending`); **Remove email** only if X is also connected (`DELETE /dashboard/email`).
  - **X** (`#x`): "Connected as @{handle}" or **Connect X** (`{BACKEND}/auth/x/connect`); **Disconnect X** only if email exists (`DELETE /dashboard/x`); outcome messages from `?x_login`.
  - **Plan**: read-only (`free` default).
  - Settings: **Default model** (`ModelSelector`, `PUT /dashboard/user {preferred_model}`), **Default privacy** (Private / Circles (coming soon, disabled) / Public), **External references** (only if `external_content_available`; Off / On (experimental)), **Public sharing** (only if `share_v1_available`), **Default AI usage** (None / Chat / Train), **Craft mode** (`#craft`, Off/On), **AI Preferences → View** (`/artifacts/ai_preferences`). Each select saves immediately via `PUT /dashboard/user {field}` and replaces `user` from the response.

#### Welcome `/welcome` — `pages/WelcomePage.js`
- Fade-in sequence: logo, "Welcome to *Loore*.", alpha paragraph; card "YOUR FIRST ENTRY" / "What brought you to Loore — and what are you hoping to find here?" / **Start writing** (opens global new-entry modal) / "You can type or record a voice note — whatever feels natural."; `PrefillConsentCard` ("Already on X?"); import card "ALREADY HAVE A JOURNAL?" + `ImportData` picker button **Import data →**; "See practical tips & workflows →" (`/how-to`); closing "There's no wrong way to do this." …

#### Confirm email `/confirm-email` — `pages/ConfirmEmailPage.js`
- Once per visit (ref guard; StrictMode-safe): `POST /dashboard/email/confirm {token}` → merge `emailState`.
- States: loading "Confirming your email / One moment."; no token "This link is incomplete"; signed out "Sign in to confirm" + "Sign in →" (`/login?returnUrl=…`); ok "Email confirmed" + "{email} is now your sign-in address." + "Back to your account →" (`/account#email`) or "Continue →" (`/alpha-thank-you` if unapproved); failure "Not confirmed" + retry ("Try again") for `reason=failed`, or "Sign out and use the other account →" (`{BACKEND}/auth/logout?next=…`) for `reason=other_account`.

### 3.3 PUBLIC / MARKETING

#### Landing `/landing` — `components/LandingPage.js`
- Full-bleed, grain SVG overlay, radial gradients (`--gradient-overlay-1/2`). Hero: "LOORE" mark, H1 "Uncover your *lore*. / Author yourself." (serif 300 `clamp(2.4rem,6vw,4.5rem)`), subtitle "A tool for seeing the story you're actually living — and shaping it with intention.", **Join the Alpha →** (`/login?returnUrl=%2F`), pulsing scroll line.
- Three narrative sections that fade in only after the user scrolls: "You are already living a *story*.", "Surface what's *hidden*.", "Your lore becomes an *offering*."; mock app window "A glimpse inside"; closing "AI is rapidly gaining agency. **Loore helps you gain yours.**" + **Begin your lore →** + "Why Loore →"; footer "© {year} Loore".
- No API calls.

#### Login `/login` — `components/LoginPage.js`
- Card (max 400, radius 12, `bg-card`, shadow `0 16px 60px rgba(0,0,0,.3)`), wordmark above, grain + gradient backdrop, fade-in 1s.
- "Welcome back" / "Sign in to continue your lore". Error line in `accent`. **Sign in with X** (X logo). Note: "Sign in with X makes a new account unless your X is already connected to one. To add X to an email account, sign in with email, then use Connect X under Account." Divider "or". **Sign in with Email** → form "EMAIL ADDRESS" + input (`you@example.com`) + **Send Sign-in Link** ("Sending...") → "Check your inbox / We sent a sign-in link to {email}. It expires in 15 minutes." Errors: server `error` or "Network error. Please try again."

#### Vision / Why Loore / How To — `pages/VisionPage.js`, `WhyLoorePage.js`, `HowToPage.js`
- Static long-form content with `Fade` reveals and `CtaButton`s ("Join the Alpha →" to `/login?returnUrl=%2F`, "Read the full vision →"). Vision sections: Effortless *journaling*, AI that helps you *see yourself*, Your lore becomes an *offering*, Find your *people* (Intention Market). How To: "The Basics" tips (◇◈✦⬡◆▣ glyphs) + workflow cards (Daily prioritization, Walk-and-reflect, Chat with your archive, Import and discover) with step chips joined by →. No API calls.

#### Alpha thank-you `/alpha-thank-you` — `pages/AlphaThankYouPage.js`
- Logged out → `/landing`. "You're part of this now." + thank-you paragraph.
- Email card: form "GET NOTIFIED" (no email & nothing pending, or editing) → `POST /dashboard/email {email}` (one request at a time); pending card ("We sent a confirmation link to {pending}. Open it to finish — until then we have no way to reach you." / expired variant) with **Send it again** / **Send a new link** and **Use a different address** (+ "Keep {pending}"), hint "Nothing after a few minutes? Check the spelling. An address that already signs in to Loore can't be added here."
- `PrefillConsentCard` ("WHILE YOU WAIT"): only X-login users with no answer; "Start from what you've already written?" + **Yes, seed from my tweets** / **Not now** → `PUT /dashboard/user {prefill_consent:'yes'|'no'}`.
- "WHAT HAPPENS NEXT" 01/02/03 list; italic closing line; "Read the vision →".

#### Public page `/@username` — `pages/PublicSharePage.js`
- `GET /share/public/{username}` → `{username, shares[] {id, public_node_id, permalink, share_type, content, published_at, pinned}, canonical}`. Shell max 640px. H1 username; cards (type eyebrow, markdown, date, pin icon); tap → thread. Empty "{username}'s public entries will appear here as they share thoughts with Loore." Not found "Nothing here."

#### Public thread `/node/:id` (visitor), `/@u/:slug` (visitor) — `pages/PublicThreadPage.js`
- `GET /commons/node/{id}` → `{thread (recursive: id, username, node_type, llm_model, content, created_at, children), focus_id, truncated}`. Nested cards (focused one has an `accent` border); author line "{model} · via {user}" for LLM nodes; author links to `/@{user}`. "thread truncated". Funnel: "Respond, or ask the AI about this thread — from your own Loore." + **Sign in to respond** (`/login?returnUrl=/node/{id}`). Not found: visitor → login redirect; member → "Nothing here."

### 3.4 ADMIN

#### Admin `/admin` — `components/AdminPanel.js`
- Tab bubbles: **Users** (default), **Activity**, **Feedback**, **Polls**.
- Users: whitelist form (`POST /admin/whitelist`, `XLookupConfirmDialog` for paid X lookup), wide table (ID, user, Approved, Plan, Email, spend totals with sort + hide-zero toggles, Limit ($), pre-fill status, Actions): toggle approve (`POST /admin/users/{id}/toggle`), activate & welcome, spam, email/plan/spend-limit edits, infer/cancel intentions, build profile, CA/X prefill with status polling (`GET /admin/prefill/status/{task}`), `AdminRefusalDialog` for policy refusals.
- Activity `GET /admin/activity`; Feedback `GET /admin/feedback`, `PUT /admin/feedback/{id}` (new/reviewed/done); Polls `GET|POST /admin/polls`, responses, close; `GET /nodes/models`.
- Desktop-table UI; a candidate for "open on the web" in the native app (decision for the design doc).

---

## 4. Global UI

### 4.1 Modals and dialogs

Shared dialog shell (Delete, Rename, CraftMode, NewToken, RegenerateTts, ApplyToReplies, PublicReply, SplitContent, AdminRefusal, XLookupConfirm, prompt-edit confirm, NodeFormModal): overlay `rgba(0,0,0,0.7)` + `backdrop-filter: blur(8px)`, centered card `bg-card`, 1px `border`, radius 12, padding 2rem, width 440–480 (NodeFormModal 1170), max 90vw / 90vh, scrolls. Title serif 1.4rem 400. Body sans 300 .92rem `text-secondary` line-height 1.6. Choice buttons stacked full-width, left-aligned, `bg-deep` fill, 1px border, radius 6, padding 10px 16px, bold first line + muted subline (.82rem). Backdrop click and Esc close (except TermsModal and NewTokenDialog).

| Component | Trigger | Content / API |
|---|---|---|
| `TermsModal` | `user && !terms_up_to_date` (App) | Terms v2.0 text **hard-coded in the component** (TL;DR 5 items, sections 1–10, OpenAI Content Sharing Agreement, table of processors). **I Agree** → `POST /terms/accept`; "Processing..."; error "Error accepting terms. Please try again." No close, no Esc, z 10000. |
| `UpdatesModal` | unread `/updates` (App, once per session) | Card 560px (mobile: full width, padding 1.4rem 1.15rem, max-height `innerHeight − 72`). Header "While you were away" / "What's new in Loore since your last visit." Order: notifications, polls, changelog. Changelog: date, serif title, markdown body; **Later** (`POST /updates/changelog/{id}/skip`) / **Got it** (`…/read`); internal links record a skip, close, navigate. Notifications: eyebrow by `type` (`fix_ready` "Your issue has been fixed", `issue_declined` "Your issue — closed without a fix", `x_disconnected` "Action needed"), stamp "v{n} · {date}", **Later** / **Take a look** (skip + open link: external → new tab, internal → close + navigate) / **Got it**. Polls: "A QUESTION FROM THE DEVELOPER", consent note naming model + data source, **No thanks** (`/decline`), **Later**, **Write my own**, **Draft with AI** (`POST /updates/polls/{id}/draft`, then polls `GET /updates/polls/{id}` every 5s while `drafting`), **Send to developer** (`PUT …/response {content}` then `POST …/send`). Modal closes itself when no items remain. |
| `SearchModal` | ⌘K, magnifiers | Overlay `rgba(5,4,3,.75)` blur 10px, card 640px max 90vw/70vh, top offset 12vh, shadow `0 24px 80px rgba(0,0,0,.5)`. Input (auto-focus, 16px) "Search your entries..." / "Search your references..."; admin-only **Semantic/Keyword** toggle; **Dates** toggle → From/To date inputs + Clear. Debounce 300ms. `GET /search/semantic` (default) or `GET /search` with `q, from, to, per_page=20, page, scope=external`. Result row: type chip (node_type or source label), author (external), date, "n replies", semantic score "%"; external title; 2-line snippet **HTML with `<mark>`** (`dangerouslySetInnerHTML`). Tap → `/node/{id}` or `/references/{id}`. States "Searching...", "No results found", "Type to search your entries/references". Keyword mode pages with "Show more (x of y)" + auto-load on visibility. |
| `NodeFormModal` | New entry, Edit, Reply, Edit Reference | Shell with × button top-right; hosts `NodeForm` or children. |
| `CraftModeDialog` | ⋮ Craft mode ON | "Turn on craft mode?" + list (privacy & AI usage per entry; auto-generate switch, model picker, voice upload; prompt editing & export in ⋮) + **Turn on** / **Not now** ("Keep Loore's current simple UI."). |
| `NewTokenDialog` | clipper token created | "Your clipper token", one-time plaintext box (tap to copy, check icon turns `success` 1.5s), **Done, I've saved it**. No backdrop/Esc dismissal. |
| `DeleteConfirmDialog` | Log/NodeDetail/References | modes `single` (with/without children), `thread`, `reference`, `prompt` (copy in the other maps). |
| `RenameThreadDialog` | Log kebab | "Rename thread", one field, placeholder = fallback title; empty clears. |
| `RegenerateTtsDialog` | editing text that has generated TTS | "Regenerate audio?" keep vs regenerate. |
| `ApplyToRepliesDialog` | privacy/AI change on a node with replies | "Apply to replies too?" |
| `PublicReplyDialog` | replying under a public node | "This reply will be public"; don't-show-again in `localStorage.loore_public_reply_ack`. |
| `SplitContentDialog` | entry over `NODE_CHAR_CAP` | "Long entry" split consent. |
| `VersionHistoryDrawer` | "history" on Profile/Todo/Artifacts/Prompts | Slides in from the right, 420px, .25s; version list with generated-by labels; diff vs previous ("Changes (n)" / "Full text"); **Revert to this version**. |
| Unsaved-changes modal | ArtifactsPage bubble nav while dirty | "Unsaved changes" / Cancel / Leave. |

### 4.2 Banners, toasts, watchers

- **ToastContext** — `addToast(message, durationMs = 3000) → id`, `removeToast(id)`. Stack fixed bottom-center, `bottom: calc(24px + --floating-player-offset + --spendcap-banner-offset)`, z 9999, gap 8. Toast: `bg-card`, 1px border, radius 8, padding 10px 20px, sans 300 .85rem `text-secondary`, shadow `0 4px 20px rgba(0,0,0,.3)`, tap to dismiss, enters with `toast-in` .25s (fade + 8px rise). Plain strings only; no types/icons.
- **SpendCapBanner** — listens to `loore:spend-capped`. Bottom-center card max 680, `accent` 1px border, radius 8: "LIMIT REACHED" label + message (server `message` or "You've reached your monthly usage limit for the free alpha. It resets at the start of next month.") + ×. Stays until dismissed; publishes its height to `--spendcap-banner-offset`. `utils/spendCap` keeps a session flag (`markSpendBlocked` from `user.spend_blocked`, any 402) used to refuse recordings/uploads before they start; reset date = first of next **UTC** month.
- **OfflineBanner** — muted "You're offline" line from `navigator.onLine` events; used only on VoicePage. Record/Send buttons disable offline.
- **RecoveryBanner** — VoicePage only (see 3.1).
- **GlobalAudioPlayer** — rendered by NavBar (not on `/voice`). Desktop: inline row in the bar: title (≤200px), chapter `<select>` (TTS with >1 chapter), play/pause, stop (resets to 0, keeps player), −10s, +10s, speed button "{rate}x", time "m:ss / m:ss", "●" while TTS generating, 150px seek bar, ✕ close. Mobile ≤640px: floating card fixed bottom (`max(16px, safe-area-inset-bottom)`), width `100vw − 24px` max 480, z 9998, one row without seek bar; publishes `--floating-player-offset`. State lives in `AudioContext` (queue of chunks, cumulative time, chapters, Safari unlock).
- **ProfileGenerationWatcher** — renders nothing. Starts polling `GET /export/profile-progress[?task_id=]` when the user payload has `profile_generation_task_id` or `profile_batch_pending` (or on window event `loore_profile_started`, which nothing dispatches today). Interval 5s (sync) / 60s (batch), no duration cap, stops after 10 consecutive errors. Broadcasts `loore_profile_progress {running, status, progress, message, source, latestProfileId}` and `loore_profile_done`; toasts "Your profile has been updated ✓" (6s), "Profile generation failed", "Profile generation stopped before finishing — it will be retried in the background". Clears the flags on the cached user.
- **ProtectedRoute** — see §1 guards.

### 4.3 Contexts

- **UserContext** `{user, loading, error, setUser}`. One `GET /dashboard?profile=0` on mount (the user without the profile); no refetch on focus, interval or after actions (pages merge server responses with `setUser`). After load: `markSpendBlocked()` if `spend_blocked`; `PATCH /dashboard/timezone {timezone}` when the browser tz differs, then patches `user.timezone`. No login/logout methods (both are full-page navigations).
  User fields (from `GET /dashboard` → `user`, same shape from `PUT /dashboard/user` → `user`): `id, username, description, accepted_terms_at, terms_up_to_date, approved, email, is_admin, plan, voice_mode_enabled, craft_mode, preferred_model, profile_generation_task_id, profile_batch_pending, default_privacy_level, default_ai_usage, twitter_login, twitter_handle, pending_email, pending_email_expired, prefill_consent, prefilled_handle, timezone, spend_blocked, share_v1_enabled, share_v1_available, public_sharing_enabled, external_content_available, external_content_enabled`. Without `profile=0` the response also carries `latest_profile` (ProfilePage requests it that way); since #481 it carries no thread cards. Email endpoints return `{email, pending_email, pending_email_expired}` merged via `utils/emailState`.
  Feature gates keyed on user fields: `approved` (everything), `terms_up_to_date` (TermsModal), `is_admin` (Admin, Read card, Relevant tweets, SemanticNeighbors, keyword search, read rerun), `share_v1_enabled` (Commons, Share card, My public page, public-reply/sharing UI), `craft_mode` (power features), `voice_mode_enabled` (TTS speaker/download on private content), `external_content_enabled` (clipper card), `twitter_login` (PrefillConsentCard, Remove email).
- **ThemeContext** `{theme, setTheme, toggleTheme}`, `'dark'` default. Initial value from `<html data-theme>` set by a pre-paint script in `public/index.html`: `localStorage.loore_theme` if `light|dark`, else `prefers-color-scheme: light` → light. `setTheme` writes `localStorage` and the attribute. Tokens switch via `[data-theme="light"]`. No "system" option once the user toggles.
- **ToastContext** — §4.2.
- **AudioContext** — global playback (voice map covers it).

### 4.4 Cross-component signals (web mechanisms that need a native equivalent)

| Signal | Producer → consumer |
|---|---|
| `window` event `loore:spend-capped` | api.js 402 interceptor, `notifySpendBlocked`, NodeForm → SpendCapBanner, spendCap flag |
| `loore_profile_progress`, `loore_profile_done` | ProfileGenerationWatcher → ProfilePage |
| `loore_profile_started` | (no producer) → Watcher, ProfilePage |
| `loore_artifacts_changed` | ArtifactsPage save/revert → ArtifactsNav |
| CSS vars `--floating-player-offset`, `--spendcap-banner-offset` | GlobalAudioPlayer, SpendCapBanner → toast position |
| Module caches | ArtifactsNav list, node titles (`utils/nodeLinks`) |

### 4.5 Client-side persisted preferences (per device, not synced)

`loore_theme` (light/dark), `loore_craft_mode` (fallback only; server is authoritative), `loore_auto_generate` (default true; Home Read, Text mode, NodeDetail toggle, NodeForm), `loore_agentic_reply`, `loore_last_privacy_level`, `loore_last_ai_usage`, `loore_public_reply_ack`. Drafts are server-side (`/drafts/`).

---

## 5. Design tokens

Source of truth: `frontend/src/index.css` `:root` (dark, default) and `[data-theme="light"]`. `App.css` holds only CRA leftovers (`.App*`), unused by the design.

### 5.1 Colours

| Token | Dark (`:root`) | Light (`[data-theme="light"]`) | Use |
|---|---|---|---|
| `--bg-deep` | `#0e0d0b` | `#f5efe4` | page background, button/input fill in dialogs |
| `--bg-surface` | `#181714` | `#ebe4d6` | NavBar, mock window bar |
| `--bg-card` | `#211f1b` | `#e0d8c7` | cards, modals, toasts, dropdowns |
| `--bg-card-hover` | `#282520` | `#d6cdb9` | row hover |
| `--bg-input` | `#151311` | `#fbf7ee` | textareas, inputs |
| `--text-primary` | `#ede8dd` | `#1f1c17` | headings, body emphasis |
| `--text-secondary` | `#a89f91` | `#5d564a` | body text, button text |
| `--text-muted` | `#736b5f` | `#857c6c` | meta, nav links, placeholders |
| `--accent` | `#c4956a` | `#a87547` | amber: active nav, primary outline buttons, links, error lines |
| `--accent-hover` | `#d4a574` | `#8a5d36` | link hover |
| `--accent-dim` | `#a07a55` | `#bb8c66` | tag text, hairlines |
| `--accent-glow` | `#c4956a40` | `#a8754740` | glows, `mark` background, pressed button |
| `--accent-subtle` | `#c4956a15` | `#a8754718` | tag background, button hover fill |
| `--border` | `#302c27` | `#cdc4b1` | all 1px borders, dividers, toggle off |
| `--border-hover` | `#433e36` | `#b3a98f` | hovered borders |
| `--success` | `#4ade80` | `#1f8a4d` | copied check |
| `--error` | `#e74c3c` | `#c0392b` | error text/dots, destructive "Delete" in Share |
| `--warning` | `#ffc107` | `#b88600` | — |
| `--info` | `#61dafb` | `#0b6e9a` | — |
| `--gradient-overlay-1` | `#1a150f` | `#e8dfca` | Login/Landing radial backdrop |
| `--gradient-overlay-2` | `#1a130d08` | `#d4c8aa20` | Login/Landing radial backdrop |

Hex values with 8 digits are `#RRGGBBAA`. Pre-paint boot colours in `index.html`: dark bg `#0e0d0b` / fg `#ede8dd`; light bg `#f5efe4` / fg `#1f1c17`. `theme-color` meta: `#0e0d0b` (dark), `#f5efe4` (light).

Literal colours outside the tokens: overlays `rgba(0,0,0,0.7)` (dialogs), `rgba(5,4,3,0.75)` (search); shadows `rgba(0,0,0,.2–.5)`; radial page glow `rgba(196,149,106,0.06)` / `0.05` (Home, Voice, Write — same value in light mode); YouTube frame `#000`. Destructive actions mostly use `accent`, not `error`. `StreamingAudioPlayer.js` has Bootstrap greys but is dead code.

### 5.2 Typography

- Fonts from Google Fonts: **Cormorant Garamond** (300, 400, 500, 600; italic 300, 400; Landing also loads italic 500) and **Outfit** (200, 300, 400, 500, 600). Fallbacks: `--serif: 'Cormorant Garamond', Georgia, 'Times New Roman', serif`; `--sans: 'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`. Code: `source-code-pro, Menlo, Monaco, Consolas, 'Courier New', monospace`. Both families are OFL and can be bundled.
- Serif (Cormorant) for display: page titles 2rem/300; hero `clamp(1.8rem,4.5vw,2.8rem)`/300 (Home), `clamp(2.4rem,6vw,4.5rem)`/300 (Landing); dialog titles 1.4rem/400; UpdatesModal header 1.65rem/400, item titles 1.4rem/400; wordmark 1.15rem/300 uppercase tracking .3em (nav) / 1.1rem tracking .35em (login/landing); italic serif for emphasis (`<em>` in `accent`) and voice prompts. Markdown headings serif: h1 2.2em/700, h2 1.8em/700, h3 1.5em/600, h4 1.25em/600, h5 1.1em/600, h6 .95em/600.
- Sans (Outfit) for UI/body, mostly weight **300**: body .9–1.05rem, line-height 1.6–1.8; nav/menu .85rem; meta/footer .75rem; eyebrow labels .65–.72rem uppercase, letter-spacing .08–.18em (often `accent` at .6–.7 opacity); buttons .85–.95rem 300–400. Most frequent sizes: .85rem, .9rem, .75rem, .8rem, .92rem.
- Base rem = 16px. Inputs use 16px to avoid iOS zoom (irrelevant natively).

### 5.3 Shape, spacing, elevation

- Radii: buttons/inputs 6px (most common); cards 10–12px (Bubble 10, focal node 10, dialogs/cards 12, Welcome cards 14); toasts/dropdowns/SpendCap 8; tags 4; search chips 3; pills 16 (ArtifactsNav); toggle 9; CTA buttons **0** (square, `CtaButton`).
- Borders: 1px `--border` everywhere; focal thread node adds 3px `--accent` left border; thread branches 2px `--border` left rule.
- Padding: cards 1.6rem 1.8rem (Bubble), 1.8rem 2rem (focal), 2.2rem 1.8rem (Home cards), dialogs 2rem; page gutters 24px / 2rem; page top padding 60px (app pages) or 3rem.
- Global button: transparent, 1px `--border`, radius 6, padding 10px 20px, `text-secondary`; hover `accent-subtle` fill + `border-hover` + `text-primary`; active `accent-glow`; disabled opacity .45. Primary = `accent` border + `accent` text (outline). Filled `accent` with `bg-deep` text appears only in UpdatesModal ("Got it", "Draft with AI", "Send to developer").
- CTA (`CtaButton`): outline `accent`, square corners, padding 14px 36px, .95rem 400, tracking .06em, trailing "→" that shifts 3px on hover; hover `accent-subtle` + `0 0 30px accent-glow`, lift 1px.
- Shadows: dropdown `0 8px 32px rgba(0,0,0,.4)`; toast/player `0 4px 20px rgba(0,0,0,.3)`; search `0 24px 80px rgba(0,0,0,.5)`; login card `0 16px 60px rgba(0,0,0,.3)`; hovered Home card `0 12px 48px rgba(0,0,0,.35), 0 0 40px accent-glow`; hovered Bubble `0 4px 24px rgba(0,0,0,.3)`.
- Z-order: nav 1000, nav dropdown 1001, dialogs 1000–1100, search 1100, drawer 1100/1101, floating player 9998, toasts & spend banner 9999, Terms 10000.
- Breakpoints: 640 (`useIsMobile`, Landing), 480 (login card padding, feed picks), 412 and 340 (NavBar squeeze), 1200 (SemanticNeighbors panel).
- Brand assets in `public/`: `loore-logo-transparent.svg` (nav, Voice card), `loore-logo.svg/png`, `apple-touch-icon.png`, `android-chrome-192/512`, `og-image.png`.

### 5.4 Motion

- Easing: `cubic-bezier(0.22, 1, 0.36, 1)` (ease-out-quint-like) for all reveals and CTAs.
- **Fade** (`utils/Fade.js`): on first intersection (8% visible, IntersectionObserver, latches) opacity 0→1 and translateY 28px→0 over **0.9s** with a `delay` prop (pages step by .08–.1s). Landing's `FadeSection` uses 32px and also waits for the first scroll.
- Hero keyframes `loore-fade-down` (20px rise) 1–1.2s with staggered delays 0.2 / 0.4 / 0.7 / 0.9 / 1.5s; login wordmark/card 1s at 0.1 / 0.25s; scroll hint pulse 2s infinite.
- Home: greeting .5s, question .5s +200ms, cards .5s at 400ms + 120ms × i, then hover transitions .4s.
- Micro: theme flip .2s on bg/colour/border; link colour .3s; toggle knob .2s; toast-in .25s; spend banner .25s; drawer slide .25s; kebab opacity .15s; card hover .3s (border, shadow, −1px); spinner 1s linear; pulse 1.5s; craft-glow 2.4s (1.2s with reduced motion — the only `prefers-reduced-motion` handling besides the unused App-logo spin).
- Voice: ECG line draw 1.5s (delay .6s), scanline 3s loop, waveform bars 1.2s alternate.
- Product intent (docs/LOORE-ESSENCE.md): "serif warmth, amber on near-black, generous space, slow fades … every visual signal says *nothing here is counting you*"; "never a SaaS dashboard"; no vanity metrics (Commons shows reply counts as muted text, not badges).

---

## 6. Frontend jest tests (`*.test.js`)

| File | Behaviour it pins down |
|---|---|
| `App.test.js` | Placeholder (no assertions of value). |
| `components/Bubble.test.js` | `splitPreview` strips `# ` and splits title/body; a thread name replaces a heading title, keeps plain text as body, blank name falls back. |
| `components/ExternalQuoteBubble.test.js` | Owner-only Mark as read/unread and ⊕/⊖ verdict controls that do not open the post; opening the post marks it read and logs the open; verdict also marks read; reply id attached; verdicts from a parallel Read are labelled. |
| `components/FeedPicks.test.js` | "Open on X" marks a pick read; reopening logs; rating marks read; a verdict given here is not labelled as another reply's. |
| `components/MarkdownBody.test.js` | Raw HTML (block and inline) renders as code, HTML comments dropped, recursion into blockquotes, non-HTML untouched. |
| `components/MarkdownBody.stable.test.js` | Toggling one checklist item keeps the others mounted/focused (#321); a half-typed "+" task survives parent re-render. |
| `components/ModelSelector.test.js` | Picker option lists (featured first, selected non-featured first, expand by provider, read-only models for Read); defaults replace deprecated/non-offered selections; "More models…" expands in place; keyboard operation; outside click closes; disabled stays shut. |
| `components/ProfileGenerationWatcher.test.js` | Clears the indicator when polling gives up with stale data; a `failed` answer is handled once. |
| `components/ProposalInline.test.js` | Proposal parsing: feedback/issue take first line; lead-in vs trailing commentary split; `:::share` fences (multiple, unclosed, legacy headings); todo tick/untick moves items between Completed and New Tasks, creating sections as needed. |
| `components/ReadReply.test.js` | Read window line wording; "Mark all as read" tail; all-read state; silent before quotes load. |
| `components/ReferenceFeedback.test.js` | Icon size default 16px and passed to CSS var. |
| `components/SpeakerIcon.test.js` | TTS chunks arriving at once all reach the audio queue in order. |
| `hooks/useStreamingTranscription.test.js` | A 402 from `/drafts/streaming/init` never opens the mic, returns to idle with a toast; other init failures end in error. |
| `pages/AccountPage.test.js` | One email link per submit (⌘↩ and Enter, in-flight guard); pending Resend/Cancel (pending-only route); expired link; Remove email only with X; Connect X link; Disconnect only with email; `x_login` outcome messages. |
| `pages/AlphaThankYouPage.test.js` | Email form posts to the verified flow; pending/expired states and resend; replace or keep a mistyped pending address; hint text; confirmed address shows nothing. |
| `pages/ConfirmEmailPage.test.js` | Signed out asks to sign in and posts nothing; posts token once under StrictMode; waitlisted continue to thank-you; other-account message; retry on no answer; no token → no POST. |
| `utils/date.test.js` | `yyyy/mm/dd` padding; "Mon D, YYYY"; "today"; `yyyy/mm/dd HH:MM`; fallback. |
| `utils/diff.test.js` | Line diff, collapse of unchanged runs, word-level refinement rules (version history drawer). |
| `utils/intentions.test.js` | Intentions artifact parsing (Endorsed/Inferred, name/status/body/notes). |
| `utils/markdown.test.js` | Link-bearing checklist items toggle/insert by stripped label. |
| `utils/nodeLinks.test.js` | Node-link recognition (loore.org, www, staging, same origin, relative); batched, cached title lookups; failures not cached. |
| `utils/references.test.js` | YouTube URL parsing (watch, youtu.be, shorts, live, embed, start time); source labels; `bodyWithoutTitle`. |
| `utils/spendCap.test.js` | Reset date = first of next UTC month; toast names the refused action; only the spend-cap 402 is recognised. |
| `utils/voiceTiming.test.js` | Voice turn timing sent once per turn keyed by first reply node; ignores other nodes, cancelled turns, non-first chunks. |

Screens with no tests: Home, Log, NodeDetail, Voice, Write, Profile, Todo, Artifacts, Share, Commons, public pages, NavBar, modals other than those above.

---

## 7. Surprises worth knowing

1. **No mobile navigation design.** NavBar has identical items on phone and desktop; `useIsMobile` is used only by `GlobalAudioPlayer` and `UpdatesModal`. Everything secondary lives in the ⋮ menu.
2. **Several screens have no navigation entry**: References (only via Import's "View references" when counts > 0, search, or its detail page), Search (⌘K or the Log/References magnifier), Todo (only via the ArtifactsNav bubble row), Share (Home card / Commons link).
3. The NavBar item labelled **"Artifacts" opens `/profile`**.
4. **Default post-login destination is Profile**, not Home: LoginPage's default `returnUrl` and the backend's default `next` are both `/dashboard`, which redirects to `/profile`. The explicit Login/CTA links pass `returnUrl=/`.
5. **Terms text is hard-coded in `TermsModal`** (v2.0, 2026-02-09); the backend only knows whether the accepted version is current.
6. Search snippets are server-generated **HTML with `<mark>`**, injected raw.
7. The page glow `rgba(196,149,106,0.06)` is the dark accent hard-coded in light mode too.
8. Unapproved users get a ⋮ menu with only About + Logout, and cannot switch theme.
9. Vestigial bits: `loore_profile_started` has listeners but no dispatcher; `?fresh=1` on `/voice` and `?x_connect=` on `/import` are ignored; the PermalinkRoute comment still says `/u/:username/:slug`.
10. The Commons nav link and Share card are gated on `share_v1_enabled` (env flag AND user opt-in); Share and Commons render "Not available." for users without it.
11. Read / "Relevant tweets" / SemanticNeighbors / keyword search are admin-only surfaces inside core screens.

## 8. Main risks for a native port (this area)

1. **Authentication.** The backend only knows a session cookie. X sign-in, X connect and X-bookmark connect end in a 302 to `https://loore.org/...`; magic links are opened from Mail into Safari and set the cookie there, not in the app. The native app needs `ASWebAuthenticationSession` (or an in-app `WKWebView`) that ends on a loore.org URL and copies the `session`/`remember_token` cookies into `HTTPCookieStorage`, and/or universal links for `/auth/magic-link/verify` and `/confirm-email` (requires an `apple-app-site-association` file on loore.org: a static-file server change, even if Flask stays unchanged). ASWebAuthenticationSession with an `https` callback needs iOS 17.4+ and associated domains. This is the largest blocker.
2. **Cookie + SSE + polling client.** Four SSE hooks (`/api/sse/nodes/{id}/llm-stream`, `/api/sse/{nodes|…}/{id}/tts-stream`, `/api/sse/nodes/{id}/transcription-stream`, `/api/sse/drafts/{sid}/transcription-stream`) use `EventSource` with cookies, opened against `REACT_APP_BACKEND_URL` directly; native needs an SSE client over `URLSession` bytes with reconnect and foreground re-sync (the web code already works around iOS suspending SSE in the background).
3. **Global cross-cutting state** implemented with window events, CSS variables and module caches (§4.4) must become an app-level store: user/session, spend-cap flag, profile-generation watcher, artifacts list, toasts, audio player.
4. **Gating logic is scattered** across ~10 user flags (§4.3). One `FeatureGates` model derived from the `/dashboard` user avoids divergence. Unapproved users must be routed to the waitlist screen; every other API returns 403 for them.
5. **Desktop-first interaction** to replace: hover-revealed kebabs placed outside cards, 420px right drawer, cmd-click new tab, ⌘K/⌘↩/Esc, hover tooltips carrying explanations (e.g. craft toggle, pin, read buttons), wide admin tables.
6. **Web-only capabilities**: blob download of the export (→ share sheet), `.zip` pickers and client-side JSZip extraction for Claude/ChatGPT imports (→ document picker + a ZIP library or upload the whole zip), X tweet widget and YouTube iframe (→ `WKWebView`), clipboard copy, `beforeunload` guards (→ dirty-state confirmation on back/swipe).
7. **Deep links**: web URLs are `/node/{id}`, `/@user`, `/@user/slug` (dual member/visitor), `/references/{id}`, `/artifacts/{kind}`, `/account#…`, plus `?awaitLlm=` hand-off semantics between an entry and its pending reply. Links inside markdown to `loore.org/node/{id}` render as titles and must route in-app.
8. **Per-device preferences in localStorage** (§4.5) — decide whether to keep per-device (UserDefaults) or accept drift; `craft_mode` is on the server, the rest are not.
9. **Content parity**: the marketing pages, Terms and many empty-state explainers are static copy embedded in JS; they must be copied into the app (or the marketing pages opened on the web).
10. **Fonts and theme**: bundle Cormorant Garamond + Outfit; implement tokens as asset-catalog colours with light/dark variants; web defaults to dark unless the OS prefers light, and the in-app toggle is per device.
