# Map B: API contract, auth and data model (Loore iOS port)

Mapped 2026-09-30 from `origin/main` at `2774ab2` (worktree `.claude/worktrees/ios-app`). Read-only; nothing in the repo was changed. Backend: Flask + Flask-Login 0.6.3 + flask-dance (X OAuth 1.0a) + Celery. Frontend: CRA React with axios.

Purpose: enough detail for a Swift engineer to write the networking layer, the auth flow and the `Codable` models without reading the JS or Flask code. Where a statement comes from reading code rather than from a test, that is the case unless a test is cited in §7.

Contents
- §0 Conventions that apply to every endpoint (hosts, cookies, 401/403/402, dates, trailing slashes, limits)
- §1 `api.js` and per-environment configuration
- §2 Endpoint catalogue (2.1 auth, 2.2 nodes + media, 2.3 drafts/recording/voice/textmode/read/SSE, 2.4 account and workspace, 2.5 import/export/references/share/commons/admin)
- §3 Auth end to end (cookies, magic link, X OAuth, Connect X, logout, gating, email change, CORS)
- §4 Native auth options (no backend change vs small backend change) and the recommendation
- §5 Data model (enums, entities, relations, where each JSON shape is defined, proposals)
- §6 Async task patterns (polling, profile progress, warnings, spend cap, error codes, SSE summary)
- §7 Backend tests that pin the contract
- §8 Risks and open questions for the design doc
## 0. Conventions that apply to every endpoint

- **Hosts.** Production `https://loore.org`, staging `https://staging.loore.org`. The API, auth, media and SSE all live on the same host as the web app (nginx routes `/api/`, `/auth/`, `/media/` and `/api/sse/` to gunicorn; see `configs/nginx.txt`). There is no separate API domain. `FRONTEND_URL` (backend env) is the same origin on prod/staging, so every backend redirect "to the frontend" lands on `https://loore.org/...`.
- **Auth is cookie-only.** Every `/api/*` route that needs a user uses Flask-Login sessions (`@login_required` or a `current_user` check). There is no bearer-token auth for the app; the only bearer tokens (`loore_…`, scope `external:write`) are for the Chrome clipper and are accepted on two external-reference write routes only (`backend/utils/api_tokens.py`).
- **Unauthenticated behaviour differs by path prefix** (`backend/__init__.py` `unauthorized()`):
  - path starts with `/api` → `401 {"error": "Unauthorized"}`.
  - any other `@login_required` path (e.g. `/auth/logout`) → `302` to `/auth/login`, which itself starts the X OAuth flow (302 to `/auth/twitter` → 302 to api.twitter.com). A native client must not follow redirects blindly on non-`/api` paths; treat a 302 whose `Location` contains `/auth/login` or `twitter.com` as "signed out".
- **Approval gate** (`block_unapproved_users` before_request): a signed-in user with `approved == false` gets `403 {"error": "Your account is not approved. Please wait for approval."}` on every `/api` call except: any `GET /api/dashboard*`, `PUT /api/dashboard/user*`, `POST|DELETE /api/dashboard/email` and `/api/dashboard/email/*`, anything under `/api/terms`, plus `/auth/*` and public-page endpoints.
- **Spend cap** (per-user monthly USD cap): any cost-incurring call from a capped user returns `402 {"error": "monthly_spend_limit_reached", "message": "You've reached your monthly usage limit for the free alpha. It resets at the start of next month."}`. Raised by `@require_spend_headroom` (or an inline check) on: `POST /api/nodes/` in multipart (audio file) mode, `POST /api/nodes/<id>/llm`, `POST /api/nodes/<id>/tts`, `POST /api/nodes/upload/init`, `POST /api/nodes/streaming/init`, `POST /api/drafts/streaming/init`, `POST /api/profile/<id>/tts`, `POST /api/external/items/<id>/tts`; and by the app-wide `SpendCapExceeded` error handler on any path that creates an LLM placeholder node (textmode/voice/read starts etc.). The reset is the first day of the next month, UTC.
- **Provider failures.** Provider-side failures (OpenAI/Anthropic 429, org spend limit, overload) do not become HTTP errors: the async task fails and the raw exception text lands in the node's `llm_task_error`, which `GET /api/nodes/<id>/llm-status` returns with `status: "failed"`.
- **CORS** (`flask-cors`) allows only `FRONTEND_URL` with credentials; CORS is enforced by browsers only, so native `URLSession` calls are unaffected.
- **Timestamps.** Almost every route serializes with `backend/utils/timefmt.iso_utc`: naive UTC datetimes as `datetime.isoformat() + "Z"`. Python's `isoformat()` emits **microseconds (6 digits) when non-zero and no fraction when zero**: `"2026-09-30T12:34:56.123456Z"` or `"2026-09-30T12:34:56Z"`. A few timezone-aware columns (e.g. `prefill_consent_at`) come out with `+00:00` instead of `Z`. `/api/updates` changelog entries carry a date-only `"2026-09-30"`. The `/api/log` cursor is an opaque string built from a naive isoformat (`"<iso>|<id>"`); pass it back unchanged. Swift: write one tolerant date decoder (try fractional, then non-fractional, then `+00:00`, then date-only); `ISO8601DateFormatter` with `.withFractionalSeconds` must not be assumed to accept 6 digits on every iOS version, so trim the fraction to 3 digits before parsing.
- **Trailing slashes.** Routes declared as `"/"` under a prefix (e.g. `GET /api/dashboard/`, `POST /api/nodes/`, `GET/POST/DELETE /api/drafts/`) are canonical with the slash; the slashless form gets a `308` to the slash form. This applies to `/api/dashboard/`, `/api/nodes/` (POST), `/api/drafts/` (GET/POST/DELETE), `/api/todo/` (GET/PATCH/PUT), `/api/artifacts/` (GET), `/api/profile/` (POST), `/api/prompts/` (GET), `/api/voice/` (POST). URLSession follows 308 keeping method and body, but call the canonical form to save a round trip. The opposite case: `GET /api/updates` has no slash and `/api/updates/` is a 404.
- **Request headers the web app sends.** `X-Timezone: <IANA tz>` on every request (a hint used for LLM time grounding; the authoritative value is persisted with `PATCH /api/dashboard/timezone`). Status polls add `Cache-Control: no-cache`. JSON bodies use `Content-Type: application/json`.
- **Limits.** nginx `client_max_body_size 200M` on `/api/` and server-wide; nginx `proxy_read_timeout 60s` on `/api/` (long work must go through Celery + polling); `/api/sse/` has buffering off and a 7200 s read timeout.
- **Error body convention.** Errors are JSON `{"error": "<human text or code>"}`, sometimes with `"message"`, `"code"` or `"details"`. Codes the web app branches on are listed per endpoint in §2 and collected in §6.4.

## 1. `frontend/src/api.js` and environment configuration

### 1.1 axios instance (`frontend/src/api.js`)

| Setting | Value | Native equivalent |
|---|---|---|
| `baseURL` | `process.env.REACT_APP_API_URL \|\| "/api"` | `https://loore.org/api` |
| `withCredentials` | `true` (send cookies cross-origin) | `URLSessionConfiguration.default` with `httpCookieStorage = .shared`, `httpShouldSetCookies = true`, `httpCookieAcceptPolicy = .always` |
| `timeout` | `60000` ms (overridden to `10000` ms for status polls in `useAsyncTaskPolling`) | `timeoutIntervalForRequest = 60`; 10 s for polls |
| Request interceptor | Adds `X-Timezone: Intl.DateTimeFormat().resolvedOptions().timeZone` to every request | `TimeZone.current.identifier` header on every request |
| Response interceptor | On `402` with `data.error === "monthly_spend_limit_reached"` dispatches the window event `loore:spend-capped` (shows `SpendCapBanner`) and re-rejects | A global response hook that flips an app-wide "spend capped" flag and shows a banner |
| Retry | None in axios. Retries exist only inside specific hooks (chunked upload, streaming audio-chunk upload, polling that keeps going after errors, SSE reconnect); see §2 and §6. | Same: no global retry |
| Auth header | None. Cookies only. | Cookies only |

Non-axios calls:
- `LoginPage.js` uses `fetch(`${REACT_APP_BACKEND_URL}/auth/magic-link/send`)` (no credentials needed) and sets `window.location.href = `${REACT_APP_BACKEND_URL}/auth/login?next=<returnUrl>`` for "Sign in with X".
- `NavBar.js` logout is a plain link `<a href="${backendUrl}/auth/logout">`; `ConfirmEmailPage.js` uses `/auth/logout?next=/confirm-email?...`.
- `AccountPage.js` "Connect X" is a link to `${backendUrl}/auth/x/connect`; `ExternalImport.js` "Connect X bookmarks" links to `${REACT_APP_BACKEND_URL}/api/external/twitter/connect`.
- `useSSE.js` builds `EventSource(`${REACT_APP_BACKEND_URL}/api/sse/...`, {withCredentials: true})`.
- `useStreamingTranscription.js` falls back to `navigator.sendBeacon(`${REACT_APP_BACKEND_URL}/api/drafts/streaming/<session>/audio-chunk`, formData)` when the page is being hidden.
- `DownloadAudioIcon.js` uses `fetch(url, {credentials: 'include'})` to download audio as a blob; `SpeakerIcon.js`/`useVoiceSession.js` prefix relative audio URLs (`/media/...`, `/api/...`) with `REACT_APP_BACKEND_URL`.

### 1.2 Environments

| Environment | `REACT_APP_API_URL` | `REACT_APP_BACKEND_URL` | Where set |
|---|---|---|---|
| Production | `https://loore.org/api` | `https://loore.org` | `.github/workflows/deploy.yml` build env (also `REACT_APP_SENTRY_DSN`) |
| Staging | `https://staging.loore.org/api` | `https://staging.loore.org` | `.github/workflows/deploy-staging.yml` |
| CI build | `https://loore.org/api` | `https://loore.org` | `.github/workflows/ci.yml` |
| Local dev (CRA) | empty → `"/api"` (same-origin) | empty → `""` | `frontend/.env.development` |
| `frontend/.env.production` (stale, overridden by CI) | `https://writeorperish.org/api` | `https://writeorperish.org` | committed; not used by the deploy workflows |
| Docker prod image default | `http://localhost:5010/api` | `http://localhost:5010` | `frontend/Dockerfile` ARGs |

Local dev: `frontend/src/setupProxy.js` proxies `/api`, `/auth`, `/media` from the CRA dev server (`:3001` in Docker, `:3000` on host) to `PROXY_TARGET` (`http://backend:5010` in Docker, default `http://localhost:5010`), with `changeOrigin: false` so Flask's 308 trailing-slash redirects keep the browser's Host. A native dev build can talk to `http://<mac-LAN-IP>:5010` directly (Flask dev server) or to `:3001` through the proxy; local dev sets `SESSION_COOKIE_SECURE=false` (docker-compose.override.yml) so cookies work over plain http. iOS ATS needs an exception for plain-http dev hosts.

Backend `FRONTEND_URL` defaults to `http://localhost:3000`; prod/staging set it to the public origin. Every auth redirect uses it (§3).
## 2. Endpoint catalogue

Every route the backend registers, grouped by feature area, with the request and response shapes read from the Flask code and the frontend call sites that use them. "Called from" names the web files; routes the web never calls are listed at the end of each area. Conventions from §0 (auth, approval gate, 402, date format, trailing slashes) apply to all of them and are only repeated where a route deviates.

| Area | Section | Routes | Called by the web app |
|---|---|---|---|
| Auth (`/auth`, incl. flask-dance `/auth/twitter*`) | 2.1 | 7 | 5 (via navigation, `fetch`, or the mailed link) |
| Nodes (`/api/nodes`) and media (`/media`) | 2.2 | 36 | 29 |
| Drafts and recording, Voice, Text mode, Read, SSE | 2.3 | 29 | 25 |
| Account, profile, todo, artifacts, prompts, log, search, feedback/GitHub, updates, terms, health | 2.4 | 60 (4 are health aliases) | 51 |
| Import, export, references, share, commons, admin, webhooks | 2.5 | 72 JSON (+14 server-rendered HTML/XML public pages) | 63 |
| **Total** (profile-progress counted once) | | **203 JSON/redirect routes** | **~172** |

Streaming endpoints: the six `GET /api/sse/...` streams (2.3). File uploads: `POST /api/nodes/` (multipart), `/api/nodes/upload/*` (chunked), `POST /api/drafts/streaming/<sid>/audio-chunk` (multipart, 15 s chunks), `/api/import/*/analyze` (multipart archives) (2.2, 2.3, 2.5). File downloads: `GET /media/<path>` (audio, Range support), `GET /api/nodes/<id>/audio-download`, `GET /api/export*` (2.2, 2.5).

### 2.1 Auth (`/auth`)

Full flows, cookies and redirects in §3. All of these are browser-navigation routes except `magic-link/send`.

| Method | Path | Auth | Request | Response |
|---|---|---|---|---|
| POST | `/auth/magic-link/send` | none | JSON `{email: String, next_url?: String (relative path)}` | `200 {message}` always (also for unknown emails); `400 {"error": "Please enter a valid email address."}` |
| GET | `/auth/magic-link/verify?token=` | none | query `token` | `302` to `FRONTEND_URL{next_url or /dashboard}` with `Set-Cookie: session`, `remember_token`; or `302 FRONTEND_URL/login?error=invalid_or_expired \| link_already_used \| confirm_needs_account` |
| GET | `/auth/login?next=` | none | query `next` (relative path) | Starts or completes "Sign in with X": `302 /auth/twitter` → X; after the callback `302 FRONTEND_URL{next or /dashboard}` with cookies; errors `302 FRONTEND_URL` or `/login?error=…` |
| GET | `/auth/twitter` | none | — | flask-dance: `302` to `api.twitter.com/oauth/authenticate` (stores the request token in the session) |
| GET | `/auth/twitter/authorized` | none (session-bound) | X's `oauth_token`, `oauth_verifier` or `denied` | `302 /auth/login`; foreign callback → `302 FRONTEND_URL/login?error=x_try_again` |
| GET | `/auth/x/connect` | session (not `@login_required` on purpose) | — | signed out → `302 FRONTEND_URL/login?returnUrl=%2Faccount`; signed in → X round trip ending `302 FRONTEND_URL/account?x_login=linked\|other_x\|taken\|taken_placeholder\|cancelled\|failed#x` |
| GET | `/auth/logout?next=` | `@login_required` (signed out → `302 /auth/login` → X!) | query `next` (relative path) | `302 FRONTEND_URL{next}` or `302 FRONTEND_URL`, clears session + remember cookie |
### 2.2 Nodes (`/api/nodes`) and media (`/media`)

Source: `backend/routes/nodes.py` (blueprint `nodes_bp`, prefix `/api/nodes`), `backend/routes/media.py` (blueprint `media_bp`, prefix `/media`), `backend/utils/serialization.py`, `backend/utils/quotes.py`, `backend/utils/llm_nodes.py`.

Conventions that hold for every route below:

- All `/api/nodes/*` routes except `GET /api/nodes/media/<path>` are `@login_required`. Not logged in: `401 {"error": "Unauthorized"}` (app-level `unauthorized_handler`). Logged in but `approved == false`: `403 {"error": "Your account is not approved. Please wait for approval."}` (app-level `before_request`; no `/api/nodes` path is exempt).
- Timestamps are ISO 8601 strings from `iso_utc()`: naive UTC datetimes get a `Z` suffix (`"2026-09-30T12:34:56.123456Z"`); microseconds are present when non-zero. Parse with a formatter that accepts optional fractional seconds. Exception: `tool_calls_meta[]._batch.submitted_at` is `datetime.utcnow().isoformat(timespec="seconds")` with NO `Z` (UTC, no zone marker).
- Error bodies are `{"error": "<human-readable string>"}` unless stated. `get_or_404` misses return Flask's default HTML 404 page, not JSON: treat any 404 whose body is not JSON as "not found".
- `402 {"error": "monthly_spend_limit_reached", "message": "You've reached your monthly usage limit for the free alpha. It resets at the start of next month."}` is the spend-cap response (decorator `require_spend_headroom`, `spend_cap_response()`, or the app-level `SpendCapExceeded` handler). The web client's axios interceptor turns any 402 with that `error` into a global banner; call sites then return silently.
- Enumerations: `privacy_level` ∈ `"private" | "circles" | "public"` (circles is unimplemented: behaves like private for non-owners). `ai_usage` ∈ `"none" | "chat" | "train"`. `node_type` ∈ `"user" | "llm" | "link"`. Task statuses (`llm_task_status`, `tts_task_status`, `transcription_status`) ∈ `null | "pending" | "processing" | "completed" | "failed"`; `llm_task_status` can also be `"cancelled"`.
- Ownership: a node's "owner" for pin/rename/LLM/feed-picks is `human_owner_id ?? user_id`. LLM nodes have `user_id` = the model's pseudo-user (username = model id, e.g. `"claude-opus-4.6"`) and `human_owner_id` = the requesting human. Edit/delete permission (`can_user_edit_node`) = `user_id == me || human_owner_id == me`. Read access (`can_user_access_node`) = alive AND (`user_id == me` OR `human_owner_id == me` OR `privacy_level == "public"`).
- `NODE_CHAR_CAP = 100000` characters per node (backend `utils/node_split.py`, frontend `SplitContentDialog.js`).
- Audio size cap `MAX_AUDIO_BYTES = 200 MB`; allowed audio extensions: `webm, wav, m4a, mp3, mp4, mpeg, mpga, ogg, oga, flac, aac`. nginx `client_max_body_size 200M`.

#### 2.2.0 Node JSON shapes

Several routes serialize nodes differently. There is no single "Node" object; model them as separate Swift types (or one type with many optionals).

##### A. `NodeDetail` (focal node) — `GET /api/nodes/<id>`; `node` in `PUT /api/nodes/<id>` (same fields minus the tree)

Built by `_focal_own_fields()` + tree keys added in `get_node()`.

```
{
  id: Int,
  content: String,                 // decrypted; for a system-prompt node, the linked prompt version's text
  node_type: "user" | "llm" | "link",
  created_at: String(ISO),
  updated_at: String(ISO),
  permalink: String | null,        // "/@<human owner username>/<public_slug>" only while privacy_level == "public" and a slug exists
  user: { id: Int, username: String },   // author; for LLM nodes the model pseudo-user (username == model id)
  parent_user_id: Int | null,      // LLM node: human_owner_id. Other: parent's user_id (null for roots)
  privacy_level: String,
  ai_usage: String,
  pinned_at: String(ISO) | null,
  llm_model: String | null,        // model id, e.g. "claude-opus-4.6"; set on LLM nodes
  origin: String | null,           // null = written in Loore; "twitter" | "chatgpt" | "claude" | "markdown" for imports
  llm_task_status: String | null,  // see enums above; non-null only on LLM nodes
  has_original_audio: Bool,        // audio_original_url set OR streaming_transcription
  has_tts: Bool,                   // generated TTS exists (audio_tts_url set)

  // present only when llm_task_status in (pending, processing) and partial text exists (#367)
  streaming_content?: String,

  // present only when the node has tool_calls_meta and it parses as JSON
  tool_calls_meta?: [ToolCallMeta],
  // present only when tool_calls_meta contains an entry named "_batch" or "_feed_sample"
  feed_picks_count?: Int,

  // system-prompt fields (always present; see _system_prompt_fields below)
  is_system_prompt: Bool,
  prompt_title: String | null,
  prompt_key: String | null,
  user_prompt_id: Int | null,
  prompt_version_number: Int | null,
  context_artifacts: ContextArtifacts | null,

  // present only on read replies (Community Archive "Read" answers)
  read_reply?: true,
  read_window?: { export_id, days, scope, window_start: ISO|null, window_end: ISO|null,
                  tweets: Int, accounts: Int, excluded: Int } | null,

  // ---- GET only (tree) ----
  child_count: Int,                // number of serialized (visible) children
  ancestors: [AncestorNode],       // root first, direct parent last
  children: [TreeNode],            // sorted by descendant_count desc
  in_read_thread: Bool,
  read_reply_above: Bool,
  reply_ai_usage: String           // ai_usage a new reply under this node should default to (#362)
}
```

`_system_prompt_fields(n)`:
- Node links a prompt version: `is_system_prompt: true, prompt_title: String, prompt_key: String, user_prompt_id: Int, prompt_version_number: Int (1-based count of that user's versions of the key up to this one), context_artifacts: ContextArtifacts | null`.
- Otherwise: `is_system_prompt: Bool` (true if the node still carries a `prompt_key` stamp), `prompt_title: null`, `prompt_key: String | null`, `user_prompt_id: null`, `prompt_version_number: null`, `context_artifacts: ContextArtifacts | null`.

`ContextArtifacts` (dict, keys present only if attached; `null` when empty):
```
{
  prompt?:   { id: Int, title: String, version_number: Int, prompt_key: String },
  profile?:  { id: Int, version_number: Int, content: String },
  todo?:     { id: Int, version_number: Int, content: String },
  recent?:   { id: Int, content: String },
  memory? | scratchpad? | ai_preferences? | intentions?:   // UserArtifact INLINE_KINDS, key = kind
             { id: Int, version_number: Int, content: String },
  share_guidance?:             { content: String },   // only when prompt present and content has the placeholder
  external_content_guidance?:  { content: String },   // same
  recent_raw?: { covers_start: "YYYY-MM-DD", covers_end: "YYYY-MM-DD", source_tokens: Int }
}
```
These carry the text that was substituted for `{user_profile}`, `{user_todo}` etc. placeholders so the client can render the system prompt as the model saw it (web: `QuotedContent`).

`ToolCallMeta` (list element; loosely typed JSON, decode as `[String: JSONValue]`):
- `name: String`. Names starting with `_` are internal markers the UI hides: `"_batch"` (`{name, batch_id, custom_id, model, provider, key_type, submitted_at, status: "submitted"|"cancelling"|...}`), `"_read"`, `"_feed_sample"`, `"_mode"`.
- Visible tool entries have `status: "success" | <other>` plus tool-specific keys. Names the web UI renders: `propose_todo`, `apply_todo_changes`, `read_todo`, `propose_share`, `apply_share`, `propose_feedback`, `apply_feedback`, `propose_github_issue`, `apply_github_issue`, `read_artifact`, `update_artifact`, `update_ai_preferences`. Keys read by the UI: `apply_status: "started"|"completed"|"failed"`, `apply_error: String`, `created`, `kind`.

##### B. `AncestorNode` — elements of `ancestors` in `GET /api/nodes/<id>`

Alive and accessible ancestor:
```
{ id, username: String ("Unknown" if user missing), llm_model: String|null,
  content: String, preview: String (first 200 chars + "..." if longer),
  node_type, child_count: Int (ALL child rows incl. deleted/private), created_at,
  user_id: Int, parent_user_id: Int|null, ai_usage, privacy_level,
  + _system_prompt_fields }
```
Soft-deleted ancestor the viewer could see before deletion (tombstone):
```
{ id, deleted: true, deleted_at: ISO, username: String|null, node_type, created_at,
  child_count, ai_usage, privacy_level, + _system_prompt_fields }
```
Privacy-blocked ancestors are omitted from the list entirely (so `ancestors[0]` is the topmost ancestor the viewer can see, not necessarily the thread root).

##### C. `TreeNode` — elements of `children` (recursive), from `serialize_node_recursive`

Full node:
```
{ id, content: String, node_type, child_count: Int (visible children), created_at, updated_at,
  username: String, llm_model: String|null, origin: String|null, descendant_count: Int,
  user_id: Int, parent_user_id: Int|null, children: [TreeNode], has_tts: Bool,
  privacy_level, ai_usage, + _system_prompt_fields }
```
Tombstone (soft-deleted, viewer had access, and it has visible descendants — tombstones with no visible descendants are pruned):
```
{ id, deleted: true, deleted_at, username: String|null, node_type, created_at,
  child_count, descendant_count, children: [TreeNode] }
```
Inaccessible stub (not normally reachable in children because the visibility filter drops them first): `{ id, inaccessible: true }`.
Notes: the whole subtree is returned (no depth limit, no pagination); children are sorted by `descendant_count` descending. `TreeNode` has `username` (string) where `NodeDetail` has `user {id, username}`; TreeNode has no `pinned_at`, `llm_task_status`, `permalink`, `tool_calls_meta`.

##### D. Small create/upload shapes

- Text create (`POST /api/nodes/` JSON): `{ id, content, node_type, parent_id: Int|null, linked_node_id: Int|null, created_at, username, privacy_level, permalink: String|null, ai_usage, split_into: Int, tip_id: Int }`.
- Audio create / chunked finalize: `{ id, audio_original_url: String ("/media/user/<uid>/node/<nid>/original.<ext>"), content, node_type, created_at, transcription_status: String, transcription_task_id: String|null }`. **Bug to know about:** `content` here is the raw DB column (`node.content`), which is ciphertext when encryption is on. Ignore it; fetch the node or poll `transcription-status` for the text.
- LLM status `node` (`GET .../llm-status`): `{ id, content, node_type, llm_model, created_at }`.
- Quote data (`resolve-quotes`): see that endpoint.

---

#### 2.2.1 Node CRUD

##### `POST /api/nodes/`
- Backend: routes/nodes.py:create_node
- Auth: login.
- Called from: `NodeForm.js` (new text node; small audio upload), `NodeDetail.js` (`submitReadReplyMessage` when `ai_usage == "none"`), `WritePage.js` (craft-mode fallback when AI is not allowed).
- Path note: trailing slash. `POST /api/nodes` (no slash) gets a 308 to `/api/nodes/`.
- Request, JSON mode (`Content-Type: application/json`):
  - `content: String` (required, non-empty)
  - `parent_id: Int | null` (null/absent = root)
  - `node_type: String` (default `"user"`; web never sends it)
  - `linked_node_id: Int | null` (web never sends it)
  - `privacy_level: String` (default `"private"`)
  - `ai_usage: String` (default `"none"`)
  - Web sends: `{content, parent_id, privacy_level, ai_usage}`.
- Request, multipart mode (`Content-Type: multipart/form-data`, used for audio files ≤ 10 MB by the web; larger use the chunked flow):
  - `audio_file`: file part (required; filename must have an allowed extension)
  - `parent_id` (string int, optional), `node_type` (default `"user"`), `privacy_level` (default `"private"`), `ai_usage` (default `"none"`)
- Response 201 (JSON mode): shape D "Text create". If `content` exceeded 100 000 chars it is split at newline boundaries into a serial chain: the returned `id` keeps the first segment, each further segment is a child of the previous; `split_into` = number of nodes; `tip_id` = the last node in the chain (the node to navigate to / reply under). A public ROOT gets a permalink slug at creation.
- Response 201 (multipart): shape D "Audio create". `transcription_status: "pending"`; a Celery `transcribe_audio` task is enqueued (if an OpenAI key is configured) and its id is in `transcription_task_id`. Content is the placeholder `"[Voice note – transcription pending]"` until transcription completes. Poll `GET /api/nodes/<id>/transcription-status`.
- Errors:
  - 400 `{"error":"Content is required"}` (JSON mode)
  - 400 `{"error":"Invalid privacy_level: <v>"}` / `{"error":"Invalid ai_usage: <v>"}`
  - 400 `{"error":"Invalid parent_id"}`; 404 `{"error":"Parent node not found"}`; 410 `{"error":"Parent node has been deleted"}`
  - 400 `{"error":"Replies under public posts are public.","code":"public_reply_required"}` when the parent is public and `privacy_level != "public"` (the web forces public before sending, and shows a consent dialog `PublicReplyDialog`)
  - multipart only: 402 spend cap (checked before anything is stored); 400 `{"error":"Field 'audio_file' is required"}`, 400 `{"error":"No selected file"}`, 415 `{"error":"Unsupported file type"}`, 413 `{"error":"File too large"}`
  - 500 `{"error":"DB error creating node"}`
- Frontend special handling: `NodeForm` shows `err.response.data.error` inline; for an upload that hits 402 it toasts a spend-cap message. After a successful top-level JSON create with auto-generate on (and agentic prompt off) it immediately calls `POST /api/nodes/<new id>/llm` with `{source_mode: "textmode"}`. For replies, `NodeDetail.handleInlineSuccess` calls `/llm` (with `model`, `source_mode: "textmode"`) if auto-generate is on and every node in `[node, ...ancestors]` has `ai_usage` in `chat|train`.

##### `GET /api/nodes/<int:node_id>`
- Backend: routes/nodes.py:get_node
- Auth: login + `can_user_access_node`.
- Called from: `NodeDetail.js` (page load; refetch after delete/edit/rerun), `NodeForm.js` (fetch parent to inherit `privacy_level` and default `ai_usage` from `reply_ai_usage`).
- Request: none.
- Response 200: shape A (`NodeDetail`) with `ancestors`, `children` (full subtree, recursive), `child_count`, `in_read_thread`, `read_reply_above`, `reply_ai_usage`.
- Errors: 404 `{"error":"Node not found"}` for missing, soft-deleted (even for the owner), or not accessible (same body; the API never reveals which).
- Frontend special handling: 404 or 403 → "This node doesn't exist, was deleted, or isn't shared with you." If `permalink` is set, the web rewrites the address bar to it. `?awaitLlm=<id>` in the web URL is purely client-side.
- Notes: cost scales with ancestors + whole subtree (every node decrypted). For a streaming reply (`llm_task_status` pending/processing) subscribe to `GET /api/sse/nodes/<id>/llm-stream` and seed with `streaming_content`.

##### `PUT /api/nodes/<int:node_id>`
- Backend: routes/nodes.py:update_node
- Auth: login + `can_user_edit_node` (author, or human owner of an LLM node).
- Called from: `NodeForm.js` (edit mode), `NodeDetail.js` (`saveNodeContent` — checklist toggles, inline edits: `{content}` only), `ProposalInline.js` (rewrites proposal content: `{content}` only).
- Request JSON (body must be JSON; a missing body raises a 500):
  - `content: String` — REQUIRED on every call, including settings-only edits (send the current content unchanged). Max 100 000 chars (no auto-split on edit).
  - `privacy_level?: String` — setting `"private"` also unpins the node.
  - `ai_usage?: String` — `"train"` is rejected on Community Archive read nodes.
  - `detach_prompt?: Bool` — for a system-prompt node: when true AND the text changed, removes the link to the prompt version so the edited text is stored on the node (the `prompt_key` stamp is kept).
  - `regenerate_tts?: Bool` — when true AND content changed, deletes generated TTS audio + chunk rows so the next TTS request regenerates. Original recordings are never touched.
  - `apply_to_descendants?: Bool` — cascade the privacy/AI-usage change (only the one(s) that actually changed) to every alive reply below that the user may edit.
- Response 200: `{ message: "Node updated", node: <shape A without the tree keys>, descendants_updated: Int }`.
- Side effects: if content changed, the old text is saved as a `NodeVersion`, placeholders are re-synced to context artifacts, `token_count` recomputed. Public page cache invalidated when relevant.
- Errors: 403 `{"error":"Not authorized"}`; 400 `{"error":"Content required for update"}`; 422 `{"error":"Content exceeds the 100,000-character per-entry limit. Please split it into multiple entries.","char_cap":100000}`; 400 invalid privacy/ai_usage; 400 `{"error":"A Community Archive read cannot be used for training: it quotes other people's tweets."}`; 404 (HTML) missing node; 500 `{"error":"DB error updating node"}`.
- Frontend special handling: merges `node` into the held node (tree unchanged); if `descendants_updated > 0`, toasts "Applied to N replies too" and refetches `GET /nodes/<id>`. If the edited focal node is not an LLM node and auto-generate is on, fires `POST /nodes/<id>/llm` (a new sibling reply). The web asks "regenerate audio?" when `has_tts` is true and content changed.

##### `GET /api/nodes/<int:node_id>/delete-impact`
- Backend: routes/nodes.py:delete_impact
- Auth: login + `can_user_edit_node`.
- Called from: `NodeDetail.js` (only when the thread root `is_system_prompt` and the target is not the root).
- Request query: `delete_descendants=true|false` (`"true"|"1"|"yes"` = true).
- Response 200: `{ orphaned_system_prompt_id: Int | null }` — the thread root's id when this delete would leave the session with only its system prompt alive.
- Errors: 403 `{"error":"Not authorized"}`; 404 (HTML).
- Frontend: advisory; on error it deletes anyway. When non-null, asks "delete the system prompt too?" and passes the answer as `delete_orphaned_prompt`.

##### `DELETE /api/nodes/<int:node_id>`
- Backend: routes/nodes.py:delete_node
- Auth: login + `can_user_edit_node`.
- Called from: `NodeDetail.js` (`params: {delete_descendants, delete_orphaned_prompt}`), `Log.js` (on the thread root: `params: {delete_descendants}`).
- Request: flags in the query string OR a JSON body; truthy values `"true"|"1"|"yes"` (case-insensitive):
  - `delete_descendants` — also soft-delete editable descendants (own nodes and LLM nodes you own). Without it only the target is tombstoned.
  - `delete_orphaned_prompt` — also tombstone the system-prompt root if nothing else stays alive under it (re-checked under a row lock).
- Response 200: `{ scheduled: Int (nodes tombstoned), grace_days: Int (SOFT_DELETE_GRACE_DAYS), orphaned_prompt_deleted: Int | null (root id if taken), deleted_pinned_ids: [Int] }`.
- Side effects: soft delete (`deleted_at` set; purge after grace days by a Celery beat task). Any published Share of this node is marked revoked. Public cache invalidated.
- Errors: 403 `{"error":"Not authorized"}`; 404 `{"error":"Not found or not authorized"}` (race); 404 (HTML) missing; 500 `{"error":"Error deleting node","details":String}`.
- Frontend: toasts "Deleted N nodes"; Log removes every card whose thread root is the target or whose id is in `deleted_pinned_ids`; NodeDetail navigates to the nearest alive ancestor, else to `/log` (or `/commons` for a public root).

##### `GET /api/nodes/<int:node_id>/children`  (unused by web)
- Backend: routes/nodes.py:get_children
- Not used by the web. Do not use from the app.
- Response 200: `{ children: [{ id, preview: String (200 chars + "..."), child_count: Int, node_type }] }`.

##### `POST /api/nodes/<int:node_id>/link`  (unused by web)
- Backend: routes/nodes.py:add_linked_node
- Auth: login.
- Request JSON: `{ linked_node_id: Int (required), content?: String (default "") }`.
- Response 201: `{ message: "Linked node added", node: { id, content, node_type: "link", linked_node_id, created_at, username } }`. Note: privacy/ai_usage are left at column defaults (`private`, `none`).
- Errors: 400 `{"error":"linked_node_id is required"}`; 404 `{"error":"Linked node not found"}`; 410 `{"error":"Linked node has been deleted"}`; parent guard 400/404/410 as in create; 500 `{"error":"DB error adding linked node"}`.

---

#### 2.2.2 Threads, quotes, titles

##### `PUT /api/nodes/<int:node_id>/thread-name`
- Backend: routes/nodes.py:set_thread_name
- Auth: login + owner (`human_owner_id ?? user_id == me`).
- Called from: `Log.js` (rename card), `RenameThreadDialog` flow. Target = the card's `thread_root_id` (or its `id`).
- Request JSON: `{ thread_name: String | null }`. Empty/whitespace/null clears the name. Max 120 chars after trimming.
- Response 200: `{ thread_name: String | null }` (the stored, trimmed name).
- Errors: 403 `{"error":"Only the owner can rename this thread"}`; 400 `{"error":"Only a thread root can be named"}` (node has a parent); 400 `{"error":"thread_name must be a string"}`; 400 `{"error":"Thread name is limited to 120 characters","max_length":120}`; 404 (HTML).
- Notes: does not bump the node's `updated_at`. The name is shown on Log cards as `thread_name` (Log endpoint, other section).

##### `GET /api/nodes/<int:node_id>/resolve-quotes`
- Backend: routes/nodes.py:resolve_node_quotes
- Auth: login + `can_user_access_node` on the node.
- Called from: `NodeDetail.js` when the focal node's content contains `{quote:ID}` or `{quote_ext:ID}` markers (regex `\{quote(?:_ext)?:\d+\}`).
- Request: none.
- Response 200:
  ```
  {
    quotes: { "<nodeId>": QuotedNode | null },          // keys are strings
    external_quotes: { "<itemId>": QuotedExternal | null },
    has_quotes: Bool
  }
  QuotedNode (alive, accessible): { id, content: String, username: String, user_id: Int,
                                    created_at: ISO, node_type, ai_usage }
  QuotedNode (deleted, viewer had access): { id, deleted: true, content: null, username, user_id,
                                             created_at, node_type, ai_usage }
  null = missing or not accessible → render "[Quoted node inaccessible]"-style text

  QuotedExternal (owner = node's human owner): { id, content: String, source: String, author_handle: String|null,
      title: String|null, url: String|null, posted_at: ISO|null, user_id: Int, read_at: ISO|null,
      feedback: String|null,
      // added only when the viewer is the owner AND the node recommended this item (FeedPick row):
      feedback_shared?: Bool, rated_before?: { feedback: String, at: ISO } | null, recommendation_id?: Int }
  null = item missing or not owned by the node's owner
  ```
  With no markers: `{ quotes: {}, external_quotes: {}, has_quotes: false }`.
- Errors: 403 `{"error":"Not authorized to access this node"}`; 404 (HTML).
- Frontend: failures are silent (quotes just don't render). Only the focal node's quotes are resolved; ancestors/children with quotes show the raw marker or are resolved elsewhere.

##### `GET /api/nodes/titles?ids=1,2,3`
- Backend: routes/nodes.py:get_node_titles
- Auth: login.
- Called from: `utils/nodeLinks.js` (batches every `https://loore.org/node/<id>` link rendered in markdown bodies within one tick into one request; results cached per page session).
- Request query: `ids` = comma-separated ints; duplicates dropped; first 50 kept.
- Response 200: `{ titles: { "<id>": { id: Int, title: String } | { id: Int, deleted: true, title: null } | null } }`. Title = the thread's user-given name if the node is a named root, else the first line of content stripped of markdown markers. `null` = missing or not visible (render as "[Node inaccessible]").
- Errors: none specific (empty/invalid ids → `{ titles: {} }`).

---

#### 2.2.3 Models

##### `GET /api/nodes/models`
- Backend: routes/nodes.py:get_models
- Auth: login.
- Called from: `ModelSelector.js`, `AdminPanel.js`.
- Response 200: `{ models: [{ id: String, name: String, provider: "openai"|"anthropic", featured: Bool, read: Bool }] }` — active (non-deprecated) models in config order (newest first within provider). `featured` = short list; `read` = may be used for a Read (Read picker lists only these).

##### `GET /api/nodes/default-model?purpose=chat|read`
- Backend: routes/nodes.py:get_default_model
- Auth: login.
- Called from: `ModelSelector.js` when there is no node context.
- Response 200: `{ suggested_model: String, source: "user_preference" | "default" }` for chat (`preferred_model` then `LLM_NAME`); `{ suggested_model, source: "default" }` for read (`READ_DEFAULT_MODEL`).
- Errors: 400 `{"error":"purpose must be 'chat' or 'read'"}`.

##### `GET /api/nodes/<int:node_id>/suggested-model?purpose=chat|read`
- Backend: routes/nodes.py:get_suggested_model
- Auth: login.
- Called from: `ModelSelector.js` with `nodeId`.
- Response 200: `{ suggested_model: String, source: "predecessor" | "user_preference" | "default" }`. `predecessor` = the closest earlier non-read LLM reply's model (chat) or closest earlier read's model (read).
- Errors: 400 bad purpose; 404 (HTML).
- Frontend rule: applies the suggestion once per node when `source == "predecessor"` or when the currently selected model is not in the offered list.

---

#### 2.2.4 LLM generation (async task)

##### `POST /api/nodes/<int:node_id>/llm`
- Backend: routes/nodes.py:request_llm_response
- Auth: login + `require_spend_headroom` + owner of the PARENT node (`human_owner_id ?? user_id == me`).
- Called from: `NodeDetail.js` (`requestLlmFor`: "LLM Response" button, auto-generate after reply/edit; body `{model: selectedModel, source_mode: "textmode"}`), `NodeForm.js` (top-level auto-generate; body `{source_mode: "textmode"}`, no model).
- Request JSON:
  - `model?: String` — a `SUPPORTED_MODELS` key. Absent → `resolve_chat_model` (predecessor → user preference → `LLM_NAME`). A deprecated model is silently replaced by the chat default; under a Read thread a non-read model is replaced by the read model.
  - `source_mode?: "voice" | "textmode"`.
- Response 202: `{ message: "LLM response generation started", task_id: String (Celery id), status: "pending", node_id: Int (the NEW placeholder LLM node) }`.
- Side effects: creates a child LLM node under `node_id` (content `"[LLM response generation pending...]"`, `node_type: "llm"`, `llm_task_status: "pending"`, inherits the parent's `privacy_level` and `ai_usage`; `ai_usage` "train" is downgraded to "chat" for read turns), enqueues `generate_llm_response`.
- Errors: 403 `{"error":"You can only request an AI response on your own nodes. Reply first, then generate on your reply."}`; 400 `{"error":"Invalid source_mode: <v>"}`; 400 `{"error":"Unsupported model: <id>","supported_models":[String]}`; 410 `{"error":"Parent node not found" | "Parent node has been deleted"}`; 400 `{"error":<message>}` for a misconfigured `{user_export}`/`{ca_tweets}` placeholder or plan gate (the web toasts it); 402 spend cap; 404 (HTML).
- Frontend: navigates to the new node with `?awaitLlm=<node_id>`; 402 → silent (banner); other errors → toast with `error`.
- Follow-up: poll `GET /api/nodes/<node_id>/llm-status` and/or subscribe `GET /api/sse/nodes/<node_id>/llm-stream`.

##### `GET /api/nodes/<int:node_id>/llm-status`
- Backend: routes/nodes.py:get_llm_status
- Auth: login + (`can_user_access_node` or admin).
- Called from: `NodeDetail.js` via `useAsyncTaskPolling` (interval 2000 ms, max 30 min; for a batch read 15000 ms, max 25 h), `useVoiceSession.js` (interval 1500 ms), `ProposalInline.js` (every 2000 ms after "apply todo" until `propose_todo.apply_status` is completed/failed), `VoicePage.js` (once, to get `tool_calls_meta`).
- Response 200 (header `Cache-Control: no-cache, no-store, must-revalidate`):
  ```
  {
    node_id: Int,
    status: String | null,            // llm_task_status
    progress: Int,                    // 0-100
    error: String | null,             // user-facing failure text
    task_info: Object | null,         // Celery PROGRESS meta when state == PROGRESS
    continuation_node_id: Int | null, // answer continues on another node (retrieval chaining #158)
    tts_task_status: String | null,
    tts_streaming: Bool,              // TTS is being spoken while the reply is written (tts_task_id == "voice-stream")
    content?: String,                 // present when status is "completed" or "cancelled"
    tool_calls_meta?: [ToolCallMeta],
    stage?: "batch",                  // present while a batch read waits at the provider
    batch_submitted_at?: String,      // with stage (no "Z", UTC)
    feed_picks_count?: Int,           // batch read completed
    warnings: [String],               // always present; toasts (useLlmTaskWarnings)
    node?: { id, content, node_type, llm_model, created_at }   // only when this request observed Celery SUCCESS
  }
  ```
- Errors: 403 `{"error":"Unauthorized"}`; 404 (HTML).
- Notes: terminal statuses for the poller: `completed`, `failed`, `cancelled`. When `continuation_node_id` is set, follow it.

---

#### 2.2.5 Audio, TTS, transcription

Audio URLs returned by these endpoints are site-relative (`/media/...`). The web prefixes them with `REACT_APP_BACKEND_URL` (e.g. `https://loore.org`). See `GET /media/<path>` (§2.2.8).

##### `GET /api/nodes/<int:node_id>/audio`
- Backend: routes/nodes.py:get_audio_urls
- Auth: login. If the node is not public: `current_user.has_voice_mode` required (admin, or plan in `{"alpha","pro"}`).
- Called from: `SpeakerIcon.js` (play; `validateStatus` accepts 200 and 404), `DownloadAudioIcon.js`.
- Response 200: `{ original_url: String | null, tts_url: String | null, has_audio_chunks: Bool }` — URLs only if the file (or `.enc`) exists on disk. `tts_url` may carry a cache-busting query (`tts.mp3?v=123`).
- Response 202: `{ status: "generating", message: "TTS generation in progress", progress: Int, task_id: String }` when no audio exists and TTS is pending/processing.
- Errors: 403 `{"error":"Voice mode not enabled for this account"}`; 404 `{"error":"No audio available for this node"}`.
- Web playback order: `has_audio_chunks` → `GET audio-chunks` queue; else `original_url`; else `tts_url` (+ `tts-chapters`); else (voice-mode users only) `POST tts` and subscribe to the TTS SSE stream.

##### `GET /api/nodes/<int:node_id>/audio-chunks`
- Backend: routes/nodes.py:get_audio_chunks
- Auth: as `/audio`.
- Called from: `SpeakerIcon.js`.
- Response 200: `{ chunks: [{ url: String ("/media/nodes/<uid>/<nid>/chunk_0000.webm" or ".mp4"), duration: Float (seconds, via ffprobe; 300.0 fallback) }] }` in playback order. Only for nodes recorded with streaming transcription.
- Errors: 403 voice mode; 404 `{"error":"Not a streaming transcription node"}`; 404 `{"error":"No audio chunks found"}`.
- Notes: slow for encrypted chunks (each is decrypted to a temp file to measure duration). iOS recordings are `.mp4` (AAC); desktop are `.webm` (Opus) — AVPlayer cannot play WebM/Opus, so for WebM chunks use `audio-download?format=mp3` instead.

##### `GET /api/nodes/<int:node_id>/audio-download?format=original|mp3`
- Backend: routes/nodes.py:download_audio
- Auth: as `/audio`.
- Called from: `DownloadAudioIcon.js` via `fetch(<backend>/api/nodes/<id>/audio-download?format=mp3, {credentials: "include"})`.
- Request query: `format` = `original` (default; `webm` is a deprecated alias) or `mp3`.
- Response 200: binary file, `Content-Disposition: attachment; filename="node-<id>-recording.<ext>"`. `original`: merged chunks in the source container (`audio/webm` or `audio/mp4`). `mp3`: re-encoded, `audio/mpeg`. Synchronous ffmpeg work (can take many seconds for long recordings); no Range support (Flask `send_file` of a temp file).
- Errors: 400 `{"error":"Unsupported format, use 'original' or 'mp3'"}`; 403 `{"error":"Voice mode not enabled"}`; 404 `{"error":"Not a streaming transcription node"}` / `{"error":"No audio files found"}`; 500 `{"error":"Failed to convert to MP3"}`.
- Notes: only for streaming-transcription nodes. For single-file recordings use `original_url` from `/audio`.

##### `POST /api/nodes/<int:node_id>/tts`
- Backend: routes/nodes.py:generate_tts
- Auth: login + `voice_mode_required` (401 `{"error":"Authentication required"}` / 403 `{"error":"Voice mode not enabled for this account"}`) + `require_spend_headroom`.
- Called from: `SpeakerIcon.js`, `useVoiceSession.js` (voice turn; idempotent re-fire by a watchdog), `useStreamingTTS.js`.
- Request: empty body.
- Responses:
  - 200 `{ message: "TTS already available", tts_url: String }` — play it directly.
  - 202 `{ message: "TTS generation already in progress", task_id, status: "pending"|"processing", node_id }` — no new task.
  - 202 `{ message: "TTS generation started", task_id: String, status: "pending", node_id: Int }` — enqueued `generate_tts_audio`.
- Errors: 409 `{"error":"Original audio exists – TTS not required"}`; 500 `{"error":"TTS not configured (missing API key)"}`; 402 spend cap; 404 (HTML).
- Follow-up: subscribe to `GET /api/sse/nodes/<id>/tts-stream` (events `chunk_ready` with `audio_url`, `duration`, `section_index`, and `all_complete` with `tts_url`), with `GET tts-status` as fallback. The web waits for this POST to return before opening the SSE (else the SSE may see no pending task).
- Frontend: 402 → reset silently; other errors → error state with `error`. A network-level failure (no HTTP response) in voice mode is retried by the watchdog because the endpoint is idempotent.
- Note: the web hides the speaker for `ai_usage == "none"` nodes.

##### `GET /api/nodes/<int:node_id>/tts-status`
- Backend: routes/nodes.py:get_tts_status
- Auth: login + (`can_user_access_node` or admin).
- Called from: `SpeakerIcon.js` via `useAsyncTaskPolling` (2000 ms; fallback path), `useVoiceSession.js` (reconcile loop, request timeout 10000 ms).
- Response 200 (no-cache headers): `{ node_id: Int, status: String | null, progress: Int, task_info: Object | null, node?: { id: Int, audio_tts_url: String | null } }`. `node` present only when `status == "completed"`. On Celery FAILURE, `task_info = { error: String (≤200 chars, scrubbed) }` (status may still read pending/processing in the DB).
- Errors: 403 `{"error":"Unauthorized"}`; 404 (HTML).

##### `GET /api/nodes/<int:node_id>/tts-chapters`
- Backend: routes/nodes.py:get_tts_chapters
- Auth: login + (`can_user_access_node` or admin).
- Called from: `SpeakerIcon.js` (after the first streamed chunk, on each new section, on completion, and when playing an existing `tts_url`; accepts 200/404).
- Response 200: `{ chapters: [{ section_index: Int, title: String ("Introduction" when untitled), chunk_index: Int, start_time: Float (seconds, cumulative) }] }`. Empty list when the text has fewer than two markdown sections.
- Errors: 403 `{"error":"Unauthorized"}`; 404 (HTML).

##### `GET /api/nodes/<int:node_id>/transcription-status`
- Backend: routes/nodes.py:get_transcription_status
- Auth: login + (`can_user_access_node` or admin).
- Called from: `NodeForm.js` via `useAsyncTaskPolling` after an audio upload (default 2000 ms).
- Response 200 (no-cache headers): `{ node_id, status: String|null, progress: Int, error: String|null, started_at: ISO|null, completed_at: ISO|null, content: String|null (only when completed), task_info: Object|null }`.
- Errors: 403 `{"error":"Unauthorized"}`; 404 (HTML).

##### Node-level streaming transcription (unused by web; the web records through `/api/drafts/streaming/*` instead)

`POST /api/nodes/streaming/init` — login + spend headroom. JSON `{ parent_id?, node_type? ("user"), privacy_level? ("private"), ai_usage? ("none"), chunk_interval_ms? (300000, informational) }`. 201 `{ node_id: Int, session_id: String (uuid4), sse_url: "/api/sse/nodes/<id>/transcription-stream" }`. Creates a node with `transcription_status: "processing"`, `streaming_transcription: true`. Errors: 400 invalid privacy/ai_usage; parent guard 400/404/410; 402.

`POST /api/nodes/<int:node_id>/audio-chunk` — login + author (`user_id == me`). multipart: `chunk` (file), `chunk_index` (int, 0-based), `session_id`. Stored as `chunk_NNNN.webm` regardless of real container. 202 `{ chunk_index, task_id, status: "processing" }`; re-upload of a non-failed chunk: 200 `{ message: "Chunk already uploaded", chunk_index, status }`. Errors: 403 `{"error":"Unauthorized"}`; 400 `Node is not in streaming transcription mode` / `Missing chunk file` / `Missing chunk_index` / `Missing session_id` / `Invalid chunk_index` / `Session ID mismatch`; 404 `Streaming session not found`.

`POST /api/nodes/<int:node_id>/finalize-streaming` — login + author. JSON `{ session_id: String, total_chunks: Int }`. 202 `{ message: "Finalization started", task_id, node_id, total_chunks }`. Errors: 403; 400 not-streaming / `Missing session_id` / `Missing total_chunks` / `Session ID mismatch`.

`GET /api/nodes/<int:node_id>/streaming-status` — login + (author or admin). 200 (no-cache) `{ node_id, session_id, transcription_status, streaming_transcription: Bool, total_chunks: Int|null, completed_chunks: Int, failed_chunks: Int, chunks: [{ chunk_index, status, text: String|null (completed only), error: String|null (failed only) }], content: String|null (completed only) }`. Errors: 403; 400 not-streaming.

---

#### 2.2.6 Chunked upload of large audio files

Used by `NodeForm.js` for audio files larger than 10 MB (`utils/chunkedUpload.js`). Smaller files go through multipart `POST /api/nodes/`.

Client algorithm (`uploadFileInChunks`):
1. `chunkSize = 5 * 1024 * 1024` bytes; `total_chunks = ceil(file.size / chunkSize)`; `upload_id = "<Date.now()>-<9 random base36 chars>"` (any unique string; it becomes a directory name, so keep it `[A-Za-z0-9-]`).
2. `POST /api/nodes/upload/init` → `node_id`.
3. For each chunk sequentially: `POST /api/nodes/upload/chunk` multipart, request timeout 120 s, up to 3 attempts with backoff 1 s, 2 s (then fail).
4. `POST /api/nodes/upload/finalize` → node info; then poll `transcription-status` for `node.id`.
5. On any error: `POST /api/nodes/upload/cleanup` (best effort), rethrow. The placeholder node created by init is NOT deleted by cleanup.

##### `POST /api/nodes/upload/init`
- Backend: routes/nodes.py:init_chunked_upload
- Auth: login + `require_spend_headroom` (402 before any node exists).
- Request JSON: `{ filename: String (required, allowed extension), filesize: Int (required, bytes, ≤ 200 MB), total_chunks: Int (required), upload_id: String (required), parent_id?: Int, node_type?: String ("user"), privacy_level?: String ("private"), ai_usage?: String ("none") }`. Web sends `parent_id, node_type: "user", privacy_level, ai_usage`.
- Response 201: `{ node_id: Int, upload_id: String }`. Creates a placeholder node (`"[Voice note – upload in progress]"`, `transcription_status: "pending"`).
- Errors: 400 `{"error":"Missing required fields"}`; 415 `{"error":"Unsupported file type"}`; 413 `{"error":"File too large"}`; 400 invalid privacy/ai_usage; parent guard 400/404/410; 402.

##### `POST /api/nodes/upload/chunk`
- Backend: routes/nodes.py:upload_chunk
- Auth: login + `node.user_id == me`.
- Request multipart: `chunk` (file part, the raw byte slice), `chunk_index` (0-based int as string), `upload_id`, `node_id`.
- Response 200: `{ message: "Chunk received", chunk_index: Int, uploaded_chunks: Int (count so far), total_chunks: Int }`. Idempotent per index (re-sending overwrites).
- Errors: 400 `{"error":"Missing chunk file"}` / `{"error":"Missing required fields"}` / `{"error":"Invalid chunk_index or node_id"}`; 403 `{"error":"Unauthorized"}`; 404 `{"error":"Upload session not found"}`; 404 (HTML) unknown node.

##### `POST /api/nodes/upload/finalize`
- Backend: routes/nodes.py:finalize_chunked_upload
- Auth: login + `node.user_id == me`.
- Request JSON: `{ upload_id: String, node_id: Int }`.
- Response 200: shape D "Audio create" (ignore its `content`, see bug note). Reassembles to `original.<ext>`, encrypts it, sets `audio_mime_type = "audio/<ext>"`, enqueues `transcribe_audio`, deletes the chunk directory. A size mismatch is logged, not rejected.
- Errors: 400 `{"error":"Missing required fields"}` / `{"error":"Invalid node_id"}`; 403 `{"error":"Unauthorized"}`; 404 `{"error":"Upload session not found"}`; 400 `{"error":"Incomplete upload","missing_chunks":[Int]}` (the client may re-send those chunks and finalize again).

##### `POST /api/nodes/upload/cleanup`
- Backend: routes/nodes.py:cleanup_chunked_upload
- Auth: login (scoped to the caller's own chunk directory).
- Request JSON: `{ upload_id: String }`.
- Response 200: `{ message: "Cleanup successful" }` or `{ message: "Nothing to clean up" }`.
- Errors: 400 `{"error":"Missing upload_id"}`; 500 `{"error":"Cleanup failed"}`.

---

#### 2.2.7 Pins and Read feed picks

##### `POST /api/nodes/<int:node_id>/pin`
- Backend: routes/nodes.py:pin_node
- Auth: login + owner (`human_owner_id ?? user_id`).
- Called from: `NodeDetail.js` (shown when owner and `privacy_level != "private"`).
- Response 200: `{ message: "Node pinned", pinned_at: ISO }`.
- Errors: 403 `{"error":"Only the owner can pin this node"}`; 400 `{"error":"Cannot pin a private node"}`; 400 `{"error":"Node is already pinned"}`; 404 (HTML).
- Frontend: sets `node.pinned_at` from the response; on error shows `error`.

##### `DELETE /api/nodes/<int:node_id>/pin`
- Backend: routes/nodes.py:unpin_node
- Auth: login + owner.
- Response 200: `{ message: "Node unpinned" }` (idempotent).
- Errors: 403 `{"error":"Only the owner can unpin this node"}`; 404 (HTML).

##### `GET /api/nodes/<int:node_id>/feed-picks`
- Backend: routes/nodes.py:get_feed_picks
- Auth: login + owner (`human_owner_id ?? user_id`).
- Called from: `FeedPicks.js` (a Community Archive "Read" reply's list of recommended tweets).
- Response 200:
  ```
  { node_id: Int,
    picks: [{
      rank: Int, relevance: Int | null, recommended: Bool, picked_by: String | null, why: String | null,
      item: {  // ExternalItem list shape (routes/external.py:_serialize_item) plus extras
        id, source: String, external_id: String, author_handle: String|null, title: String|null,
        preview: String (280 chars + "…"), url: String|null, posted_at: ISO|null, fetched_at: ISO,
        read_at: ISO|null, feedback: String|null, feedback_at: ISO|null, edited_at: ISO|null,
        has_tts: Bool, surfaced_count: Int, last_surfaced_at: ISO|null,
        content: String,                                   // full text
        feedback_shared: Bool, rated_before: { feedback, at } | null, recommendation_id: Int
        // note: `feedback` is overwritten with the verdict that counts for THIS pick
      }
    }] }   // ordered by rank ascending
  ```
- Errors: 403 `{"error":"Unauthorized"}`; 404 (HTML).
- Frontend: on error shows "Could not load the picks."; opening a pick calls `POST /api/external/items/<id>/read` with `{node_id, via: "open"}` (external section).

##### `POST /api/nodes/<int:node_id>/feed-picks/read`
- Backend: routes/nodes.py:mark_feed_picks_read
- Auth: login + owner.
- Called from: `FeedPicks.js` ("Mark all as read"), `ReadReply.js` (`ReadReplyTail`).
- Request: empty body.
- Response 200: `{ node_id: Int, read_at: { "<itemId>": ISO } }` — for every pick; already-read items keep their original `read_at`.
- Errors: 403 `{"error":"Unauthorized"}`; 404 (HTML).

---

#### 2.2.8 Media files

##### `GET /media/<path:filename>`
- Backend: routes/media.py:serve_media (production path: nginx proxies `/media/` to Flask with `proxy_buffering off`).
- Auth: the app sends the session cookies with every media request (see `docs/IOS-APP-DESIGN.md` §9.3).
- Called from: the web audio player (`<audio src>` / queue) with URLs from `/audio`, `/audio-chunks`, `tts_url`, SSE `chunk_ready.audio_url`; `DownloadAudioIcon` via `fetch`.
- Response: plain file on disk → Flask `send_file` (Werkzeug conditional/Range support, mimetype guessed from extension). Encrypted file (`<path>.enc` on disk; URLs never include `.enc`) → decrypted in memory and served with `Accept-Ranges: bytes`, `Content-Disposition: inline; filename="<name>"`, `Content-Length`; a `Range: bytes=start-end` header returns 206 with `Content-Range`. MIME map for decrypted files: `.mp3 audio/mpeg`, `.webm audio/webm`, `.wav audio/wav`, `.m4a audio/mp4`, `.ogg audio/ogg`, `.flac audio/flac`, anything else (including `.mp4`) `application/octet-stream`.
- Errors: 404 `{"error":"File not found"}`; 500 `{"error":"Encrypted file found but encryption is disabled"}`; 500 `{"error":"Failed to decrypt file: ..."}`.
- iOS notes: pass the session cookies to AVPlayer (`AVURLAssetHTTPCookiesKey`). Decrypted `.mp4` chunks come back as `application/octet-stream`; AVPlayer may need the file extension (it is in the URL) or a download-then-play fallback. WebM/Opus is not playable by AVFoundation.

##### `GET /api/nodes/media/<path:filename>`  (unused by web; test helper)
- Backend: routes/nodes.py:serve_audio_file
- Test helper. Do not use.

---

#### 2.2.9 Routes the web frontend never calls

| Method + path | Function | Note |
|---|---|---|
| `GET /api/nodes/<id>/children` | get_children | not used by the web; do not use |
| `POST /api/nodes/<id>/link` | add_linked_node | legacy "link" nodes |
| `POST /api/nodes/streaming/init` | init_streaming_transcription | superseded by `/api/drafts/streaming/*` |
| `POST /api/nodes/<id>/audio-chunk` | upload_audio_chunk | superseded by drafts streaming |
| `POST /api/nodes/<id>/finalize-streaming` | finalize_streaming_transcription | superseded by drafts streaming |
| `GET /api/nodes/<id>/streaming-status` | get_streaming_status | superseded by drafts streaming |
| `GET /api/nodes/media/<path>` | serve_audio_file | test helper; do not use |

Everything else in this section is called by the web (`/media/<path>` indirectly, through audio URLs).

#### 2.2.10 Things a Swift client must not assume

- `GET /api/nodes/<id>` returns the full subtree with no pagination; very large threads produce very large responses.
- Different endpoints name the author differently: `user {id, username}` on the focal node, `username` + `user_id` on ancestors/children/quotes.
- `ancestors` skips privacy-blocked ancestors, so do not treat `ancestors[0]` as the true root.
- Soft-deleted nodes appear as tombstones (`deleted: true`, no `content`) in `ancestors` and `children`; decode `content` as optional in tree types.
- Several 404s come from `get_or_404` and return HTML, not JSON.
- `POST /api/nodes/` with multipart and `POST /api/nodes/upload/finalize` return the stored (possibly encrypted) `content` column; do not display it.
- `/audio`, `/tts`, `/audio-chunks`, `/audio-download` check voice-mode entitlement but not node access; `/media/*` checks nothing. The app should only request audio for nodes it has fetched through an access-checked endpoint.

### 2.3 Voice, recording, drafts, Text mode, Read and SSE

Scope: `backend/routes/drafts.py` (`/api/drafts`), `textmode.py` (`/api/textmode`), `voice.py` (`/api/voice`), `read.py` (`/api/read`), `sse.py` (`/api/sse`). Every route here is `@login_required` (cookie session; unauthenticated `/api/*` → `401 {"error":"Unauthorized"}`; logged-in but unapproved → `403 {"error":"Your account is not approved. Please wait for approval."}`). A route marked "cap-checked" returns the standard spend-cap body `402 {"error":"monthly_spend_limit_reached","message":"You've reached your monthly usage limit for the free alpha. It resets at the start of next month."}`; any route that calls `create_llm_placeholder` can also raise it (global `SpendCapExceeded` handler → same 402), unless the route catches it (noted per route).

Common vocabulary:
- `privacy_level`: `"private" | "circles" | "public"` everywhere except `POST /api/textmode/start`, which validates against `{"private","anonymous","public"}` (see §5.1).
- `ai_usage`: `"none" | "chat" | "train"`.
- `model`: a key of `Config.SUPPORTED_MODELS` (dotted ids, e.g. `"claude-opus-4.6"`, `"gpt-6-luna"`); unknown → `400 {"error":"Unsupported model: <id>"}`. When omitted, the server picks: thread ancestry → `user.preferred_model` → `LLM_NAME` (`pick_model_for_generation`).
- `NODE_CHAR_CAP`: per-entry char limit; over it → `422 {"error":"Content exceeds the N-character per-entry limit.","char_cap":N}`.
- Timestamps in this area are ISO-8601 UTC strings with a trailing `Z` (drafts use `isoformat()+"Z"`, textmode uses `iso_utc`).
- `task_id` values are Celery task ids (UUID strings). The client never polls a task id directly; it polls the node (`GET /api/nodes/<id>/llm-status`, `/tts-status`, owned by the nodes map) or the draft session (`/status` below).

Draft `streaming_status` values: `"recording"` → `"finalizing"` → `"completed"` (or `"failed"`). Transcript chunk `status` values: `"stored"` (on disk, not yet sent to transcription), `"processing"` (in a Celery batch), `"pending"` (legacy), `"completed"`, `"failed"`.

---

#### 2.3.1 Drafts (typed-text autosave)

A "draft" is the server copy of unsent form text, keyed by `(user, node_id)` when editing a node, or `(user, parent_id)` / `(user, top-level)` when composing. Drafts that belong to agent proposals (labels `todo_pending`, `github_issue_pending`, `feedback_pending`, `share_pending`) are never returned or touched by these three routes.

##### `GET /api/drafts/`
- Backend: routes/drafts.py:get_draft
- Auth: login; `node_id` requires edit rights on the node (owner or `human_owner_id`).
- Called from: hooks/useDraft.js (on form mount), used by NodeForm.
- Request: query `node_id?: int`, `parent_id?: int` (neither = top-level composing draft). Note the trailing slash: `/api/drafts/?parent_id=12`. Without the slash Flask answers 308 to the slashed URL.
- Response 200:
  ```
  { "id": int, "content": string, "node_id": int|null, "parent_id": int|null,
    "created_at": iso, "updated_at": iso,
    "parent_deleted"?: true, "warning"?: string,        // parent soft-deleted: parent_id is nulled
    "session_id"?: string, "has_stored_chunks"?: bool }  // draft came from a recording
  ```
  Excludes drafts already consumed by the server-side voice chain (`llm_node_id` set) or carrying a `streaming_warning`. Returns the most recently updated match.
- Errors: `404 {"error":"No draft found"}` (normal, useDraft treats 404 as "no draft"); `404 {"error":"Node not found"}` (node missing or soft-deleted); `403 {"error":"Not authorized to access drafts for this node"}`.
- Notes: when `session_id` and `has_stored_chunks` are present, NodeForm runs recovery: `POST .../transcribe-remaining`, then polls `GET .../status` every 3 s (5-minute cap) and fills the form with `content`.

##### `POST /api/drafts/`
- Backend: routes/drafts.py:save_draft
- Auth: login; `node_id` requires edit rights.
- Called from: hooks/useDraft.js (debounced 1000 ms after typing; also direct saves from NodeForm).
- Request JSON: `{ "content": string, "node_id"?: int, "parent_id"?: int }` (upsert on the same key as GET).
- Response 200: `{ "id", "content", "node_id", "parent_id", "created_at", "updated_at" }` (same types as GET, no session fields).
- Errors: `404 {"error":"Node not found"}` / `404 {"error":"Parent node not found"}`; `410 {"error":"Node has been deleted"}` / `410 {"error":"Parent node has been deleted"}`; `403 {"error":"Not authorized to edit this node"}`.

##### `DELETE /api/drafts/`
- Backend: routes/drafts.py:delete_draft
- Auth: login; `node_id` requires edit rights.
- Called from: hooks/useDraft.js `deleteDraft` (after a successful save/submit or explicit discard). 404 is ignored by the client.
- Request: query `node_id?`, `parent_id?` (same key as GET).
- Response 200: `{ "message": "Draft deleted" }`
- Errors: `404 {"error":"No draft found"}`, `404 {"error":"Node not found"}`, `403`.

---

#### 2.3.2 Streaming recording sessions (`/api/drafts/streaming/...`)

A recording is a Draft row with a `session_id` (UUID string). Audio is uploaded in ~15-second chunks while the user speaks; the server stores them encrypted under `data/audio/drafts/<user_id>/<session_id>/` and transcribes them in batches of 20 stored chunks (about 5 minutes of audio) with `gpt-4o-transcribe`. The transcript accumulates in the draft's `content`. No node exists until the session is saved (`save-as-node`, `POST /api/voice`) or the finalize task's server-side Voice chain creates one.

##### `GET /api/drafts/interrupted`
- Backend: routes/drafts.py:get_interrupted_drafts
- Auth: login (only the caller's drafts).
- Called from: hooks/useInterruptedRecovery.js on VoicePage mount; VoicePage shows RecoveryBanner for `res.data[0]`.
- Request: none.
- Response 200: JSON array, newest first; drafts with `streaming_status=="recording"`, a session, no `llm_node_id`, at least one chunk row, and an edit target that is not soft-deleted:
  ```
  [ { "id": int, "session_id": string, "parent_id": int|null, "label": string|null,
      "content": string, "chunk_count": int, "has_stored_chunks": bool,
      "streaming_mime_type": "audio/webm"|"audio/mp4"|null,
      "created_at": iso, "updated_at": iso,
      "parent_deleted"?: true, "warning"?: string } ]
  ```
- Errors: none specific (empty list when nothing to recover).
- Notes: `chunk_count` is the number of chunk rows, which the web client uses as the next `chunk_index` when resuming. If an earlier chunk never reached the server, that index is lower than `max(chunk_index)+1` and the first resumed chunk collides with an existing row (answered "already uploaded" and its audio is dropped). A native client should take `max(chunks[].chunk_index)+1` from `GET .../status` instead.

##### `POST /api/drafts/streaming/init`
- Backend: routes/drafts.py:init_streaming
- Auth: login; cap-checked (`@require_spend_headroom` → 402 before the mic opens).
- Called from: hooks/useStreamingTranscription.js `initSession` (used by StreamingMicButton in NodeForm, and by useVoiceSession for Voice mode).
- Request JSON: `{ "parent_id"?: int|null, "privacy_level"?: string = "private", "ai_usage"?: string = "none", "label"?: string }`. `label` is `"Voice"` from Voice mode, absent from the form mic. The values are stored on the draft and used when the node is created later. Side effect: first deletes the caller's stale streaming drafts already turned into nodes.
- Response 201:
  ```
  { "session_id": string(uuid), "draft_id": int,
    "sse_url": "/api/sse/drafts/<session_id>/transcription-stream" }
  ```
- Errors: 402 spend cap (web: toast "record" message, back to idle, `err.spendCapped`).

##### `POST /api/drafts/streaming/<session_id>/audio-chunk` (FILE UPLOAD)
- Backend: routes/drafts.py:upload_streaming_chunk
- Auth: login; the session must belong to the caller.
- Called from: hooks/useStreamingTranscription.js `uploadChunkWithRetry` (axios, `timeout: 600000`), plus a `navigator.sendBeacon` copy when the page is hidden.
- Request: `multipart/form-data`:
  - `chunk`: file part, the raw MediaRecorder blob; web filename `chunk_<index>.webm|.mp4` (the server ignores the filename).
  - `chunk_index`: decimal string, 0-based, strictly increasing per recorder.
  - `mime_type`: e.g. `"audio/webm;codecs=opus"`, `"audio/mp4"`, `"audio/mp4;codecs=mp4a.40.2"`. Only the family before `;` matters and it must be `audio/webm` or `audio/mp4` (default `audio/webm` when missing). Chunk 0 locks the session family.
- Response 202:
  ```
  { "chunk_index": int, "status": "stored", "task_id"?: string, "batch_queued"?: true }
  ```
  `task_id`/`batch_queued` appear when this upload brought the stored count to 20 and a transcription batch was queued.
  Response 200 (duplicate, already stored/processing/completed): `{ "message": "Chunk already uploaded", "chunk_index": int, "status": string }`. A chunk whose previous copy `failed` is re-stored.
- Errors:
  - `404 {"error":"Streaming session not found"}`; `404 {"error":"Streaming session directory not found"}`.
  - `400 {"error":"Streaming session is not active"}` (status not recording/finalizing).
  - `400 {"error":"Missing chunk file"}`, `{"error":"Missing chunk_index"}`, `{"error":"Invalid chunk_index"}`, `{"error":"Unsupported audio mime: <family>"}`.
  - `400 {"error":"Chunk mime 'x' does not match session mime 'y'","code":"mime_mismatch"}`.
  - `400 {"error":"Could not parse init segment from first chunk","detail":string,"code":"init_parse_failed"}` (chunk 0 only). The web client treats `code=="init_parse_failed"` as fatal: no retry, recorder stopped, session state `error`, message "Your audio recording could not be processed…".
  - 5xx on KMS/disk failure (retried by the client).
- Client retry policy (web): up to 5 attempts, backoff 2 s, 4 s, 8 s, 16 s; chunks still failing go to a queue re-sent on the browser `online` event. Uploads run concurrently (each chunk its own promise); the server accepts any arrival order.
- Audio format contract (important for a native recorder):
  - Chunk 0 must begin with the container's init segment followed by media: WebM (EBML/Segment/Tracks, then Clusters) or fragmented MP4 (`ftyp` + `moov`, then `moof`+`mdat`). The server extracts and stores the init bytes; the MP4 walker raises `init_parse_failed` if it meets `moof`/`mdat` before `moov`, so a non-fragmented `.m4a` whose `moov` sits at the end will be rejected.
  - Chunks 1..N are raw continuation fragments of the same stream (WebM Clusters or `moof`+`mdat`), which the server byte-concatenates after the init segment and remuxes with ffmpeg.
  - A chunk N>0 whose bytes 4..8 are `ftyp` (or starts with the WebM magic) is treated as a new "subsession" (a restarted recorder): its init is stored separately and it is transcribed separately from the chunks before it. So restarting the encoder mid-session (after an interruption) is allowed, but every chunk being a standalone file would transcribe each 15 s clip on its own.
  - On iOS, the natural producer is `AVAssetWriter` with `outputFileTypeProfile = .mpeg4AppleHLS` and `preferredOutputSegmentInterval` ≈ 15 s (initialization segment + separable `moof`/`mdat` segments), sending init+first segment as chunk 0 and each later segment as its own chunk with `mime_type=audio/mp4`. This has to be verified against the backend's ffmpeg remux with a real device recording before relying on it.
- Size: nginx allows 200 MB bodies; 15 s of Opus/AAC is tens of KB.

##### `GET /api/drafts/streaming/<session_id>/status`
- Backend: routes/drafts.py:get_streaming_status
- Auth: login; own session.
- Called from: useStreamingTranscription (every 5 s while `finalizing`, and immediately on page foreground: the fallback for a dropped SSE); useInterruptedRecovery (every 2 s during recovery, 5-minute cap); NodeForm draft recovery (every 3 s).
- Response 200:
  ```
  { "session_id": string, "draft_id": int,
    "streaming_status": "recording"|"finalizing"|"completed"|"failed",
    "streaming_mime_type": "audio/webm"|"audio/mp4"|null,
    "total_chunks": int|null,            // set by finalize
    "completed_chunks": int, "failed_chunks": int,
    "chunks": [ { "chunk_index": int, "status": string,
                  "text": string|null,   // only when completed
                  "error": string|null } ],  // only when failed
    "content": string,                   // assembled transcript so far
    "llm_node_id"?: int,                 // Voice chain created the reply
    "warning"?: string }                 // reply skipped (spend cap, bad placeholder…)
  ```
- Errors: `404 {"error":"Streaming session not found"}`.
- Side effect: if the draft is still `recording`, has chunks, and none are stored/processing/pending, this GET flips it to `completed` (how an abandoned recording's recovery finishes).
- Notes: the web "finalizing" poll treats `streaming_status=="completed" && content` as done and reads `llm_node_id`/`warning`. See risk R1 below: after the SSE `all_complete` of a Voice turn the server deletes the draft, so this endpoint then returns 404.

##### `POST /api/drafts/streaming/<session_id>/finalize`
- Backend: routes/drafts.py:finalize_streaming
- Auth: login; own session; draft must be `recording`. For a capped user the Voice reply is skipped with a warning.
- Called from: useStreamingTranscription `stopStreaming` (axios `timeout: 120000`), after the recorder's last `ondataavailable` and after all pending uploads settle.
- Request JSON: `{ "total_chunks": int (required; = highest chunk_index + 1), "label"?: "Voice", "parent_id"?: int, "model"?: string }`. When `label` is `"Reflect"|"Orient"|"Voice"` and `model` is missing, the server resolves one.
- Response 202: `{ "message": "Finalization started", "task_id": string, "draft_id": int, "total_chunks": int }`
- Errors: `404 {"error":"Streaming session not found"}`, `400 {"error":"Streaming session is not in recording state"}`, `400 {"error":"Missing total_chunks"}`.
- Side effects: sets status `finalizing`; queues Celery `finalize_draft_streaming`, which waits for the remaining chunks, transcribes the rest, writes the final `content`, then:
  - `label=="Voice"` with a model and a non-empty transcript: creates (a) a system node with the `voice` prompt when `parent_id` is absent, (b) the user node holding the transcript (audio moved onto it; very long transcripts split into a chain), (c) an LLM placeholder under the tip with `tts_task_status="pending"`; sets `draft.llm_node_id` and `streaming_status="completed"` in one commit; enqueues generation. TTS is dispatched per node by the generation task. Spend cap / refused placeholder / deleted parent → no reply, `draft.streaming_warning` set, status `completed`.
  - Any other label: status `completed`; the client then saves via `save-as-node`.
  - Anthropic prompt-cache pre-warm for long Voice recordings (server-internal).

##### `POST /api/drafts/streaming/<session_id>/transcribe-remaining`
- Backend: routes/drafts.py:transcribe_remaining
- Auth: login; own session.
- Called from: useInterruptedRecovery `handleRecover` (only when `has_stored_chunks`), NodeForm draft recovery.
- Request: no body.
- Response 202: `{ "task_id": string, "chunk_count": int, "chunk_indices": [int] }`. Response 200 when nothing is stored: `{ "message": "No stored chunks to transcribe", "chunk_count": 0 }`.
- Errors: `404 {"error":"Streaming session not found"}`.
- Notes: then poll `GET .../status` until `completed`/`failed` (the auto-complete rule in `/status` finishes a `recording` draft once no chunk is pending).

##### `POST /api/drafts/streaming/<session_id>/save-as-node`
- Backend: routes/drafts.py:save_streaming_as_node
- Auth: login; own session; draft `streaming_status` must be `completed` or `finalizing`.
- Called from: NodeForm submit (recorded top-level entry or reply), WritePage (`{content, agentic:true, auto_generate}`), NodeDetail reply under a Read reply (`{content, agentic:true, auto_generate, model}`), useStreamingTranscription `saveAsNode` (`{content}`).
- Request JSON: `{ "content"?: string (defaults to the draft transcript; send the user-edited text), "agentic"?: bool=false, "auto_generate"?: bool=false, "model"?: string }`. Privacy and AI usage come from the draft (set at init).
- Response 201:
  ```
  { "id": int, "user_node_id": int,     // same value: the head node
    "tip_id": int,                      // last node when the text was split into a chain
    "content": string, "parent_id": int|null,
    "privacy_level": string, "ai_usage": string, "created_at": iso,
    "conversation_id"?: int,            // the textmode prompt/system node (agentic)
    "llm_node_id"?: int, "task_id"?: string,   // auto_generate reply placeholder
    "spend_capped"?: true,              // auto_generate skipped by the cap
    "llm_error"?: string }              // reply skipped: bad placeholder / parent deleted
  ```
- Errors: `404 {"error":"Streaming session not found"}`, `400 {"error":"Streaming session is not complete"}`, `400 {"error":"agentic / auto_generate require ai_usage of 'chat' or 'train'"}`, `400 {"error":"Unsupported model: x"}`, `404 {"error":"Parent node not found"}`.
- Side effects: node created with `transcription_status="completed"`, `streaming_transcription=true`; audio moved to the node; draft deleted. The web client dispatches the spend-cap banner itself when `spend_capped` is true (it is a 201, not a 402), then navigates to the node with `?awaitLlm=<llm_node_id>`.

##### `DELETE /api/drafts/streaming/<session_id>/discard`
- Backend: routes/drafts.py:discard_streaming_draft
- Auth: login; own session.
- Called from: useInterruptedRecovery `handleDiscard` (RecoveryBanner "Discard"); useStreamingTranscription after a mic-permission denial left an empty session.
- Response 200: `{ "message": "Draft discarded" }`
- Errors: `404 {"error":"Streaming session not found"}`.
- Side effects: deletes chunk rows, the audio directory, and the draft.

---

#### 2.3.3 Voice mode (`/api/voice`)

##### `POST /api/voice/`
- Backend: routes/voice.py:create_voice_session
- Auth: login; `parent_id` must be owned (`human_owner_id`).
- Called from: useVoiceSession `onComplete` fallback only, when the transcription finished without a server-created `llm_node_id` (the normal path lets `finalize` build the turn). Note the trailing slash; the web posts to `/voice` and relies on the 308 redirect.
- Request JSON: `{ "content": string (required), "model"?: string, "parent_id"?: int, "session_id"?: string, "ai_usage"?: string }`. `ai_usage` is read only for a new thread (else inherited from the thread, looking through a Read). `session_id` moves that recording's audio onto the new user node and deletes its draft.
- Response 202: `{ "parent_id": int, "user_node_id": int, "llm_node_id": int, "task_id": string }` (`parent_id` = the node the user node hangs under: the new system node or the given parent).
- Errors: `400 {"error":"Content is required"}`, `404 {"error":"Parent node not found"}`, `403 {"error":"Unauthorized"}`, `400 {"error":"Unsupported model: x"}`, `400 {"error":"<placeholder validation message>"}` (web toasts `error`), 402 spend cap (global handler).
- Notes: all nodes created here are `privacy_level="private"`.

##### `POST /api/voice/from-node/<node_id>`
- Backend: routes/voice.py:create_voice_from_node
- Auth: login; node must be owned (`human_owner_id`).
- Called from: NodeDetail `handleSessionFromNode('voice')` (the "Voice" button on a thread). Web then navigates to `/voice?resume=<llm_node_id>&parent=<parent_id>[&fresh=1]`.
- Request JSON: `{ "model"?: string }`
- Response: always `mode:"processing"`; four cases by (agentic prompt above node?, node is an LLM reply?):
  - prompt + user node → 202 `{ "mode":"processing", "llm_node_id": new, "parent_id": new, "fresh": true }` (reply generation starts under the node).
  - prompt + LLM node → 200 `{ "mode":"processing", "llm_node_id": node_id, "parent_id": node_id }` (play the existing reply).
  - no prompt + user node → 202 as the first case, with a `voice` system node inserted under the node.
  - no prompt + LLM node → 200 `{ "mode":"processing", "llm_node_id": node_id, "parent_id": <new system node id> }`.
- Errors: `404 {"error":"Node not found"}`, `403 {"error":"Unauthorized"}`, `400 {"error":"Unsupported model: x"}`, 402 (web ignores the body; banner shows).
- Notes: VoicePage then polls `GET /api/nodes/<llm_node_id>/llm-status` (1500 ms) and triggers TTS on completion, exactly like a live turn.

##### `GET /api/voice/timing/clock`
- Backend: routes/voice.py:voice_timing_clock
- Called from: utils/voiceTiming.js `measureClock` (3 round trips, keeps the lowest RTT).
- Response 200: `{ "t": float }` (server epoch seconds).

##### `POST /api/voice/timing`
- Backend: routes/voice.py:report_voice_timing
- Called from: utils/voiceTiming.js `markPlaying`, once per turn when the first reply audio plays. Diagnostics only; failures are ignored.
- Request JSON: `{ "node_id": int (the turn's first reply node), "marks": { stage: ms_epoch_client }, "offset_ms": number (server − client), "rtt_ms": number }`. Accepted stage names: `rec_stop, finalize_acked, llm_node_known, tts_attach, chunk_ready, autoplay_blocked, play_pressed, playing`; others are ignored.
- Response 200: `{ "node_id": int, "marks": {stage: epoch_s}, "facts": {name: string}, "stages": {name: seconds} }` (backend marks joined in).
- Errors: `404 {"error":"Node not found"}` (missing/not owned/non-int), `400 {"error":"Bad clock values"}`, `400 {"error":"Bad marks"}`.
- Notes: optional for a native client; useful to compare native vs web latency (#371).

##### `GET /api/voice/timing`
- Backend: routes/voice.py:list_voice_timing. Not called by the frontend (developer view).
- Request: `?limit=int` (default 20, max 50).
- Response 200: `{ "stages": {name: "from -> to"}, "median": {name: seconds}, "turns": [record as above] }`.

---

#### 2.3.4 Text mode (`/api/textmode`)

##### `POST /api/textmode/start`
- Backend: routes/textmode.py:start_conversation
- Auth: login. Handles the spend cap inline (no 402).
- Called from: WritePage (typed entry: `{content, privacy_level, ai_usage, auto_generate}`), NodeForm agentic top-level entry, App.js.
- Request JSON: `{ "content": string (required), "model"?: string, "ai_usage"?: "chat"|"train" (default user.default_ai_usage), "privacy_level"?: string (default user.default_privacy_level or "private"), "auto_generate"?: bool = true }`
- Response 202 (all variants):
  ```
  { "conversation_id": int,           // system node carrying the textmode prompt (thread root)
    "user_node_id": int,
    "llm_node_id"?: int, "task_id"?: string,   // when a reply was queued
    "spend_capped"?: true,            // auto_generate on but user capped: nodes kept, no reply
    "llm_error"?: string }            // reply refused ({user_export} validation); nodes kept
  ```
- Errors: `400 {"error":"Content is required"}`, `422 {..., "char_cap"}`, `400 {"error":"Invalid privacy_level: x"}`, `400 {"error":"Invalid ai_usage: x"}`, `400 {"error":"Text mode requires ai_usage of 'chat' or 'train'"}` (the web routes `ai_usage=="none"` entries to `POST /api/nodes/` instead), `400 {"error":"Unsupported model: x"}`.
- Notes: web then navigates to `/node/<user_node_id>?awaitLlm=<llm_node_id>` and NodeDetail polls/streams the reply.

##### `POST /api/textmode/from-node/<node_id>`
- Backend: routes/textmode.py:continue_from_node
- Auth: login; `human_owner_id` must be the caller.
- Called from: NodeDetail `submitReadReplyMessage` (the reply box under a Read reply, typed path).
- Request JSON: `{ "content": string (required), "model"?: string, "auto_generate"?: bool = true, "ai_usage"?: "chat"|"train" }`. Privacy comes from the node.
- Response 202:
  ```
  { "prompt_node_id": int|null,   // textmode prompt inserted under the node, or null inside an agentic thread
    "user_node_id": int,
    "llm_node_id"?: int, "task_id"?: string, "spend_capped"?: true, "llm_error"?: string }
  ```
- Errors: 404 (`get_or_404`, HTML-less default body), `403 {"error":"Unauthorized"}`, `400 {"error":"Content is required"}`, 422 char cap, `400 {"error":"Invalid ai_usage: x"}`, `400 {"error":"Text mode requires ai_usage of 'chat' or 'train'"}`, `400 {"error":"Unsupported model: x"}`. The spend cap is handled inline (`spend_capped`), not as a 402.
- Notes: `ParentDeletedError` from `create_llm_placeholder` is not caught here and would surface as a 500. Web navigates to `/node/<user_node_id>?awaitLlm=<llm_node_id>`.

##### `GET /api/textmode/from-node/<node_id>`
- Backend: routes/textmode.py:get_conversation_from_node. Not called by the frontend.
- Response 200: `{ "conversation_id": int (root id), "messages": [ { "id", "role": "user"|"assistant", "content", "created_at", "llm_model": string|null, "llm_task_status": string|null, "tool_calls_meta"?: [..] } ] }` (root excluded, chronological).
- Errors: 404, `403 {"error":"Unauthorized"}`.

##### `POST /api/textmode/<conversation_id>/message`
- Backend: routes/textmode.py:add_message. Not called by the frontend (the web replies through `/api/nodes/` + `/api/nodes/<id>/llm`).
- Request JSON: `{ "content": string, "parent_id": int (required, must descend from conversation_id), "model"?: string }`.
- Response 202: `{ "user_node_id", "llm_node_id", "task_id" }` or `{ "user_node_id", "llm_error" }`.
- Errors: 400 content/parent_id/"Invalid parent_id"/"parent_id does not belong to this conversation"/model, 422, 403, 404, 402.

---

#### 2.3.5 Read (Community Archive picks; admin-only PoC)

All three routes return `403 {"error":"{ca_tweets} (the Community Archive feed) is not available on your account yet."}` unless `user.is_admin`. The web shows the Read card only when `user.is_admin`. Every node a read creates has `ai_usage="chat"`. Replies run as provider Batch API jobs (minutes to a day); the reply node stays `processing` until the batch returns, and the thread page polls it.

##### `POST /api/read/start`
- Backend: routes/read.py:start_read
- Called from: HomePage `startRead` (`{auto_generate}` from localStorage `loore_auto_generate`, default true).
- Request JSON: `{ "model"?: string (must be a model flagged "read"), "auto_generate"?: bool = true }`
- Response 202: `{ "prompt_node_id": int, "llm_node_id"?: int, "task_id"?: string }` (only `prompt_node_id` when `auto_generate` is false). Web navigates to `/node/<llm_node_id || prompt_node_id>`.
- Errors: 403 (not admin), `400 {"error":"Reads run on <names>; not on <model>."}`, `400 {"error":"Reading the archive needs AI usage of 'chat' or 'train'."}` (user default is none), `400 {"error":<placeholder message>}`, 402.

##### `POST /api/read/from-node/<node_id>`
- Backend: routes/read.py:start_read_from_node
- Called from: NodeDetail `handleReadFromNode` (`{model: readModel||undefined, auto_generate}`).
- Request JSON: `{ "model"?: string, "auto_generate"?: bool = true }`
- Response 202: outside a read thread, as `/read/start` (the `read_thread` prompt attached under the node); inside a read thread ("read further"): `{ "llm_node_id": int, "task_id": string }` (auto_generate ignored).
- Errors: 403 (not admin / not owner `{"error":"Unauthorized"}`), `404 {"error":"Node not found"}`, `400 {"error":"AI usage is off for this thread."}`, 400 model/placeholder, 402.

##### `POST /api/read/<node_id>/rerun`
- Backend: routes/read.py:rerun_read
- Called from: NodeDetail `rerunRead(live)` (admin button), then `GET /api/nodes/<id>`.
- Request JSON: `{ "live"?: bool = false }` (true: live API instead of batch).
- Response 202: `{ "llm_node_id": int, "task_id": string, "live": bool, "cancelled_batches": [string] }`
- Errors: 403, `404 {"error":"Node not found"}`, `400 {"error":"Not a read reply."}`, `409 {"error":"This reply is finished; start a new read instead."}`, `400 {"error":"The reply has no model."}`.

---

#### 2.3.6 Server-Sent Events (`/api/sse/...`)

General facts for all six endpoints:
- Auth is the session cookie only (EventSource `withCredentials: true`); there is no token or query-string auth. A native client sends the same cookies (`HTTPCookieStorage` handles this for `URLSession`). Same 401/403 JSON as other `/api` routes.
- Pre-stream failures and "nothing to stream" answers are ordinary JSON responses (`Content-Type: application/json`) with status 200/400/403/404. A native client must check the status code and `Content-Type` before treating the body as an event stream.
- Stream response: `200`, `Content-Type: text/event-stream`, headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`. nginx has a dedicated `location /api/sse/` with `proxy_buffering off`, `proxy_read_timeout 7200s`.
- Wire format (from `format_sse_message`): every message is exactly two lines plus a blank line: `event: <name>\n` then `data: <json>\n\n`. The JSON is produced by `json.dumps` and is always a single line. No `id:` fields, no `retry:` field, no comments, no unnamed events. So `Last-Event-ID` is not supported; resumption uses the `last_chunk` query parameter where the endpoint has one.
- Parsing with `URLSession.bytes(for:).lines`: that sequence drops empty lines, so the blank-line event terminator is not visible. Because each event is exactly one `event:` line followed by one `data:` line, dispatch when a `data:` line arrives, using the last seen `event:` name. Also set a long `timeoutIntervalForRequest` (heartbeats arrive every 15 s) and keep the request off the default 60 s timeout.
- Implementation: each stream is a server loop polling the database every 1 s (0.5 s for llm-stream) inside a gevent worker. Heartbeat `event: heartbeat` every ~15 s. After a terminal event (`all_complete`, `done`, `error`, `close`) the server ends the response. A browser EventSource would auto-reconnect after that; the web client prevents it by disabling the hook, and a native client should simply stop.
- Server-side lifetime caps (then `event: close` `{"message":"Connection timeout"}`): node transcription 600 s; TTS streams and llm-stream 1800 s; draft transcription 7200 s.

##### `GET /api/sse/drafts/<session_id>/transcription-stream`
- Backend: routes/sse.py:draft_transcription_stream
- Auth: draft owner (or admin).
- Called from: useSSE.js `useDraftTranscriptionSSE`, enabled while the recording session is `recording` or `finalizing`.
- Query: `last_chunk?: int = -1` (chunks with index ≤ this are not re-sent). The web adds it only after a reconnect.
- Pre-stream errors: `404 {"error":"Streaming session not found"}`, `403 {"error":"Unauthorized"}`.
- Events:
  - `chunk_complete` `{ "chunk_index": int, "text": string, "status": "completed" }` (per transcribed chunk; chunks are transcribed in batches of up to 20, so these arrive in bursts about every 5 minutes while recording and once more after finalize; the first chunk of a batch carries the whole batch's text and the others carry `""`, so build the transcript from `content_update`, not from these).
  - `chunk_error` `{ "chunk_index": int, "error": string|null, "status": "failed" }`
  - `content_update` `{ "content": string, "completed_chunks": int }` (the whole assembled transcript; sent on connect, then whenever it changes; the web replaces its text with `content`).
  - `all_complete` `{ "message": "Transcription complete", "content": string, "draft_id": int, "llm_node_id"?: int, "warning"?: string }` then the stream ends. When `llm_node_id` or `warning` is present the server deletes the draft just before sending this event.
  - `error` `{ "error": "Draft not found" }` or `{ "message": "Transcription failed" }` then end.
  - `heartbeat` `{ "timestamp": float, "completed_chunks": int, "total_chunks": int|null, "status": string }`
  - `close` `{ "message": "Connection timeout" }` (after 2 h).
- Web reconnect: on `EventSource.CLOSED` the base hook reconnects after 3000 ms; on a drop the URL is rebuilt with `?last_chunk=<highest seen>`; a stale-connection check (every 10 s) forces a reconnect when no event arrived for 45 s. While `finalizing`, the web also polls `GET /api/drafts/streaming/<id>/status` every 5 s and on foreground, because iOS kills EventSources in the background without an error event.

##### `GET /api/sse/nodes/<node_id>/tts-stream`
- Backend: routes/sse.py:tts_stream (shared generator `_tts_stream_generator`)
- Auth: node owner, admin, or anyone who can read the node (`can_user_access_node`).
- Called from: useSSE.js `useTTSStreamSSE` (entityType `node`), used by useVoiceSession (voice replies) and SpeakerIcon (the play button on a node).
- Query: `last_chunk?: int = -1`. The web never sends it (it replays from 0 and deduplicates by `chunk_index`).
- Pre-stream answers: if `tts_task_status` is not `pending|processing|completed`: `200 {"status":"completed","tts_url":string}` when the node has audio, else `400 {"error":"TTS not in progress for this node"}`; `403 {"error":"Unauthorized"}`; 404.
- Events:
  - `chunk_ready` `{ "chunk_index": int, "audio_url": string, "status": "ready", "duration"?: float (seconds), "section_index"?: int, "section_title"?: string|null }`. `audio_url` is a relative `/media/...` path (cache-busting query included); prefix the backend origin and fetch with the session cookie.
  - `all_complete` `{ "message": "TTS generation complete", "tts_url": string|null, "continuation_node_id": int|null, "preview": string (first 200 chars of the node text) }` then end. `tts_url` is the full-file audio. `continuation_node_id` non-null means the turn continues on another node (within-turn tool chain): the voice client advances to that node, polls its llm-status, and appends its audio to the same queue.
  - `error` `{ "message": "TTS generation failed" }` or `{ "error": "Node not found" }` then end.
  - `heartbeat` `{ "timestamp": float, "completed_chunks": int, "total_chunks": int, "progress": int|null }`
  - `close` after 1800 s.
- A completed node's stream replays every chunk after `last_chunk` and then `all_complete` (so reconnecting after a cut is safe).
- Web recovery (useVoiceSession, #242): every 7 s and on foreground, while a voice turn is undelivered, `GET /api/nodes/<id>/tts-status`; if it reports `completed` for 3 s while the SSE is still silent, either force an SSE reconnect (some chunks already arrived) or play `node.audio_tts_url` in full; if the `POST /api/nodes/<id>/tts` produced no answer for 20 s, re-send it (idempotent: 200 with `tts_url` if already generated, 202 if running). A 60 s safety net moves the UI to playback if no chunk arrives after the reply completed.

##### `GET /api/sse/profiles/<profile_id>/tts-stream`
- Backend: routes/sse.py:profile_tts_stream. Same events and pre-stream answers as the node stream, except `all_complete` has only `message` and `tts_url` (no `continuation_node_id`/`preview`).
- Auth: profile owner or admin.
- Called from: SpeakerIcon on ProfilePage (entityType `profile`).

##### `GET /api/sse/items/<item_id>/tts-stream`
- Backend: routes/sse.py:item_tts_stream. Same events as the profile stream.
- Auth: the item must belong to the caller (`404 {"error":"not found"}` otherwise).
- Pre-stream: `200 {"status":"completed","tts_url"}` / `400 {"error":"TTS not in progress for this reference"}`.
- Called from: SpeakerIcon on ReferenceDetailPage (entityType `item`).

##### `GET /api/sse/nodes/<node_id>/llm-stream`
- Backend: routes/sse.py:llm_stream (`STREAMING_REPLIES`, on by default)
- Auth: `can_user_access_node` or admin; `403 {"error":"Unauthorized"}`.
- Called from: useSSE.js `useLlmTextStream`, used by NodeDetail while a reply node is `pending`/`processing` (seeded with the node's `streaming_content`). Voice mode does not use it.
- Query: none.
- Events:
  - `snapshot` `{ "text": string }` the whole text so far; sent first (once there is any text) and whenever the new text is not an extension of what was sent (the model call restarted). Replace the displayed text.
  - `delta` `{ "text": string }` text appended since the last event. Append.
  - `done` `{ "status": "completed"|"failed"|"cancelled"|null, "continuation_node_id": int|null, "error": string|null }` then end. The final content must be read from the node (`GET /api/nodes/<id>` or `/llm-status`), not assembled from deltas.
  - `error` `{ "error": "Node not found" }` (missing or soft-deleted) then end.
  - `heartbeat` `{ "timestamp": float }`; `close` after 1800 s.
- If the node is already finished on connect, the first event is `done`.

##### `GET /api/sse/nodes/<node_id>/transcription-stream`
- Backend: routes/sse.py:transcription_stream. Legacy node-based streaming transcription. The hook `useTranscriptionSSE` exists in useSSE.js but no component mounts it; the current recording flow is draft-based.
- Auth: node owner or admin. Pre-stream `400 {"error":"Streaming transcription not enabled for this node"}`.
- Query: `last_chunk?: int`.
- Events: `chunk_complete` / `chunk_error` (as the draft stream), `all_complete` `{ "message", "content" }`, `error` `{ "message":"Transcription failed", "error": string }`, `heartbeat` `{ "timestamp", "completed_chunks", "total_chunks" }`, `close` after 600 s.

##### `useStreamingTTS.js`
Wraps `useTTSStreamSSE` for `StreamingAudioPlayer`, which no component imports (dead code). Ignore for the port.

---

#### 2.3.7 SSE event reference

| Endpoint | Event | Payload fields | Meaning / client action |
|---|---|---|---|
| drafts/<sid>/transcription-stream | `chunk_complete` | chunk_index, text, status="completed" | a chunk (or batch member) transcribed; track max index for `last_chunk` |
| same | `chunk_error` | chunk_index, error, status="failed" | chunk transcription failed (web plays an error tone) |
| same | `content_update` | content, completed_chunks | replace the live transcript with `content` |
| same | `all_complete` | message, content, draft_id, llm_node_id?, warning? | recording done; Voice: start the reply flow on `llm_node_id`, or toast `warning`; form mic: hold `session_id` for save-as-node; stop |
| same | `error` | error or message | failed; stop |
| same | `heartbeat` | timestamp, completed_chunks, total_chunks, status | keep-alive / stale detection |
| nodes/<id>/tts-stream | `chunk_ready` | chunk_index, audio_url, status="ready", duration?, section_index?, section_title? | enqueue audio chunk (dedupe on chunk_index) |
| same | `all_complete` | message, tts_url, continuation_node_id, preview | all audio generated; advance to continuation node if non-null; stop |
| same | `error` | message or error | TTS failed; stop |
| same | `heartbeat` | timestamp, completed_chunks, total_chunks, progress | keep-alive |
| profiles/<id>/tts-stream, items/<id>/tts-stream | `chunk_ready`, `all_complete` (message, tts_url), `error`, `heartbeat` | as node stream | profile / saved-reference narration |
| nodes/<id>/llm-stream | `snapshot` | text | replace reply text |
| same | `delta` | text | append to reply text |
| same | `done` | status, continuation_node_id, error | generation ended; fetch final node; stop |
| same | `error` | error | node gone; stop |
| same | `heartbeat` | timestamp | keep-alive |
| nodes/<id>/transcription-stream (legacy) | `chunk_complete`, `chunk_error`, `all_complete` (message, content), `error`, `heartbeat` | see 2.3.6 | unused by the web app |
| all | `close` | message="Connection timeout" | server lifetime cap reached; reconnect if still needed |

---

#### 2.3.8 Native call sequences

**A. Voice turn (Voice mode, one round)**
1. (Optional, on screen open) `GET /api/drafts/interrupted`; if non-empty, offer resume/discard (sequence C).
2. `POST /api/drafts/streaming/init` `{ parent_id: <thread parent or null>, privacy_level:"private", ai_usage: user.default_ai_usage, label:"Voice" }` → `session_id`. On 402 stop and show the cap message.
3. Start the recorder (fMP4 or WebM, see the format contract). Open `GET /api/sse/drafts/<sid>/transcription-stream`.
4. Every ~15 s: `POST .../audio-chunk` multipart `{chunk, chunk_index, mime_type}`; retry with backoff; keep failed chunks for a later retry. Stop the session on `code:"init_parse_failed"`.
5. On stop: finalize the last segment, wait for all uploads, then `POST .../finalize` `{ total_chunks: last_index+1, label:"Voice", parent_id?, model? }` (send `model` = user's preferred model when set).
6. Wait for `all_complete` on the SSE; in parallel poll `GET .../status` every 5 s and on app foreground (take `streaming_status=="completed"` with `llm_node_id`/`warning`).
   - `warning` present → show it; the transcript was saved as a node without a reply; back to ready.
   - `llm_node_id` present → go to step 7.
   - neither (label not Voice, empty transcript, or server chain failed) → fallback `POST /api/voice/` `{ content, model?, ai_usage?, parent_id?, session_id }` → `llm_node_id`.
7. Poll `GET /api/nodes/<llm_node_id>/llm-status` every 1500 ms.
   - If `tts_streaming` is true while still processing (STREAMING_VOICE_TTS on; the server speaks the reply while it is written): open `GET /api/sse/nodes/<llm_node_id>/tts-stream` now.
   - When `status=="completed"`: if the TTS stream is not open yet, `POST /api/nodes/<llm_node_id>/tts`; 200 with `tts_url` → play the full file; 202 → open the tts-stream. Empty `content` → no audio; end the turn.
   - `status=="failed"` → show `error`; back to ready.
8. Play `chunk_ready` audio in order. On `all_complete` with `continuation_node_id` → set `llm_node_id` to it and repeat step 7 for that node, appending to the same queue. Otherwise the turn is over; the next turn's `parent_id` is the final reply node id.
9. Recovery while undelivered: every ~7 s and on foreground, `GET /api/nodes/<id>/tts-status`; if TTS is completed but the stream is silent, reconnect the SSE (with `?last_chunk=<highest received>` to avoid replays) or play `node.audio_tts_url`.
10. Optional: `GET /api/voice/timing/clock` ×3 then `POST /api/voice/timing` once the first audio plays.

Starting Voice from an existing thread: `POST /api/voice/from-node/<node_id>` `{model?}` → treat `llm_node_id` as step 7's node (a new reply is being generated when `fresh`, else play the existing one), and use `parent_id` as the thread parent for the next recording.

**B. Recording a written entry or reply (form mic)**
1. `POST /api/drafts/streaming/init` `{ parent_id?, privacy_level, ai_usage }` (no label). The web disables the mic when `ai_usage=="none"`.
2. Record and upload chunks as in A.3–A.4; show `content_update.content` appended to any text typed before recording started (web joins with a blank line).
3. `POST .../finalize` `{ total_chunks }` (no label/model).
4. Wait for `all_complete` (or `/status` `completed`). Keep `session_id`. Save the combined text as a typed draft too (`POST /api/drafts/` with `parent_id`) so it survives a restart.
5. On submit: `POST /api/drafts/streaming/<sid>/save-as-node` `{ content: <edited text>, agentic?, auto_generate?, model? }`:
   - top-level entry with AI on: `agentic: true` when Agentic Reply is on, `auto_generate: true` when Auto-generate is on;
   - reply under a Read reply: `{ content, agentic: true, auto_generate, model }`;
   - other replies: `{ content }` only, then request the reply separately via the nodes API (`POST /api/nodes/<id>/llm`), as NodeDetail does.
6. `DELETE /api/drafts/?parent_id=…` to clear the typed draft. If the response has `spend_capped: true`, show the cap banner. Open the new node; if `llm_node_id` is present, poll/stream it.

**C. Interrupted-recording recovery**
1. `GET /api/drafts/interrupted` → take the first entry.
2. Resume: `GET /api/drafts/streaming/<sid>/status` to learn the highest `chunk_index` and `streaming_mime_type`; start a new encoder in the same family; upload the first new chunk at `max_index+1` (it carries its own init segment, so the server opens a subsession); continue as A.3–A.8 (use the entry's `parent_id` as the thread parent). No `init` call and no spend-cap check on this path.
3. Transcribe what is there without recording more: if `has_stored_chunks`, `POST .../transcribe-remaining`; poll `GET .../status` every 2 s (5-minute cap) until `completed`/`failed`; then treat it like B.4–B.5 (the draft keeps its session; save with `save-as-node`, or post the text via `POST /api/voice/` with `session_id` for a Voice turn).
4. Discard: `DELETE /api/drafts/streaming/<sid>/discard`.
5. Also: `GET /api/drafts/?parent_id=…` returns `session_id` + `has_stored_chunks` for a form draft that came from an unfinished recording; run step 3 for it (NodeForm does this automatically).

---

#### 2.3.9 Risks and quirks found in this area

- R1. Lost Voice reply id: when a Voice turn's draft stream yields `all_complete`, the server deletes the draft first. If that event never reaches the client (connection killed during backgrounding), `GET .../status` then returns 404 and the `llm_node_id` is unrecoverable from this API; the web client keeps polling a 404 every 5 s. A native client needs a fallback, e.g. read the thread (`parent_id`'s children via the nodes API) to find the new user node and its reply. Opening a fresh SSE with `last_chunk` does not help (404).
- R2. Audio container: the backend accepts only WebM or fragmented MP4 whose chunk 0 has `moov` before the first `moof`/`mdat`. `AVAudioRecorder` output is not suitable as chunk 0; `AVAssetWriter` HLS-fMP4 segments should be, but need a real-device test through the ffmpeg remux and `gpt-4o-transcribe`.
- R3. Resume index: the web resumes at `chunk_count` (row count), which can collide with an existing index after a lost chunk; use `max(chunk_index)+1`.
- R4. SSE termination: EventSource-style auto-reconnect after a normal server close would re-open finished streams (the web avoids it by disabling hooks). The TTS stream replays from `last_chunk`; the web never sends `last_chunk` for TTS and dedupes instead.
- R5. `URLSession` line iteration drops the blank event separators; dispatch on `data:` lines (safe here because every event is exactly `event:` + one `data:` line).
- R6. Each open SSE holds a gevent greenlet polling Postgres every 0.5–1 s; a native client should keep at most the streams the web opens (one transcription stream while recording, one TTS stream per playing node, one llm-stream per visible pending reply) and close them on terminal events.
- R7. `POST /api/textmode/from-node/<id>` does not catch `ParentDeletedError` (would be a 500 if the node is soft-deleted between checks).
- R8. `POST /api/voice/` and `POST /api/drafts/` are routed at `/` of their blueprints: call them with the trailing slash (`/api/voice/`, `/api/drafts/`) to avoid a 308 redirect, which `URLSession` follows for POST but only when the body can be re-sent.

---

#### 2.3.10 Routes in these files the frontend never calls

| Route | Notes |
|---|---|
| `GET /api/textmode/from-node/<id>` | conversation chain as role/content messages |
| `POST /api/textmode/<conversation_id>/message` | add a message + reply inside a textmode conversation |
| `GET /api/voice/timing` | per-user latency records and medians (diagnostics) |
| `GET /api/sse/nodes/<id>/transcription-stream` | legacy node-based transcription stream (hook defined, never mounted) |

#### 2.3.11 Backend tests that pin this area
- `test_streaming_save_as_node.py`: save-as-node flags `agentic` / `auto_generate` / `model`, response fields, ai_usage=none refusal.
- `test_spend_cap_recording.py`: 402 at streaming init; a started recording is always transcribed; Voice reply skipped with `warning`.
- `test_textmode.py`: `/textmode/start` validation and node creation, `/message` auth and descendant check, `/from-node` auth.
- `test_voice_from_node.py`: the four-case matrix of `POST /voice/from-node`.
- `test_voice_timing.py`: timing marks, record join, medians.
- `test_read_routes.py`, `test_read_turns.py`: Read entry points, admin gate, read-further, model rules.
- `test_llm_stream_sse.py`: snapshot/delta decision of the llm-stream.
- `test_tts_stream_sse.py`: tts-stream of a completed node replays remaining chunks then `all_complete`.
- `test_live_streaming.py`: replies (and voice TTS) while written, `tts_streaming`.

### 2.4 Account, workspace, search, updates (dashboard / profile / todo / artifacts / prompts / log / search / terms / feedback / github / updates / health)

Source files: `backend/routes/{dashboard,profile,todo,artifacts,prompts,log,search,terms,feedback,github_issues,updates,health}.py`, plus `GET /api/export/profile-progress` from `export_data.py` (it is the profile-generation poller, so it is documented here too).

#### Conventions that apply to every endpoint in this section

- **Timestamps** are strings from `iso_utc()` (`backend/utils/timefmt.py`): naive UTC values get a `Z` suffix, e.g. `"2026-09-30T12:34:56.123456Z"`. Microseconds are present when non-zero. `null` for missing values. Parse with an ISO8601 formatter that accepts fractional seconds and also accepts none. Exceptions: changelog `date` is a date only (`"2026-09-28"`), and the Log `next_cursor` embeds a naive `isoformat()` without `Z` (treat it as opaque).
- **Unauthenticated**: every route here except health is `@login_required`. For paths under `/api` the unauthorized handler returns `401 {"error": "Unauthorized"}` (no redirect).
- **Unapproved (waitlisted) users**: a global `before_request` returns `403 {"error": "Your account is not approved. Please wait for approval."}` for every `/api` call EXCEPT: any `GET /api/dashboard...`, `PUT /api/dashboard/user...`, `POST`/`DELETE` on `/api/dashboard/email` and `/api/dashboard/email/...`, and anything under `/api/terms`. Note that `PATCH /api/dashboard/timezone` and `DELETE /api/dashboard/x` are NOT exempt, so they 403 for a waitlisted user (the web app fires the timezone PATCH anyway and ignores the failure).
- **Spend cap**: endpoints decorated `@require_spend_headroom`, and any path that raises `SpendCapExceeded`, return `402 {"error": "monthly_spend_limit_reached", "message": "You've reached your monthly usage limit for the free alpha. It resets at the start of next month."}`. The web app's axios interceptor turns every such 402 into a global banner. In this section only `POST /api/profile/<id>/tts` is decorated.
- **Trailing slashes (important for URLSession)**: routes registered as `"/"` under a prefix have a canonical URL WITH a trailing slash: `/api/dashboard/`, `/api/profile/`, `/api/todo/`, `/api/artifacts/`, `/api/prompts/`. Calling them without the slash gets a `308 Permanent Redirect` to the slashed URL (the web app does this for `/dashboard`, `/todo`, `/artifacts`; `setupProxy.js` comments on it). 308 keeps the method and body, but the native client should call the slashed form directly and skip the redirect. Conversely `/api/updates` is registered as `""` and has NO trailing slash; `/api/updates/` is a 404. `/api/log`, `/api/search*`, `/api/terms/accept` etc. have no trailing slash.
- **Request headers**: the web client sends `X-Timezone: <IANA name>` on every request (a hint; persistence is via `PATCH /api/dashboard/timezone`). JSON bodies are `Content-Type: application/json`. Cookies carry the session (see auth section).
- **Encrypted content**: every `content` field below is decrypted server-side and returned as plain text (Markdown). Nothing client-side needs to decrypt.
- Enumerations used below: `privacy_level` ∈ `"private" | "circles" | "public"`; `ai_usage` ∈ `"none" | "chat" | "train"` (`"chat"` and `"train"` permit AI reading); `plan` ∈ `"free" | "alpha" | "pro"` (voice mode = admin or plan alpha/pro; unrestricted `{user_export}` = admin or pro).

---

#### 2.4.1 Current user and account settings (`/api/dashboard`)

##### `GET /api/dashboard/`
- Backend: routes/dashboard.py:get_dashboard
- Auth: login_required; allowed for unapproved users (it is how the client learns `approved: false`).
- Called from: contexts/UserContext.js (on app mount; this is the web app's "who am I" call), pages/ProfilePage.js (to read `latest_profile`).
- Request: query `page` (int, default 1), `per_page` (int, default 20, max 100). The web app sends neither.
- Response 200:
```
{
  "user": CurrentUser,                // see below; the app's current-user model
  "pinned_nodes": [DashboardNodeCard],// nodes pinned by this user, newest pin first, not paginated
  "nodes": [DashboardNodeCard],       // this user's top-level (parent_id null) nodes, newest first, paginated
  "has_more": bool,
  "page": int,
  "total_nodes": int,
  "latest_profile": LatestProfile | null
}

CurrentUser = {
  "id": int,
  "username": string,
  "description": string | null,        // max 128 chars
  "accepted_terms_at": iso | null,
  "terms_up_to_date": bool,            // false => show the terms modal (see POST /api/terms/accept)
  "approved": bool,                    // false => waitlist screen; most /api calls 403
  "email": string | null,              // null for an X-only account
  "is_admin": bool,
  "plan": "free" | "alpha" | "pro",
  "voice_mode_enabled": bool,          // admin OR plan in {alpha, pro}; gates TTS generation and voice UI
  "craft_mode": bool,
  "preferred_model": string | null,    // SUPPORTED_MODELS key (e.g. "claude-opus-4.6"); null if unset OR if the saved one is deprecated
  "profile_generation_task_id": string | null, // Celery id of an in-flight sync profile build
  "profile_batch_pending": bool,       // a Batch-API profile build is in flight (no task id)
  "default_privacy_level": "private" | "circles" | "public",  // default "private"
  "default_ai_usage": "none" | "chat" | "train",              // default "chat"
  "twitter_login": bool,               // account has an X login bound (twitter_id set)
  "twitter_handle": string | null,
  "pending_email": string | null,      // address awaiting confirmation (#260)
  "pending_email_expired": bool,       // true => the mailed link is dead; offer "send a new link"
  "prefill_consent": "yes" | "no" | null, // answer to the X-prefill consent card; null = not asked yet
  "prefilled_handle": string | null,
  "timezone": string,                  // IANA name, "UTC" when unset
  "spend_blocked": bool,               // monthly spend cap reached; block cost actions up front
  "share_v1_enabled": bool,            // public side usable: env SHARE_V1 AND user opt-in
  "share_v1_available": bool,          // env SHARE_V1 only: whether Account shows the opt-in toggle
  "public_sharing_enabled": bool,      // the user's own opt-in
  "external_content_available": bool,  // env SEMANTIC_SEARCH_AGENTIC: whether Account shows the references toggle
  "external_content_enabled": bool     // user opt-in: AI may search saved references
}

DashboardNodeCard = {                  // _serialize_node_for_list
  "id": int,                           // display node id: for a system-prompt root, its FIRST child
  "preview": string,                   // first 200 chars + "..." if longer
  "node_type": string,                 // "user" | "llm" | ... (see Node model section)
  "child_count": int,                  // len(node.children) of the ROOT row (includes soft-deleted children)
  "created_at": iso,
  "pinned_at": iso | null,
  "username": string,                  // author; "Unknown" if missing
  "human_owner_username": string | null, // for llm nodes: the human the AI node belongs to
  "llm_model": string | null,
  "origin": string | null,             // import/source origin marker; null = native Loore
  "has_original_audio": bool,          // recorded audio exists (audio_original_url or streaming transcription)
  "prompt_key": string | null          // set when the root is a system-prompt root
}

LatestProfile = {                      // newest UserProfile row (including pipeline intermediates)
  "id": int,
  "content": string,                   // full Markdown profile text (can be long)
  "generated_by": string,              // "user" | model id | ...
  "tokens_used": int,
  "created_at": iso,
  "source_tokens_used": int | null,
  "source_origin_stats": object | null, // {origin_or_"loore": {"nodes": int, "tokens": int}}
  "source_data_cutoff": iso | null,
  "generation_type": string | null,    // "initial" | "update" | "iterative" | "revert" | "integration" | null ...
  "has_tts": bool                      // TTS audio exists; drives the "regenerate audio?" prompt on edit
}
```
- Errors: 401 unauthenticated. No other error paths.
- Notes: The web app ignores `nodes`/`pinned_nodes` from this endpoint (the home list comes from `GET /api/log`). A native app can use it as `GET /me`. The web app calls `/api/dashboard` (no slash) and follows the 308.

##### `GET /api/dashboard/<username>`
- Backend: routes/dashboard.py:get_public_dashboard
- Auth: login_required; GET is exempt from approval gating.
- Called from: nobody (the web route `/dashboard/:username` now redirects client-side to the public profile page).
- Response 200: same envelope as above but `user` is only `{"id", "username", "description"}`, and nodes are filtered to those the viewer can access. Not used by the frontend.
- Errors: 404 (HTML 404 from `first_or_404`) for an unknown username.

##### `PUT /api/dashboard/user`
- Backend: routes/dashboard.py:update_user
- Auth: login_required; exempt from approval gating.
- Called from: pages/AccountPage.js (username; preferred_model; default_privacy_level; default_ai_usage; external_content_enabled; public_sharing_enabled; craft_mode), components/NavBar.js (`craft_mode` toggle), components/PrefillConsentCard.js (`prefill_consent`).
- Request (JSON object; send only the fields being changed — each is applied only if the key is present):
  - `username`: string. Trimmed; validated: non-empty, ≤ 64 chars, `[A-Za-z0-9_]+`, not reserved, not taken case-insensitively, not another account's former handle. A rename records history and invalidates cached public pages.
  - `description`: string, ≤ 128 chars.
  - `craft_mode`: bool.
  - `public_sharing_enabled`: bool.
  - `external_content_enabled`: bool.
  - `preferred_model`: string | null. Must be an active (non-deprecated) SUPPORTED_MODELS key, or null/empty to clear.
  - `default_privacy_level`: `"private" | "circles" | "public"`.
  - `default_ai_usage`: `"none" | "chat" | "train"`.
  - `prefill_consent`: `"yes" | "no"` (also stamps `prefill_consent_at`).
  - `email`: FORBIDDEN — its presence returns 400 (email changes go through `POST /api/dashboard/email`).
- Response 200: `{"message": "Profile updated successfully.", "user": CurrentUser}` — the same keys as `GET /api/dashboard/` `user`. The web app replaces its cached user with it.
- Errors (all `{"error": string}`):
  - 400 `"Email changes go through verification: use POST /api/dashboard/email."`
  - 400 `"Description exceeds maximum length of 128 characters."`
  - 400 username messages: `"Username cannot be empty."`, `"Username must be 64 characters or fewer."`, `"Username may only contain letters, numbers, and underscores."`, `"That username is reserved."`, `"That username is already taken."`
  - 400 `"Model not offered: <id>"`, `"Invalid privacy level: <v>"`, `"Invalid AI usage value: <v>"`, `"Invalid prefill_consent value: <v>"`
  - 409 `"Your profile changed in another request. Reload and try again."` (unique-constraint race)
  - 500 `{"error": "Failed to update profile.", "details": string}`
- Frontend handling: username errors are shown inline from `error`; other toggles fail silently; PrefillConsentCard shows a generic retry message.

##### `PATCH /api/dashboard/timezone`
- Backend: routes/dashboard.py:update_timezone
- Auth: login_required; NOT exempt from approval gating (403 for waitlisted users).
- Called from: contexts/UserContext.js, right after `GET /api/dashboard` when the device timezone differs from `user.timezone`.
- Request: `{"timezone": "Europe/Prague"}` (IANA name; `"UTC"` always valid).
- Response 200: `{"timezone": string}`
- Errors: 400 `{"error": "Invalid timezone."}`; 500 `{"error": "Failed to update timezone.", "details": string}`.
- Notes: native equivalent is `TimeZone.current.identifier`. Used to stamp local times into LLM context.

##### `POST /api/dashboard/email`
- Backend: routes/dashboard.py:request_email_change  (#260 email add/change)
- Auth: login_required; exempt from approval gating (waitlisted signups add their address on the thank-you page).
- Called from: pages/AccountPage.js (add/change and "resend"), pages/AlphaThankYouPage.js.
- Request: `{"email": string}` (trimmed, lower-cased server-side).
- Response 200: `EmailState` plus message:
```
{ "message": "Confirmation link sent to <addr>. The address becomes yours once you confirm it.",
  "email": string | null, "pending_email": string | null, "pending_email_expired": bool }
```
- Side effects: sets `pending_email`, mails a link to the NEW address: `{FRONTEND_URL}/confirm-email?token=<token>` (a web URL, valid `EMAIL_CHANGE_EXPIRY_SECONDS` = 24 h). Each call voids the previous link. If the address already belongs to another account, the response is identical but the mail says "address in use" and carries no usable link (anti-enumeration).
- Errors: 400 `"Please enter a valid email address."`, 400 `"That is already your email address."`, 502 `"Could not send the confirmation email. Please try again."`.
- Frontend: merges `email`/`pending_email`/`pending_email_expired` into the cached user (utils/emailState.js); shows `error` text inline.

##### `POST /api/dashboard/email/confirm`
- Backend: routes/dashboard.py:confirm_email_change
- Auth: login_required, and only succeeds inside a session of the account that requested the change. Exempt from approval gating.
- Called from: pages/ConfirmEmailPage.js (web route `/confirm-email?token=...`, auto-POSTs once the user is loaded).
- Request: `{"token": string}` (the `token` query param from the mailed link).
- Response 200: `{"message": "Email confirmed.", "email", "pending_email", "pending_email_expired"}` (idempotent: a repeat after success also returns 200).
- Errors (each carries a machine `reason`):
  - 400 `{"error": "That confirmation link is invalid or has expired. Request a new one.", "reason": "invalid_or_expired"}`
  - 403 `{"error": "This confirmation link was requested from a different Loore account. Sign in to that account to use it.", "reason": "other_account"}` — web UI offers "sign out and use the other account" via `/auth/logout?next=...`.
  - 409 `{"error": "That email was claimed by another account before you confirmed it.", "reason": "taken"}`
- Frontend: branches on `reason`; a missing response is treated as `reason: "failed"` with a retry button.
- Native note: the link in the mail points to the WEB frontend. A native app has to either (a) let the user open it in Safari while signed in on the web (the token only works within the requesting account's session, and a Safari session is a different cookie jar), (b) accept a pasted link/token and POST it itself, or (c) claim `/confirm-email` via universal links (needs an apple-app-site-association file on loore.org — a small backend/nginx change).

##### `DELETE /api/dashboard/email/pending`
- Backend: routes/dashboard.py:cancel_email_change
- Auth: login_required; exempt from approval gating.
- Called from: pages/AccountPage.js ("Cancel" on the pending notice).
- Response 200: `{"message": "Pending email change cancelled.", "email", "pending_email", "pending_email_expired"}`. Never touches the bound email.

##### `DELETE /api/dashboard/email`
- Backend: routes/dashboard.py:remove_email
- Auth: login_required; exempt from approval gating.
- Called from: pages/AccountPage.js ("Remove email", only offered when X login exists).
- Response 200: `{"message": "Email removed.", "email": null, "pending_email": null, "pending_email_expired": false}`
- Errors: 400 `{"error": "This email is your only way to sign in. Add another address first."}` when the account has no X login.

##### `DELETE /api/dashboard/x`
- Backend: routes/dashboard.py:disconnect_x  (#311)
- Auth: login_required; NOT exempt from approval gating.
- Called from: pages/AccountPage.js ("Disconnect X").
- Response 200: `{"message": "X disconnected.", "twitter_login": false, "twitter_handle": null}`; also drops the session's X OAuth token.
- Errors: 400 `{"error": "X is your only way to sign in. Add an email first."}` when the account has no email.
- Notes: the counterpart "Connect X" is a browser navigation to `/auth/x/connect` (auth section), not an API call.

---

#### 2.4.2 Terms (`/api/terms`)

##### `POST /api/terms/accept`
- Backend: routes/terms.py:accept_terms
- Auth: login_required; exempt from approval gating.
- Called from: components/TermsModal.js (shown by App.js whenever `user.terms_up_to_date` is false).
- Request: no body.
- Response 200: `{"message": "Terms accepted", "accepted_terms_at": iso, "accepted_terms_version": "2.0"}`
- Side effects: for an unapproved user, emails the admin a signup notification.
- Errors: none specific; the modal shows a generic retry message.
- Notes: `CURRENT_TERMS_VERSION = "2.0"` lives in terms.py. There is NO endpoint that serves the terms text; the web app hard-codes it in TermsModal.js. A native app must bundle the same text. `terms_up_to_date` is also false when the account was deactivated after its last acceptance.

---

#### 2.4.3 Profile (`/api/profile`) and profile generation progress

Profile = the AI-generated (or user-edited) Markdown portrait of the user, append-only versioned. The current profile is `latest_profile` on `GET /api/dashboard/` (ProfilePage reads it from there, not from `/api/profile`).

##### `GET /api/profile/versions`
- Backend: routes/profile.py:get_profile_versions
- Auth: login_required.
- Called from: pages/ProfilePage.js (version count and history drawer).
- Response 200:
```
{ "versions": [ {                     // newest first; pipeline intermediates hidden
    "id": int, "generated_by": string, "tokens_used": int, "created_at": iso,
    "version_number": int,             // total - index (newest = highest)
    "source_tokens_used": int | null, "source_origin_stats": object | null,
    "source_data_cutoff": iso | null, "generation_type": string | null } ] }
```

##### `GET /api/profile/versions/<int:version_id>`
- Backend: routes/profile.py:get_profile_version
- Called from: pages/ProfilePage.js (history drawer: selected version and the one before it, for a diff).
- Response 200: `{"profile": {"id", "content", "generated_by", "tokens_used", "created_at"}}`
- Errors: 404 (HTML) unknown id; 403 `{"error": "Unauthorized"}` not the owner.

##### `POST /api/profile/revert/<int:version_id>`
- Backend: routes/profile.py:revert_profile
- Called from: pages/ProfilePage.js (history drawer "Revert").
- Response 200: `{"profile": {"id", "content", "generated_by", "tokens_used", "created_at", "privacy_level", "ai_usage"}}` (a new row, `generation_type: "revert"`).
- Errors: 404; 403 `Unauthorized`; 400 `{"error": "Already the current version"}`.

##### `POST /api/profile/`
- Backend: routes/profile.py:create_profile
- Called from: pages/ProfilePage.js (save when no profile exists yet). The web app posts to `/profile` and follows the 308.
- Request: `{"content": string, "privacy_level"?: "private"|"circles"|"public"}` (default private). `ai_usage` is taken from the account default and cannot be set.
- Response 201: `{"message": "Profile created successfully", "profile": {"id", "content", "generated_by": "user", "tokens_used": 0, "created_at", "privacy_level", "ai_usage"}}`
- Errors: 400 `"Content is required"`, `"Content cannot be empty"`, `"Invalid privacy_level: <v>"`; 500 `{"error": "Failed to create profile", "details"}`.

##### `PUT /api/profile/<int:profile_id>`
- Backend: routes/profile.py:update_profile
- Called from: pages/ProfilePage.js (edit and save).
- Request: `{"content": string, "privacy_level"?: string, "regenerate_tts"?: true}`. The web app asks "keep or regenerate audio?" when `latest_profile.has_tts` is true and the text changed; `regenerate_tts: true` clears the stored TTS.
- Response 200: `{"message": "Profile updated successfully", "profile": {"id", "content", "generated_by", "tokens_used", "created_at", "privacy_level", "ai_usage"}}`.
- Special case: if the account's `default_ai_usage` is `"none"` and the edited row is AI-readable, the edit is saved as a NEW version (different `id`) instead of in place. The client should use the returned `profile.id`.
- Errors: 404; 403 `Unauthorized`; 400 `"Content is required"`, `"Content cannot be empty"`, `"Invalid privacy_level: <v>"`; 500 with `details`.

##### `GET /api/profile/<int:profile_id>/audio`
- Backend: routes/profile.py:get_audio
- Called from: components/SpeakerIcon.js (profile speaker button on ProfilePage), with `validateStatus` accepting 200 and 404.
- Response 200: `{"tts_url": string}` — a path such as `/media/...` (relative to the backend origin; the web app prefixes `REACT_APP_BACKEND_URL`).
- Response 202 (generation running): `{"status": "generating", "message": "TTS generation in progress", "progress": int, "task_id": string}`
- Errors: 404 `{"error": "No audio found for this profile"}` (the web app then POSTs `/tts` if `voice_mode_enabled`); 403 `Unauthorized`.
- Notes: unlike nodes, profiles have no `original_url`, no `/audio-chunks`, and no `/tts-chapters` route.

##### `POST /api/profile/<int:profile_id>/tts`
- Backend: routes/profile.py:generate_tts
- Auth: login_required + `@require_spend_headroom` (402 when capped).
- Called from: components/SpeakerIcon.js (only when `user.voice_mode_enabled`).
- Response 202: `{"message": "TTS generation started", "task_id": string}` — Celery task `generate_tts_audio_for_profile`.
- Response 200 (already generated): `{"message": "TTS already available", "tts_url": string}`.
- Errors: 402 spend cap; 403 `Unauthorized`; 500 `{"error": "TTS not configured (missing API key)"}`.
- Follow-up: the web app then opens the SSE stream `GET /api/sse/profiles/<profile_id>/tts-stream` (see SSE section) and plays `chunk_ready` audio URLs as they arrive; the final `tts_url` comes in the completion event.

##### `GET /api/profile/<int:profile_id>/tts-status`
- Backend: routes/profile.py:get_tts_status
- Called from: nobody (SSE replaced it). Usable as a polling fallback.
- Response 200: `{"status": "pending"|"processing"|"completed"|"failed"|null, "progress": int, "task_id": string|null, "profile": {"id": int, "audio_tts_url"?: string}}` (`audio_tts_url` only when completed).

##### `GET /api/export/profile-progress`
- Backend: routes/export_data.py:get_profile_progress (#258)
- Auth: login_required.
- Called from: components/ProfileGenerationWatcher.js (app-wide poller) via `useAsyncTaskPolling`.
- How a build starts: there is NO user-facing "generate profile" endpoint. Builds are started by backend periodic tasks (and by the admin endpoint `POST /api/admin/users/<id>/build_profile`). The client learns a build is running from `GET /api/dashboard/` (`profile_generation_task_id` non-null or `profile_batch_pending` true) and then polls this endpoint. (The web app listens for a `loore_profile_started` window event, but nothing in the current frontend dispatches it.)
- Request: optional query `task_id` = the last sync task id seen running; lets the server report that task's terminal state after the guard is cleared.
- Response 200 (always 200):
```
{ "running": bool,
  "source": "sync" | "batch" | null,
  "status": "pending" | "processing" | "progress" | "completed" | "failed" | "stalled" | "idle",
  "progress": int (0-100),
  "message": string,                 // e.g. "Generating profile: Chunk 3 of ~7"
  "task_id": string | null,
  "error": string | null,
  "latest_profile": {"id": int, "generation_type": string|null, "created_at": iso} | null,
  "batch_step_failed": bool          // only present in the idle (not running) shape
}
```
- Polling cadence in the web app: every 5 s while `source` is sync, every 60 s while batch; no duration cap; stops after 10 consecutive request errors. Terminal logic: `failed` → failure toast; `stalled`, or `idle` after having seen it running with `batch_step_failed` → "stopped, will retry"; `completed`, or `idle` with a different `latest_profile.id` than when polling began → success toast ("Your profile has been updated ✓") and refetch of `/api/dashboard/`.

---

#### 2.4.4 Todo (`/api/todo`)

Todo = one Markdown checklist per user, append-only versioned. `PATCH` edits the current version in place (checkbox ticks, quick-add); `PUT` creates a new version (explicit edit/save).

TodoVersion JSON (field presence varies by endpoint, listed per endpoint): `id: int, content: string, generated_by: string ("user" | "revert" | model id from a voice merge), tokens_used: int, created_at: iso, privacy_level: string, ai_usage: string, version_number: int`.

##### `GET /api/todo/`
- Backend: routes/todo.py:get_todo
- Called from: pages/TodoPage.js (as `/todo`, via 308).
- Response 200: `{"todo": null}` when none exists, else `{"todo": {"id", "content", "generated_by", "tokens_used", "created_at", "privacy_level", "ai_usage", "version_number"}}` (`version_number` = total version count).

##### `PATCH /api/todo/`
- Backend: routes/todo.py:patch_todo
- Called from: pages/TodoPage.js (checkbox toggles, quick-add to the "Today" section, insert-after; optimistic update, reverted on failure).
- Request: `{"content": string}` (full Markdown text).
- Response 200: `{"todo": {"id", "content", "generated_by", "tokens_used", "created_at", "version_number"}}` (no privacy_level/ai_usage).
- Errors: 400 `"Content is required"`, `"Content cannot be empty"`; 404 `{"error": "No todo exists to update"}`.

##### `PUT /api/todo/`
- Backend: routes/todo.py:update_todo
- Called from: pages/TodoPage.js (Save in edit mode).
- Request: `{"content": string, "generated_by"?: string (default "user"), "tokens_used"?: int}`. The web app sends `generated_by: "user"`.
- Response 200: same shape as PATCH (new row).
- Errors: 400 `"Content is required"`, `"Content cannot be empty"`.

##### `GET /api/todo/versions`
- Called from: pages/TodoPage.js (history drawer).
- Response 200: `{"versions": [{"id", "generated_by", "tokens_used", "created_at", "version_number"}]}` newest first.

##### `GET /api/todo/versions/<int:version_id>`
- Response 200: `{"todo": {"id", "content", "generated_by", "tokens_used", "created_at"}}`. Errors: 404; 403 `Unauthorized`.
- Called from: TodoPage history drawer (selected + previous version for a diff).

##### `POST /api/todo/revert/<int:version_id>`
- Response 200: `{"todo": {"id", "content", "generated_by": "revert", "created_at", "version_number"}}`. Errors: 404; 403.

##### `POST /api/todo/apply-draft`
- Backend: routes/todo.py:apply_todo_draft
- Called from: components/ProposalInline.js ("Apply to todo" on an AI reply that proposed todo changes via the `propose_todo` tool).
- Request: `{"llm_node_id": int}` (the AI reply node carrying the proposal).
- Response 202: `{"status": "started", "task_id": string, "llm_node_id": int}` — Celery task `apply_voice_todo` merges the proposal into the todo with an LLM call (no visible node is created).
- Errors: 400 `"llm_node_id is required"`; 404 `{"error": "No pending todo changes found"}`; 403.
- Completion polling (web): every 2 s `GET /api/nodes/<llm_node_id>/llm-status` (nodes section) and read `tool_calls_meta` → entry with `name == "propose_todo"`; `apply_status` goes `"started"` → `"completed"` or `"failed"` (with `apply_error`). No timeout in the web code.

---

#### 2.4.5 Artifacts (`/api/artifacts`)

Artifacts = named, append-only versioned Markdown documents keyed by a `kind` slug. Built-in kinds (always listed, even before first write): `memory` ("Memory"), `scratchpad` ("Scratchpad"), `predictions` ("Predictions"), `ai_preferences` ("AI Interaction Preferences"), `intentions` ("Intentions"). Custom kinds match `^[a-z0-9][a-z0-9_-]{0,47}$`. The `intentions` artifact has a structured renderer on the web (components/IntentionsView.js, utils/intentions.js). Descriptions may contain `{name}`, substituted server-side with the username.

Artifact JSON:
```
{ "id": int | null,            // null for a built-in kind never written
  "kind": string, "title": string, "description": string | null,
  "generated_by": string | null, // "user" | "revert" | model id | null
  "created_at": iso | null,
  "privacy_level": string,      // "private" default
  "ai_usage": string,           // "chat" default
  "content": string,            // omitted in the versions list
  "version_number": int }       // only where noted
```

##### `GET /api/artifacts/`
- Backend: routes/artifacts.py:list_artifacts
- Called from: pages/ArtifactsPage.js, components/ArtifactsNav.js (both as `/artifacts`, via 308).
- Response 200: `{"artifacts": [Artifact]}` — unwritten built-in kinds first (with `id: null`, `content: ""`), then every written kind's latest version sorted by kind. No `version_number` here.

##### `GET /api/artifacts/<kind>`
- Backend: routes/artifacts.py:get_artifact
- Called from: nobody in the web app (it uses the list). Useful for native.
- Response 200: `{"artifact": Artifact + version_number}`; for an unwritten built-in kind, the default placeholder without `version_number`.
- Errors: 404 `{"error": "Artifact not found"}` for an unknown custom kind.

##### `PUT /api/artifacts/<kind>`
- Backend: routes/artifacts.py:update_artifact
- Called from: pages/ArtifactsPage.js (save an edit, or create a new kind — the new kind is simply a new slug).
- Request: `{"content": string (required, may be ""), "title"?: string, "description"?: string, "generated_by"?: string}`. The web app sends `content`, `description` (trimmed) and `generated_by: "user"`. Title defaults to the previous version's title, then the built-in title, then the slug title-cased. Title truncated to 128, description to 255.
- Response 200: `{"artifact": Artifact + version_number}` (new row).
- Errors: 400 `"Invalid kind: use a short lowercase slug (letters, digits, dashes)."`; 400 `"Content is required"`.

##### `POST /api/artifacts/<kind>/viewed`
- Called from: pages/ArtifactsPage.js, once per kind per visit, errors ignored. Feeds the admin activity tab.
- Response: 204, empty body.

##### `GET /api/artifacts/<kind>/versions`
- Response 200: `{"versions": [Artifact without content, with version_number]}` newest first.

##### `GET /api/artifacts/versions/<int:version_id>`
- Response 200: `{"artifact": Artifact}` (with content, no version_number). Errors: 404; 403 `Unauthorized`.

##### `POST /api/artifacts/<kind>/revert/<int:version_id>`
- Response 200: `{"artifact": Artifact + version_number}` (new row, `generated_by: "revert"`).
- Errors: 404; 403; 400 `"Version does not belong to this artifact"`; 400 `"Already the current version"`.

The web app dispatches a `loore_artifacts_changed` window event after save/revert so ArtifactsNav refetches; a native app needs its own invalidation.

---

#### 2.4.6 Prompts (`/api/prompts`)

Per-user editable system prompts, keyed by `prompt_key`, with file defaults on the server. Keys (from backend/utils/prompts.py `PROMPT_DEFAULTS`): `profile_generation`, `profile_update`, `profile_integration`, `narrative_detection`, `letter_from_the_future`, `orient_apply_todo`, `voice`, `textmode`; admin-only: `read`, `read_thread`; hidden (never listed, but still valid keys): `reflect`, `orient`. `version_number` 0 means "the file default, never edited". `default_updated: true` means the file default changed since the user's customised version was based on it (show a notice with "view new default" / "revert to default" / "dismiss").

Prompt JSON: `{"id": int|null|"default", "prompt_key": string, "title": string, "content": string, "generated_by": "user"|"revert"|"default", "created_at": iso|null, "version_number": int, "default_updated": bool}`. Note `id` is the STRING `"default"` for the file default in the versions list and `/default` responses.

##### `GET /api/prompts/`
- Called from: pages/PromptsPage.js (as `/prompts/`).
- Response 200: `{"prompts": [{"prompt_key", "title", "preview": string (first 150 chars), "version_number": int, "generated_by": string, "created_at": iso|null, "default_updated": bool}]}`.

##### `GET /api/prompts/<prompt_key>`
- Called from: pages/PromptDetailPage.js.
- Response 200: `{"prompt": Prompt}`. Errors: 404 `{"error": "Unknown prompt key"}`.

##### `PUT /api/prompts/<prompt_key>`
- Called from: pages/PromptDetailPage.js (save).
- Request: `{"content": string}`.
- Response 200: `{"prompt": Prompt}` (new row, `generated_by: "user"`).
- Errors: 404 unknown key; 400 `"Content is required"`; 400 `{"error": <message>}` from `{user_export}` placeholder validation (unknown placeholder keys, or an uncapped export on a non-Pro plan). The web app shows this `error` in a 10-second toast.

##### `GET /api/prompts/<prompt_key>/versions`
- Response 200: `{"versions": [{"id": int, "generated_by", "created_at", "version_number"}, ..., {"id": "default", "generated_by": "default", "created_at": null, "version_number": 0}]}` (newest first, file default appended last).

##### `GET /api/prompts/<prompt_key>/default`
- Called from: PromptDetailPage (view the default; version drawer when `id === "default"`).
- Response 200: `{"prompt": {"id": "default", "content": string, "generated_by": "default", "created_at": null}}`.

##### `GET /api/prompts/<prompt_key>/versions/<int:version_id>`
- Response 200: `{"prompt": {"id", "content", "generated_by", "created_at"}}`. Errors: 404; 403; 400 `"Version does not belong to this prompt"`.

##### `POST /api/prompts/<prompt_key>/revert/<int:version_id>`
- Response 200: `{"prompt": Prompt}` (new row, `generated_by: "revert"`). Errors: 404; 403; 400.

##### `POST /api/prompts/<prompt_key>/revert-to-default`
- Response 200: `{"prompt": Prompt}` (new row, `generated_by: "default"`; such rows auto-upgrade when the file default changes). Errors: 404 unknown key / `"Default prompt not found"`.

##### `POST /api/prompts/<prompt_key>/acknowledge-default`
- Dismisses the `default_updated` notice. Response 200: `{"prompt": Prompt}`. Errors: 404 `"No prompt to acknowledge"`.

---

#### 2.4.7 Log — the home list (`/api/log`)

##### `GET /api/log`
- Backend: routes/log.py:get_log
- Called from: components/Log.js (the web home page's thread list), as `/log?per_page=20` then `&cursor=<encodeURIComponent(next_cursor)>`.
- Request: query `per_page` (int, 1–100, default 20), `cursor` (opaque string from the previous `next_cursor`), or legacy `page` (int, offset paging; ignored when `cursor` is present).
- Content: the user's own (or AI-replies-they-own) top-level nodes and pinned nodes, excluding public roots, ordered by `coalesce(pinned_at, created_at)` desc then id desc. Soft-deleted roots with alive descendants still appear (the card then shows the newest alive descendant).
- Response 200:
```
{ "nodes": [LogCard], "has_more": bool, "next_cursor": string | null, "page": int }

LogCard = {
  "id": int,                   // DISPLAY node id (open this): a system-prompt root shows its first alive entry; a deleted root shows its newest alive descendant
  "thread_root_id": int,       // root to target for rename/delete; the row itself when the root is someone else's
  "newest_node_id": int,       // most recently updated alive node in the thread ("jump to newest")
  "thread_name": string | null,// user-given thread name (Thread table)
  "can_rename": bool,
  "preview": string,           // 200 chars + "..."
  "node_type": string,
  "child_count": int,          // alive replies of the display node
  "created_at": iso,           // display node's
  "pinned_at": iso | null,     // the row's pin time
  "username": string,
  "human_owner_username": string | null,
  "llm_model": string | null,
  "origin": string | null,
  "has_original_audio": bool,
  "prompt_key": string | null
}
```
- Cursor format: `"<ISO timestamp without Z>|<node id>"`, e.g. `"2026-09-30T10:11:12.345678|98765"`. Treat as opaque and URL-encode it (it contains `|` and `:`).
- Duplicates: a later page can repeat an `id` already shown; the client must drop ids it already holds (the web app does).
- Errors: 400 `{"error": "Invalid cursor"}`.

---

#### 2.4.8 Search (`/api/search`)

##### `GET /api/search`  (keyword / date search)
- Backend: routes/search.py:search
- Called from: components/SearchModal.js (Cmd+K; keyword mode and "load more" pagination; `scope=external` on the References page).
- Request (query): `q` (string), `from`, `to` (ISO 8601 date or datetime, parsed with Python `datetime.fromisoformat`, e.g. `2026-09-01`), `node_type` (optional filter), `page` (default 1), `per_page` (default 20, max 100), `scope` (`"external"` to search saved references instead of the archive). At least one of `q`/`from`/`to` is required.
- Matching: case- and diacritics-insensitive substring over decrypted content (and reference titles for external). Excludes deleted nodes and system-prompt text.
- Response 200 (archive):
```
{ "results": [ { "id": int, "preview": string, "snippet": string | null,  // ~80 chars either side of the first match, "..." affixes, every match wrapped in literal <mark>…</mark> tags (raw user text otherwise, not HTML-escaped); null without q
                 "node_type": string, "created_at": iso, "username": string,
                 "child_count": int, "parent_id": int | null, "score": 1.0 } ],
  "page": int, "per_page": int, "total": int, "has_more": bool, "search_type": "keyword" }
```
- Response 200 (`scope=external`): same envelope plus `"scope": "external"`; each result is a reference:
  `{"id": int, "kind": "external", "source": string, "author_handle": string|null, "title": string|null, "external_url": string|null, "preview": string, "snippet": string|null, "created_at": iso, "score": 1.0}`.
- Errors: 400 `{"error": "Provide at least a keyword (q) or date range (from/to)."}`; 400 `"Invalid 'from' date format. Use ISO 8601."` / `'to'`. The web app suppresses logging for 400.

##### `GET /api/search/semantic`
- Backend: routes/search.py:semantic_search (#155)
- Called from: components/SearchModal.js (default mode is semantic). It also sends `per_page`/`page`, which this route ignores.
- Request (query): `q` (required), `limit` (default 20, max 50), `min_score` (float, default 0.2), `from`/`to`, `scope` (`"external"` = references only), `include_external` (default `"1"`; `"0"`/`"false"` to exclude references from archive results).
- Response 200:
```
{ "results": [ ArchiveResult | ExternalResult ],  // merged, sorted by score desc, at most `limit`
  "total": int, "mode": "semantic", "scope": "archive" | "external" }
```
  ArchiveResult has the keyword shape with `snippet: null` and a real `score` (cosine, 4 decimals); ExternalResult has `kind: "external"` (shape above). Discriminate on the presence of `kind`.
- Errors: 400 `"Provide a query (q)."`, bad dates; 503 `{"error": "Semantic search is not configured."}`; 502 `{"error": "Embedding the query failed."}`.
- Notes: costs an embedding call (logged to the user). Only AI-readable (`ai_usage != "none"`) nodes are embedded, so such nodes are only findable by keyword. No pagination (single ranked set).

##### `GET /api/search/neighbors`
- Backend: routes/search.py:semantic_neighbors
- Called from: components/SemanticNeighbors.js (related entries under a node) with `{node_id, limit: 5}`.
- Request (query): `node_id` (int, required), `limit` (default 5, max 20).
- Response 200: `{"results": [{"id", "preview" (160 chars), "node_type", "created_at", "username", "score"}], "total": int, "mode": "neighbors"}`; empty results when the node has no embedding yet.
- Errors: 400 `"Provide a node_id."`; 404 `"Node not found."`; 403 `"Not authorized."` (only own nodes / AI replies the user owns).

---

#### 2.4.9 Proposal actions: feedback and GitHub issue

Both confirm a proposal an AI reply made through a tool (`propose_feedback`, `propose_github_issue`); the pending state is a Draft row found by walking up from `llm_node_id`. Called from components/ProposalInline.js; the `error` string is shown inline on failure.

##### `POST /api/feedback/submit`
- Backend: routes/feedback.py:submit
- Request: `{"llm_node_id": int}`.
- Response 200: `{"status": "completed", "feedback_id": int}`; also writes `apply_status: "completed"` and `feedback_id` into the origin node's `tool_calls_meta` entry `propose_feedback`.
- Errors: 400 `"llm_node_id is required"`; 404 `"Node not found"`, `"No pending feedback found"`, `"Origin node not found"`; 403; 400 `{"error": <reason from submit_feedback_from_node>}`.

##### `POST /api/github/create-issue`
- Backend: routes/github_issues.py:create_issue
- Request: `{"llm_node_id": int}`.
- Response 200: `{"status": "completed", "issue_url": string, "issue_number": int}`; updates `propose_github_issue` in `tool_calls_meta` the same way.
- Errors: 400 `"llm_node_id is required"`, `"Could not parse issue from proposal"`; 404 `"Node not found"`, `"No pending GitHub issue found"`, `"Origin node not found"`; 403; 500 `{"error": <GitHub error>}` (e.g. token not configured).

---

#### 2.4.10 Dev updates: changelog, notifications, polls (`/api/updates`)

One modal on the web (components/UpdatesModal.js), fetched once per session by App.js after the user is loaded, approved and `terms_up_to_date`; shown only when any list is non-empty. Semantics: "read" dismisses for good; "skip" keeps it unread (comes back next session). Following a link counts as skip. Kill switch `DEV_UPDATES_V1=false` makes `GET /api/updates` return three empty lists.

##### `GET /api/updates`   (no trailing slash)
- Backend: routes/updates.py:get_updates
- Called from: App.js.
- Response 200:
```
{ "changelog": [ { "id": string,          // section slug from backend/user_changelog.md
                   "title": string,
                   "date": "YYYY-MM-DD" | null,
                   "body": string } ],     // Markdown; may contain in-app links like /account#model
  "notifications": [ { "id": int,
                       "type": string,     // "profile_ready" | "briefing_ready" | "fix_ready" | ...
                       "title": string, "body": string | null,
                       "link": string | null,   // in-app path ("/profile") or absolute https URL
                       "created_at": iso,
                       "meta": {"version": int, "created_at": iso} | null } ],  // only for profile_ready
  "polls": [ PendingPoll ] }

PendingPoll = { "id": int, "question": string, "created_at": iso,
                "draft_terms": {"model": string /* display name */, "data_source": "derived" | "recent_window"},
                "response": PollResponse | null }

PollResponse = { "status": "drafting" | "draft" | "draft_failed" | "sent" | "declined",
                 "content": string, "generated_by": string | null /* model id when AI-drafted verbatim */,
                 "draft_requested_at": iso | null, "sent_at": iso | null }
```
- Changelog sections dated before the user's signup are omitted; notifications are the 20 newest unread.
- Native note: `link` and changelog body links are WEB paths (`/profile`, `/account#model`, `/node/123`); the app needs a path-to-screen router, with absolute `http(s)` links opened externally.

##### `POST /api/updates/changelog/<section_id>/<action>`
- `action` ∈ `read` | `skip`. Response 200: `{"section_id": string, "status": "read" | "skipped"}`. Errors: 404 `{"error": "Unknown action"}` / `{"error": "Unknown section"}`. Web: fire-and-forget.

##### `POST /api/updates/notifications/<int:notification_id>/<action>`
- `action` ∈ `read` | `skip`. Response 200: `{"id": int, "status": "unread" | "read"}` (skip leaves `unread`). Errors: 404. Web: fire-and-forget.

##### `GET /api/updates/polls/<int:poll_id>`
- Called from: UpdatesModal, every 5 s while a draft is generating.
- Response 200: `{"id": int, "question": string, "active": bool, "draft_terms": {...}, "response": PollResponse | null}`. Errors: 404.
- Web logic: stop polling when `response.status != "drafting"`; `"draft"` → put `content` in the editor; `"draft_failed"` → message and manual answer.

##### `POST /api/updates/polls/<int:poll_id>/draft`   (opt-in 1: let the AI draft an answer)
- Response 202: `{"response": PollResponse}` with `status: "drafting"` (Celery `submit_poll_draft`, Batch API — can take minutes). Response 200 with the same shape if already drafting.
- Errors: 409 `"This poll is closed."`, `"Already sent."`; 403 `"Your AI-usage setting doesn't allow this. You can still write an answer yourself."` (account `default_ai_usage` is `none`). No spend-cap check (cost goes to a system account).

##### `PUT /api/updates/polls/<int:poll_id>/response`
- Request: `{"content": string}`. Saves a private draft (`status: "draft"`); clears `generated_by` unless the text equals the AI draft verbatim.
- Response 200: `{"response": PollResponse}`. Errors: 409 closed / already sent; 400 `"Answer is empty."`.

##### `POST /api/updates/polls/<int:poll_id>/send`   (opt-in 2: share with the admin)
- The web app always PUTs `/response` first, then POSTs `/send`.
- Response 200: `{"response": PollResponse}` with `status: "sent"`. Errors: 409 closed / `"Draft still generating."`; 400 `"Nothing to send yet."`.

##### `POST /api/updates/polls/<int:poll_id>/decline`
- Response 200: `{"response": PollResponse}` with `status: "declined"`. Errors: 409 `"Already sent."`; 404. Web: fire-and-forget.

---

#### 2.4.11 Health (no auth)

##### `GET /api/health` (also `/health`)
- Backend: routes/health.py:health. Response 200 `{"status": "ok"}`.

##### `GET /api/ready` (also `/ready`)
- Backend: routes/health.py:ready. Response 200 `{"status": "ready", "database": true, "redis": true}` or 503 `{"status": "not_ready", "database": bool, "redis": bool}`.
- Notes: public nginx forwards only `/api`, `/auth`, `/media` to Flask, so the native app should use the `/api/...` forms. UptimeRobot watches `/api/ready`. Not called by the web app; a native app could use `/api/health` as a reachability probe.

---

#### 2.4.12 Routes in these files that the web frontend never calls

| Route | Note |
|---|---|
| `GET /api/dashboard/<username>` | Old public dashboard; web route now redirects. |
| `GET /api/profile/<id>/tts-status` | Superseded by SSE `/api/sse/profiles/<id>/tts-stream`; usable as a polling fallback. |
| `GET /api/artifacts/<kind>` | Web uses the list endpoint; handy for native single-artifact loads. |
| `GET /health`, `GET /ready`, `GET /api/health`, `GET /api/ready` | Monitoring only. |
| `GET /stats`, `GET /stats/<username>` (routes/stats.py) | Blueprint is NOT registered in `create_app` — dead code, unreachable. |

Frontend calls in this area that live in OTHER route files: `GET /api/nodes/<id>/llm-status` (todo-apply polling), `GET /api/sse/profiles/<id>/tts-stream` (profile TTS), `/auth/x/connect` and `/auth/logout?next=...` (browser navigations from Account / ConfirmEmail pages), `GET /api/voice/timing/clock` and `POST /api/voice/timing` (voiceTiming.js).

### 2.5 Import, export, references (external), share, commons, admin, webhooks, public pages

Scope: `backend/routes/{import_data,export_data,external,share,commons,admin,webhooks,public_pages}.py`. All paths below are full paths as the client sends them (blueprint prefix included). Timestamps produced by `iso_utc()` are ISO-8601 strings with an explicit UTC marker (`...Z` for naive DB values); a few import routes emit naive `isoformat()` strings with no zone (noted where they occur).

Conventions that hold for every route in this section:
- "login" means `@login_required`. Unauthenticated calls to any `/api/...` path get `401 {"error": "Unauthorized"}` (JSON, not a redirect; see `backend/__init__.py` unauthorized handler).
- Logged-in but unapproved users get `403 {"error": "Your account is not approved. Please wait for approval."}` from the global `before_request` gate on every route here (none of these paths are exempt).
- nginx (configs/nginx.txt): `client_max_body_size 200M`; `/api/` has `proxy_read_timeout 60s`. Uploads above 200 MB get an nginx 413 (HTML body, not JSON); the ChatGPT import UI special-cases 413.
- Error bodies are `{"error": string}` unless stated; some 500s add `"details": string` (the Python exception text).

---

#### 2.5.1 Archive import (Import page)

Frontend: `components/ImportData.js` (mounted by `pages/ImportPage.js`). Every import is a two-step analyze -> confirm. The analyze step for Markdown, Claude and ChatGPT returns the parsed content to the client, and the client posts that same content back on confirm. The Twitter archive stays on the server and is referenced by `import_token`. For Claude and ChatGPT exports the client unzips the user's export locally with JSZip and uploads only `conversations.json`.

Shared confirm-step conflict (all four confirm routes):
- `409 {"error": "deleted_content_matches", "deleted_matches": int}` when some items match nodes the user soft-deleted and the request carried no `on_deleted`. The frontend (`handleDeletedConflict`) shows a restore-or-skip dialog and re-sends the same confirm with `on_deleted: "restore" | "skip"`.

Shared confirm response counters (Markdown, Claude, ChatGPT confirm, and the Twitter task result): `created` (== `nodes_created`), `skipped` (dedup hits left untouched), `restored` (soft-deleted nodes revived because `on_deleted: "restore"`), `updated` (dedup hits whose privacy/ai_usage changed to this import's values). `profile_update_task_id` is a Celery task id or null; the frontend passes it to `onProfileUpdateStarted` so the profile-progress watcher (see `/api/export/profile-progress`) picks it up. It is non-null only when `ai_usage` is `chat`/`train` and the user's plan is a voice-mode plan.

##### `POST /api/import/analyze`
- Backend: routes/import_data.py:analyze_import
- Auth: login
- Called from: ImportData.js `handleImportFile`
- Request: `multipart/form-data`, field `zip_file` (filename must end in `.zip`). Every `*.md` in the zip (excluding `__MACOSX/`) that decodes as UTF-8 is read.
- Response 200:
```
{
  "files": [ { "name": str, "filename_without_ext": str, "content": str,
               "size": int, "modified_at": str /* naive ISO, zip entry mtime, no Z */,
               "token_count": int } ],
  "total_files": int, "total_tokens": int, "total_size": int
}
```
- Errors: 400 `No zip_file provided` / `No file selected` / `File must be a .zip file` / `No .md files found in the zip archive` / `No valid .md files could be read from the zip archive` / `Invalid zip file`; 500 `{"error": "Failed to analyze zip file", "details"}`.
- Notes: file upload. Response carries full file text, so it grows with the archive.

##### `POST /api/import/confirm`
- Backend: routes/import_data.py:confirm_import
- Auth: login
- Called from: ImportData.js `handleConfirmImport`
- Request JSON:
```
{ "files": [ { "filename_without_ext": str, "content": str, "modified_at": str } ],  // from analyze
  "import_type": "single_thread" | "separate_nodes",   // default separate_nodes
  "date_ordering": "modified" | "created",             // default modified
  "privacy_level": str,  // default "private"
  "ai_usage": str,       // default "none"
  "on_deleted"?: "restore" | "skip" }
```
- Response 201: `{ "message": "Import successful", "nodes_created": int, "thread_count": int, "profile_update_task_id": str|null, "created": int, "skipped": int, "restored": int, "updated": int }`
- Errors: 400 `No data provided` / `No files provided` / invalid `import_type` / invalid `date_ordering`; 409 `deleted_content_matches`; 500 `{"error": "Failed to import files", "details"}`.
- Notes: synchronous. A file whose content does not start with `# ` gets `# <filename>\n\n` prepended. `single_thread` chains files as a reply chain; `separate_nodes` makes one root per file.

##### `POST /api/import/twitter/analyze`
- Backend: routes/import_data.py:analyze_twitter_import
- Auth: login
- Called from: ImportData.js (Twitter archive picker)
- Request: `multipart/form-data`, field `zip_file` (`.zip`, the X data export).
- Response 200: `{ "import_token": str, "total_tweets": int, "original_count": int, "reply_count": int, "skipped_retweets": int, "total_tokens": int, "original_tokens": int, "total_size": int }`
- Errors: 400 `No zip_file provided` / `No file selected` / `File must be a .zip file` / archive-format message from `TwitterArchiveError`; 500 `Could not store the archive for import` or `{"error": "Failed to analyze Twitter export", "details"}`.
- Notes: tweets are stashed server-side per user; only counts return.

##### `POST /api/import/twitter/confirm`
- Backend: routes/import_data.py:confirm_twitter_import
- Auth: login
- Called from: ImportData.js `handleConfirmTwitterImport`
- Request JSON: `{ "import_token": str, "import_type": "single_thread"|"separate_nodes", "include_replies": bool, "privacy_level": str, "ai_usage": str, "on_deleted"?: "restore"|"skip" }`
- Response 202: `{ "task_id": str, "status": "queued", "total": int }`
- Errors: 400 `No data provided` / `Import data expired — please upload the archive again.` (stash missing) / invalid `import_type`; 409 `deleted_content_matches`.
- Notes: async. Celery task `backend.tasks.imports.import_twitter_archive`; poll `GET /api/import/status/<task_id>`. The frontend polls every 1500 ms, shows `done/total` while `running`, and stops on `completed`/`failed`.

##### `GET /api/import/status/<task_id>`
- Backend: routes/import_data.py:import_status
- Auth: login (task meta carries `user_id`; another user's task reads as `queued` forever)
- Called from: ImportData.js `poll()`
- Response 200 (always 200):
```
{ "task_id": str,
  "status": "queued" | "running" | "completed" | "failed",
  "done": int|null, "total": int|null,
  "result": null | { "message": "Import successful", "nodes_created": int, "thread_count": int,
                     "profile_update_task_id": str|null, "created": int, "skipped": int,
                     "empty": int, "restored": int, "updated": int },
  "error": str|null }
```
- Notes: `result` is the Celery task's return dict minus `user_id`.

##### `POST /api/import/claude/analyze`
- Backend: routes/import_data.py:analyze_claude_import
- Auth: login
- Called from: ImportData.js `handleClaudeImportFile`
- Request: `multipart/form-data`, field `conversations_file` (the `conversations.json` blob the client extracted from the Claude export zip, filename `conversations.json`).
- Response 200:
```
{ "conversations": [ { "name": str, "created_at": str,
                       "messages": [ { "text": str, "sender": "human"|"assistant",
                                       "created_at": str, "token_count": int, "uuid": str } ],
                       "message_count": int, "token_count": int } ],
  "total_conversations": int, "total_messages": int, "total_tokens": int, "total_size": int }
```
- Errors: 400 `No conversations_file provided` / `No file selected` / no conversations with messages / `{"error": "conversations.json is not valid UTF-8", "details"}` / `{"error": "Failed to parse conversations JSON", "details"}`; 500 `{"error": "Failed to analyze Claude export", "details"}`.

##### `POST /api/import/claude/confirm`
- Backend: routes/import_data.py:confirm_claude_import
- Auth: login
- Called from: ImportData.js
- Request JSON: `{ "conversations": [...as returned by analyze...], "privacy_level": str (default "private"), "ai_usage": str (default "none"), "on_deleted"?: "restore"|"skip" }`
- Response 201: same shape as `/api/import/confirm`.
- Errors: 400 `No data provided` / `No conversations provided`; 409 `deleted_content_matches`; 500 `{"error": "Failed to import Claude conversations", "details"}`.
- Notes: synchronous; one thread per conversation, assistant messages become `llm` nodes.

##### `POST /api/import/chatgpt/analyze`
- Backend: routes/import_data.py:analyze_chatgpt_import
- Auth: login
- Called from: ImportData.js (ChatGPT picker)
- Request: `multipart/form-data`, field `conversations_file` (client-extracted `conversations.json`).
- Response 200:
```
{ "conversations": [ { "name": str, "created_at": str /* naive ISO or "" */, "default_model": str,
                       "messages": [ { "text": str, "role": str, "created_at": str, "model": str|null,
                                       "mapping_id": str, "token_count": int } ],
                       "message_count": int, "token_count": int } ],
  "total_conversations": int, "total_messages": int, "total_tokens": int, "total_size": int }
```
- Errors: same set as Claude analyze, with `Failed to analyze ChatGPT export`. Frontend special-cases HTTP 413 ("conversations.json is too large to upload") and shows `error: details` when both are present.

##### `POST /api/import/chatgpt/confirm`
- Backend: routes/import_data.py:confirm_chatgpt_import
- Auth: login
- Called from: ImportData.js `handleConfirmChatGPTImport`
- Request JSON: `{ "conversations": [...from analyze...], "privacy_level", "ai_usage", "on_deleted"? }`
- Response 201: same shape as `/api/import/confirm`.
- Errors: as Claude confirm.

---

#### 2.5.2 Export and profile-build progress

##### `GET /api/export/threads`
- Backend: routes/export_data.py:export_threads
- Auth: login
- Called from: NavBar.js overflow menu "Export data" (`api.get(..., {responseType: "blob"})`, then saved client-side as `loore-export-YYYY-MM-DD.txt`)
- Response 200: `text/plain` body (human-readable tree of all the user's threads, including AI replies and `ai_usage: none` rows), header `Content-Disposition: attachment; filename="write-or-perish-export-YYYYMMDD-HHMMSS.txt"`. With no threads the body is `No threads found to export.` (still 200).
- Notes: download. It can take long for large accounts and the axios timeout is 60 s, same as the nginx `/api/` read timeout. A native client should use a download task with a timeout of at least 60 s.

##### `GET /api/export/profile-progress`
- Backend: routes/export_data.py:get_profile_progress
- Auth: login
- Called from: components/ProfileGenerationWatcher.js (the single app-wide poller; mounted in App). It polls every 5 s while the sync pipeline runs and every 60 s while the batch pipeline runs, and stops when `running` is false.
- Query: `task_id` (optional): the sync task id the client last saw running. It lets the server report `completed`/`failed` after the guard has cleared.
- Response 200:
```
{ "running": bool,
  "source": "batch" | "sync" | null,
  "status": "idle" | "pending" | "processing" | "progress" | "completed" | "failed" | "stalled" | <lowercased celery state>,
  "progress": int /* 0-100 */,
  "message": str,           // e.g. "Generating profile: Chunk 3 of ~7"
  "task_id": str | null,
  "error": str | null,
  "latest_profile": null | { "id": int, "generation_type": str, "created_at": str },
  "batch_step_failed"?: bool   // only on the idle/not-running branch
}
```
- Notes: the client detects "a new profile version landed" by `latest_profile.id` changing, then fetches the content from `/api/dashboard`. The watcher dispatches `loore_profile_progress` / `loore_profile_done` window events that other components listen to.

##### `GET /api/export`
- Backend: routes/export_data.py:export_data
- Auth: login
- Called from: not called by the frontend.
- Response 200: `{ "user": {id, twitter_id, username, description, created_at}, "nodes": [{id, content, node_type, parent_id, linked_node_id, token_count, created_at, updated_at}], "versions": [{id, node_id, content, timestamp}] }` (own nodes only; decrypts every node: expensive).

##### `DELETE /api/delete_my_data`
- Backend: routes/export_data.py:delete_my_data
- Auth: login
- Called from: not called by the frontend.
- Response 200: `{"message": "All your app data has been deleted."}`; 500 `{"error": "Error deleting data", "details"}`. Hard-deletes the user's nodes, node versions and thread names.

---

#### 2.5.3 References (external items)

An external item ("reference") is a saved tweet, bookmark, Community Archive tweet or web clip. Pages: `pages/ReferencesPage.js` (list), `pages/ReferenceDetailPage.js` (view/edit/delete), `components/ReferenceReadToggle.js`, `ReferenceFeedback.js`, `ExternalQuoteBubble.js` (a `{quote_ext:ID}` card inside a node), `FeedPicks.js` (Read picks), `SpeakerIcon.js` (TTS, `baseUrl = /external/items/<id>`), `ExternalImport.js` (the Import page's references/X/clipper card).

Item JSON (`_serialize_item`), used by list and detail:
```
{ "id": int,
  "source": "twitter_bookmark" | "twitter_like" | "community_archive" | "web_clip" | "read_pick",
  "external_id": str,            // tweet id for tweet sources; URL hash for web_clip
  "author_handle": str | null,
  "title": str | null,           // <= 512 chars
  "preview": str,                // first 280 chars of content + "…" if longer
  "url": str,
  "posted_at": str | null, "fetched_at": str,
  "read_at": str | null,         // the user's own read mark
  "feedback": "good" | "bad" | null, "feedback_at": str | null,
  "edited_at": str | null,       // set when the user edited title/content
  "has_tts": bool,
  "surfaced_count": int, "last_surfaced_at": str | null,
  "content"?: str                // detail GET and PUT responses only (full text; Markdown for clips)
}
```
`read_pick` rows are Read picks the user has not saved. The list endpoint excludes them (`ExternalItem.saved()`); `{quote_ext}` markers and feed picks can still point at them.

##### `GET /api/external/items`
- Backend: routes/external.py:list_items
- Auth: login
- Called from: ReferencesPage.js (`{sort: "saved", page, per_page: 20}`, infinite scroll on `has_more`); ExternalImport.js (`{per_page: 1}` just for `counts`)
- Query: `source` (optional exact filter), `page` (default 1), `per_page` (default 50, max 200), `sort` (`saved` = newest saved first by id desc; anything else = `posted_at desc nulls last, id desc`).
- Response 200: `{ "items": [Item], "total": int, "has_more": bool, "counts": { "<source>": int, ... } }` (`counts` are over all saved items, ignoring the `source` filter).

##### `GET /api/external/items/<item_id>`
- Backend: routes/external.py:get_item
- Auth: login (owner only)
- Called from: ReferenceDetailPage.js
- Response 200: Item + `content`. 404 `{"error": "not found"}` (the page shows a not-found state on 404).

##### `PUT /api/external/items/<item_id>`
- Backend: routes/external.py:update_item
- Auth: login (owner only)
- Called from: ReferenceDetailPage.js (edit form, `ReferenceEditForm.js`)
- Request JSON: `{ "title"?: str, "content"?: str, "regenerate_tts"?: bool }` (at least one of title/content)
- Response 200: Item + `content`.
- Errors: 404 `not found`; 400 `title or content is required` / `content cannot be empty` / `title must be a string`; 422 `{"error": "Content exceeds the 100,000-character per-entry limit.", "char_cap": 100000}`.
- Notes: a changed content drops the embedding. When `regenerate_tts` is true the stored TTS is also cleared; the editor asks the user to keep or regenerate the audio.

##### `DELETE /api/external/items/<item_id>`
- Backend: routes/external.py:delete_item
- Auth: login (owner only)
- Called from: ReferencesPage.js, ReferenceDetailPage.js
- Response 200: `{"deleted": true, "id": int}` or, when a Read had picked the tweet, `{"deleted": true, "id": int, "kept_as_pick": true}`: the row goes back to `read_pick` instead of being deleted.
- Errors: 404 `not found`.

##### `POST /api/external/items/<item_id>/read` and `DELETE /api/external/items/<item_id>/read`
- Backend: routes/external.py:mark_item_read
- Auth: login (owner only)
- Called from: ReferenceReadToggle.js, ExternalQuoteBubble.js, FeedPicks.js (`{node_id, via: "open"}` when the user opens a pick), ReferenceDetailPage.js (no body)
- Request JSON (optional, both methods; axios DELETE sends it as `{data: body}`): `{ "node_id"?: int /* the reply node the reference was shown in */, "via"?: "open" }`
- Response 200: `{ "id": int, "read_at": str | null }`
- Notes: POST is idempotent (keeps the original `read_at`); DELETE clears it. Each call is logged for the recommendation record.

##### `POST /api/external/items/<item_id>/feedback`
- Backend: routes/external.py:set_item_feedback
- Auth: login (owner only)
- Called from: ReferenceFeedback.js
- Request JSON: `{ "feedback": "good" | "bad" | null, "node_id"?: int }`
- Response 200: `{ "id": int, "feedback": str|null, "feedback_at": str|null, "read_at": str|null }` (a non-null verdict also sets `read_at` if unset)
- Errors: 404; 400 `feedback must be 'good', 'bad' or null`.

##### Reference TTS: `GET /api/external/items/<id>/audio`, `POST .../tts`, `GET .../tts-status`, `GET .../tts-chapters`
- Backend: routes/external.py:get_item_audio / generate_item_tts / get_item_tts_status / get_item_tts_chapters
- Auth: login (owner only). `POST .../tts` also has `@require_spend_headroom`, which returns 402 `{"error": "monthly_spend_limit_reached", "message"}` for a capped user.
- Called from: SpeakerIcon.js (with `baseUrl = /external/items/<id>`; the same component and contract serve `/nodes/<id>` and `/profile/<id>`); completion arrives over SSE `GET /api/sse/items/<id>/tts-stream` (sse.py; another mapper covers it), and `tts-status` is the useAsyncTaskPolling fallback.
- `GET .../audio` responses: 200 `{"tts_url": str}` (relative `/media/...` path; the client prefixes `REACT_APP_BACKEND_URL`) | 202 `{"status": "generating", "progress": int, "task_id": str}` | 404 `{"error": "No audio for this reference"}`.
- `POST .../tts` responses: 200 `{"message": "TTS already available", "tts_url": str}` | 202 `{"message": "TTS generation already in progress", "task_id", "status"}` | 202 `{"message": "TTS generation started", "task_id": str, "status": "pending"}` | 500 `TTS not configured (missing API key)` | 404 | 402.
- `GET .../tts-status` response 200 (headers `Cache-Control: no-cache, no-store, must-revalidate`): `{ "status": "pending"|"processing"|"completed"|"failed"|null, "progress": int, "task_id": str|null, "item": { "id": int, "audio_tts_url"?: str /* only when completed */ } }`
- `GET .../tts-chapters` response 200: `{ "chapters": [ { "section_index": int, "title": str, "chunk_index": int, "start_time": float /* seconds */ } ] }` (an empty list when there is at most one section).

##### `POST /api/external/community-archive/fetch`
- Backend: routes/external.py:fetch_community_archive_route
- Auth: login
- Called from: ExternalImport.js `fetchCA`
- Request JSON: `{ "username": str /* leading @ stripped */, "max_items"?: int /* default 2000, max 10000 */ }`
- Response 202: `{ "task_id": str, "status": "pending" }`. There is no status endpoint: the frontend re-fetches `/external/items?per_page=1` + `/external/twitter/status` every 5 s, 12 times.
- Errors: 400 `username is required`.
- Notes: Celery `backend.tasks.external_sync.fetch_community_archive` (no API credentials needed).

##### `POST /api/external/bookmarks/import`
- Backend: routes/external.py:import_bookmarks_json
- Auth: login
- Called from: ExternalImport.js `importBookmarksFile`. The client reads the JSON file itself and posts `{bookmarks: [...]}` when the file holds an array, or the parsed object as-is.
- Request: JSON array, or `{bookmarks: [...]}` / `{data: [...]}`, or multipart field `file` containing that JSON. Entries accept `id|tweet_id|url`, `text|full_text`, `author|username|screen_name`, `created_at`, plus optional `quoted{author,text}`, `card{title,description,domain}`, `media[{type,alt}]`, `links[str]`.
- Response 200: `{ "created": int, "skipped": int, "unrecognized": int }`
- Errors: 400 `File is not valid JSON` / `Provide a JSON array of bookmarks (or {bookmarks: [...]}).`
- Notes: synchronous; items are stored as source `twitter_bookmark`.

##### `GET /api/external/twitter/status`
- Backend: routes/external.py:twitter_status
- Auth: login
- Called from: ExternalImport.js (on mount and while polling a sync every 3 s, up to 20 ticks)
- Response 200: `{ "configured": bool /* X_CLIENT_ID set */, "connected": bool, "revoked": bool, "handle": str|null, "last_synced_at": str|null, "last_sync_created": int|null }`

##### `GET /api/external/twitter/connect` (browser navigation, not XHR)
- Backend: routes/external.py:twitter_connect
- Auth: login (session cookie must be sent, so the request has to be a top-level navigation carrying the cookie)
- Called from: ExternalImport.js, as a plain link `<a href="${REACT_APP_BACKEND_URL}/api/external/twitter/connect">` ("Connect X" / "Reconnect X").
- Flow, step by step:
  1. Browser GETs `https://<backend>/api/external/twitter/connect`. Server generates PKCE `verifier` + `state` and stores them in the Flask signed session cookie under `session["x_oauth"] = {"verifier", "state"}`, which means a `Set-Cookie: session=...` comes back on this response. Responds `302 Location: https://twitter.com/i/oauth2/authorize?response_type=code&client_id=…&redirect_uri=<X_REDIRECT_URI>&scope=tweet.read users.read bookmark.read offline.access&state=…&code_challenge=…&code_challenge_method=S256`.
  2. X redirects to `X_REDIRECT_URI`, which defaults to `https://loore.org/api/external/twitter/callback`: a backend URL, registered with X.
  3. `GET /api/external/twitter/callback` (routes/external.py:twitter_callback, `@login_required`) pops `session["x_oauth"]`, checks `state`, exchanges the code, calls `GET https://api.twitter.com/2/users/me`, stores tokens on `ExternalAccount(provider="twitter")`, marks any unread `x_disconnected` notifications read, and logs a $0.010 cost row. It then responds `302 Location: <FRONTEND_URL>/import?x_connect=ok`. Any failure (missing/mismatched state, missing code, token exchange or /users/me error) gives `302 <FRONTEND_URL>/import?x_connect=failed`.
- Cookies/session relied on: the Flask-Login session (and/or `remember_token`) cookie on both the connect and callback requests (`SameSite=Lax`, which is sent on top-level GET navigations coming back from x.com), plus the Flask `session` cookie carrying `x_oauth` between steps 1 and 3. If the callback arrives without a logged-in session, the unauthorized handler returns `401 {"error":"Unauthorized"}` JSON (because the path starts with `/api`) instead of redirecting.
- Errors: 503 `{"error": "X API is not configured (set X_CLIENT_ID / X_REDIRECT_URI)."}` on connect when unconfigured.
- Notes: the frontend does not read `x_connect`; it just re-fetches `/external/twitter/status` on mount. For iOS, this flow ends on a web URL (`FRONTEND_URL/import?...`). Without backend changes, the options are a WKWebView that holds the session cookie and watches for navigation to `/import?x_connect=`, or opening it in Safari with no way to return. `ASWebAuthenticationSession` with an https callback would need an associated-domains (apple-app-site-association) file, or a custom-scheme redirect, and either is a backend change.

##### `POST /api/external/twitter/sync`
- Backend: routes/external.py:twitter_sync
- Auth: login
- Called from: ExternalImport.js `syncX`. The client then polls `/external/twitter/status` + `/external/items?per_page=1` every 3 s until `last_synced_at` changes (reports `last_sync_created`) or `revoked` becomes true, and gives up after 20 ticks.
- Response 202: `{ "task_id": str, "status": "pending" }`
- Errors: 400 `X account not connected`.
- Notes: Celery `sync_twitter_bookmarks` (pay-per-use X API cost billed to the user).

##### Personal API tokens: `GET /api/external/tokens`, `POST /api/external/tokens`, `DELETE /api/external/tokens/<token_id>`
- Backend: routes/external.py:list_tokens / create_token / revoke_token
- Auth: login (session only; a token cannot mint tokens)
- Called from: ExternalImport.js (clipper card, rendered only when `user.external_content_enabled`); `NewTokenDialog.js` displays the plaintext once.
- Token JSON: `{ "id": int, "name": str, "prefix": str /* 8 chars after "loore_" */, "scope": "external:write", "created_at": str|null, "last_used_at": str|null }`
- GET 200: `{ "tokens": [Token] }` (active only, newest first)
- POST request `{ "name"?: str /* default "Chrome clipper", max 64 */ }` -> 201 `Token + { "token": "loore_<43 url-safe chars>" }` (plaintext shown only here). Errors: 403 `Turn on external content under Account first.` (`external_content_enabled` false); 400 `You already have 10 active tokens; revoke one first.`
- DELETE 200: `{ "revoked": true, "id": int }`; 404 `not found`.
- Notes: tokens have a single scope, `external:write`. It authorizes only `POST /api/external/clip` and `GET /api/external/clip/status`, so it cannot serve as a general native-app credential without backend changes.

##### `POST /api/external/clip` and `GET /api/external/clip/status` (Chrome extension)
- Backend: routes/external.py:clip / clip_status
- Auth: `@token_or_login_required("external:write")`: `Authorization: Bearer loore_…` or a session. A present but invalid bearer returns 401 `{"error": "invalid_token"}` even when a session cookie exists. The bearer path enforces `approved` and not-deactivated itself.
- Called from: `extension/background.js` (clip) and `extension/options.js` (status). The web app does not call them.
- clip request: `{ "url": "http(s)://…", "content": str, "title"?: str, "author"?: str, "posted_at"?: ISO str, "author_protected"?: true }`. Responses: 201 `{ "created": true, "id", "source", "title", "truncated": bool }`; 200 for an already-known URL `{ "created": false, "updated": bool, "id", "source", "title", "truncated": bool }`. Errors 400 `url is required` / `content is required`. Content is truncated (not rejected) at 100,000 chars.
- status 200: `{ "ok": true, "username": str, "counts": {source: int}, "token_name": str|null }`.
- Notes: a native share extension could reuse this route: the user mints a token in the app via `POST /api/external/tokens`, and the extension posts clips with it.

---

#### 2.5.4 Share (publishing to the public side)

Gating: every member route returns `404 {"error": "Not found"}` unless `SHARE_V1` is on AND `current_user.public_sharing_enabled` (the per-user Account toggle). The frontend reads the combined flag as `user.share_v1_enabled` from `/api/dashboard` (HomePage shows the Share card only then). `public/<username>` needs only `SHARE_V1`.

Share JSON (`_serialize`):
```
{ "id": int, "content": str,
  "share_type": "need" | "offering" | "insight" | "exploration" | "intention" | "other",
  "status": "draft" | "published" | "revoked",
  "source_node_id": int|null,      // private node the proposal came from
  "public_node_id": int|null,      // the public root node created on publish
  "permalink": str|null,           // "/@<username>/<slug>" while that node is public, else null
  "created_at": str, "updated_at": str|null, "published_at": str|null, "revoked_at": str|null }
```

##### `GET /api/share`
- Backend: routes/share.py:list_shares; Auth: login + gate; Called from: pages/SharePage.js
- Response 200: `{ "shares": [Share] }` newest first. The page groups them by `status`.

##### `POST /api/share`
- Backend: routes/share.py:create_share; Auth: login + gate; Called from: SharePage.js
- Request: `{ "content": str, "share_type"?: str }` (unknown type becomes `other`)
- Response 201: Share (status `draft`). Errors: 400 `content is required`.

##### `PATCH /api/share/<share_id>`
- Backend: routes/share.py:update_share; Auth: login + gate (owner); Called from: SharePage.js
- Request: `{ "content"?: str, "share_type"?: str }`
- Response 200: Share. Errors: 404; 409 `Revoke before editing a published share`; 400 `content cannot be empty` / `invalid share_type`.

##### `DELETE /api/share/<share_id>`
- Backend: routes/share.py:delete_share; Auth: login + gate (owner); Called from: SharePage.js
- Response 200: `{ "status": "deleted" }` (soft-deletes the public node too). Errors: 404.

##### `POST /api/share/<share_id>/publish`
- Backend: routes/share.py:publish_share; Auth: login + gate (owner); Called from: SharePage.js
- Response 200: Share (status `published`, `public_node_id`, `permalink`). Idempotent for an already-published share. Republishing unchanged content revives the same public node, so its replies reattach. Errors: 404.

##### `POST /api/share/<share_id>/revoke`
- Backend: routes/share.py:revoke_share; Auth: login + gate (owner); Called from: SharePage.js
- Response 200: Share (status `revoked`). Errors: 404; 409 `Only published shares can be revoked`.

##### `POST /api/share/save-proposal`
- Backend: routes/share.py:save_proposal; Auth: login + gate
- Called from: components/ProposalInline.js (the Save button on each `:::share` block in a node)
- Request: `{ "node_id": int /* or "llm_node_id" */, "share_index"?: int /* 0-based block index; omit to save all unsaved blocks */ }`
- Response 200: `{ "status": "completed" | "partial", "share": Share /* first saved by this call */, "shares": [Share], "saved_indexes": [int], "total": int }`
- Errors: 400 `node_id is required` / `Invalid share_index` / `Already saved` / parse error text; 404 `Node not found` / `Origin node not found` / `No pending share found`.

##### `GET /api/share/public/<username>` (no auth)
- Backend: routes/share.py:public_shares
- Auth: none (public)
- Called from: pages/PublicSharePage.js (route `/@<username>`)
- Response 200:
```
{ "username": str,
  "shares": [ { "id": int, "content": str /* first 600 chars + "…" */, "share_type": str|null,
                "public_node_id": int, "permalink": str|null, "pinned": bool, "published_at": str } ],
  "canonical"?: "/@<current_username>" }   // present when a former handle or case variant resolved
```
- Errors: 404 `Not found` for unknown user, sharing off, flag off, or a former handle with no public roots. The frontend treats every 404 the same way ("notfound").
- Notes: it lists all of the user's living public root nodes, pinned first, not only those that came from share drafts.

---

#### 2.5.5 Commons (public forum)

##### `GET /api/commons/feed`
- Backend: routes/commons.py:feed
- Auth: login; 404 `Not found` unless `SHARE_V1` and `current_user.public_sharing_enabled`
- Called from: pages/CommonsPage.js (`?page=N`, loads more on `has_more`)
- Query: `page` (default 1), `per_page` (default 20, max 100)
- Response 200: `{ "items": [ { "id": int, "username": str|null, "permalink": str|null, "content": str /* 600-char preview */, "created_at": str, "reply_count": int } ], "has_more": bool, "page": int }`

##### `GET /api/commons/permalink/<username>/<slug>` (no auth)
- Backend: routes/commons.py:resolve_permalink
- Called from: App.js `PermalinkRoute` (route `/@<username>/<slug>`). A logged-in user is shown the full NodeDetail for the returned id; a logged-out visitor gets PublicThreadPage.
- Response 200: `{ "node_id": int, "canonical"?: "/@<current>/<slug>" }`; 404 `Not found` for anything non-public or flag off.

##### `GET /api/commons/node/<node_id>` (no auth)
- Backend: routes/commons.py:public_thread
- Called from: pages/PublicThreadPage.js
- Response 200:
```
{ "thread": PublicNode,       // rooted at the nearest public ancestor
  "focus_id": int,            // the requested node
  "truncated": bool }         // true when the 500-node budget ran out
PublicNode = { "id": int, "username": str|null /* for llm nodes: the human who asked */,
               "node_type": "user" | "llm" | ..., "llm_model": str|null,
               "content": str, "created_at": str, "children": [PublicNode] }
```
- Errors: 404 `Not found` for missing, private, deleted or author-opted-out nodes, and when the flag is off.

---

#### 2.5.6 Admin (compact)

Admin detection: `User.is_admin` (DB column). The server checks it in `admin_required` (`abort(403)` gives Flask's default HTML 403 page, not JSON). The frontend reads `user.is_admin` from `GET /api/dashboard`: NavBar shows the `/admin` link (AdminPanel), and HomePage/NodeDetail/SearchModal/SemanticNeighbors show admin-only extras. Every route below is `login` + `admin_required`, under `/api/admin`. Requests to `/api/admin/*` do not update `last_seen`.

| Method | Path | Request | Response 2xx | Notable errors | Frontend |
|---|---|---|---|---|---|
| GET | `/users` | - | `{users:[{id, twitter_id, username, description, created_at, accepted_terms_at, approved, email, plan, deactivated_at, total_spending_usd, current_month_spending_usd, cache_hit_rate, cache_served_tokens, cache_input_tokens, spend_limit_usd, spend_limit_is_override, spend_blocked, profile_batch_pending, profile_force_batch, prefilled_handle, prefill_consent, prefill_consent_at, spam, intentions, profile:{versions, last_generation_type, last_created_at, state, waiting, seed_error}}], cache_since, allowed_plans:[str], per_user_limit_default_usd}` | - | AdminPanel |
| POST | `/users/<id>/toggle` | - | `{message, approved}` | 404 | AdminPanel |
| GET | `/activity?days=` (7-60, default 14) | - | `activity_report()` dict | - | AdminPanel |
| POST | `/users/<id>/build_profile` | - | 202 `{message, queued:true}` or 200 `{message, queued:false}` | 400 `{error, code}` (prefill refusal) | AdminPanel |
| POST | `/users/<id>/infer_intentions` | `{mode?: "batch"\|"sync", cancel_pending?: bool}` | 202 `{task_id, mode, cancelled, errors}` | 400 | AdminPanel |
| POST | `/users/<id>/cancel_intentions` | - | `{cancelled, errors, message?}` | - | AdminPanel |
| POST | `/users/<id>/toggle_spam` | - | `{message, spam}` | - | AdminPanel |
| PUT | `/users/<id>/update_email` | `{email}` | `{message, email}` | 400, 409 | AdminPanel |
| PUT | `/users/<id>/update_plan` | `{plan}` | `{message, plan}` | 400 | AdminPanel |
| PUT | `/users/<id>/update_spend_limit` | `{limit_usd: number >= 0}` | `{message, limit_usd, spend_blocked, current_month_spending_usd}` | 400 | AdminPanel |
| POST | `/whitelist` | `{handle, x_lookup?: bool}` | 201 `{message, source, matched:{username, display_name}, user:{id, username, twitter_id, approved, accepted_terms_at, email}}` | 400; 404/502 `{error, reason, x_lookup_cost_usd?}` (reason `not-in-archive`/`not-on-x`/`archive-error`/`x-error`; 409 `ambiguous`); 409 `{error, user_id}` already exists | AdminPanel (+XLookupConfirmDialog for the paid lookup) |
| POST | `/users/<id>/activate_and_welcome` | - | `{message, approved:true, email_sent: bool}` | 400 no email | AdminPanel |
| POST | `/users/<id>/prefill` | `{handle?, include_replies?, force_parquet?}` | 202 `{task_id, handle}` | 400 (incl. `{error, code}` refusal) | AdminPanel |
| GET | `/prefill/check?handle=&user_id=` | - | Community Archive coverage summary + `import_source`, `profile_threshold_tokens`, `already_imported?` | 400, 404, 502 | AdminPanel |
| GET | `/prefill/x/check?handle=&user_id=&max_tweets=` | - | `{timeline_cap, fetchable, ...}` | 400, 404, 502 | AdminPanel |
| POST | `/users/<id>/prefill-x` | `{handle?, max_tweets?, include_replies?}` | 202 `{task_id, handle, max_tweets}` | 400 | AdminPanel |
| GET | `/prefill/status/<task_id>` | - | `{task_id, status: queued\|running\|completed\|failed, done, total, stage: "fetching"\|"importing"\|null, result, error}` | - | AdminPanel (polls) |
| GET | `/spend` | - | `{month, total_usd, anthropic_usd, openai_usd, limit_usd, limit_fraction_used, per_user_limit_usd, users_blocked_this_month, thresholds, alerts_fired:[{threshold, spend_usd, at}]}` | - | not called |
| GET | `/feedback?status=` | - | `{feedback:[{id, user_id, username, content, category, source, status, created_at}]}` (max 500) | - | AdminPanel |
| PUT | `/feedback/<id>` | `{status: new\|reviewed\|done}` | `{id, status}` | 400 | AdminPanel |
| POST | `/polls` | `{question, model_id?, data_source?}` | 201 `{id, question, model_id, data_source, created_at}` | 400 | AdminPanel |
| GET | `/polls` | - | `{default_model_id, polls:[{id, question, model_id, data_source, created_at, closed_at, sent_count, declined_count}]}` | - | AdminPanel |
| GET | `/polls/<id>/responses` | - | `{poll:{id, question}, responses:[{id, username, content, llm_drafted, sent_at}]}` | 404 | AdminPanel |
| POST | `/polls/<id>/close` | - | `{id, closed_at}` | 404 | AdminPanel |
| GET | `/voice-timing?days=&limit=` | - | `{days, stages, median, users, turns}` | - | not called (console/dev use) |

`activate_and_welcome` is relevant to auth: it builds the welcome magic link as `request.host_url + "/auth/magic-link/verify?token=…"` (a backend URL, `next_url="/welcome"`, 30-day expiry) and emails it.

---

#### 2.5.7 Webhooks

##### `POST /api/webhooks/github`
- Backend: routes/webhooks.py:github_webhook
- Auth: HMAC `X-Hub-Signature-256` with `GITHUB_WEBHOOK_SECRET` (503 when unset, 403 on bad signature). Server-to-server only; no client use.
- Effect: on an `issues` `closed` event for an issue labelled `loore:<username>`, creates a `fix_ready` (or similar) UserNotification that the user sees via `/api/updates`.

---

#### 2.5.8 Server-rendered public pages (HTML, no JSON)

Blueprint `public_pages` (no prefix). nginx routes `/@…`, `/node/<id>`, `/sitemap.xml`, `/`, `/landing`, `/why-loore`, `/vision`, `/how-to`, `/login`, `/alpha-thank-you` to Flask. Everything returns HTML (the built SPA `index.html` with injected metadata and article content), Atom XML, Markdown, or PNG. The SPA never calls these with XHR; its JSON equivalents are `/api/share/public/<username>`, `/api/commons/permalink/...` and `/api/commons/node/<id>`.

| Path | Returns |
|---|---|
| `/@<username>` | profile HTML (404 shell if no public roots) |
| `/@<username>/<slug>` | article HTML; 410 shell for revoked/deleted within grace; 301 to current handle |
| `/@<username>/<slug>.md` | `text/markdown` article |
| `/@<username>/feed.xml` | Atom feed |
| `/@<username>/og.png`, `/@<username>/<slug>/og.png` | `image/png` social cards |
| `/node/<node_id>` | public node HTML |
| `/sitemap.xml` | XML |
| `/`, `/landing`, `/why-loore`, `/vision`, `/how-to`, `/login`, `/alpha-thank-you` | SPA shell with marketing metadata |

For iOS: none are needed. The `https://loore.org/@user/slug` and `/node/<id>` URLs are the ones a universal-links setup would claim, if one is added later.

---

#### 2.5.9 Routes in these files the web frontend never calls

| Method + path | Used by |
|---|---|
| `GET /api/export` | nobody (legacy JSON export) |
| `DELETE /api/delete_my_data` | nobody (no UI) |
| `POST /api/external/clip` | Chrome extension (`extension/background.js`, bearer token) |
| `GET /api/external/clip/status` | Chrome extension (`extension/options.js`, bearer token) |
| `GET /api/external/twitter/callback` | X's OAuth redirect (browser navigation; the frontend links only to `/connect`) |
| `GET /api/admin/spend` | nobody |
| `GET /api/admin/voice-timing` | nobody in the UI (dev/diagnostics) |
| `POST /api/webhooks/github` | GitHub |
| all `public_pages` routes | crawlers, no-JS clients, direct browser loads |

Count for this section: 72 JSON endpoints (import 9, export 4, external 22 counting POST/DELETE `/read` separately, share 8, commons 3, admin 25, webhooks 1) plus 14 HTML/XML/PNG public-page routes.

---

## 3. Auth end to end

Sources: `backend/routes/auth.py`, `backend/oauth.py`, `backend/utils/magic_link.py`, `backend/__init__.py`, `backend/config.py`, `backend/routes/terms.py`, `backend/routes/dashboard.py` (email flow), Flask-Login 0.6.3 (installed version), `frontend/src/components/LoginPage.js`, `contexts/UserContext.js`, `components/ProtectedRoute.js`, `App.js`, `components/TermsModal.js`, `pages/AlphaThankYouPage.js`, `pages/ConfirmEmailPage.js`, `pages/AccountPage.js`.

### 3.1 Cookies

| Cookie | Set by | Attributes (prod) | Lifetime | Content |
|---|---|---|---|---|
| `session` | Flask signed-cookie session (itsdangerous) | `Secure` (unless `SESSION_COOKIE_SECURE=false`, local dev only), `HttpOnly`, `SameSite=Lax`, `Path=/`, no `Domain` (host-only `loore.org`), no `Expires` (browser-session cookie) | Client: until the app/browser session ends. Server: the signature is accepted for `PERMANENT_SESSION_LIFETIME` = 31 days from when the cookie was last written (Flask default). The session is only rewritten when modified, so in practice ~31 days from login. | `_user_id`, `_fresh`, `_id` (sha512 of IP + User-Agent), flask-dance X token, `next_url`, `x_connect`, `x_oauth_request_token` |
| `remember_token` | Flask-Login `login_user(user, remember=True)` (both login paths pass `remember=True`) | `Secure`, `HttpOnly` (Flask-Login default), `SameSite=Lax`, `Path=/`, host-only | `Expires` = login + 30 days (`REMEMBER_COOKIE_DURATION`). `REMEMBER_COOKIE_REFRESH_EACH_REQUEST` is not set, so the expiry does not slide. | `"<user_id>\|<hmac_sha512_hex(user_id, SECRET_KEY)>"`. **No timestamp: the server accepts it indefinitely** (until `SECRET_KEY` changes). |

How a request is authenticated (Flask-Login 0.6.3 `_load_user`): `session["_user_id"]` first; if absent and a `remember_token` cookie is present, the user is loaded from it, `session` is repopulated (`_fresh = False`) and a new `session` cookie comes back on that response. There is no request/header loader, so `Authorization` headers are ignored everywhere except the two clipper routes.

Session protection is Flask-Login's default `"basic"`: when IP or User-Agent differ from the values at login, the session is only marked non-fresh; nothing uses `fresh_login_required`, so IP/UA changes (mobile networks, cookies copied from a WKWebView with a different UA) have no effect.

Logout (`GET /auth/logout`) calls `logout_user()`, which clears the session keys and tells the browser to delete `remember_token`.

### 3.2 Magic link (email) sign-in and sign-up

1. **Send.** `POST /auth/magic-link/send`, JSON `{"email": "<addr>", "next_url": "<relative path, optional>"}`. No auth.
   - Email is trimmed and lower-cased; invalid → `400 {"error": "Please enter a valid email address."}`.
   - `next_url` must be a safe relative path (`/...`, not `//`, no scheme/host); otherwise it is silently dropped.
   - Token = itsdangerous `URLSafeTimedSerializer(SECRET_KEY)` over `{"email", "next_url"?}` with salt `"magic-link"`. If an account with that email exists, `sha256(token)` and expiry (now + `MAGIC_LINK_EXPIRY_SECONDS`, default 900 s) are stored on the user; for an unknown email nothing is stored.
   - The mailed URL is `request.host_url + "/auth/magic-link/verify?token=<token>"`, i.e. the **backend host of the send request** (`https://loore.org/auth/magic-link/verify?token=…` in prod; a request to staging gets a staging link). The email's plain-text part contains the full URL; the HTML part has a button linking to it. The token is long (base64 payload + timestamp + signature, ~100+ chars); there is no short code.
   - Always `200 {"message": "If that email is associated with an account, you'll receive a sign-in link shortly. If you're new, a link has been sent to create your account."}` (also when sending fails, to prevent enumeration).
2. **Verify.** `GET /auth/magic-link/verify?token=<token>`. Always answers with a **302 redirect to `FRONTEND_URL`**, never JSON:
   - Bad signature, wrong salt or older than `MAGIC_LINK_EXPIRY_SECONDS` → `302 {FRONTEND_URL}/login?error=invalid_or_expired`.
   - Existing account whose stored hash differs (a newer link was requested, or the account was created by an earlier use of this same link) → `302 {FRONTEND_URL}/login?error=link_already_used`.
   - No account, and `next_url` is `/confirm-email…` → `302 {FRONTEND_URL}/login?error=confirm_needs_account&returnUrl=<next>` (sign-in on the way to confirming an email change never creates an account).
   - No account otherwise → creates `User(username=<derived from email local part>, email, approved=False)`.
   - Success → `login_user(user, remember=True)` and `302 {FRONTEND_URL}{next_url}` or `302 {FRONTEND_URL}/dashboard`. **The `Set-Cookie` headers for `session` and `remember_token` are on this 302 response itself.** (The web app's `/dashboard` route then client-side redirects to `/profile`.)
   - Admin "Activate & welcome" (`POST /api/admin/users/<id>/activate_and_welcome`) mails the same kind of link with `next_url="/welcome"` and a 30-day `max_age` embedded in the token, so welcome links stay valid far longer than the 15-minute sign-in links.
   - Reuse: for an **existing** account the token is not cleared on use ("so email-client prefetch doesn't consume it"); it works any number of times until it expires (15 min) or a newer link is requested. For a **new** email the first successful verify creates the account without storing a hash, so any second use of the same link returns `link_already_used`. Consequence for native: a sign-up link opened in Safari first can no longer be used in the app; a sign-in link for an existing account can.
3. **Frontend.** `LoginPage.js` posts `{email, next_url: returnUrl}` (default `returnUrl` is `/dashboard`) and shows "check your email". Error codes it maps from `?error=`: `invalid_or_expired`, `link_already_used`, `confirm_needs_account`, `x_try_again`.

### 3.3 "Sign in with X" (OAuth 1.0a via flask-dance)

Redirect chain (every hop is a browser redirect; X's callback URL is registered on the backend host):

1. Browser → `GET {BACKEND}/auth/login?next=<relative path>` (`LoginPage.handleTwitterLogin`). `next` is stored in `session["next_url"]` if safe.
2. Not yet authorized with X → `302 /auth/twitter` (flask-dance `twitter.login`). The `oauth_before_login` hook stores the request token in `session["x_oauth_request_token"]`.
3. `302 https://api.twitter.com/oauth/authenticate?oauth_token=…` — the person signs in at X.
4. X → `GET {BACKEND}/auth/twitter/authorized?oauth_token=…&oauth_verifier=…`. `_refuse_foreign_callback` requires the `oauth_token` to equal the one saved in *this* session cookie (login-CSRF protection); a mismatch → `302 {FRONTEND_URL}/login?error=x_try_again`. flask-dance exchanges the token, stores it in the session, → `302 /auth/login`.
5. `/auth/login` again: calls X `account/verify_credentials.json`; failure → token dropped, `302 {FRONTEND_URL}`. Finds the user **by numeric X id only** (never by handle); unknown id → creates `User(twitter_id, twitter_handle, username=<derived from handle>)` with `approved=False` (column default). Refused (no account creation) when `next_url` is `/confirm-email…` → `302 {FRONTEND_URL}/login?error=confirm_needs_account&returnUrl=…`.
6. `login_user(user, remember=True)` → `302 {FRONTEND_URL}{next_url}` or `302 {FRONTEND_URL}/dashboard`, with the auth cookies on this response.

Cancel at X: X returns with `denied=…`, flask-dance redirects to `/auth/login`, which (not authorized) starts the flow again (step 2).

### 3.4 Connect X to an existing account (#311) and X bookmarks

- `GET /auth/x/connect` (Account page link). Signed out → `302 {FRONTEND_URL}/login?returnUrl=%2Faccount`. Signed in → stores `session["x_connect"] = {"user_id"}`, drops any cached X token, `302 /auth/twitter` → X → callback → `/auth/login`, which sees the connect intent and ends with `302 {FRONTEND_URL}/account?x_login=<outcome>#x`, outcome one of `linked`, `other_x` (account already has a different X id), `taken` (X id belongs to another account), `taken_placeholder` (belongs to a never-signed-in placeholder account), `cancelled`, `failed`. `AccountPage.js` maps these via `X_LOGIN_MESSAGES`.
- X bookmark sync uses a **separate OAuth 2 PKCE** flow: top-level navigation to `GET /api/external/twitter/connect` (stores `session["x_oauth"] = {verifier, state}`, `302` to `https://twitter.com/i/oauth2/authorize?…`) → X → `X_REDIRECT_URI` (default `https://loore.org/api/external/twitter/callback`, `@login_required`; without a session it answers `401` JSON, not a redirect) → `302 {FRONTEND_URL}/import?x_connect=ok` or `?x_connect=failed`. Details in §2.5.3.

### 3.5 Logout

`GET /auth/logout[?next=<relative path>]`, `@login_required`. Clears the session, the remember cookie, the stored X token and any connect intent, then `302 {FRONTEND_URL}{next}` or `302 {FRONTEND_URL}`. **Gotcha:** called without a valid session, `@login_required` on this non-`/api` path redirects to `/auth/login`, which starts X OAuth. A native client should send it with redirects disabled and clear its own cookie store regardless of the answer.

### 3.6 Gating after sign-in (what the app must check, in order)

The single bootstrap call is `GET /api/dashboard/` (UserContext). `401` → signed out. On `200`, the `user` object drives everything (full shape in §2.4.1 and §5):

1. `user.approved == false` → web app routes to `/alpha-thank-you` (waitlist page). Only the dashboard GETs, `PUT /api/dashboard/user`, the email endpoints, and `/api/terms/*` work; everything else is 403. The waitlist page lets the user leave an email (`POST /api/dashboard/email`) and shows a pending-email state (`pending_email`, `pending_email_expired`), plus the tweets pre-fill consent card (`PrefillConsentCard`).
2. `user.terms_up_to_date == false` (i.e. `accepted_terms_version != "2.0"`; `CURRENT_TERMS_VERSION` in `routes/terms.py`) → blocking Terms modal. The terms text is **hard-coded in `frontend/src/components/TermsModal.js`** (326 lines); the native app must ship the same text. Accept = `POST /api/terms/accept` (no body) → `200 {"message": "Terms accepted", "accepted_terms_at": iso, "accepted_terms_version": "2.0"}`; for an unapproved user this also emails the admins a signup notice. After accepting, an unapproved user is sent to `/alpha-thank-you`.
3. Approved and terms current → app. Once per session the web app then calls `GET /api/updates` and opens the updates modal if anything is unread.
4. `user.spend_blocked == true` → mark spend-capped for the session (block starting recordings up front).
5. If the device time zone differs from `user.timezone` → `PATCH /api/dashboard/timezone {"timezone": "<IANA>"}`. (This route is not in the approval exemption list, so it returns 403 for a waitlisted user; only send it when `approved`.)

Admin detection: `user.is_admin` (boolean DB column, from the dashboard payload). The web app shows the Admin nav item, admin-only search options, semantic neighbours and "re-run read" when it is true. Every `/api/admin/*` route checks `current_user.is_admin` server-side (not the username). Plan gating: `user.plan` in `{"free","alpha","pro"}`; voice mode requires `alpha`/`pro` or admin (`User.has_voice_mode`).

### 3.7 Email confirmation / email change (#260)

- `POST /api/dashboard/email {"email"}` sends a confirmation mail whose link is **`{FRONTEND_URL}/confirm-email?token=<token>`** (a frontend URL, not a backend one). Token salt `"email-change"`, lifetime `EMAIL_CHANGE_EXPIRY_SECONDS` (default 86400 s). It is not a sign-in token.
- `ConfirmEmailPage.js` reads `?token=` and, once signed in as the account that asked, calls `POST /api/dashboard/email/confirm {"token"}`. Signed out, it asks the person to sign in (with `returnUrl=/confirm-email?token=…`); signed in as the wrong account it offers `/auth/logout?next=/confirm-email?token=…`.
- `DELETE /api/dashboard/email/pending` cancels a pending change; `DELETE /api/dashboard/email` removes the address. Shapes in §2.

### 3.8 CORS

`CORS(app, supports_credentials=True, origins=[FRONTEND_URL])`. CORS is enforced by browsers only; nothing here blocks a native client sending cookies from `URLSession`.

## 4. Native auth options

Key facts that decide this: auth is cookie-only; both login flows end in a **302 to `FRONTEND_URL`** with the cookies set on that 302; the magic link points at `https://loore.org/auth/magic-link/verify`; the backend can only redirect to `FRONTEND_URL + <relative path>` (no custom schemes); there is no endpoint that returns a token or JSON on login; the `remember_token` cookie is a durable credential with no server-side expiry.

### 4.1 Options that need NO backend change

**A. Magic link, pasted into the app (recommended primary path).**
1. App: `POST https://loore.org/auth/magic-link/send` with `{"email": e}` (omit `next_url`, or pass a marker such as `"/profile"`).
2. The person opens Mail, long-presses the "Sign in to Loore" button (or copies the URL from the plain-text part) and pastes the URL into the app. The app accepts either the full URL or the bare token and extracts `token`.
3. App: `GET https://loore.org/auth/magic-link/verify?token=<t>` with a `URLSessionTaskDelegate` that returns `nil` from `willPerformHTTPRedirection` (stop at the 302). Read `Location`: path `/login` with `?error=` → map to the §3.2 error messages; anything else = success. `Set-Cookie` on that 302 is stored in `HTTPCookieStorage.shared` automatically (default `httpShouldSetCookies = true`; verify in the prototype, and if needed parse `HTTPCookie.cookies(withResponseHeaderFields:for:)` from the 302 yourself).
4. App: `GET /api/dashboard/` to confirm and load the user; continue with §3.6 gating.
5. Persist: copy `remember_token` (and `session`) into the Keychain; on launch put them back into `HTTPCookieStorage`. `session` is a session-only cookie and `HTTPCookieStorage` drops it on app termination; `remember_token` re-creates the session on the next request. Because the server never checks the remember token's age, re-injecting it after its 30-day `Expires` keeps the app signed in. That relies on a Flask-Login property rather than a designed feature, and it makes the Keychain copy a long-lived credential that logout does not revoke; the design doc should decide whether to rely on it or accept a monthly re-login.
   Caveats: 15-minute lifetime; a newer link invalidates older ones; a **sign-up** link works once only (§3.2), so a new user who taps it in Safari first must request another; UX friction of copy/paste.

**B. Everything inside a `WKWebView` (works for X and as a magic-link fallback).** Load `https://loore.org/auth/login?next=/ios-signed-in` (X) or `https://loore.org/login` in a `WKWebView` with the default persistent `WKWebsiteDataStore`. In `decidePolicyFor navigationAction`, when the URL is `https://loore.org/ios-signed-in` (or `/dashboard`, `/profile`), cancel the navigation, then `WKHTTPCookieStore.getAllCookies` (includes HttpOnly cookies) and copy `session` + `remember_token` for `loore.org` into `HTTPCookieStorage.shared` / Keychain. The request-token check in `backend/oauth.py` is satisfied because the whole OAuth round trip stays in one cookie jar. Caveats: X's login page inside an embedded web view may be degraded or blocked for accounts that sign in to X via Google/Apple (Google refuses embedded web views); App Store review generally accepts web-view login for first-party sites. `next` must be a safe relative path; `/ios-signed-in` does not exist in the SPA, which is harmless because the app cancels the navigation (if not cancelled, the SPA's `*` route redirects to `/`).

**C. Connect X, X bookmark connect, and email confirmation** also end in frontend redirects (`/account?x_login=…`, the import page, `/confirm-email?token=…`). Run them in a `WKWebView` after injecting the app's cookies into `WKHTTPCookieStore`, and detect the final URL. The email-change confirmation can alternatively be done natively: the person pastes the `/confirm-email?token=…` link and the app calls `POST /api/dashboard/email/confirm {"token"}` (pure JSON, no redirect).

`ASWebAuthenticationSession` is not usable without backend changes: it needs a custom-scheme callback (or, on iOS 17.4+, an `https` callback whose host is in the app's associated domains), the backend cannot redirect to one, and its cookies land in Safari's jar rather than the app's `URLSession` jar.

### 4.2 Options that need SMALL backend changes

| Change | What it enables | Size / where |
|---|---|---|
| Serve `/.well-known/apple-app-site-association` with an `applinks` entry for `/auth/magic-link/verify*` (and optionally `/confirm-email*`) | Tapping the mailed link opens the app directly (universal link); the app extracts the token and runs option A step 3. No paste. Note that every iPhone with the app installed would then open sign-in links in the app, not Safari. | Static JSON. Either `frontend/public/.well-known/apple-app-site-association` (CRA copies it into `build/`, nginx `location /` serves it) plus an nginx `location = /.well-known/apple-app-site-association { default_type application/json; }` on the VM, or a 5-line Flask route plus an nginx proxy rule. No Python auth changes. |
| Also list `webcredentials:loore.org` in the AASA | Password-manager / `ASWebAuthenticationSession` https-callback eligibility (iOS 17.4+). | Same file. |
| `REMEMBER_COOKIE_REFRESH_EACH_REQUEST = True` in `Config` | Sliding 30-day remember cookie, so active app users are never forced to re-login and the app need not re-inject an expired cookie. | One line in `backend/config.py`; affects the web app too (harmless). |
| A short numeric code in the magic-link email plus `POST /auth/magic-link/verify-code {"email","code"}` returning JSON and setting the cookies | The cleanest native UX (type 6 digits, no link handling). | New route + email template change + code storage (hash + expiry on `User`, a column exists for the link hash only). Small but real backend work. |
| `Accept: application/json` branch on `/auth/magic-link/verify` returning `{ "ok": true, "user": … }` / `{ "error": "<code>" }` instead of a 302 | Simpler native parsing than inspecting `Location`. | ~10 lines in `routes/auth.py`. Optional; option A already works. |
| Allow a whitelisted custom-scheme `next` (e.g. `loore://auth-done`) plus a one-time handoff code endpoint | `ASWebAuthenticationSession` for X login (system browser, shares Safari's X session). | More invasive: redirect validation, a short-lived handoff code table/cache, an exchange endpoint. Only worth it if embedded-web-view X login proves unreliable. |
| Bearer-token auth for the app (extend `ApiToken` scopes + a `request_loader`) | Header auth instead of cookies. | Not needed; cookies work from `URLSession`. Listed for completeness. |

**Recommendation:** ship with option A (paste magic link, `URLSession`, stop at the 302, persist cookies in the Keychain) plus option B for "Sign in with X", and ask for the AASA file as the first backend change because it removes the paste step with no Python changes.

## 5. Data model as the frontend sees it

The backend has no shared serializer layer: each route builds its own dict, so the same entity comes back in several shapes. The field-by-field JSON for every shape is in §2 next to the endpoint that returns it; this section lists the entities, their enums, how they relate, and where each shape is defined. Swift advice: one `Codable` struct per wire shape (e.g. `NodeDetail`, `TreeNode`, `AncestorNode`, `LogCard`), all optionals where §2 says "present only when", and a small `enum` with an `unknown(String)` fallback for every string enum below (the backend adds values without versioning).

### 5.1 Enumerations

| Field | Values | Notes |
|---|---|---|
| `privacy_level` | `"private"`, `"circles"`, `"public"` | `circles` is unimplemented and behaves like private for everyone but the owner. **Quirk:** `POST /api/textmode/start` validates against `{"private","anonymous","public"}` instead (so it rejects `circles` and accepts an undocumented `anonymous`). Content of `public` nodes is stored in plaintext, others encrypted; clients do not see the difference. |
| `ai_usage` | `"none"`, `"chat"`, `"train"` | `chat`/`train` allow the AI to read the content. The web disables the record button and auto-generate when `none`. New replies default to the parent's `reply_ai_usage` (from `GET /api/nodes/<parent>`). |
| `node_type` | `"user"`, `"llm"`, `"link"` | `link` is legacy (only `POST /api/nodes/<id>/link`, unused). Treat an unknown type as `user`. |
| LLM/TTS/transcription task status | `null`, `"pending"`, `"processing"`, `"completed"`, `"failed"`; LLM also `"cancelled"` | Same vocabulary on `Node.llm_task_status`, `tts_task_status`, `transcription_status`, and the status endpoints. |
| Draft `streaming_status` | `"recording"` → `"finalizing"` → `"completed"` \| `"failed"` | Recording sessions (§2.3.2). |
| Transcript chunk `status` | `"stored"`, `"processing"`, `"pending"` (legacy), `"completed"`, `"failed"` | |
| `plan` | `"free"`, `"alpha"`, `"pro"` | Voice mode = `alpha`/`pro`/admin (`voice_mode_enabled` on the user). Unrestricted `{user_export}` = `pro`/admin. |
| `origin` (Node) | `null` (written in Loore), `"twitter"`, `"chatgpt"`, `"claude"`, `"markdown"` | Import source badge. |
| Reference `source` | `"community_archive"`, `"twitter_bookmark"`, `"twitter_like"`, `"web_clip"`, `"read_pick"` | `read_pick` = picked by a Read, not saved; excluded from the references list. |
| Reference `feedback` | `"good"`, `"bad"`, `null` | |
| Share `share_type` / `status` | `need`, `offering`, `insight`, `exploration`, `intention`, `other` / `draft`, `published`, `revoked` | |
| Profile `generation_type` | `"initial"`, `"update"`, `"iterative"`, `"revert"`, `"integration"`, `null`, … | Open set. |
| `generated_by` (profile/todo/artifact/prompt versions) | `"user"`, `"revert"`, `"default"` (prompts), or a model id | |
| Prompt keys | `profile_generation`, `profile_update`, `profile_integration`, `narrative_detection`, `letter_from_the_future`, `orient_apply_todo`, `voice`, `textmode`; admin-only `read`, `read_thread`; hidden `reflect`, `orient` | `Node.prompt_key` on a thread root is `voice` or `textmode` for agentic sessions (or `read`). |
| Artifact `kind` | built-in `memory`, `scratchpad`, `predictions`, `ai_preferences`, `intentions`; custom slugs `^[a-z0-9][a-z0-9_-]{0,47}$` | `INLINE_KINDS` (shown inline in system-prompt context) = memory, scratchpad, ai_preferences, intentions. |
| Poll response `status` | `"drafting"`, `"draft"`, `"draft_failed"`, `"sent"`, `"declined"` | |
| Notification `type` | `"profile_ready"`, `"briefing_ready"`, `"fix_ready"`, `"x_disconnected"`, … | Open set; `link` is a web path or absolute URL. |
| `prefill_consent` | `"yes"`, `"no"`, `null` | Waitlist-page consent card. |
| Model ids | keys of `Config.SUPPORTED_MODELS` (dotted, e.g. `"claude-opus-4.6"`, `"gpt-6-luna"`) | Always fetch the list from `GET /api/nodes/models`; never hard-code. LLM nodes' author `username` equals the model id. |

### 5.2 Entities, relations and where their shapes live

**User (current user).** From `GET /api/dashboard/` → `user` (29 keys: identity, `approved`, `terms_up_to_date`, `is_admin`, `plan`, `voice_mode_enabled`, `craft_mode`, `preferred_model`, defaults `default_privacy_level`/`default_ai_usage`, X link state, email + `pending_email`/`pending_email_expired`, `prefill_consent`, `timezone`, `spend_blocked`, share/external-content flags, profile-build state). Full shape: §2.4.1, "CurrentUser". Other users appear only as `username` strings (plus `user_id` ints) inside nodes; there is no public user object for the app except `GET /api/dashboard/<username>` (unused, returns another user's profile text) and public pages.

**Node.** The core entity: every entry, reply and AI reply. A **thread** is a root node (`parent_id == null`) and its descendants. Relations:
- `parent_id` / `children`: a tree. `GET /api/nodes/<id>` returns the focal node, its `ancestors` (root first, privacy-blocked ones omitted) and the **entire** subtree under it as nested `children` (no pagination, sorted by `descendant_count` desc).
- Authorship vs ownership: `user` / `user_id` is the author; for LLM nodes that is a per-model pseudo-user whose `username` is the model id, and the human who asked is the owner (`parent_user_id` on the wire; `human_owner_id` in the DB). Edit/delete = author or human owner. Read access = alive and (author, owner, or `public`).
- AI replies: `node_type == "llm"`, `llm_model`, `llm_task_status`, `streaming_content` (partial text while generating), `tool_calls_meta` (tool calls + internal markers `_batch`, `_read`, `_feed_sample`, `_mode`), `continuation_node_id` (via llm-status/SSE: the answer continues on another node).
- Audio: `has_original_audio` (recorded), `has_tts` (generated speech); URLs come from `GET /api/nodes/<id>/audio` (`original_url`, `tts_url`, `has_audio_chunks`), chunks from `/audio-chunks`, chapters from `/tts-chapters`. The DB columns `audio_original_url`, `audio_tts_url`, `audio_duration_sec`, `audio_mime_type` appear raw only in upload responses.
- System-prompt roots: `is_system_prompt`, `prompt_key`, `prompt_title`, `user_prompt_id`, `prompt_version_number`, `context_artifacts` (the profile/todo/recent/artifact texts that were substituted into the prompt).
- Read replies: `read_reply`, `read_window`, `feed_picks_count`, `in_read_thread`, `read_reply_above`.
- Other: `pinned_at`, `permalink` (public roots with a slug), `origin`, `privacy_level`, `ai_usage`, `reply_ai_usage`, `created_at`, `updated_at`. Soft-deleted nodes appear as tombstones `{id, deleted: true, deleted_at, username, node_type, created_at, …}` when they still have visible descendants.
- Content markers the client must render: `{quote:<nodeId>}` and `{quote_ext:<itemId>}` (resolve with `GET /api/nodes/<id>/resolve-quotes`), links to `https://loore.org/node/<id>` (titles via `GET /api/nodes/titles?ids=`), fenced `:::share <type>` … `:::` blocks and `### <Section>` proposal headings in AI replies (§5.3). Content is Markdown.
- Versions: edits store the previous text in `NodeVersion`, but **no endpoint exposes node versions** (only data export includes them).
- Shapes: §2.2.0 A (`NodeDetail`), B (`AncestorNode`), C (`TreeNode`), D (create/upload); list cards: `LogCard` (§2.4.7), `DashboardNodeCard` (§2.4.1), `PublicNode` (§2.5.5), quote payloads (§2.2.2). Char cap 100 000 per node (create auto-splits into a chain and returns `split_into`, `tip_id`; edit returns 422).

**Thread name.** `thread_name` on `LogCard`; set via `PUT /api/nodes/<root>/thread-name`. Stored in a separate `Thread` table keyed by the root node.

**Draft.** Server copy of unsent text keyed by `(node_id)` for edits, `(parent_id)` for replies, or top-level; also the container for a recording session (`session_id`, `streaming_status`, chunk counts, `has_stored_chunks`, `llm_node_id`, `warning`). Shapes: §2.3.1–2.3.2.

**Profile (UserProfile).** Append-only versioned Markdown portrait. Current = `latest_profile` on `GET /api/dashboard/`; history via `/api/profile/versions`. Fields: `id, content, generated_by, tokens_used, created_at, privacy_level, ai_usage, source_tokens_used, source_origin_stats, source_data_cutoff, generation_type, has_tts`. TTS like nodes (`/api/profile/<id>/audio|tts|tts-status`, SSE `profiles/<id>/tts-stream`). Shapes: §2.4.3.

**Todo (UserTodo).** One Markdown checklist per user, append-only versioned; `PATCH` edits in place (ticks), `PUT` creates a version. Fields: `id, content, generated_by, tokens_used, created_at, privacy_level, ai_usage, version_number`. Shapes: §2.4.4. The Markdown has `### Today`-style sections and `- [ ]` items; the web's parsing is in `TodoPage.js` / `utils/markdown.js`.

**Artifact (UserArtifact).** Named, append-only versioned Markdown documents by `kind`. Fields: `id (null for an unwritten built-in), kind, title, description, generated_by, created_at, privacy_level, ai_usage, content, version_number?`. The `intentions` kind has a structured renderer (`IntentionsView.js`, `utils/intentions.js`). Shapes: §2.4.5.

**Prompt (UserPrompt).** Per-user editable system prompts keyed by `prompt_key`, with server file defaults. Fields: `id (Int, null, or the string "default"), prompt_key, title, content, generated_by, created_at, version_number (0 = file default), default_updated`. Note the polymorphic `id`. Shapes: §2.4.6.

**Recent context (UserRecentContext).** Not exposed directly; appears as `context_artifacts.recent` / `recent_raw` on system-prompt nodes.

**Reference (ExternalItem).** Saved tweet, bookmark, Community Archive tweet or web clip. Fields: `id, source, external_id, author_handle, title, preview, url, posted_at, fetched_at, read_at, feedback, feedback_at, edited_at, has_tts, surfaced_count, last_surfaced_at, content?` (detail only). TTS like nodes (`/api/external/items/<id>/…`, SSE `items/<id>/tts-stream`). List response adds `total`, `has_more`, `counts{source: n}`. Shapes: §2.5.3.

**Feed pick (FeedPick).** A Read's recommendation of a reference, attached to a read reply node: `GET /api/nodes/<id>/feed-picks`, `POST …/feed-picks/read`. Shapes: §2.2.7.

**Share (ShareDraft).** A piece extracted from private writing, published to the public side on demand. Fields: `id, content, share_type, status, source_node_id, public_node_id, permalink, created_at, updated_at, published_at, revoked_at`. Gated by `user.share_v1_enabled`. Shapes: §2.5.4.

**Updates.** Changelog sections `{id: String slug, title, date: "YYYY-MM-DD"|null, body: Markdown}`, notifications `{id, type, title, body, link, created_at, meta}`, polls `{id, question, created_at, draft_terms, response: PollResponse|null}`. Shapes: §2.4.10.

**Model.** `GET /api/nodes/models` → `{id, name, provider: "openai"|"anthropic", featured, read}`; suggestions from `/api/nodes/default-model` and `/api/nodes/<id>/suggested-model` (`{suggested_model, source}`).

**API token (ApiToken).** Clipper tokens: `{id, name, prefix, scope, created_at, last_used_at, active}` plus the plaintext once at creation. Shapes: §2.5.3.

**Import / export jobs.** Transient; see §2.5.1 and §6.2.

### 5.3 Proposals (not a server entity)

A "proposal" is something an AI reply suggests the user apply: todo changes, a share, feedback to the team, a GitHub issue. There is no endpoint that lists them. Server-side the pending payload is a `Draft` row labelled `todo_pending`, `github_issue_pending`, `feedback_pending` or `share_pending` (never returned by the drafts API); the apply endpoints find it from `llm_node_id`. The client derives what to show from the LLM node:
- `tool_calls_meta` entries named `propose_todo`, `propose_share`, `propose_feedback`, `propose_github_issue` (and `apply_*` results with `apply_status: "started"|"completed"|"failed"`, `apply_error`).
- Markdown in `content`: `### Completed`, `### New Tasks`, `### Priority`, `### Note` (todo); `### Issue Title` / `### Title` + `### Description` + `### Category` (GitHub issue); `### Feedback` + `### Feedback Category`; fenced `:::share <type>` … `:::` blocks (share; legacy `### Share` + `### Share Type`); inline tags `[<word>-proposal:…]` are stripped for display. Parsing: `frontend/src/components/ProposalInline.js` (`parseOrientResponse`, `parseShareBlocks`, `parseTodoItems`, `stripProposalTag`, `hasProposalSections`).
- Apply actions: `POST /api/todo/apply-draft {llm_node_id}` (async, completion via `llm-status` → `tool_calls_meta[propose_todo].apply_status`), `POST /api/share/save-proposal`, `POST /api/feedback/submit`, `POST /api/github/create-issue`; edits to the proposal text are saved with `PUT /api/nodes/<id> {content}`. Shapes: §2.4.9 (feedback/GitHub), §2.5.4, §2.4.4.

## 6. Async task patterns

All long work runs in Celery. The HTTP call that starts it returns quickly (usually `202`) with a Celery `task_id` and, for LLM work, the id of a **placeholder node** that the task fills in. Progress is read either by polling a status endpoint or from an SSE stream (§6.5); the web app uses SSE where it exists and keeps polling as the fallback. The status value set is the same everywhere: `null | "pending" | "processing" | "completed" | "failed"`, plus `"cancelled"` for LLM (a read withdrawn before it ran) and profile-specific values (§6.2).

### 6.1 The generic poller (`frontend/src/hooks/useAsyncTaskPolling.js`)

- `GET <endpoint>` immediately, then every `interval` ms (default 2000), request timeout 10 s, header `Cache-Control: no-cache` (Safari cached poll responses before this).
- Reads `result.status`, `result.progress` (default 0), keeps the whole body as `data`.
- Stops on `completed`, `failed`, `cancelled`; on `failed` exposes `result.error || "Task failed"`.
- Request errors do not stop polling unless `maxConsecutiveErrors` (default 0 = never) is reached.
- Gives up after `maxDuration` (default 30 min; `0` disables).
- Polls once immediately when the page becomes visible again (iOS throttles timers in the background). Native: poll on `scenePhase == .active` too.
- Discards a response if the endpoint changed while it was in flight.

### 6.2 What is polled where

| Work | Started by | Returns | Status endpoint | Interval / cap (web) | Terminal signal | Streaming alternative |
|---|---|---|---|---|---|---|
| LLM reply (text mode, NodeDetail) | `POST /api/nodes/<parent>/llm` | `202 {task_id, status:"pending", node_id}` (new LLM node) | `GET /api/nodes/<node_id>/llm-status` | 2000 ms, 30 min; batch read (`tool_calls_meta` has `_batch` with status `submitted`/`cancelling`): 15000 ms, 25 h | `status` ∈ completed/failed/cancelled; follow `continuation_node_id` when set | `GET /api/sse/nodes/<node_id>/llm-stream` for the text while it is written (§6.5) |
| LLM reply (voice turn) | `POST /api/drafts/streaming/<sid>/finalize` with `label: "Voice"` (the server's finalize chain creates the user node and the reply; `llm_node_id` arrives in the draft SSE `all_complete` or in `GET …/status`); fallback `POST /api/voice/`; from a thread `POST /api/voice/from-node/<id>` | `llm_node_id` | same `llm-status` | 1500 ms (`useVoiceSession.js`) | same | Voice does not use `llm-stream`; it opens `/api/sse/nodes/<id>/tts-stream` early when `llm-status.tts_streaming` is true |
| LLM reply (Text mode start / written entry) | `POST /api/textmode/start`, `POST /api/textmode/from-node/<id>`, `POST /api/drafts/streaming/<sid>/save-as-node` with `auto_generate`/`agentic` | `llm_node_id` (web navigates with `?awaitLlm=<id>`) | same `llm-status` | 2000 ms | same | `llm-stream` |
| TTS for a node | `POST /api/nodes/<id>/tts` | `200 {tts_url}` (already done) or `202 {task_id, status, node_id}` | `GET /api/nodes/<id>/tts-status` | 2000 ms (fallback only) | `status == "completed"` → `node.audio_tts_url` | `GET /api/sse/nodes/<id>/tts-stream` (`chunk_ready`, `all_complete`) |
| TTS for a profile / reference | `POST /api/profile/<id>/tts`, `POST /api/external/items/<id>/tts` | as node TTS | `GET /api/profile/<id>/tts-status`, `GET /api/external/items/<id>/tts-status` | 2000 ms | same | `/api/sse/profiles/<id>/tts-stream`, `/api/sse/items/<id>/tts-stream` (path segment chosen by `useTTSStreamSSE` `entityType`) |
| Transcription of an uploaded file | `POST /api/nodes/` (multipart) or `/api/nodes/upload/finalize` | `transcription_task_id` in the node JSON | `GET /api/nodes/<id>/transcription-status` | 2000 ms | `completed` → `content` | `/api/sse/nodes/<id>/transcription-stream` |
| Streaming transcription (recording) | `POST /api/drafts/streaming/init`, chunk uploads, `POST …/finalize` | `session_id` | `GET /api/drafts/streaming/<sid>/status` | every 5000 ms and on foreground while `finalizing`; after `transcribe-remaining` every 2000–3000 ms with a 5-minute cap | `streaming_status` ∈ `completed`/`failed` | `/api/sse/drafts/<sid>/transcription-stream` (`content_update`, `all_complete`) |
| Profile generation | No user-facing start endpoint: backend periodic tasks (sync Celery task or Batch API chain) and the admin route `POST /api/admin/users/<id>/build_profile` start builds. The app learns a build is running from the dashboard user (`profile_generation_task_id` non-null or `profile_batch_pending` true). | — | `GET /api/export/profile-progress[?task_id=<last sync task>]` — **one endpoint for both pipelines** | sync: 5000 ms, batch (`profile_batch_pending`): 60000 ms; no duration cap; stops after 10 consecutive errors (`ProfileGenerationWatcher.js`, mounted app-wide) | `running == false`; then `status` ∈ `completed`/`failed`/`idle`/`stalled`; batch success = `latest_profile` changed | none |
| Archive import | `POST /api/import/confirm`, `/api/import/{twitter,claude,chatgpt}/confirm` | `task_id` | `GET /api/import/status/<task_id>` (always 200; `status` ∈ queued/running/completed/failed, `done`/`total` counts, `result`, `error`) | see §2.5.1 | `completed`/`failed` | none |
| Poll-response draft (updates channel) | `POST /api/updates/polls/<id>/draft` | `202 {response: {status: "drafting"}}` | `GET /api/updates/polls/<id>` | 5000 ms | `response.status != "drafting"` | none |
| X bookmark sync | `POST /api/external/twitter/sync` | `202 {task_id, status}` | `GET /api/external/twitter/status` (+ `GET /api/external/items?per_page=1` for counts) | 3000 ms, 20 ticks | `last_synced_at` changed or `revoked` | none |
| Apply todo proposal | `POST /api/todo/apply-draft {llm_node_id}` | `202 {status: "started", task_id, llm_node_id}` | `llm-status` of that node, `tool_calls_meta[name=="propose_todo"].apply_status` | 2000 ms, no cap | `completed`/`failed` | none |
| Admin pre-fill / intentions | `/api/admin/users/<id>/prefill*` | `task_id` | `GET /api/admin/prefill/status/<task_id>` | admin only | — | none |

`GET /api/export/profile-progress` body (all branches): `{running: Bool, source: "batch"|"sync"|null, status: String ("progress" for batch; Celery state mapped to pending/processing/…; "completed"|"failed"|"stalled"|"idle" when not running), progress: Int 0–100, message: String ("Generating profile: Chunk n of ~N", "Integrating profile versions", or the task's status text), task_id: String|null, error: String|null, latest_profile: <snapshot, see §2>, batch_step_failed?: Bool (idle branch only)}`. In the idle branch `latest_profile` is `{id, generation_type, created_at}` or null. The app should start polling when the dashboard user has `profile_generation_task_id` or `profile_batch_pending` set. Web terminal logic (`ProfileGenerationWatcher.js`): `failed` → failure toast; `stalled`, or `idle` with `batch_step_failed` after having seen it running → "stopped, will retry"; `completed`, or `idle` with a different `latest_profile.id` than at start → success toast and a refetch of `GET /api/dashboard/`.

### 6.3 LLM task warnings (`useLlmTaskWarnings.js`)

When an `llm-status` response has `status == "completed"` and a non-empty `warnings: [String]`, each distinct string is shown once as a toast (8 s). Deduplicate by text across polls. The backend fills these from `Node.llm_task_warnings` (e.g. unrecognised `{user_export}` parameters).

### 6.4 Spend cap, rate limits and errors with special handling

- **Spend cap (402).** Body `{"error": "monthly_spend_limit_reached", "message": "…"}`. Web behaviour: `api.js` interceptor raises a global banner (`SpendCapBanner.js`) for any such 402; `utils/spendCap.js` keeps a session flag set from `user.spend_blocked` on load or from any 402; before starting a recording or an audio upload the UI checks the flag and shows the toast `"You've reached your monthly usage limit, so you can't start a new recording|upload audio until it resets on <Month D>."` (reset date = first of next month in UTC). Call sites that get a 402 (NodeDetail LLM request, HomePage, useStreamingTTS) return silently because the banner already explains it. A recording already in progress is never lost to the cap (`test_spend_cap_recording.py`): chunk uploads and finalize are not cap-checked, only `*/init` and LLM/TTS starts.
- **Provider rate limits / spend limits.** Not HTTP errors. They end the Celery task with `llm_task_status = "failed"` and the exception text in `error` of `llm-status` (e.g. an Anthropic `429 … enforced_spend_limit_reached`). Show `error` as-is with a retry action (`POST …/llm` again on the parent).
- **Other statuses the web branches on** (details per endpoint in §2): `401` signed out; `403` not approved (text above) or not allowed; `404`/`403` on `GET /api/nodes/<id>` → "This node doesn't exist, was deleted, or isn't shared with you." (the backend does not distinguish missing/private/deleted; some 404s are Flask HTML pages, not JSON); `404` from `GET /api/nodes/<id>/audio` and `tts-chapters` is a normal "no audio" answer; `404` from drafts = no draft; `409 {"error": "deleted_content_matches", …}` from import confirm (offer to restore deleted content, `ImportData.js`); `413` on import upload (file too large); `409` from `POST /api/nodes/<id>/tts` when original audio exists; `400` from search is shown inline, other search errors are logged; `400 {"code": "init_parse_failed"}` from streaming audio-chunk upload means the recorder must restart (session dead).
- **Login redirect errors** arrive as `?error=` on `FRONTEND_URL/login` (§3.2/§3.3), and Connect X outcomes as `?x_login=` on `/account` (§3.4).

### 6.5 SSE

The SSE endpoints, event names, payload fields, reconnect logic and the native call sequences for voice turns and streaming transcription are catalogued in §2.3.6 (endpoints), §2.3.7 (SSE event reference) and §2.3.8 (native call sequences). Summary of the transport facts:
- All SSE routes are under `/api/sse/` and authenticate with the same cookies (`EventSource(url, {withCredentials: true})`); there is no token in the query string.
- nginx serves `/api/sse/` with `proxy_buffering off`, `proxy_read_timeout 7200s`, `chunked_transfer_encoding on`.
- Native: `URLSession` `bytes(for:)` (or a `URLSessionDataDelegate`) reading lines, parsing `event:` / `data:` / blank-line frames; reconnect on drop with the endpoint-specific resume parameter (`last_chunk` for transcription/TTS streams); close streams when the app backgrounds and reconcile by polling on return (the web does the same because iOS Safari kills SSE connections on lock/background).

## 7. Backend tests that pin the API contract

All under `backend/tests/`. Run with `cd backend && python -m pytest`. One line each; the ones most useful as executable contract documentation for the Swift client are marked ★.

**Auth, gating, access**
- `test_magic_link.py` — token generate/verify, expiry, tampering, email-change tokens are not sign-in tokens, username derivation from email. (No test drives `/auth/magic-link/verify` end to end; the redirect/cookie behaviour in §3.2 is from reading the route.)
- `test_auth.py` — `is_safe_redirect_url` rules for `next`/`next_url` (relative paths only).
- ★ `test_x_login_linking.py` — X login finds accounts by numeric X id only; Connect X outcomes (`linked`, `other_x`, `taken`, `taken_placeholder`, `cancelled`, `failed`); confirm-email sign-in never creates an account.
- `test_x_oauth_request_local_session.py` — the X OAuth1 session is per request.
- ★ `test_email_change.py` — `POST/DELETE /api/dashboard/email*`, confirm-token binding, `pending_email*` fields, approval-gate exemptions.
- `test_prefill_consent.py` — the waitlist-page tweets-seed consent (`prefill_consent`).
- `test_admin_access.py` — `/api/admin/*` requires `is_admin` (not the username).
- `test_audio_access.py` — audio endpoints: voice-mode plan gate, public vs private node audio, 401 unauthenticated.
- `test_node_privacy.py`, `test_privacy_utils.py`, `test_profile_privacy.py` — privacy/ai_usage validation and access rules.
- ★ `test_log_dashboard_privacy.py` — what `/api/log`, the dashboard and node detail include for owner vs other viewers.
- `test_node_detail_ownership.py` — `user_id` / `parent_user_id` on ancestors and children.
- `test_health.py` — `/health`, `/ready` (and `/api/…`).

**Nodes, threads, content**
- ★ `test_node_deletion.py` — soft delete, tombstones in ancestors/children, `delete_descendants`, `delete_orphaned_prompt`, response fields.
- `test_node_split.py` — the 100 000-char cap, auto-split on create (`split_into`, `tip_id`), 422 on edit.
- `test_apply_settings_to_descendants.py` — `PUT /nodes/<id>` with `apply_to_descendants` and `descendants_updated`.
- `test_thread_name.py` — `PUT /nodes/<root>/thread-name` validation and the Log's `thread_name`.
- `test_node_titles.py` — `GET /nodes/titles` batch lookup (null/deleted/title rules).
- `test_quotes.py`, `test_quote_as_response.py` — `{quote:ID}` / `{quote_ext:ID}` resolution.
- `test_detached_prompt_stays_agentic.py`, `test_context_artifact_pinning.py` — system-prompt fields and `context_artifacts`.
- `test_wave6_backend.py` — assorted route fixes (#110, #104).

**LLM generation and streaming**
- ★ `test_textmode.py` — `/api/textmode/start` validation, `/from-node`, `/message`, `source_mode` on `/nodes/<id>/llm`.
- ★ `test_live_streaming.py` — `streaming_content`, `tts_streaming` in llm-status, replies spoken while written.
- `test_llm_stream_sse.py` — llm-stream `snapshot` vs `delta`.
- `test_tts_stream_sse.py` — tts-stream of a finished node replays chunks then `all_complete`.
- `test_task_warnings.py` — `warnings` list in llm-status.
- `test_retrieval_loop.py` — tool loop and `continuation_node_id` chaining.
- `test_read_routes.py`, `test_read_turns.py`, `test_read_batch_poll.py`, `test_reference_log.py` — Read entry points (admin-only), batch stage (`stage: "batch"`), feed picks.
- `test_user_export_placeholder.py`, `test_ca_tweets_placeholder.py` — the 400 errors a prompt edit or `/llm` returns for bad placeholders.

**Voice, recording, audio**
- ★ `test_streaming_save_as_node.py` — `POST /drafts/streaming/<sid>/save-as-node` flags and response.
- ★ `test_spend_cap_recording.py` — 402 at `streaming/init`; an in-progress recording is always transcribed; Voice reply skipped with `warning`.
- `test_resume_subsessions.py` — resumed recordings (a chunk with its own init segment starts a subsession).
- `test_webm_utils.py` — init-segment extraction (why chunk 0 must carry the WebM/fMP4 header; `init_parse_failed`).
- `test_voice_from_node.py` — `POST /voice/from-node/<id>` four-case matrix.
- `test_voice_timing.py` — timing marks.
- `test_tts_chapters.py`, `test_tts_invalidation.py`, `test_tts_slow_calls.py`, `test_tts_strip.py`, `test_tts_stream_text.py` — chapters, `regenerate_tts`, TTS task behaviour.
- `test_encrypt_file_atomically.py` — concurrent audio writes.

**Account data and workspace**
- ★ `test_artifacts.py` — `/api/artifacts` CRUD, versions, revert, kinds, and the agentic artifact/feedback tools.
- `test_prompt_defaults.py` — `default_updated`, revert-to-default, acknowledge.
- ★ `test_profile_progress.py` — `GET /export/profile-progress` in every state (idle/sync/batch/stalled/completed/failed, `task_id` query).
- `test_profile_ai_usage_api.py` — profile `ai_usage` follows the account setting (#346).
- `test_updates.py` — `/api/updates` changelog/notifications/polls and their actions.
- `test_search.py`, `test_semantic_search.py` — `/api/search`, `/api/search/semantic`, neighbors.
- ★ `test_user_spend_cap.py`, `test_spend_monitor.py` — the 402 body and which actions are capped.
- `test_share.py`, `test_forum.py`, `test_public_pages.py` — Share, Commons and public pages behind `SHARE_V1` / `public_sharing_enabled`.
- `test_external_content.py`, `test_recommendations.py` — references (`/api/external/items*`), clipper tokens.
- `test_twitter_import.py`, `test_chatgpt_import.py`, `test_claude_import.py`, `test_import_encryption.py` — import analyze/confirm contracts.
- `test_bookmark_nightly_sync.py` — X bookmark sync and `revoked` state.
- `test_github_webhook.py` — the `fix_ready` notification source.
- `test_timefmt.py` — `iso_utc` output format (the `Z` suffix rule in §0).
- `test_default_model_guard.py` — valid model ids.

## 8. Risks and open questions for the design doc

**Auth**
1. No JSON login endpoint and no universal links: every login ends in a 302 to the web origin. Paste-the-link works without backend changes but is clumsy; the AASA file (§4.2) is the smallest change that fixes it.
2. Sign-up links work once. A new user who taps the link in Mail (opens Safari) cannot then paste it into the app (`link_already_used`); they must request another link.
3. X login in a `WKWebView` depends on X keeping its login page usable inside embedded web views; accounts that log in to X with Google cannot complete it there.
4. Session persistence: `HTTPCookieStorage` drops the session-only `session` cookie on termination; the `remember_token` restores it. It expires client-side 30 days after login (no sliding refresh), but the server accepts it forever. Decide between re-injecting it from the Keychain (indefinite sign-in; the Keychain copy is a durable credential that logout does not revoke) and a monthly re-login, or ask for `REMEMBER_COOKIE_REFRESH_EACH_REQUEST = True`.
5. Non-`/api` `@login_required` routes (`/auth/logout`) redirect a signed-out client into X OAuth. Disable redirect following on those calls.
6. Terms text is hard-coded in `TermsModal.js`; the version string `"2.0"` lives in `backend/routes/terms.py`. A terms bump requires an app release unless the app shows the web page.

**Contract**
7. No API versioning. The web and the app share the backend; a backend deploy can change a field the app relies on. Keep decoders tolerant (optionals, unknown enum cases) and pin shapes with the tests in §7.
8. Several shapes per entity (Node alone has four), polymorphic `id` on prompts (`Int | null | "default"`), keyed-by-string dictionaries (`quotes`, `titles`, `counts`), loosely typed `tool_calls_meta`. Budget time for decoders.
9. Some 404s are Flask HTML pages, and admin 403s are HTML. Check `Content-Type` before decoding error bodies.
10. Dates: microsecond fractions, mixed `Z` / `+00:00` / no zone, and date-only strings (§0).
11. `GET /api/nodes/<id>` returns the entire subtree with every node decrypted; long threads are slow and large on a phone connection. There is no paginated alternative.
12. Upload responses from `POST /api/nodes/` (multipart) and `/api/nodes/upload/finalize` return the raw (encrypted) `content` column; ignore it.

**Audio and streaming**
13. Recording format: `POST /api/drafts/streaming/<sid>/audio-chunk` takes `mime_type` `audio/webm` or `audio/mp4` (fixed per session) and parses chunk 0's init segment (`backend/utils/webm_utils.py`): for MP4 it walks the boxes and requires `moov` before the first `moof`/`mdat`, else `400 {code: "init_parse_failed"}`. The format it was written for is Safari's MediaRecorder fMP4: chunk 0 = `ftyp`+`moov`+first `moof`/`mdat`, later chunks = bare `moof`+`mdat` fragments whose timestamps continue the recording; the stored init segment is prepended to later transcription batches. A later chunk that starts with `ftyp` opens a new subsession. The native equivalent is `AVAssetWriter` with `outputFileTypeProfile = .mpeg4AppleHLS` and `preferredOutputSegmentInterval` ≈ 15 s: send the initialization segment concatenated with the first media segment as chunk 0, then each media segment alone. `AVAudioRecorder`'s plain `.m4a` (moov written at the end) fails chunk 0. This needs a real-device test through the backend's ffmpeg remux and `gpt-4o-transcribe`; if it cannot be made to pass, this is the one place a backend change could become necessary.
14. Playback: desktop recordings are WebM/Opus, which AVFoundation does not play. `GET /api/nodes/<id>/audio-download?format=mp3` converts, but only for streaming-transcription nodes, synchronously (slow for long recordings, subject to the 60 s nginx timeout). TTS files are MP3 and play natively.
15. A Voice turn's reply id arrives only in the draft SSE `all_complete` or `GET …/status`; the draft is deleted just before `all_complete`, so a client that misses it while backgrounded gets 404s and must recover the reply from the thread via the nodes API.
16. SSE: named single-line events, no `id:`/`retry:`, resume via `?last_chunk=`, heartbeats every 15 s, server lifetime caps (10 min to 2 h), and each open stream costs the server a DB poll every 0.5–1 s. iOS will kill streams in the background; the app must reconcile by polling on foreground, as the web does.
