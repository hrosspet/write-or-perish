# Loore Android app — design

**Date:** 2026-11-22
**Status:** design only; nothing is built. The Android app is the lowest-priority item before the Beta.
**Requirement (Peter):** "The android app should behave the same as the iPhone app, only with some idiomatic differences."
**Reference:** the SwiftUI iPhone app in `ios/` on `origin/main` at `df79cad8` (2026-11-22), and the backend routes it calls. Swift paths below are relative to `ios/`.

This document lets a builder start the Android app without first reading all of `ios/` (about 41,000 lines of Swift). §3 lists every flow of the iPhone app: what the user sees, the Swift files that implement it, the routes it calls, and how Android does the same. §4 collects every place where Android behaves differently and why. Appendix A lists every route with its method. When a detail is missing here, the Swift files named in the flow's section are the specification.

**Precedence.** For what the app does, the iPhone code wins over this document, except for the differences in §4. When the iPhone app changes, the Android app follows. For how Android does it, this document holds until a milestone shows that a mechanism does not work; then change the mechanism, keep the behaviour, and note why in `android/PROGRESS.md`. Record every intentional difference from the iPhone app in `android/PROGRESS.md` under "Differences from the iPhone app".

Other references: `docs/IOS-APP-DESIGN.md` (why a native app, and the first iPhone decisions); `ios/PROGRESS.md` (as-built notes per feature and the conventions; where it and the code disagree, the code wins); `ios/README.md` (device and staging checklists, adapted in §7); `ios/docs/web-app-map/A–E` (the web app as of 2026-09-30).

Contents: [0 Goal](#0-goal-and-non-goals) · [1 Stack](#1-stack) · [2 Architecture](#2-architecture-iphone-pieces-and-their-android-counterparts) · [3 Flows](#3-flows) · [4 Differences](#4-where-android-differs-from-the-iphone-and-why) · [5 Server](#5-what-the-server-needs) · [6 Project](#6-project-layout-sdk-levels-dependencies) · [7 Testing](#7-testing) · [8 CI](#8-ci) · [9 Milestones](#9-build-order) · [10 Needs Peter](#10-needs-peter) · [11 Decisions](#11-decisions-made-in-this-document) · [12 Heuristics](#12-heuristics-introduced) · [A Routes](#appendix-a-routes)

---

## 0. Goal and non-goals

**Goal.** A native Android phone app with the iPhone app's screens, flows, texts and server calls, against the unchanged backend. A Voice turn on a locked Android phone works as on a locked iPhone: record, stop from the lock screen, the reply plays by itself, record the next turn from the lock screen.

**Non-goals**, the same as the iPhone app today: push notifications, home-screen widgets, receiving shared text from other apps, tablet-specific layouts, offline reading, buying a subscription in the app. The admin page stays a web view.

---

## 1. Stack

- **Kotlin and Jetpack Compose**, one Activity. Material 3 components styled with Loore's tokens; no dynamic (wallpaper) colours.
- **Coroutines and Flow.** One `ViewModel` per screen; one application-scoped `AppState` (the counterpart of `Loore/App/AppState.swift`).
- **Networking:** OkHttp; kotlinx.serialization for JSON; an own SSE parser on OkHttp's streaming response body (a port of `Loore/Core/Networking/SSE.swift` and its tests).
- **Recording:** `AudioRecord` → `MediaCodec` (AAC) → an own fragmented-MP4 writer, producing the same chunks as the iPhone (§3.11).
- **Playback:** Media3 ExoPlayer with a Media3 `MediaSession`.
- **Background:** one foreground service for audio (§3.18); WorkManager for chunk uploads that outlive the service or the process (§3.12).
- **Storage:** DataStore for preferences; files in app-private storage; the sign-in cookies encrypted with a key held in the Android Keystore (§3.6).
- **Markdown:** commonmark-java with its GFM extensions, rendered by an own Compose renderer, as the iPhone renders swift-markdown's tree itself (§3.24).
- **No** analytics, crash reporting, advertising or other third-party SDKs, and no dependency on Google Play services.

The reason for a native app is the iPhone's (`docs/IOS-APP-DESIGN.md` §0): the Voice loop has to keep running with the screen off, which on Android needs a foreground service and a media session. A web page in Chrome cannot hold either.

---

## 2. Architecture: iPhone pieces and their Android counterparts

| iPhone (`Loore/…`) | What it does | Android |
|---|---|---|
| `App/AppState.swift` | current user, auth phase, environment, craft mode, theme, banners, toasts, cross-screen signals (`nodeCreated`, `logChanged`, `todoChanged`, `artifactsChanged`, `referencesChanged`, `profileGenerationStarted`) | `AppState` (application scope) exposing `StateFlow`s; signals as counters in `StateFlow`s that screens collect |
| `App/RootView.swift` | gating by phase | `RootScreen` composable that switches on the phase |
| `App/MainTabView.swift`, `Router.swift`, `AppRoute.swift` | tabs, a stack per tab, link parsing | `NavigationBar` + one back stack per tab (Navigation Compose); `AppRoute` sealed class with the same parser |
| `App/AppEnvironment.swift`, `LaunchOptions.swift` | environments, debug launch arguments | build types + debug-only intent extras |
| `Core/Networking/APIClient.swift`, `APIError.swift`, `APIPaths.swift` | requests, error mapping, the trailing-slash rule | `ApiClient` on OkHttp, `ApiError` sealed class, `ApiPath` (copy the table and `canonical`) |
| `Core/Networking/SSE.swift`, `Poller.swift` | SSE with stall detection; polling | the same on coroutines |
| `Core/Auth/*` | sign-in, cookie vault, link parsing, X sign-in web view | `AuthService`, `SessionStore`, `MagicLink`, `XSignInScreen` (WebView) |
| `Core/Models/*`, `NodeTreeDecoding.swift` | tolerant decoding; the node tree decoded without recursion | `@Serializable` classes with defaults, custom serializers for dates and flexible numbers; iterative tree building |
| `Core/Util/*` | date formatting, spend-cap texts, line diff, version labels, network status, temporary files | the same, ported with their tests |
| `Markdown/*` | marker split, parser, renderer, checklist edits, selection | markdown model in `:core`, Compose renderer in `:app` |
| `Audio/AudioCenter.swift` | owns the player, sounds, session, lock screen and the Voice controller | `AudioCenter` (application scope) + `AudioService` (the foreground service) |
| `Audio/Recording/*` | capture, AAC, fMP4, chunk packaging, uploader, limits, log | `Recorder` (`AudioRecord`), `AacEncoder` (`MediaCodec`), `Fmp4Writer` + `SegmentPackager` (`:core`), `ChunkUploader` + `UploadWorker` |
| `Audio/Voice/*` | the Voice turn state machine behind a `VoiceBackend` protocol | `VoiceTurnController` in `:core` behind a `VoiceBackend` interface, tested with fakes as on the iPhone |
| `Audio/Playback/*` | queue player, generated sounds, listen-aloud, downloads | `ChunkQueuePlayer` over ExoPlayer, `ToneSynth` (`AudioTrack`), `ListenAloud`, `AudioDownloader` |
| `Audio/Session/*` | audio session and routes, Now Playing, Live Activity | `AudioRouting` (`AudioManager`), the `MediaSession`, the service's notification |
| `Audio/Dictation/*` | dictation into the text editor | `DictationController` |
| `DesignSystem/*` | tokens, fonts, components | `designsystem` package |
| `Features/*` | screens | one `feature/*` package per iPhone feature folder |

**Conventions to keep** (from `ios/PROGRESS.md` "Conventions"): every endpoint goes through `ApiPath`; every field is read tolerantly (only ids are required; a missing or mistyped field becomes its default); unknown enum values decode to `Unknown(raw)`, and AI usage `"none"` is its own value; never convert snake_case keys automatically (artifact kinds and source names are map keys); no user content in logs (ids, statuses and byte counts only); per-device preferences keep the iPhone's key names (§3.28); cross-screen signals instead of screens reloading each other.

---

## 3. Flows

Each flow lists: what the iPhone app does, the Swift files, the routes (methods in Appendix A), the Android design, and the differences (also collected in §4).

### 3.1 App shell, tabs, routes and links

**iPhone**
- Five tabs, no badges or counts: **Home** (`HomeView`, icon the Loore mark), **Artifacts** (`WorkspaceView`), **Log** (`LogView`), **Commons** (approved users with `share_v1_enabled` only; if it disappears while selected the app switches to Home), **More** (`MoreView`). Each tab has its own navigation stack; the mini-player sits above the tab bar; a node-opening spinner covers all tabs and lets taps through.
- **Link parser** (`AppRoute.swift:63-142`). Accepts relative paths and http(s) URLs on Loore's hosts (loore.org, www.loore.org, staging.loore.org, and the current backend and frontend hosts); other URLs are external.

  | Path | Route | Tab |
  |---|---|---|
  | `/` | home | Home |
  | `/node/<id>[?awaitLlm=<id>]` | thread | current tab |
  | `/voice?parent=&resume=&glean=1` | voice | Home |
  | `/textmode[?glean=1]` | text mode | Home |
  | `/log`, `/feed` | log | Log |
  | `/profile`, `/dashboard` | profile | Artifacts |
  | `/todo` | todo | Artifacts |
  | `/artifacts[?create=1]`, `/artifacts/<kind>`, `/ai-preferences` | artifacts / new artifact | Artifacts |
  | `/references`, `/references/<id>` | references / reference | More |
  | `/prompts`, `/prompts/<key>` | prompts / prompt | More |
  | `/account#<anchor>`, `/import#<anchor>` | account / import | More |
  | `/share`, `/welcome` | share / welcome | Home |
  | `/commons` | commons | Commons (Home without the flag) |
  | `/admin` | admin web view | More |
  | `/confirm-email?token=`, `/confirm-account-deletion?token=` | confirm pages | More |
  | `/@user`, `/@user/<slug>`, `/dashboard/<user>`, `/landing`, `/vision`, `/why-loore`, `/how-to` | public web page | — |
  | `/alpha-thank-you` | ignored | — |
  | `/login` and anything else | home | Home |

- **Opening a route** (`Router.swift:91-162`): public web pages open in an in-app browser without cookies; external http(s) links open the in-app browser; `mailto:` opens Mail; any other scheme is ignored. Otherwise the app switches to the route's tab and pushes the screen, except: tab-root routes pop the tab to its root; workspace documents switch in place in the Artifacts tab; a Voice route with `parent` or `resume` opened from another tab copies that tab's stack onto Home, so Back returns to the thread; a thread opens on the current tab.
- **Permalinks** `/@user/<slug>` for a signed-in, approved user: `GET /api/commons/permalink/<user>/<slug>`; a `node_id` opens the native thread, otherwise the public page opens.
- **Opening nodes without a loading page** (`Features/Thread/NodePrefetch.swift`, #447/#448): `open(id)` fetches the node while the current screen stays; a spinner covers the tabs; the new page opens when the node arrives or after 2 s (it then waits for the same request); opening another node cancels the first fetch; nothing opens if the tab or screen that asked has changed; the page takes the fetched node once if it is less than 10 s old. Every thread-to-thread move, search result, node link and sent entry goes through it.
- No link from outside the app opens it (no URL scheme, no universal links); emailed links are pasted (§3.4).

**Files:** `App/MainTabView.swift`, `App/Router.swift`, `App/AppRoute.swift`, `App/RootView.swift`, `Features/Thread/NodePrefetch.swift`, `DesignSystem/Components/Surfaces.swift` (spinner), `Features/Home/PlaceholderScreen.swift`.

**Android**
- `NavigationBar` with the same five items and glyphs. One back stack per tab, kept while switching tabs.
- **System Back** pops the current tab's stack; at the root of another tab it selects Home; at Home's root it closes the Activity. A Voice conversation keeps running (§3.18) when the user leaves the app with the Home gesture or the app switcher while the Voice screen is open. Predictive back is on (`android:enableOnBackInvokedCallback="true"`). Sheets and dialogs close on Back. Screens that guard unsaved changes on the iPhone (artifact, profile and todo editors) show the same guard on Back. Leaving the Voice screen with Back is the iPhone's pop (§3.10).
- Public web pages open in **Custom Tabs** (`androidx.browser`). External links from the Updates sheet open the default browser, as on the iPhone.
- `AppRoute` and NodePrefetch are ported with their tests (`AppTests`, `NodePrefetchTests`).
- **Links from outside the app:** App Links for the sign-in link only, if Peter agrees (§3.4, §10 item 3). No other loore.org paths are claimed.
- After Android ends the process in the background, Navigation's saved state brings the stacks back and each screen reloads its data.

**Differences:** Back gesture and button (platform); stacks restored after the process ends (platform); sign-in links may open the app (§10 item 3).

### 3.2 Environments and debug options

**iPhone** (`App/AppEnvironment.swift`, `App/LaunchOptions.swift`, `Features/Account/AccountView.swift:90`)

| Environment | Backend | Frontend |
|---|---|---|
| production | `https://loore.org` | same |
| staging | `https://staging.loore.org` | same |
| local | `http://localhost:5010` | `http://localhost:3001` |

- Release builds: production only, no switcher, no launch arguments (compiled out).
- Debug builds: `-LooreEnvironment`, else the stored choice, else local in the simulator and production on a device. The local URLs can be overridden for a phone on the same Wi-Fi. Switcher: touch and hold the version line on Account for 0.6 s; switching signs out.
- Launch arguments: `-LooreEnvironment`, `-LooreSessionCookie <value>`, `-LooreRoute /path`, `-LooreTheme light|dark`, `-LooreResetState YES`, `-LooreSkipUpdates YES`, `-LooreDebugAudioFile <path>` (feeds a file to the recorder; its end acts as Stop), `-LooreDebugVoiceAutoStart YES`, `-LooreDebugListenNode <id>`, `-LooreDebugImportFile <path>` + `-LooreDebugImportKind`.

**Android**
- Build types `debug` and `release`. Release: production only; debug code behind `BuildConfig.DEBUG` and removed by R8.
- Debug: the emulator defaults to local, reaching the Mac's `localhost` as `http://10.0.2.2:5010` and `http://10.0.2.2:3001`; a physical phone defaults to production; local URLs can be overridden for a phone on the same Wi-Fi. Cleartext HTTP is allowed only in debug builds (a `network_security_config` in the debug source set).
- Same switcher on Account's version line.
- Launch arguments become intent extras, read only in debug builds: `adb shell am start -n org.loore.app/.MainActivity --es loore.environment local --es loore.sessionCookie "$(ios/scripts/local_backend.sh session-cookie)" --es loore.route /node/123 …`, one extra per iPhone argument. The debug audio file is decoded with `MediaExtractor` + `MediaCodec` and fed to the recorder at real time; push it with `adb push` to the app's files.
- Same rule as for the iPhone: on the local backend sign in only as the test user 5 (`seowriter`).

### 3.3 Networking

**iPhone** (`Core/Networking/APIClient.swift`, `APIError.swift`, `APIPaths.swift`, `Poller.swift`, `Core/Util/NetworkStatus.swift`)
- Ephemeral `URLSession` with an in-memory cookie jar; memory-only cache of 8 MB, no disk cache.
- Every request: `Accept` (JSON by default), `X-Timezone: <device time zone id>`, `Content-Type` when there is a body, the cookies. Polls add `Cache-Control: no-cache`. A `+` in a query is sent as `%2B`.
- Timeouts: 60 s per request, 10 s for polls, 2 h per resource.
- Trailing slashes: `ApiPath.canonical` adds a slash to `/api/{dashboard,nodes,drafts,todo,artifacts,profile,prompts,voice}` and removes one from `/api/updates`, `/api/share`, `/api/tool-approvals`.
- Redirects are followed only when the original path starts with `/api/` (the 308s of the slash rule). `/auth/*` redirects come back to the caller.
- Error mapping and global events:

  | Answer | Error | Event |
  |---|---|---|
  | 401 | unauthorized | sign out locally |
  | 402 `error=monthly_spend_limit_reached` | spend cap (message, `lifts_with_payment`) | "LIMIT REACHED" banner |
  | 402 `error=subscription_required` | subscription required | "REPLIES PAUSED" banner, refresh payments |
  | 402 `error=no_subscription` | server | refresh payments (No subscription screen) |
  | 403 whose `error` starts with "Your account is not approved", or `code=account_not_active` | not approved | reload the user |
  | other non-2xx | server(status, body) | — |
  | no answer | transport | — |

  Error bodies are parsed as JSON only when `Content-Type` contains "json".
- Offline = no usable network path (`NWPathMonitor`).
- `Poller`: 2 s interval, gives up after 30 min (configurable), keeps going on errors, polls at once when the app returns to the foreground, stops at completed, failed or cancelled.
- A response with `Set-Cookie` makes `AuthService` capture the cookies again (§3.6).

**Android**
- One `OkHttpClient`: an in-memory `CookieJar` filled from `SessionStore` at launch; no `Cache` (nothing on disk); `followRedirects(false)` plus an interceptor that follows a redirect only for `/api/` paths; an interceptor that adds `Accept`, `X-Timezone` (`ZoneId.systemDefault().id`), `X-Loore-Client: android` and `User-Agent: Loore-Android/<version> (Android <release>)` (§5).
- Per-call timeouts as on the iPhone (60 s; polls 10 s; chunk uploads 120 s; audio files 300 s; import analyze 300 s, confirm 90 s; export 120 s).
- Build query strings with `HttpUrl.addQueryParameter`, which sends `+` as `%2B`.
- Same `ApiError` mapping and global events. Offline when the default network has no internet capability (`ConnectivityManager.NetworkCallback`); `UnknownHostException`, `ConnectException`, `SocketTimeoutException` and `NoRouteToHostException` count as offline errors.
- `Poller` wakes on `ProcessLifecycleOwner` `ON_START`.

### 3.4 Sign-in by email link

**iPhone** (`Features/Onboarding/SignInView.swift`, `Core/Auth/AuthService.swift`, `Core/Auth/MagicLink.swift`)
- Screen: wordmark "LOORE", card "Welcome back" / "Sign in to continue your lore", "Sign in with X" with its note, then "Sign in with Email" and "I already have a sign-in link"; About links (Why Loore, Vision, How To) open the in-app browser. Steps: start → email → sent → paste.
- **Send:** `POST /auth/magic-link/send {email}` (no `next_url`, no `invite`). The server answers 200 for any valid address. Sent step: "Check your inbox … It expires in 15 minutes." Errors: offline → "Network error. Please try again."; otherwise the server's message (also its 429 text), else "Something went wrong. Please try again."
- **Paste:** a paste button (pastes and verifies in one tap) and a text field. Token extraction, in order: the `token` query item of the first http(s) URL in the text (trailing `.,;)]` trimmed, `&amp;` → `&`); else the text after `token=`, percent-decoded; else the whole text. A plausible token has at least 16 characters of `[A-Za-z0-9-_.]`; otherwise "That doesn't look like a sign-in link. Copy the whole link from the email and paste it here."
- **Verify:** `GET /auth/magic-link/verify?token=` with redirects off; the cookies are taken from the 302's `Set-Cookie`. The outcome comes from `Location`: `/login?error=<code>` → failure with that code; `/invite-mismatch?for=x|email` → the invite question; anything else → success. Success without a sign-in cookie → "Loore accepted the link but the sign-in cookie didn't arrive. Please try again." Error texts (identical to the web's `LoginPage.js`, `MagicLink.swift:6-14`): `invalid_or_expired`, `link_already_used`, `confirm_needs_account`, `x_try_again`, `x_cancelled`, `x_failed`, and a fallback. When the link's host is not the environment's, " (This link is for <host>; the app is using <Env>.)" is added. Offline → "Couldn't reach Loore. Check your connection and try again."
- **Landing page:** the `Location` path with its query and fragment, unless it is `/`, `/dashboard` or `/login`, is kept and opened once the user is approved with current terms (e.g. `/welcome`, `/confirm-email?token=…`). `/account-restore` → the restore offer (§3.28). No landing page → Home.
- **Invite made for someone else** (#439, #728): alert "This invitation is for someone else" with the body from `MagicLink.swift:98-106`; "Sign in differently" → `POST /auth/invite-choice/cancel {}`; "Continue without the invitation" → `POST /auth/invite-choice/continue {}` → `{redirect}`, used as the landing page (410 after 15 minutes: the server's text, else "This sign-in has expired. Please sign in again.").

**Android**
- Same screen, steps and texts, except the footnote about a link that creates an account working once: the Android app leaves it out, because it no longer matches the server since #703.
- Paste: a "Paste" button that reads `ClipboardManager` when tapped, plus the text field.
- **App Links** (if Peter agrees, §10 item 3): an intent filter with `android:autoVerify="true"` for `https://loore.org/auth/magic-link/verify` (and `https://staging.loore.org/…` in debug builds). The Activity receives the URL and runs the same verify as a pasted link. When the app is already signed in, it asks before using the link (wording for Peter, §10 item 3). The paste path stays.
- Verify with OkHttp, redirects off; read `Location` and `Set-Cookie` from the 302.

**Differences:** the one-time-link footnote is left out (it no longer matches the server since #703); sign-in links may open the app (§10 item 3).

### 3.5 Sign in with X, Connect X, and other signed-in web pages

**iPhone** (`Core/Auth/WebLoginView.swift`, `Features/Web/CookieWebFlowSheet.swift`, `Features/Web/WebViews.swift`, `Features/Account/AccountModel.swift:17-36`)

| Use | Start | Cookies | End |
|---|---|---|---|
| Sign in with X | `<backend>/auth/login?next=/profile` in a sheet "Sign in with X" with Cancel | fresh, not persisted | the first navigation to the frontend origin whose path does not start with `/auth/` or `/api/` is cancelled; `/login?error=<code>` → that message; otherwise the backend host's `session` and `remember_token` are taken (none → "Sign in with X did not finish. Please try again."); `/account-restore` → restore offer; any other landing → Home |
| Connect X (Account) | `<backend>/auth/x/connect` | the app's cookies copied in | first frontend landing; `x_login=` `linked`, `cancelled`, `failed`, `other_x`, `taken`, `taken_placeholder`, or path `/prepared-account` → a fixed message each; then reload the user |
| X bookmarks connect (Import) | `<backend>/api/external/twitter/connect` | copied in | first frontend landing; the card re-reads `GET /api/external/twitter/status` |
| Admin (More, admins only) | `<frontend>/admin`, "Done" button | copied in | the user closes it |

**Android**
- `android.webkit.WebView` in a full-screen dialog with the same titles and buttons. JavaScript on (X's pages need it); no JavaScript interface; no file access.
- `CookieManager` is shared by every WebView in the process and stored on disk. So: before a flow, remove all cookies; for Connect X, bookmarks connect and Admin, set the app's `session` and `remember_token` for the backend URL; detect the end in `shouldOverrideUrlLoading` with the same rule and return `true`; read the cookies with `CookieManager.getCookie(<backend URL>)`; afterwards remove all cookies and `flush()`. `getCookie` returns names and values without expiry dates, so the session store dates the sign-in as now + 30 days, as the iPhone does when the expiry is missing.

### 3.6 Session storage and sign-out

**iPhone** (`Core/Auth/CookieVault.swift`, `KeychainStore.swift`, `AuthService.swift:200-221`, `App/AppState.swift:454-472`)
- Authentication is by the Flask cookies `session` and `remember_token`. They are kept in one Keychain item per environment (service `org.loore.app.auth`, account `cookies.<environment>`, available after first unlock, this device only): JSON `{cookies:[{name,value,domain,path,expires,isSecure,isHTTPOnly,sameSite}], signedInAt, expiresAt}`. `expiresAt` is the `remember_token` cookie's expiry, else `signedInAt` + 30 days. At launch, a record past `expiresAt` is deleted and the user signs in again; `session` is restored as a session cookie.
- Cookies are captured after verify, after the invite continue, after X sign-in, after any response with `Set-Cookie`, and after an account restore. No cookie is written anywhere else.
- **Logout:** More → "Logout" asks "Log out of Loore?"; on the waitlist, No subscription and Not active screens it is immediate. `GET /auth/logout` with redirects off and a 10 s timeout, only when sign-in cookies exist; errors ignored. Then everything local is cleared: the Keychain record, the cookie jar, caches, web view data; audio stopped; the upload queue, temporary files and recording logs deleted; the user, banners and pending updates cleared; Home selected; toasts, title cache and artifacts store cleared; the profile watcher stopped. A 401 while signed in does the same without the server call.

**Android**
- `SessionStore`: an AES-256-GCM key in the Android Keystore (not exportable; one alias per environment) encrypts the same JSON, stored as a file in `noBackupFilesDir`. Jetpack Security's `EncryptedSharedPreferences` is deprecated; do not use it.
- **The key must be usable while the phone is locked**, the counterpart of the iPhone's "after first unlock": uploads in WorkManager, the Voice turn and Record from the lock screen all need the cookies with the screen locked. So the key is created without `setUnlockedDeviceRequired(true)` and without `setUserAuthenticationRequired(true)`; the file lives in credential-encrypted storage, which Android opens at the first unlock after a restart.
- Backup: nothing in `noBackupFilesDir` (the session file, the upload queue, the recording logs) is backed up or moved to a new phone. `loore_recording_device_key` (one per install) lives in its own preferences file, which `android:dataExtractionRules` excludes from cloud backup and device-to-device transfer, so a restored phone makes its own key.
- Logout clears the same things, plus WebView cookies and storage (`CookieManager.removeAllCookies`, `WebStorage.deleteAllData`), the upload work in WorkManager, and the files waiting in the share folder (§3.31).

### 3.7 Launch and gates

**iPhone** (`App/LooreApp.swift`, `App/AppState.swift:114-332`, `App/RootView.swift:41-101`)
- At launch: clear the system cookie store; restore cookies (or inject the debug cookie); no sign-in cookie → signed out; otherwise `GET /api/dashboard/?profile=0`. 401 → signed out. Another error before the user is known → "unreachable": "Couldn't reach Loore. Check your connection." (offline) or "Loore didn't answer as expected. Please try again in a moment.", with "Try again" and "Sign out".
- After the user loads, in order: mirror `craft_mode` into `loore_craft_mode`; spend cap; payments state; `PATCH /api/dashboard/timezone {timezone}` when the device zone differs (approved users only, errors ignored); `GET /api/updates` once per launch (approved, let in, terms current); open the debug route and the sign-in landing page (approved, terms current); resume chunk uploads a closed app left behind and `POST …/release` abandoned recordings (§3.12); start the profile-generation watcher (approved, let in).
- **Gates**, by phase: launching → logo; signed out → Sign in; unreachable; restore offer; account deleted; signed in → (1) approved, not let in and no recording under way → **No subscription**; (2) approved → the tabs; (3) `account_blocked` → **Account not active**; (4) otherwise → **Waitlist**. The **Terms** screen covers everything while `!terms_up_to_date && !account_blocked`. The **Updates** sheet shows once per launch when signed in, approved, terms current and something is unread. Toasts and banners sit at the bottom: 24 pt above the edge, or 64 pt plus the mini-player's height for approved users.
- When the app returns to the foreground: re-read the dashboard while payments are enabled (clears "REPLIES PAUSED" when replies are back); wake every poller.

**Android:** the same order; "returns to the foreground" is `ProcessLifecycleOwner` `ON_START`.

### 3.8 Payments and spend cap

**iPhone** (`Core/Models/Subscription.swift`, `Core/Models/User.swift:138-150`, `Core/Util/DateFormatting.swift:54-106`, `Features/Onboarding/NoSubscriptionView.swift`)
- The user payload carries `payments_enabled` and `subscription {level, status, state, current_period_end, trial_end, cancel_at_period_end, plan_set_by_hand, ai_replies, alpha_tester, free_month_*}`. Replies paused = payments on and `ai_replies == false`: Voice refuses before recording, banner "REPLIES PAUSED". Not let in = approved, payments on and `state == "none"` → No subscription screen. Payment failed (`past_due` and `lapsed`) → a banner at load.
- Account shows the subscription in words (level Standard, Supporter or Reduced; trial, renewal, cancellation, past due, ended, free month), dates as "MMMM d, yyyy" in UTC.
- **The app sells nothing, links to no payment page and never says where or how to pay** (Peter, 2026-11-12; `backend/routes/billing.py`). The server's texts name only the state.
- Spend cap: banner texts for payments off, payments on, and "lifts with your first payment"; the refusal toast "You've reached your monthly usage limit, so you can't <start a new recording | upload audio | glean> until it resets on <date>." (or "… until your first payment."); the reset date is the first day of next month, UTC.

**Android:** the same. Google Play's payments policy points the same way.

### 3.9 Home

**iPhone** (`Features/Home/HomeView.swift`)
- Greeting by local hour (before 12 "Good morning", before 18 "Good afternoon", else "Good evening"), heading "What's on your mind?".
- Without Glean: cards Voice ("Speak what's present.") → `/voice`, Text ("Type what's on your mind.") → `/textmode`, Share ("Give something outward.", with `share_v1_enabled`).
- With `glean_enabled`: cards **Reflect** ("Talk it through with Loore.") and **Glean** ("Reflect, and Loore gleans for you."), then Share in its own row. Each card has a Voice / Text switch that only chooses the mode, stored per card in `home.reflectMode` and `home.gleanMode` (`voice` or `text`, default `voice`); a tap elsewhere on the card opens Voice or Text mode, with `glean` for the Glean card.

**Android:** the same.

### 3.10 Voice mode

**Files:** `Audio/Voice/VoiceTurnController.swift` (VTC), `VoiceTurnDependencies.swift`, `VoiceApproval.swift`, `SpokenRefusal.swift`, `VoiceAIBlock.swift`; `Features/Voice/VoiceView.swift`, `VoiceVisuals.swift`; `Audio/AudioCenter.swift`.

**States** (VTC:19-30): `idle, starting, recording, stopping, transcribing, awaitingAudio, playing, draining, done`, with the flags `isPaused`, `isInterrupted`, `awaitingNextNode`, `hasError`, `aiBlock`, `lengthNotice`, `waitingApprovals`, `gleanPhase`. A `generation` counter, raised by cancel, continue and teardown, makes every late continuation a no-op. Screen phases: ready (idle); recording (starting, recording, stopping); processing (transcribing, awaitingAudio, draining while waiting for the next node); playback (playing, draining, done).

**What the user sees**
- **Ready:** "What's on your mind?", a dim ECG line, "You're offline" when offline, the thread-length notice, a red dot for 3 s after an error, the 72 pt Record button (disabled offline, during a glean, or while an approval waits, with "A change to your data waits for your approval · Review"), the Glean pill in Glean conversations, and "Text Mode" at the top right (opens the last reply's thread, or Text mode).
- **AI block:** "Voice mode needs AI" or "This thread is too long", the body, "Account settings" (`/account#ai-usage`) or "Start a new thread", and "Back to the thread".
- **Recording:** ECG with a scan line, 24 level bars, `m:ss` (" · paused"), the red interruption text, and Stop. While paused or interrupted only Resume shows; Pause and Stop are on the lock screen.
- **Processing:** "Thinking...", a pulsing dot and ✕ (Cancel).
- **Playback:** −10 s / play-pause / +10 s, a progress bar with chapter ticks, time and total with a dot while audio is still being generated, the chapter list (Roman numerals) when there is more than one chapter, the proposal card, and Continue (or the thread-too-long block). No rate control on this screen.
- **Recovery banner:** "Unfinished `<label ?? Voice>` recording" with "Continue recording" and "Discard".
- The screen stays blank until the availability check and the interrupted-recording check have answered.

**One turn, in order** (routes in Appendix A)
1. **Screen opens:** abandoned recordings released (at launch); `GET /api/voice/availability?parent=` for a thread (a new conversation is decided locally from the account's default AI usage; no answer is not a block); `GET /api/drafts/interrupted` (unless the route resumes; the newest is offered; a playing reply pauses, and resumes after Discard); `GET /api/tool-approvals?node=<parent>`.
2. **Record tap:** client checks first — spend cap (toast and spoken sentence), replies paused (banner), microphone permission (asked **before** `init`), notification permission at the first tap. Then `POST /api/drafts/streaming/init {privacy_level:"private", ai_usage:<account default>, label:"Voice", device_key, parent_id|null}` (in a task that survives cancellation, so a session made by a cancelled tap is still discarded). Then the recorder starts (§3.11).
3. **Chunks** upload while recording (§3.12).
4. **Stop:** the thinking cue starts **before** the microphone stops (§3.16); the uploader settles; the audio routing switches to playback (§3.17). Nothing recorded → `DELETE …/discard`, quietly back to ready. A chunk still missing → "Part of your recording hasn't reached Loore yet (no connection?). It is kept on this phone and uploads when you're back online: then open Voice to continue or discard it." with the error sound; no finalize. Otherwise `POST …/finalize {total_chunks, label:"Voice", device_key, parent_id?, model?, entry:"glean"?}` (120 s timeout).
5. **Transcribing:** `GET …/status` every 1 s, up to 15 min ("Transcription is taking too long…"); three 404s in a row → "Transcription failed. Please try again." The draft's SSE is not used in Voice. Empty content → quietly back to ready; `warning` → toast and its `warning_code` (§3.13); `llm_node_id` → the reply starts; otherwise the older route `POST /api/voice/ {content, model, ai_usage, parent_id, session_id, entry}`.
6. **Reply:** §3.15. When the final node completes, the thread's parent becomes that node; `GET /api/tool-approvals?node=` and `GET /api/voice/approval-announcement?node=` run; a waiting change's announcement clip is added to the end of the queue (the queue end waits at most 5 s for that answer), and the approval dialog opens once nothing plays.
7. **Timing marks:** `rec_stop`, `finalize_acked`, `llm_node_known`, `tts_attach`, `chunk_ready`, `playing`; at first playback `GET /api/voice/timing/clock` three times (lowest round trip gives the clock offset), then `POST /api/voice/timing {node_id, marks, offset_ms, rtt_ms}`; failures ignored.
8. **Continue** (playback phase) is step 2 with `parent_id` = the last reply node.

**Timers** (VTC:201-226): status poll 1 s, status give-up 15 min, llm-status poll 1.5 s, reply give-up 30 min, no llm-status answer 2 min, reconcile every 7 s, re-send a lost TTS trigger after 20 s, 3 s grace before taking over a completed file, error dot 3 s, clock tick 0.25 s, long-recording warning at 59 min, thinking cue at most 10 min per wait, glean poll 2 s, error sound 0.7 s.

**Cancel, leave, continue elsewhere**
- **Cancel (✕)** is ignored while stopping (finalize must not be lost); otherwise it unloads the queue, releases the audio and returns to ready. The server keeps generating. The next turn keeps the thread's parent.
- **Leaving the Voice screen** (pop, not a tab switch): a recording with no chunk yet is discarded (`DELETE …/discard`); otherwise the recorder stops, queued chunks keep uploading, the session stays in recording and is offered for recovery once the server sees it idle (45 s). Each new Voice screen is a new conversation.
- **Continue recording** (#577/#388): microphone permission; a draft recorded in a browser that records WebM ("This recording was started in a browser that records in another format, so it can only be continued there.") stops here; otherwise **no `init`**: the recorder starts and holds its chunks, numbered from `chunk_count`; `POST …/claim {device_key}` returns `next_chunk_index`; the held chunks are renumbered and released to the uploader (the first starts with `ftyp`, so the server opens a subsession). Stop before the claim answers sends nothing.
- **Recording continued on another device:** any 409 `session_taken_over` ends the turn with "This recording was continued in another tab or on another device, so recording stopped here…" and the error sound.
- **Discard:** `DELETE /api/drafts/streaming/<sid>/discard`; the local chunks are forgotten.
- **Interruption** (call, another app takes the microphone, headset microphone lost): the recording holds; interruption sound, a toast that stays 24 h, the notification "Recording paused — tap to resume", the lock-screen controls updated; the sound again when the interruption ends and when the user returns to the app. No automatic resume. A failed resume: toast and the notification "The recording could not resume".
- **Glean in Voice:** `POST /api/read/from-node/<anchor> {}`, then llm-status every 2 s (30 min in all, 2 min without an answer, counted while the app is in front); `glean_day_done` is spoken and the screen stays; otherwise the gleaning's thread opens.

**Android**
- `VoiceTurnController` is ported to `:core` with the same states, flags, generation counter, timers and `VoiceBackend` interface, and with the iPhone's Voice tests (`VoiceTurnTests` and the other `Voice*Tests`, §7). The Android parts (recorder, player, sounds, routing, notifications) sit behind interfaces, as on the iPhone.
- The controller lives in `AudioCenter` (application scope), not in the screen; the Voice screen observes it. The foreground service (§3.18) starts at the record tap.
- Permissions at the record tap, in the iPhone's order: `RECORD_AUDIO`, then `POST_NOTIFICATIONS` (Android 13 and later), before `init`.

### 3.11 Recording: format and chunk contract

**iPhone** (`Audio/Recording/VoiceRecorder.swift`, `AudioSources.swift`, `FMP4SegmentWriter.swift`, `SegmentPackager.swift`, `RecordingLog.swift`)
- **Format:** 48 kHz mono 16-bit PCM in; AAC-LC 64 kbps, 1024 samples per packet; fragmented MP4; one media segment per 15 s of audio (about 120 KB). Timestamps come from the packet count (`packets × 1024 / 48000`), so a pause leaves no gap.
- **Chunk contract** (server: `backend/utils/webm_utils.py` `extract_mp4_init_segment`, `concat_fragmented_media`): chunk 0 = the initialization segment (`ftyp` + `moov`) followed by the first media segment (`moof` + `mdat`); chunks 1…N = one media segment each, with continuous timestamps. Chunk 0 without a `moov` before the first `moof`/`mdat` → 400 `init_parse_failed`. A chunk N>0 that starts with `ftyp` opens a server **subsession**, transcribed separately (#124). The server joins the chunks and remuxes them, so any standard fragmented MP4 with one AAC track works.
- **Upload form:** multipart; file field `chunk` (filename `chunk_<i>.mp4`, type `audio/mp4`); fields `chunk_index`, `mime_type=audio/mp4`, `device_key` (one UUID per install, `loore_recording_device_key`).
- **Segments are cut** every 15 s of audio and on pause, interruption and stop.
- **User pause** (lock screen only): the samples are dropped and the segment flushed, while the microphone keeps running, so Resume needs no restart; same writer, continuous timestamps, no subsession.
- **System interruption:** the segment is flushed and capture paused; Resume starts capture again with the same writer.
- **Writer failure** (media services reset): Resume makes a new writer that continues the numbering; its first chunk starts with `ftyp` (subsession).
- **Capture watchdog:** after a route change, restart capture now and retry after 0.5, 1 and 2 s; every 1 s, no buffer for 2 s → restart; after 2 restarts without audio → hold (as an interruption).
- **Elapsed time** = offset + seconds written; a continued draft's offset is `chunk_count × 15`.
- **Recording log** (`RecordingLog.swift`): one plain-text file per record tap in `RecordingLogs/` (route and format, preferred input, interruptions, route changes, restarts, chunk sizes, input level once a second; no audio, no words); the newest 20 kept; excluded from backup; deleted at sign-out.

**Android**
- **Capture:** `AudioRecord` at 48 kHz, mono, PCM 16-bit, always; Android resamples when the input runs at another rate (a Bluetooth headset microphone runs at 16 kHz). Source `MIC` for the phone's microphone and `VOICE_COMMUNICATION` when a headset is the communication device (§3.17). In M5 the builder records the same test passage with each source on a device, compares the transcripts and records the choice in `android/PROGRESS.md`.
- **Encoding:** `MediaCodec` AAC-LC (`audio/mp4a-latm`), 48 kHz, mono, 64 kbps; the codec's `csd-0` (AudioSpecificConfig) goes into the `esds` box. Presentation times from the frame count, as on the iPhone.
- **Fragmented MP4:** an own writer in `:core` (`Fmp4Writer`), because `MediaMuxer` does not write fragments and the segments must be cut on demand (pause, interruption, stop), not on a timer. It writes `ftyp`, `moov` (`mvhd`, one `trak` with an `mp4a`/`esds` sample entry and empty sample tables, `mvex`/`trex`), then per segment `moof` (`mfhd`, `traf` with `tfhd`, `tfdt`, `trun`) + `mdat`. Timescale 48000, sample duration 1024. `SegmentPackager` is ported unchanged.
- **Prove the chunk contract first** (the device spike after M1, §9, before anything is built on it), as the iPhone did: a 30–60 s recording against the local backend → `finalize` → the transcript appears, and the server's joined file plays to its full length. If the server refuses the chunks and no writer change fixes it, stop and report.
- **Pause, interruption, writer failure, watchdog and elapsed time:** the same rules and numbers. On Android the watchdog also restarts capture when `AudioRecord.read` returns `ERROR_DEAD_OBJECT`; an encoder error makes a new encoder and writer (subsession), as a writer failure does on the iPhone.
- **The user pause keeps `AudioRecord` running** with the samples dropped, as on the iPhone. Android would allow a restart inside the running microphone service, but keeping capture open avoids a resume that fails after a call; the microphone indicator stays on during the pause, as on the iPhone.
- **Recording log:** the same content in `noBackupFilesDir/recording-logs/`, newest 20 kept, deleted at sign-out. Read from a debug build with `adb shell run-as org.loore.app …`; release builds give no access (on the iPhone the logs too need a build installed from Xcode).

### 3.12 Uploads, offline, and recovery

**iPhone** (`Audio/Recording/ChunkUploader.swift`, `Audio/AudioCenter.swift`)
- Each chunk's multipart body is written to `VoiceUploads/<sid>/chunk_<i>.body` with a `manifest.json` before it is sent; files are protected until first unlock, so uploads work with the phone locked; a body is deleted once the server has it.
- **Order:** one worker per recording, in index order. Chunk 0 goes first and nothing overtakes it. While recording, chunk 0 is never given up on a transport error or 5xx (retried with a 16 s cap, and handed to the background uploader after 4 retries).
- **Retries:** delays 2, 4, 8, 16 s (5 attempts). Then the chunk is `failed`, gets a background copy, and the recording becomes **degraded**: later chunks get one attempt each and then a background copy. Any stored chunk clears degraded.
- **Answers:**

  | Answer | Result |
  |---|---|
  | 2xx (also 200 "Chunk already uploaded") | stored |
  | 400/413 `chunk_index_out_of_range`, `recording_too_large`, `chunk_too_large` | limit reached: this and every later chunk are never sent; the server's words once (§3.13) |
  | 400 `chunk_not_announced` | dropped silently |
  | 400 `init_parse_failed` | fatal: the recording's audio is deleted; "Your audio recording could not be processed…" |
  | 409 `session_taken_over` | fatal (continued on another device) |
  | other 409 | retry |
  | 401, 403, 404, 413, 422, other 400 | give up |
  | 402, 429 | refused: kept on the phone; the server's message once per recording, 10 s toast |
  | 5xx (`session_busy`, `recordings_busy`, `usage_catching_up`), transport error | retry |

- **Background** (`ChunkUploader.handOffPendingToBackground`): when the app goes to the background, a recording whose first chunk (the one carrying the initialization segment) is not stored yet hands only that chunk to a background `URLSession`, and its other pending chunks wait; once the first chunk is stored, every pending chunk gets a background copy. The system finishes them even if the app is closed; a background answer counts only when it is 2xx.
- **Stop:** wait for the worker; one more attempt per missing chunk in order (stop if chunk 0 fails); `total_chunks` = earlier writers' chunks + (all produced chunks less those beyond the limit, or the stored ones when some failed). The server waits up to 10 min for announced chunks.
- **Next launch** (`resumePending`): unreadable or fatal recordings are deleted; failed chunks with a body are sent again; a recording with nothing left to send whose recorder was still running gets `POST …/release` (5 s timeout), but only while its last upload is less than 40 s old (the server's 45 s idle window less 5 s): after that window the server offers the recording to other devices anyway, and one may have continued it. The Voice screen waits for those releases before asking for interrupted drafts. **Nothing is finalized automatically**: the user continues or discards through the recovery banner.
- **Sign-out** deletes the whole queue.
- **Offline:** Record, Continue and Glean are disabled with "You're offline"; the idle dictation button is disabled; lock-screen Record and Glean are refused with a notification. The uploader ignores the network state and retries.

**Android**
- The same queue, order, retry schedule, answer table, degraded mode and `total_chunks` rule, in `ChunkUploader` (one coroutine per recording). Files: `noBackupFilesDir/voice-uploads/<sid>/` (credential-encrypted storage, available after the first unlock, like the iPhone's protection class).
- **While the audio service runs** (§3.18), uploads stay in the app's process with the screen off: a process with a foreground service keeps network access, also in Doze.
- **Background copies become WorkManager work:** one unique `UploadWorker` per recording (`ExistingWorkPolicy.KEEP`, network required, exponential backoff starting at 30 s), enqueued when a chunk would get a background copy on the iPhone, when the app goes to the background with chunks pending and the service is not running, when the service stops with chunks pending, and at launch for queued recordings. The worker runs the same upload code and the same order rule: while a recording's first chunk is not stored, it sends only that chunk; once it is stored, every pending chunk. The server deduplicates by chunk index, and a per-recording lock keeps the worker and the in-process uploader from sending the same chunk at once. WorkManager survives the process ending and a reboot.
- The next launch, sign-out and offline behaviour are the same.

### 3.13 Recording limits and refusal codes

**Server limits** (`backend/utils/recording_limits.py`): 15 s per chunk; a chunk at most 8 MiB; **transcribed up to 2 h** per recording (past that, the text ends with a note and the rest is kept as audio); **at most 960 chunks** (4 h) and about 346 MB per recording; 6 open recordings counted over 24 h; an empty recording expires after 15 min, at most 30 empty starts in that time.

**App behaviour at each bound**
- **59 minutes** (Voice and dictation): a rising two-note sound, a 10 s toast and the notification "You've been recording for 59 minutes" / "One recording is turned into text up to two hours; for longer, continue in a new recording." Nothing stops.
- **2 hours** (text): dictation shows once "This recording is longer than two hours. The rest of it is saved as audio, but not turned into text." (from `chunk_error` `recording_too_long` on the transcription stream); Voice shows nothing while recording; the entry ends with the server's note.
- **4 hours, or the size bounds:** the server's words in a 10 s toast ("This recording has reached the longest Loore takes, about four hours, and has stopped. Everything before this is saved."), then Stop; finalize runs with the stored chunks and the reply is made. Dictation plays the error sound and stops; "Save audio" still holds everything.

**Refusals at `POST /api/drafts/streaming/init`** (`backend/routes/drafts.py`)

| Status / code | Voice | Dictation |
|---|---|---|
| 402 `monthly_spend_limit_reached` | banner, 8 s toast, spoken sentence | toast |
| 402 `subscription_required` | the server's message; nothing spoken | banner |
| 403 `ai_usage_none` (scope account, thread or entry) | the AI block screen; spoken | error with message |
| 409 `approval_pending` | the approval dialog; spoken | — |
| 409 `thread_too_long` | the block; spoken | — |
| 503 `usage_catching_up`, 503 `recordings_busy`, 429 `too_many_recordings` / `too_many_starts`, 400 (label, device key), 404/410 (parent) | 8 s toast with the server's text, red dot 3 s, no sound | error with message |

**Other refusals:** `claim` (400 bad device key, 404 `session_not_found`, 400 `session_not_active`, 409 `session_live`, 503 `session_busy`, 402/429/503) → "The recording could not be continued. Please try again." or the server's words; `audio-chunk` (§3.12, plus 404 `session_not_found`, 400 `session_not_active`, 400 `mime_mismatch`, unsupported type); `finalize` (400 "not in recording state" counts as success; 409 `session_taken_over`; anything else → "Your recording could not be sent. It is kept on the server: open Voice again to continue or discard it." and the error sound); the older `POST /api/voice/` (403 `ai_usage_none`, 409 `thread_too_long`, 402 spend cap, 409 `approval_pending`, 402 `subscription_required` with one of the access-ended sentences → entry saved and spoken; other 402 `subscription_required` → quiet end).

**Spoken refusals** (`Audio/Voice/SpokenRefusal.swift`; server `backend/utils/voice_refusals.py`, #651, #438, #648, #717): the codes `monthly_spend_limit_reached`, `ai_usage_none`, `approval_pending`, `thread_too_long`, `glean_day_done`, and `subscription_required_{ended,past_due,free_month,none}` (access ended while recording: the recording is kept, only the reply is skipped) — whether they come as an HTTP code, a `warning_code` or an llm-status `error_code` — are said aloud: the clip `GET /api/voice/refusal/<code>.mp3` (waited for at most 4 s, kept in memory for the app run, played on its own player, never in the reply queue); without the clip, the device's speech engine says the same sentence (an English voice, at most 15 s). No error sound, no red dot. A new recording stops the sentence.

**Android:** the same limits, texts and codes. Speech fallback: `android.speech.tts.TextToSpeech` with an English locale. The sentences for the fallback are copied from `backend/utils/voice_refusals.py`, as the iPhone's are.

### 3.14 Text mode and dictation

**Text mode** (`Features/Write/TextModeView.swift`): "What's on your mind?" above the writing form; AI usage starts from the account default. AI usage None → toast "Turning off auto-generate. AI usage on some nodes is turned off." and a plain `POST /api/nodes/` (or `save-as-node` for a dictated entry). Otherwise `POST /api/textmode/start {content, privacy_level, ai_usage, auto_generate, entry:"glean"?}` (`auto_generate` is the stored preference, off for shared entries); the user's node opens through NodePrefetch with the reply pending. A reply under a gleaning uses `POST /api/textmode/from-node/<id> {content, ai_usage, model, auto_generate}`. "Write New Entry" (More, craft mode) is a sheet of the same form with the agentic option.

**Dictation** (`Audio/Dictation/DictationController.swift`, `Features/Voice/StreamingMicButton.swift`, `Features/Write/NodeFormModel.swift`)
- States `idle, initializing, recording, finalizing, error`. Disabled when AI usage is off, a file is attached, or offline while idle; refused while a Voice turn is active ("Finish the voice conversation first.") and when spend-capped.
- Start: microphone permission, `init {parent_id, privacy_level, ai_usage, device_key}` with **no label** (the server only transcribes), then the recorder. The transcription stream `GET /api/sse/drafts/<sid>/transcription-stream?device_key=` (45 s stall, reconnect after 3 s, up to 20 reconnects, and again after the server closes the stream at 2 h) puts `content_update` into the editor after the existing text with a blank line.
- Shown: the clock, "Offline — recording continues, uploads will retry when connection returns", the interruption notice with Resume, "Stop & save", "Save audio" (writes the recording's chunks, which together are a valid `.m4a`, to a temporary file and shares it).
- Stop: settle; `finalize {total_chunks, device_key}`; `/status` every 2 s up to 15 min, or the stream's `all_complete`; the transcript lands in the form and the draft is saved. Send: `POST …/save-as-node {content, privacy_level, ai_usage, agentic?, auto_generate?, entry?}`.
- Recovered audio: a form draft with `session_id` and `has_stored_chunks` → "Transcribing recovered audio..." → `POST …/transcribe-remaining`, then `/status` every 3 s for up to 5 min.

**Android:** the same, on the shared recorder and uploader. "Save audio" shares through `Intent.ACTION_SEND` and a `FileProvider` URI (§3.31).

### 3.15 Reply audio and the queue player

**iPhone** (`Audio/Voice/VoiceTurnController.swift:941-1400`, `Audio/Playback/ChunkQueuePlayer.swift`, `ChunkQueueMath.swift`, `Core/Networking/SSE.swift`, `Core/Models/StreamEvents.swift`)
- **Attach rule:** while not attached and the reply is not finished, attach `GET /api/sse/nodes/<id>/tts-stream[?last_chunk=N]` when `tts_streaming` is true or `tts_task_status` is pending or processing. When the node completes: warnings as 8 s toasts (once each), `thread_length` noted, approvals and the announcement asked for; then `POST /api/nodes/<id>/tts` once, unless the stream already sent `all_complete`, the whole file was delivered, or the reply is being spoken while written. `POST /tts`: 200 with chunks already received → attach again (the stream replays the rest); 200 without → play the whole `tts.mp3`; 202 → attach; no answer → re-arm the watchdog; HTTP error while attached → keep the stream; otherwise the reply failed.
- **Stream events:** `chunk_ready` (deduplicated by `<node>:<chunk_index>`); `all_complete` → the continuation node (`continuation_node_id`) or the end; `error` → failed; `close` (the server ends a stream after 30 min) → attach again with `last_chunk`; a JSON answer instead of a stream: 200 with `tts_url` = the whole file, otherwise not attached and tried again later.
- **Chapters:** the first chunk of each node opens a chapter titled from the node's text (markdown removed, at most 44 characters, cut at a word after character 20), "…" while the node is unfinished, renamed from `all_complete.preview`. A chain's continuation plays in the same queue.
- **Reconcile** every 7 s while audio is missing and when the app returns to the foreground: re-send `POST /tts` if nothing arrived and the last try is over 20 s old; `tts-status` completed → wait 3 s, then attach again or play the whole file; `failed` → reply failed.
- **Failures:** with audio already queued → toast, red dot, the error sound after the queue ends; without → toast, error sound, back to ready. `cancelled` ends the turn without the error sound; `thread_too_long` and the access-ended codes are spoken instead.
- **SSE client:** cookies, `X-Timezone`, `Accept: text/event-stream`, `Cache-Control: no-cache`; 90 s request timeout; no byte for 45 s = stall (the server sends a heartbeat every 15 s); reconnect after 3 s, up to 5 failed connections in a row (any event resets the count); an HTTP error status is not retried; terminal events `done`, `all_complete`, `error`, `close`; a non-stream answer is delivered as a response.
- **Queue player:** one item per chunk plus a durations list for total time, chapters and seeking across chunks; time updates every 0.1 s; durations corrected when more than 1 s off (0.5 s at item end); unknown durations read from the file, else 300 s; "at end" within 0.5 s; a seek within 0.1 s of the end shows the end and pauses; skip ±10 s; rates 1, 1.25, 1.5, 2 (cycling, pitch kept), kept in memory for the app run; deduplicated by URL; `stop()` keeps the queue (the mini-player can replay); `close()` removes it; play at the end replays from 0; a queue that runs dry while audio is still generated waits and plays the next chunk at once. Every media request carries the session cookies. A failed item is skipped, with one 6 s toast per queue ("Network error loading audio. Try again." / "Audio playback failed." / the WebM message).
- **WebM originals** (desktop recordings): played from the server's MP3 (`audio-download?format=mp3`).

**Android**
- The controller logic is ported unchanged.
- `ChunkQueuePlayer` wraps one ExoPlayer with a playlist of `MediaItem`s (one per chunk); `ChunkQueueMath` and its tests are ported, so total time, chapters and seeking match. Media is fetched through Media3's OkHttp data source on the app's `OkHttpClient`, so cookies come from the same jar.
- `setPlaybackSpeed` for the rates; `setSeekBackIncrementMs(10_000)` and `setSeekForwardIncrementMs(10_000)`; audio attributes usage `MEDIA`, content `SPEECH`, with ExoPlayer handling audio focus; `setHandleAudioBecomingNoisy(true)` pauses when headphones are unplugged or disconnected (no automatic resume, as on the iPhone); `setWakeMode(C.WAKE_MODE_NETWORK)` keeps the CPU and Wi-Fi awake while playing.
- WebM originals: the same MP3 route as the iPhone.

### 3.16 Sounds and the thinking cue

**iPhone** (`Audio/Playback/Sounds.swift`, `Features/Voice/VoiceSettingsSection.swift`): every sound is generated in code (44.1 kHz, 16-bit mono, 5 ms attack).

| Sound | Content | Level | When |
|---|---|---|---|
| Thinking cue | 196 Hz × 0.6 + 293.66 Hz × 0.4, sin² swell over the first 2.4 s of a 4 s loop, then silence; loops | −24 dBFS in the file; Soft plays at volume 0.5 (≈ −30 dBFS), Very soft 0.2 (≈ −38 dBFS), Off not at all | from just before the microphone stops until audio plays; again when the queue runs dry while audio is generated, while waiting for a continuation, after an interruption ends; stops when audio plays, on pause during a wait, finish, cancel, error, refusal; at most 10 min per wait |
| Error | 660 → 440 → 330 Hz, near-square wave, gain 0.15, 0.7 s | ≈ −15.5 dBFS | failed turn or reply, failed listen-aloud, dictation failures |
| Interruption | the same three notes, gain 0.25 | ≈ −11 dBFS | a recording holds; again when the interruption ends and when the app becomes active while held |
| 59 minutes | 784 → 1047 Hz triangle wave, twice, gain 0.2, 1.1 s | ≈ −15 dBFS | once per recording at 59:00 |

Setting: Account → Voice, "Sound while Loore thinks": Soft / Very soft / Off, per device in `loore_thinking_cue` (`soft`, `very_soft`, `off`; default `soft`). Choosing a level plays a 2.5 s sample (not during a Voice turn). Off shows: "Without it, iOS may pause Loore while you wait, and the reply will need a tap when your phone is locked."

**Android**
- `ToneSynth` ported (same waveforms and levels), played with `AudioTrack` (static buffers; the cue loops), usage `MEDIA`, content `SONIFICATION`, so the sounds follow the media volume like the replies.
- The cue keeps its purpose of telling a person with the phone in a pocket that the reply is coming, with the same three settings and default. Keeping the app running while it waits is done by the foreground service and a wake lock (§3.18), not by the cue, so **Off shows no warning on Android**.

### 3.17 Audio routing, audio focus, Bluetooth and interruptions

**iPhone** (`Audio/Session/AudioSessionController.swift`, `AudioRoute.swift`, `Audio/AudioCenter.swift:339-369`)
- **Record tap:** play-and-record, mode `.videoChat`, Bluetooth HFP and A2DP allowed, default to the speaker; system alert sounds held back; the headset's microphone set as the preferred input (a Bluetooth HFP, wired, USB or car microphone) (#423).
- **Stop (Voice):** switch to playback / spoken audio so the reply plays over Bluetooth A2DP rather than the narrowband headset profile. Dictation releases the audio at Stop.
- The audio stays active from the record tap until the reply's queue ends, cancel, an error (0.7 s after the error sound), a refusal sentence, leaving the screen, or sign-out.
- **Events:** an interruption (call, Siri) holds a recording and pauses a playing queue (resumed afterwards only if it was playing and the system allows); headphones unplugged or AirPods removed pause a playing queue (no automatic resume); **headset microphone lost** while recording → hold with capture kept running and the samples dropped (rather than recording from the phone's own microphone in a pocket), the interruption sound, a 24 h toast "Recording paused — the headphones' microphone disconnected…", the notification; Resume is manual, also when the headset returns.

**Android**
- **Record tap:** if a Bluetooth headset (`TYPE_BLUETOOTH_SCO`, or `TYPE_BLE_HEADSET`) is among `AudioManager.getAvailableCommunicationDevices()`, set `MODE_IN_COMMUNICATION` and `setCommunicationDevice(<it>)`, and record with source `VOICE_COMMUNICATION`. A wired or USB headset microphone is chosen with `AudioRecord.setPreferredDevice`. Otherwise the phone's microphone.
- **Stop (Voice):** `clearCommunicationDevice()` and `MODE_NORMAL`, so the reply plays over A2DP. Dictation does the same at its Stop.
- **Interruptions while recording:** `AudioManager.registerAudioRecordingCallback`; when the app's recording configuration reports `isClientSilenced()` (a call, or another app that took the microphone) the recording holds, exactly as an iPhone interruption begins; when it is no longer silenced, the interruption sound plays again while held. Losing audio focus transiently while recording counts the same way.
- **Playback:** ExoPlayer's focus handling pauses on a transient loss (call) and resumes after it; `ACTION_AUDIO_BECOMING_NOISY` pauses.
- **Headset microphone lost:** `AudioDeviceCallback.onAudioDevicesRemoved` and `OnCommunicationDeviceChangedListener`, ignoring the change the app makes itself at Stop → the same hold, texts and manual Resume.
- Nothing corresponds to holding back system alert sounds or to the iPhone's rule that inactive audio cannot be started again from the background (§3.18).
- Prove on a device in M7: recording with Bluetooth headphones while walking with the phone locked, the reply over A2DP, a call during a recording, headphones switched off mid-recording.

### 3.18 Lock screen and background

This is the reason for the app; it is the part that differs most in mechanism and least in behaviour.

**iPhone** (`Audio/Session/NowPlayingController.swift`, `Audio/Session/VoiceLiveActivity.swift`, `Shared/VoiceActivity.swift`, `LooreLiveActivity/*`, `Audio/AudioCenter.swift:380-558`)
- The app has the `audio` background mode. It stays alive while audio runs: the microphone (also while paused), then the thinking cue, then the reply queue. That is why the cue starts before the microphone stops.
- **Live Activity** (iOS 18 and later; one per conversation, requested when a turn starts, while the app is in front):

  | Phase | Text | Buttons |
  |---|---|---|
  | starting | "Starting…" / "Loore · Voice" | — |
  | recording | "Recording" / running clock | Pause, Stop |
  | paused | "Paused" / m:ss | Resume, Stop |
  | interrupted | "Recording paused" / "m:ss · a call or another app took the microphone" | Resume, Stop |
  | sending, thinking | "Sending…", "Thinking…" | — |
  | replying, finished, ready | "Loore is replying", "Reply finished", "Voice" | Record ("Record a reply"); Glean in Glean conversations (dimmed while the reply comes) |
  | a change waits for approval | "Approve or reject the change in Loore" | no Record |
  | gleaning | "Gleaning…" / "Loore · Glean", then "Open Loore to read it when it's ready" | — |

  Pause and Stop run in the app; Resume and Record are the only way to start the microphone from the background. The buttons work without unlocking (#397).
- **Now Playing** (artist "Loore", artwork the Loore logo on `#0E0D0B`): recording, only when no Live Activity shows ("Recording m:ss", play = resume, pause, next = stop and send); thinking ("Voice…", no commands); playback (title "Voice", the listened item's title or "Loore", plus " — <chapter>" when there are several chapters; play, pause, ±10 s, position, rate); nothing loaded → cleared.
- **Record from the lock screen** (`AudioCenter.swift:491-558`): ignored while gleaning; spend cap → banner, toast, notification, spoken; offline → notification "You're offline. Record again when you're back online."; a change waiting for approval (list fetched again) → notification and spoken; thread too long → notification and spoken; otherwise Continue (stopping a playing reply) or start; waits up to 15 s for the start, else a notification with the refusal or the last toast, or "Open Loore and try again." Glean from the lock screen does the same checks.
- **A closed app:** the card stays; a tap posts "Loore was closed in the meantime. Open Voice in Loore to continue the conversation."; leftover cards are ended at the next launch.
- **Returning to the foreground:** approvals refreshed if one waits; the interruption sound if a recording holds; reconcile when waiting for audio.

**Android**
- **One foreground service, `AudioService`,** declared with `android:foregroundServiceType="microphone|mediaPlayback"` and the permissions `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_MICROPHONE`, `FOREGROUND_SERVICE_MEDIA_PLAYBACK`, `RECORD_AUDIO`, `POST_NOTIFICATIONS`, `WAKE_LOCK`. It hosts a Media3 `MediaSession` on the shared ExoPlayer and posts its own notification.
- **Voice:** the service enters the foreground with both types **at the record tap, while the Voice screen is in front** (Android grants the microphone type to a foreground service only when it starts from the foreground or from an allowed case such as a notification action). It stays in the foreground for the whole conversation, so Resume and Record from the lock screen can open the microphone again. It leaves the foreground when the user leaves the Voice screen, signs out, or after the conversation has been idle (no recording, no audio, nothing pending) for 30 minutes in the background; then the notification goes. Notification updates use `NotificationManager.notify` with the same id, not a second `startForeground`.
- **The notification replaces the Live Activity and Now Playing,** with the same phases, texts and buttons as the Live Activity table: Pause / Resume / Stop while recording (with a chronometer), no buttons while sending or thinking, Record (and Glean) after the reply, the approval line without Record. During the reply it is a media notification (`MediaStyle` with the session), so the lock screen and the system media controls show play/pause, ±10 s, the position and the rate; Record and Glean are custom session commands. Visibility public on the lock screen. Actions work without unlocking, as on the iPhone. The session's metadata follows the iPhone's Now Playing (artist "Loore", the same titles and artwork).
- **Thinking:** the session reports buffering with no commands; the notification says "Thinking…".
- **Notification permission refused** (Android 13 and later then hide ordinary notifications; media notifications are excepted): the recording phase moves onto the media session, as the iPhone moves it onto Now Playing when no Live Activity shows. A small Media3 `SimpleBasePlayer` stands for the recording: title "Recording m:ss" or "Paused m:ss", play = Resume, pause = Pause, next = Stop and send. The lock screen and the system media controls then keep Pause, Resume and Stop; after the reply, Record and Glean are custom commands on the reply's media notification. The five alerts of §3.19 are not shown; the toasts in the app still are.
- **Staying awake:** from Stop until audio plays, and while the queue waits for more audio, the service holds a partial wake lock with a 10-minute timeout per wait (the iPhone's cue limit), so polling and the SSE continue with the screen off whatever the cue setting. During playback ExoPlayer's wake mode holds the CPU and Wi-Fi. A process with a foreground service keeps network access in Doze.
- **Record and Glean from the lock screen:** the action's `PendingIntent` reaches the service, which runs the iPhone's checks, notifications and spoken sentences, and the 15 s wait.
- **The app swiped away from Recents during a conversation:** a foreground service keeps the process running after the task is removed, so the service handles `onTaskRemoved` and ends the conversation the way leaving the Voice screen does on the iPhone (§3.10), which is also what closing the iPhone app does: the recorder stops, a recording with no chunk yet is discarded, queued chunks go to `UploadWorker`, nothing is finalized, the service leaves the foreground and stops. The next Voice screen offers "Unfinished Voice recording" (Continue / Discard), as on the iPhone.
- **The process ended:** the notification goes with the service. A `PendingIntent` that arrives with no conversation posts the iPhone's "Loore was closed in the meantime…" notification.
- **Listen-aloud outside Voice:** the same service enters the foreground with the `mediaPlayback` type only, when playback starts from a tap; a media notification with the mini-player's controls; it leaves the foreground when the queue is closed.
- **Foreground reconcile** as on the iPhone (`ON_START`).
- **Battery:** the app does not ask to be exempted from battery optimisation (Google Play restricts that permission). Some manufacturers stop background apps more aggressively; the device checklist (§7) includes a Samsung phone.
- **Prove first:** the device spike after M1 (§9) shows a locked recording with Stop and Record from the lock screen; M7 then checks on a phone with the newest Android and one with the minimum version: a locked Voice turn whose reply plays by itself; Record from the lock screen after the reply (also after a phone call); Pause, Resume, Stop locked; an hour-long recording locked; the 30-minute idle end.

**Differences:** notification instead of Live Activity (platform); the cue is not needed to stay alive, so its Off warning goes (§3.16); the conversation's notification ends after 30 idle minutes in the background (Android's and Google Play's rules expect a foreground service to stop once its task is done; on the iPhone the system removes a Live Activity after some hours).

### 3.19 Local notifications

**iPhone** (`Audio/AudioCenter.swift:684-707`, `Audio/Voice/VoiceTurnDependencies.swift:146-177`): shown at once, default sound, a fixed identifier that replaces an earlier copy; permission (alert and sound) asked at the first record tap, before recording; no banner while the app is in front; a tap opens the app.

| Identifier `org.loore.voice.…` | Title / body | Posted / withdrawn |
|---|---|---|
| `recording-paused` | "Recording paused — tap to resume" / "The microphone stopped (a call, or another app or device took it). Everything up to the interruption is saved." | any hold (Voice, dictation); withdrawn on resume, stop, cancel |
| `long-recording` | "You've been recording for 59 minutes" / "One recording is turned into text up to two hours; for longer, continue in a new recording." | at 59:00 |
| `resume-failed` | "The recording could not resume" / "The microphone did not restart. Open Loore and press Resume; everything up to the pause is saved." | Resume failed; withdrawn when a resume works |
| `record-failed` | "Loore could not start recording" / the reason, else "Open Loore and try again." | lock-screen Record refused or failed; a tap on a closed app's card |
| `glean-failed` | "Loore could not start the glean" / the reason | lock-screen Glean refused or failed |

**Android**
- Two channels: "Voice conversation" (the service's ongoing notification; low importance, silent) and "Recording alerts" (these five; high importance). To match the iPhone, an alert is silent while Loore is in front (posted with `setSilent(true)`: no sound, no banner) and shows as a banner with sound while another app is in front or the phone is locked. Channel names are user-visible; final wording in M7.
- `POST_NOTIFICATIONS` is asked at the first record tap, as on the iPhone. If it is refused, the service still runs and the lock-screen controls fall back to the media session (§3.18).
- A tap opens the app on the screen it was on.

### 3.20 Mini-player, listen-aloud and audio download

**Mini-player** (`Features/Voice/MiniPlayerView.swift`): shown when a queue is loaded, the Voice screen is not visible, and the queue is not a Voice reply. A card above the tab bar (at most 480 wide) with the title (or "Audio"), a chapter menu when there are several chapters, ✕ (close), play/pause (at the end: replay), Stop, −10, +10, the rate button "1x / 1.25x / 1.5x / 2x", "m:ss / m:ss" with a dot while audio is generated, and a tap-to-seek bar 5 pt high. Toasts sit above it. At large text sizes the controls take two rows.

**Listen-aloud** (`Features/Voice/NodeAudioButtons.swift:7-62`, `Audio/Playback/ListenAloud.swift`), on thread nodes, profiles and saved references:
- The speaker shows when Voice mode is on, the node is public, or it is the user's own recording; it is dimmed and disabled when the node's or profile's AI usage is not chat or train (references are never disabled); a spinner while loading; accent colour while it plays; a tap while it plays starts again from the beginning.
- Order: an in-memory cache by (target, content); `GET {base}/audio` → `{original_url, tts_url, has_audio_chunks}`; for nodes with chunks (or no URL) `GET /api/nodes/<id>/audio-chunks` → a queue with the server's durations (any WebM chunk → `audio-download?format=mp3`, 300 s timeout); a WebM original → the same MP3; an original or TTS file → one item with chapters from `GET {base}/tts-chapters`, and the approval announcement appended for node TTS; nothing yet and Voice mode on → `POST {base}/tts`, then the TTS stream for nodes, profiles or items: the first chunk plays, later chunks are appended, a new `section_index` fetches chapters again, `all_complete` ends generation.
- Failures: nothing arrived → error sound and "Loore couldn't read this aloud. Tap the speaker to try again." (6 s); part arrived → after that audio, "Loore couldn't read the rest aloud. Tap the speaker to try again from the start."

**Download** (`NodeAudioButtons.swift:67-115`, `ListenAloud.swift:292-339`), thread nodes only, disabled when AI usage is None: `GET /api/nodes/<id>/audio` → the original as `node-<id>-recording.<ext>`, else chunks as `node-<id>-recording.mp3` (`audio-download?format=mp3`), else TTS as `node-<id>-tts.mp3`; saved to a temporary file and offered in the share sheet.

**Android:** the same, with `ACTION_SEND` and a `FileProvider` URI for the share (§3.31).

### 3.21 Threads

**Files:** `Features/Thread/ThreadView.swift`, `ThreadModel.swift`, `Bubble.swift`, `ThreadSheets.swift`, `ModelPicker.swift`, `ToolApprovalDialog.swift`, `NodePrefetch.swift`; `Core/Models/Nodes.swift`, `NodeTreeDecoding.swift`.

- **Loading:** the NodePrefetch hand-off, else `GET /api/nodes/<id>`. 404 or 403 → "This entry isn't shared with you. It may be private, or it may have been deleted." (#704); other errors → "Error fetching node details." The page reloads when the user returns to it; leaving it stops polling and streaming. Quotes: `GET /api/nodes/<id>/resolve-quotes`, again only when the set of quote markers changes. On first load with ancestors, the page scrolls to the focal node after 600 ms.
- **Decoding:** the node payload contains the whole reply tree. The iPhone cuts each `children` array out with an iterative byte scan, decodes flat lists and rebuilds the tree, because deep nesting breaks recursive JSON parsing (`NodeTreeDecoding.swift`). Android must also build the tree without recursion over `children` (parse to `JsonElement`, whose parser handles deep nesting, then walk it with an explicit stack), tested with a fixture nested 3,000 levels deep.
- **Layout:** header "Thread" with, for the owner of a non-public thread, "Voice Mode" (`POST /api/voice/from-node/<id> {model}`) and, in craft mode with AI usage not None, the Auto-generate switch (dimmed and locked in shared threads: "Auto-generate is off in public and shared threads."); ancestors as cards; the approvals line "N changes wait for your approval · Review"; the focal card (system-prompt line "<title> vN · Profile vN · TODO vN", the gleaning head, window line and cancelled line, the content, owner checklists, the proposal card, "▸ Actions taken (N)"); the footer (pin, speaker, download, "Only you see this reply"); the action rows ("LLM Response" with the model picker; "Glean" with its model picker; "Waiting for AI…" / "Generating…"); the thread-length notice; the inline reply form; the reply tree (flattened depth first; a level is indented only when it has more than one sibling: 14 pt + 2 pt rule + 8 pt; tombstones "[Node deleted]", stubs "[Node inaccessible]"). Cards expand when the title is over 120 characters, the body over 250 characters or over 2 lines.
- **Actions:** the kebab on ancestors and replies offers Reply to everyone and Edit, Delete and "Glean for this reflection" to the owner (an AI reply's owner is the person who asked for it); the focal kebab (owner) Edit, Delete, Glean. There is no Copy item (text selection, §3.24). **Reply** opens a sheet; on success the new node opens. **Edit:** a node carrying a prompt asks "Edit prompt for this thread only?" first; "Edit Text" sends `PUT /api/nodes/<id>`; toasts "Applied to N repl(y|ies) too" and the Train notice; an edited user node then goes through auto-generate. **Delete:** "Delete node?" (with replies: "Delete this node only" / "Delete this node and all my replies"); under a system-prompt root `GET …/delete-impact` may turn it into "Delete the system prompt too?"; `DELETE /api/nodes/<id>?delete_descendants=&delete_orphaned_prompt=`; toast "Deleted N node(s)"; then reload, the nearest live ancestor, or Log/Commons. **Pin** (owner, not private): `POST`/`DELETE …/pin`. **Checklists** (owner): toggle or "+" edits the content and sends `PUT` at once; on failure it reverts with "Couldn't save change — reverted (<reason>)".

**Android:** Compose `LazyColumn` for the flattened tree; kebabs as `DropdownMenu` (Delete in the error colour); sheets as `ModalBottomSheet`; dialogs as the design system's dialog card; `LazyListState.animateScrollToItem` for the focal scroll.

### 3.22 Writing and drafts

**Files:** `Features/Write/NodeFormView.swift`, `NodeFormModel.swift`, `DraftAutosaver.swift`, `PrivacySelector.swift`, `WritingDialogs.swift`, `TextModeView.swift`.

- **Initial values:** privacy = the edited node's, else `loore_last_privacy_level` (new top-level entries), else the account default, else private; AI usage = the edited node's, else the account default (Text mode), else `loore_last_ai_usage`, else the account default, else None; `loore_agentic_reply` and `loore_auto_generate` default to true. A reply takes its parent's privacy and `reply_ai_usage ?? ai_usage` (`GET /api/nodes/<parent>`). Under a public parent, privacy is locked: "Public — inherited from the thread." Shared privacy locks Auto-generate off with its explanation.
- **Selectors** (craft mode only): privacy Private / Circles (coming soon) / Public; AI usage None / Chat / Train, each with a one-line description; Train is not offered when `train_refused` (#689: "Posts fetched from X can't be used for training, under X's rules for developers.").
- **Editor:** grows from 3 to 12 lines (compact) or 6 to 18; status lines "Transcribing recovered audio...", "You're offline", the error, "Saving..." / "Draft saved <just now | Nm ago | Nh ago>". Buttons: Send (labels "Uploading... N%", "Transcribing... N%", "Waiting to transcribe...", "Sending..."; disabled while loading, offline or recording), Discard draft (no confirmation), the dictation microphone, Upload (craft mode; refused up front when spend-capped).
- **Server drafts:** one per (user, `node_id`) for edits or (user, `parent_id`) for new entries; `GET /api/drafts/?node_id|parent_id` (404 = none), `POST /api/drafts/ {content, node_id?, parent_id?}`, `DELETE /api/drafts/?…`. Saved 1 s after typing stops and every 3 s (which also retries a failed save); one save at a time; only non-blank text is saved; saved when the form leaves the screen and when the app goes to the background (#425: delete drops the pending text, cancels the debounce and waits for a save in flight before `DELETE`).
- **Send, in order** (`NodeFormModel.swift:405-572`): empty → "Content is required."; an edit of a node with generated audio whose text changed → "Regenerate audio?"; an edit with replies whose privacy or AI usage changed → "Apply to replies too?"; over 100,000 characters (counted in UTF-16 code units) → an error for edits and typed Text-mode or Glean replies, else "Long entry" with "Split into ~N connected entries" (N = max(2, ⌈length / 100,000⌉)); a paste that crosses the limit opens the same; a reply under a public parent → "This reply will be public" with "Don't show this again" (`loore_public_reply_ack`). Then: the screen's own submit (Text mode, Glean replies); agentic Write New Entry → `POST /api/textmode/start`; a dictated transcript → `save-as-node`; an edit → `PUT /api/nodes/<id> {content, privacy_level, ai_usage, detach_prompt?, regenerate_tts?, apply_to_descendants?}`; an audio file (at most 200 MB; webm, wav, m4a, mp3, mp4, mpeg, mpga, ogg, oga, flac, aac) → up to 10 MB `POST /api/nodes/` multipart `audio_file` (300 s), larger → `upload/init`, 5 MB chunks with 3 tries (1 s, 2 s backoff, 120 s), `upload/finalize`, `upload/cleanup` on error, then `transcription-status` every 2 s; a plain entry → `POST /api/nodes/` and `nodeCreated` (Write New Entry with Auto-generate also asks for a reply). 409 `quotes_not_public` → "Not public yet" with one row per quoted entry.

**Android:** `TextField` with min/max lines; the file picker is the Storage Access Framework (`ActivityResultContracts.OpenDocument`); the picked file is copied to the app's cache before its size is checked and it is uploaded, because a file from a cloud provider may be read again during the upload and fail (seen with Google Drive files in Chrome on Android, `docs/DECISION-LOG.md` 2026-10-16). The draft saved when the app goes to the background runs from `ON_STOP` in an application-scoped coroutine.

### 3.23 AI replies, proposals, tool approvals, long and shared threads

**AI replies** (`ThreadModel.swift:395-702`)
- **LLM Response:** `POST /api/nodes/<id>/llm {model, source_mode:"textmode"}`; the reply's page opens while it is pending. Shown to the owner when craft mode is on or the thread is shared, auto-generate is not active there, AI usage is not None, no reply is pending and the thread is below its limit. Errors: spend cap → the banner only; 409 `auto_generate_off` → nothing; 409 `thread_too_long` → the form's limit state; otherwise the server's words or "Error requesting LLM response." (8 s).
- **Watching a reply:** `GET …/llm-status` every 2 s for up to 30 min; when `stage == "batch"`, every 15 s for up to 25 h. Meanwhile `GET /api/sse/nodes/<id>/llm-stream` (not for batch): `snapshot` replaces the text, `delta` appends; polling decides. Partial text without quote markers and `:::share` fence lines, with pulsing dots; no text yet → "Thinking" / "Gleaning" / "Processing".
- **Finishing:** `completed` with `continuation_node_id` → that node opens; on this page → content, actions, picks count and thread length updated in place, quotes refreshed, approvals asked for (the dialog opens by itself); `cancelled` → toast; `failed` → toast and back to the parent (unless `thread_too_long`).
- **Auto-generate** (`loore_auto_generate`, default true; forced off in shared threads): after a user entry, `POST …/llm {model, source_mode, auto_generate:true}`; if any node in the chain has AI usage other than chat or train, the preference turns off with "Turning off auto-generate. AI usage on some nodes is turned off."
- **Model picker** (`ModelPicker.swift`): `GET /api/nodes/models` once per session; `GET /api/nodes/<id>/suggested-model[?purpose=read]` or `GET /api/nodes/default-model`; featured models first, then "More models…" by provider; Glean's picker lists models with `read: true`.

**Proposals** (`Features/Proposals/ProposalParser.swift`, `ProposalCard.swift`): the parser is a port of the web's `ProposalInline.js` with all its jest cases. Sections: completed, new tasks, priority, note, issue title, description, category, feedback, share. Accept buttons (owner only): "Apply changes to my Todo" → `POST /api/todo/apply-draft {llm_node_id}` then llm-status every 2 s until `propose_todo.apply_status` ends; "Create issue" → `POST /api/github/create-issue`; "Send feedback" → `POST /api/feedback/submit`; "Save to shares" → `POST /api/share/save-proposal {node_id, share_index}`. No reject button.

**Tool approvals** (`Features/Thread/ToolApprovalDialog.swift`, `Core/Models/ToolApprovals.swift`): from `pending_approvals`, `GET /api/tool-approvals?node=<id>` and each decision's answer; grouped by reply, oldest first; the dialog "Approve changes?" opens by itself 0.6 s after the first load or after a reply finishes; per change its title, "Based on vN", a stale note, Reject and Approve, then the line diff or the proposal text; "Later" decides nothing. `POST /api/tool-approvals/<id>/approve|reject`.

**Long threads** (#648, `Core/Models/ThreadLength.swift`): `thread_length` (owner only) is `notice` ("This thread is getting long…" with "Start a new thread") or `limit` (the server's text, "Start a new thread" opens Text mode; LLM Response, Glean and automatic replies are hidden; writing still works). No prices, token counts or percentages.

**Shared threads** (`Core/Models/SharedThread.swift`): a thread is shared when its privacy is not private; LLM Response shows there to the owner without craft mode; the owner's private AI reply under a shared parent shows "Only you see this reply"; a reply's privacy changes through Edit (409 `reply_in_progress` shows the server's words).

**Android:** the same.

### 3.24 Markdown, links, images and selection

**Files:** `Markdown/ContentSegments.swift`, `MarkdownModel.swift`, `MarkdownView.swift`, `MarkdownFlow.swift`, `SelectableText.swift`, `QuotedContentView.swift`, `NodeLinks.swift`, `MarkdownEdits.swift`, `MarkdownTargets.swift`, `MarkdownLinkedImages.swift`, `JSRegex.swift`.

- **Pipeline:** guidance markers `{share_guidance}` and `{external_content_guidance}` replaced or removed; the content split on `{quote:N}`, `{quote_ext:N}` and `{user_profile|todo|recent_raw|recent|ai_preferences|memory|scratchpad|intentions}`, each text piece its own markdown document; the clipper's linked images repaired; parsed with smart punctuation off; render trees cached (256 entries).
- **Rendering:** GFM tables (scroll sideways), strikethrough, task lists, autolinks (own scanner with GFM's trailing-punctuation rules), headings (2.2 / 1.8 / 1.5 / 1.25 / 1.1 / 0.95 em), block quotes in italics, nested lists, code blocks with a Copy button, rules. Raw HTML shows as code; HTML comments are dropped; no footnotes. **A single newline is a line break** (as the web's remark-breaks), except inside tight list items. Block identities are structural paths, so a re-render keeps rows in place (#321).
- **Markers:** `{quote:N}` → "Loading quote…", then a card "Quoted from @user" (250 characters, opens the node), or "[Quoted node deleted]" / "[Quoted node inaccessible]". `{quote_ext:N}` → a saved-reference card (byline, 500 characters, "Twitter ↗" / "Open original ↗" which also marks it read for the owner, ⊕/⊖, "Mark as read"; a gleaning pick shows the verdict pair of §3.26; the card opens the reference page for the owner). `{user_*}` in system prompts → a collapsible card "User Profile v3" etc.
- **Node links:** `/node/<digits>` relative or on Loore's hosts; a bare link shows the node's title from `GET /api/nodes/titles?ids=` (requests in the same turn of the main loop share one call, at most 50 ids each; answers cached for the session); deleted or inaccessible targets show muted text.
- **Checklists** (owner): a toggle matches every `- [ ]` line whose label, stripped of inline markdown, equals the item's plain text; "+" inserts a sibling after the item and its children with the same indent and style. The renderer's plain text must equal the stripped label, or a toggle does nothing.
- **Images:** only Loore's own `/media/…` paths on a Loore host load by themselves; any other image is a placeholder "Image from <host>: <alt>" that loads when tapped.
- **Links:** every link goes through the router: node links open the thread, Loore paths their screen, http(s) the in-app browser, `mailto:` the mail app; other schemes render as plain text.
- **Selection** (#624): runs of paragraphs, headings and lists are one selectable text, so a selection can cross paragraphs; Copy gives plain text (list items as "•\t"); a long press on a link offers "Open Link" and "Copy Link"; on a tappable card a short tap still opens the card; one selection on screen at a time; short rows in long lists use a whole-row Copy.

**Android**
- `:core` holds the marker split, the GFM parse (commonmark-java with `ext-gfm-tables`, `ext-gfm-strikethrough`, `ext-task-list-items`), the autolink scanner (port, not commonmark's autolink extension, so its tests match), checklist edits, link and image classification, and the render tree. Kotlin strings are UTF-16 like JavaScript's, so lengths and cuts match the web's without the iPhone's `jsLength` helpers; JavaScript regex semantics (`\s`, `.`, `$`, ASCII `\w`) still need care.
- `:app` renders the tree in Compose: text runs as `AnnotatedString` with `LinkAnnotation`s inside a `SelectionContainer`; tables in a horizontal scroll; images through Coil on the app's `OkHttpClient` (so `/media/…` requests carry the session cookies).
- Port the iPhone's markdown tests (`MarkdownTests`, `MarkdownSafetyTests`, `MarkdownLinkedImagesTests`, `SelectableTextTests` as far as they test text, `AppTests` link routing).

### 3.25 Log and Search

**Log** (`Features/Log/LogView.swift`, `Features/Thread/Bubble.swift`): header "Log" with a magnifier (Search with scope archive); states "Loading log...", "Error loading log.", the empty text "Your private entries will appear here as you share thoughts with Loore.", "Loading more...", "Couldn't load more entries." with Retry. `GET /api/log?per_page=20[&cursor=]`; the next page loads when the last card appears; ids already shown are dropped. Pull to refresh; reloads on `nodeCreated` and `logChanged`. A card opens `newest_node_id ?? id`. Card: title = first line without `# ` or `thread_name` (120 characters), body 2 lines (250 characters), quote markers removed; footer author, "via <origin>", date, reply count; tags "Pinned", the prompt tag ("Glean" for `read`/`read_thread`), "Voice Note". Kebab: "Rename thread" (`PUT /api/nodes/<root>/thread-name`, 120 characters, empty clears) and "Delete thread" ("Delete entire thread?", `DELETE /api/nodes/<root>?delete_descendants=true`).

**Search** (`Features/Search/SearchView.swift`): a sheet from the Log or References magnifier; 300 ms debounce (every semantic search costs money); account AI usage None → "Search needs AI" with "Account settings" for non-admins, keyword only for admins (#684); admins otherwise get a Semantic / Keyword switch. `GET /api/search/semantic` or `GET /api/search` with `q`, `from`, `to` (yyyy-mm-dd), `per_page=20`, `page`, `scope=external`; semantic is one page with a score; keyword has "Show more (x of y)". Snippets: tags removed, `<mark>` runs highlighted, `<br>` as a newline, 2 lines. A result opens the thread (NodePrefetch) or the reference. With a hardware keyboard, ⌘K opens Search from the Log and References.

**Android:** `LazyColumn` with `PullToRefreshBox`; ctrl+K with a hardware keyboard; the same requests and texts.

### 3.26 Glean and References

The product name is Glean; the code and routes say "read" and "feed picks".

**Who sees it:** `user.glean_enabled` (the rollout gate and the user's own switch). Without it there are no Glean cards, buttons or menu entries; existing gleanings still render.

**Starting a glean:** from the Home Glean card (Text or Voice with `entry:"glean"`; the thread gets `glean_thread: true`); the "Glean" button with its model picker on every turn of a Glean-card thread and on the Glean entry (not when AI usage is None, a reply is pending, or the thread is at its limit); "Glean for this reflection" in the kebab on the user's own entries in other threads. `POST /api/read/from-node/<target> {model?}` (a failed reply is not read: the target is its parent); opens `llm_node_id ?? prompt_node_id`; errors → the server's words or "Could not start the glean." (6 s).

**A gleaning's page** (`Features/References/ReadReplyViews.swift`): "Today's gleanings", "N tweet(s) from today, chosen for what you said."; empty day "Nothing worth your time today." with the count line; failed "This gleaning didn't come through." with the reason and a hint; the window line "Tweets from X to Y (your time): N by M accounts." with already-read and stale-export notes; the cancelled line; "N unread" / "All read." with "Mark all as read" (`POST /api/nodes/<id>/feed-picks/read`). Older replies with `feed_picks_count` and no markers use `GET /api/nodes/<id>/feed-picks`.

**Verdicts** (#502, `Features/References/PickVerdictView.swift`): one ⊕/⊖ pair ("Good pick" / "Bad pick") sets both parts; with any verdict it opens into "The intro ⊕⊖  The tweet ⊕⊖"; a lit glyph clears its part. `POST /api/nodes/<id>/feed-picks/<item>/verdict {intro?, tweet?}`. Error: "Could not save your verdict."

**References** (`ReferencesView.swift`, `ReferenceDetailView.swift`, `Embeds.swift`, `ReferenceUtils.swift`), under More: list `GET /api/external/items?sort=saved&page=N&per_page=20` (Log-style cards with a "Read · <source>" tag; kebab Open source / Delete; Search with scope external). Detail: `GET /api/external/items/<id>`, then `POST …/read {via:"open", node_id?}`; one card with the source sentence and kebab (Open source / Edit / Delete), byline, title, the stored text in the serif reading face, a YouTube player for videos (privacy-enhanced embed; description behind "Show stored text"), Listen, the link out (marks read), ⊕/⊖, the read toggle; under it "Shown by Loore N× · last …" or "Not yet shown by Loore in a conversation". Edit: title up to 512 characters, text up to 100,000 (`PUT`). Read marks `POST`/`DELETE …/read`; verdict `POST …/feedback {feedback: good|bad|null, node_id?}`.

**Android:** the same; the YouTube player is a `WebView` with JavaScript on, no cookies kept, and navigation limited to the embed (any other navigation opens Custom Tabs).

### 3.27 Workspace: Profile, Todo, Artifacts, version history, Prompts

**Files:** `Features/Workspace/*`, `Features/Profile/*`, `Features/Todo/*`, `Features/Artifacts/*`, `Features/Prompts/PromptsView.swift`, `Core/Util/LineDiff.swift`, `Core/Models/Workspace.swift`.

- **Bubble row:** Profile, Intentions, Todo, the built-in kinds (predictions, memory, scratchpad, ai_preferences), custom kinds by title, "+"; the document switches in place. A long press (#603) offers Edit on every bubble and Delete where the list says `deletable` ("Delete artifact?", `DELETE /api/artifacts/<kind>`, a 404 with `deleted:true` counts as done, the next bubble opens, toast "<title> deleted"). A link to a deleted kind shows "This artifact was deleted." List: `GET /api/artifacts/` (`{artifacts, deleted_kinds}`).
- **Profile:** `GET /api/dashboard/` → `latest_profile`; meta line "Built from ~N tokens of writing (96% public tweets) · model · Data through …"; version chip from `GET /api/profile/versions`; save `PUT /api/profile/<base id>` (or `POST /api/profile/` for the first), with "Save mine as a new version" when a newer version arrived while editing. The app-wide generation watcher polls `GET /api/export/profile-progress?task_id=` every 60 s (batch) or 5 s, stops after 10 errors in a row; toasts "Your profile has been updated ✓", "Profile generation failed".
- **Todo:** sections from `##` headings, nested items collapsed; tick, row "+", quick add to "Today" (created when missing), raw editor, history. Every in-place edit: `GET /api/todo/` → `PATCH {content, base_revision}`; a 409 `todo_changed` brings the newest list and the edit is applied to it once more. Editor Save: `PUT {content, generated_by:"user", base_revision}`; on conflict "Your todo list changed after you opened the editor" (Save mine anyway / Show the newest list / Keep editing). Reloads on `todoChanged` and when the app returns to the foreground.
- **Artifacts:** built-in, custom and unknown kinds; create form (kind `^[a-z0-9][a-z0-9_-]{0,47}$`, description required); `PUT /api/artifacts/<kind> {content, description, generated_by:"user"}`; unsaved-changes guard; `POST …/viewed` without waiting for it; the Intentions view (`IntentionsParser`).
- **Version history:** a sheet, list then detail; "Changes (n)" or "Full text" against the next older version; unchanged runs folded with 2 lines of context; word-level changes when lines are at least 40 % alike; past 1,500 changed lines the middle shows as one removed and one added block; a rewrite of more than half the lines and over 40 lines opens in Full text; "Revert" → `POST …/revert/<id>`. Prompts show the file default as v0.
- **Prompts** (craft mode): `GET /api/prompts/`, rows with a "default updated" dot; detail `GET`/`PUT /api/prompts/<key>`, the default-updated banner (`GET …/default`, `POST …/revert-to-default`, `POST …/acknowledge-default`), history.

**Android:** the same; `LineDiff` and `IntentionsParser` ported with their tests (ports of the web's `diff.test.js` and `intentions.test.js`).

### 3.28 Account, settings, More, theme and craft mode

**Account** (`Features/Account/AccountView.swift`, `AccountModel.swift`): scroll anchors `email`, `x`, `model`, `references`, `ai-usage`, `tweets`, `craft`, `delete-account` (scrolled to 0.3 s after the page appears).

| Row | Control | Request |
|---|---|---|
| Username | field + Save (not empty, at most 64, `^[a-zA-Z0-9_]+$`) | `PUT /api/dashboard/user {username}` |
| Email | field + "Send confirmation link"; pending notice with Resend / Cancel / "Paste the link"; "Remove email" only when X also signs in | `POST /api/dashboard/email`, `DELETE /api/dashboard/email/pending`, `DELETE /api/dashboard/email`, `POST /api/dashboard/email/confirm` |
| X | "Connect X" (§3.5) or "Connected as @h"; "Disconnect X" only when an email also signs in | `DELETE /api/dashboard/x` |
| Subscription / Plan | text (§3.8) | — |
| Default model | model picker | `PUT … {preferred_model}` |
| Default privacy | private / public | `PUT … {default_privacy_level}` |
| External references | Off / "On (experimental)", if available | `PUT …` |
| Public sharing | Off / "On (experimental)", if available | `PUT …` |
| Default AI usage | none / chat / train | `PUT … {default_ai_usage}` |
| Start from your tweets | Yes / No (nothing selected until answered) | `PUT … {prefill_consent}` |
| Craft mode | Off / On | `PUT … {craft_mode}` |
| AI Preferences | "View" | opens the artifact |
| Voice | "Sound while Loore thinks": Soft / Very soft / Off | per device (§3.16) |
| Delete my account | below | |
| Version | "Loore 0.1.0 (1)" (+ " · <Env>" in debug) | debug switcher |

**Account deletion** (#269, `Features/Account/DeleteAccount.swift`, `AccountDeletionScreens.swift`): offered when `user.account_deletion` is present (a `refusal.message` replaces the button); the dialog explains what goes and when (today + `grace_days`, default 30; username reserved `username_reserve_days`, default 365); the user types the username to enable "Delete my account" (or "Email me the confirmation link" when `confirm_by_email`); `POST /api/account/delete {confirm}` → `scheduled` (signed out, "Your account is deleted … until <date>") or `confirm_email` (paste the link; `POST /api/account/delete/confirm {token}`). Restore after a sign-in that lands on `/account-restore`: `GET /api/account/restore`, "Restore my account" (`POST`) or "Keep it deleted" (`POST …/decline`).

**More** (`Features/More/MoreView.swift`), in order: Import data; Admin (admins), Account, My public page (with `share_v1_enabled`), References; Light mode, Craft mode; with craft mode Write new entry, Export data, Prompts; About (Why Loore, Vision, How To); Logout. Turning craft mode on asks first ("Turn on" / "Not now"), then "Craft mode on. Its controls carry the sliders icon." and a 2.4 s glow on the craft rows (1.2 s with reduced motion); off → "Craft mode off.". The local value changes at once and is saved to the server.

**Theme** (`DesignSystem/Theme.swift`): `loore_theme` = `light` or `dark`; absent → follow the system; once chosen, the app no longer follows the system; the "Light mode" switch flips what shows now.

**Per-device preferences** (iPhone `UserDefaults`, Android DataStore, same keys): `loore_theme`, `loore_craft_mode`, `loore_auto_generate`, `loore_agentic_reply`, `loore_last_privacy_level`, `loore_last_ai_usage`, `loore_public_reply_ack`, `home.reflectMode`, `home.gleanMode`, `loore_thinking_cue`, `loore_recording_device_key`, and `loore.debug.*` in debug builds.

**Android:** the same rows, texts and requests.

### 3.29 Onboarding screens

**Files:** `Features/Onboarding/*`.

- **Terms** (`TermsView.swift`, `TermsText.swift`): a blocking card over a darkened background, no close; the text (version 2.0) is copied verbatim from the web's `TermsModal.js` and checked by `ios/scripts/check_terms_text.py`; "I Agree" → `POST /api/terms/accept` ("Error accepting terms. Please try again."); then updates and the landing page.
- **Waitlist** (`WaitlistView.swift`): "You're part of this now."; "Get notified" email form (`POST /api/dashboard/email`), the pending card (send again, another address, paste the link), the prefill consent card, "What happens next", "Read the vision →", account deletion, a ⋯ menu with About and Logout.
- **Starting point** (#698): while `starting_point.status` is checking or preparing, `GET /api/dashboard/starting-point` every 3 s; ready and approved → reload the user and open Profile; failed → "Your starting point isn't ready yet." with its explanation.
- **Welcome** (`/welcome`): "Your first entry" (Write New Entry), the prefill consent card ("Already on X?"), "Import data", "See practical tips & workflows →", closing lines.
- **Confirm email** (`ConfirmEmailView.swift`): paste sheet or the landing page; `POST /api/dashboard/email/confirm {token}`; "Email confirmed" or "Not confirmed" with the server's text; `other_account` offers to sign out.
- **No subscription** (`NoSubscriptionView.swift`): "Your account has no active subscription yet.", the prefill consent card, Sign out, "Download your data" (export), account deletion. No price, no link, no way to buy.
- **Account not active** (`AccountNotActiveView.swift`): "This account is not active. Write to info@loore.org.", "Download your data", Sign out, account deletion.

**Android:** the same; the Terms text is a Kotlin string checked against `TermsModal.js` by a script like `ios/scripts/check_terms_text.py` (§8).

### 3.30 Updates sheet

**iPhone** (`Features/Updates/UpdatesSheet.swift`, `Core/Models/Updates.swift`): "While you were away"; notifications, then polls, then changelog; closes when nothing is left; half height for a single notification. Notifications (`fix_ready`, `issue_declined`, `x_disconnected`, `profile_ready`): "Later" → `POST /api/updates/notifications/<id>/skip`, "Got it" → `…/read`, "Take a look" → skip and open the link (http(s) in the system browser; otherwise close, wait 0.35 s, open the route). Changelog: markdown body, Later / Got it (`/api/updates/changelog/<id>/skip|read`). Polls: "No thanks" (`…/decline`), "Later" (local), "Write my own", "Draft with AI" (`POST …/draft`, then `GET /api/updates/polls/<id>` every 5 s while drafting), "Send to developer" (`PUT …/response`, `POST …/send`). Later, Got it and No thanks do not wait for an answer.

**Android:** the same, in a `ModalBottomSheet`.

### 3.31 Import and export

**Import** (`Features/Import/ImportView.swift`, `ImportDataSection.swift`, `ImportModel.swift`, `ImportFiles.swift`)
- Archives (Claude, ChatGPT, Markdown, Tweets): `.zip` only; at most 200 MB per upload ("This file is larger than 200 MB, the most Loore accepts in one upload."). Claude and ChatGPT: the app extracts `conversations.json` (or `conversations-N.json`; ChatGPT also `sectioned_conversations.json` and `group_chats.json`) by streaming the zip; fallback: the largest `.json` that starts with `[` and ends with `]`. Markdown and Tweets send the zip (`zip_file`); Claude and ChatGPT send `conversations_file`.
- `POST /api/import/{analyze | claude/analyze | chatgpt/analyze | twitter/analyze}` (multipart, 300 s); the confirm dialog (counts, "Include replies", separate entries or one thread, date ordering; **privacy fixed to private** with "Imported entries are private. You can share or publish the ones you choose afterwards."; AI usage from the account default); `POST …/confirm` (90 s); 409 `deleted_content_matches` → Restore / Keep it deleted / Cancel; 502, 504 or a timeout → "The import took longer than the connection allows. It may still finish on the server: check your Log in a few minutes…"; a `task_id` → `GET /api/import/status/<id>` every 1.5 s, giving up after 10 min without progress. Finished: counts, notes, the rebuild question (#625: "Rebuild my profile" → `POST /api/import/profile-rebuild {token}`).
- References: counts `GET /api/external/items?per_page=1`; Community Archive `POST /api/external/community-archive/fetch {username}` then counts every 5 s, 12 times; X status/sync (`GET …/twitter/status`, `POST …/twitter/sync`, then status every 3 s, 20 times); bookmarks JSON (craft) `POST /api/external/bookmarks/import`; clipper tokens `GET`/`POST {name:"Chrome clipper"}`/`DELETE /api/external/tokens/<id>` (shown once; tap to copy).

**Export** (More with craft mode; No subscription; Not active): `GET /api/export/threads` (`Accept: text/plain`, 120 s) to `loore-export-YYYY-MM-DD.txt`, then the share sheet; "Export failed. Please try again."

**Android**
- Picker: Storage Access Framework. The picked file is copied to the app's cache first (the reason is in §3.22), and the 200 MB check runs on the copy. Zips are read with `java.util.zip.ZipFile` on the copy, in 1 MB chunks, never whole into memory.
- Sharing out (export, audio download, dictation audio): files go to `cacheDir/share/` and are offered with `Intent.ACTION_SEND` through a `FileProvider` and the system chooser. Android does not report when the receiving app has finished reading, so these files are deleted at the next launch, at sign-out, and when a newer file of the same kind replaces them (on the iPhone they are deleted when the share sheet closes).
- Copying the clipper token marks the clip as sensitive (`ClipDescription.EXTRA_IS_SENSITIVE`), so Android's clipboard preview does not show it.

### 3.32 Share and Commons

Only for approved users with `share_v1_enabled`; otherwise "Not available."

- **Share** (`Features/Share/ShareView.swift`, `Core/Models/ShareCommons.swift`): `GET /api/share` grouped as Published, Drafts, Revoked; types need, offering, insight, exploration, intention, other; new `POST /api/share {content, share_type}`; edit `PATCH /api/share/<id>`; publish asks "Where should this go? …" with "Publish to Loore" (`POST …/publish`; 409 `quotes_not_public` lists the quoted entries; "Twitter / X · coming soon" and "Substack · coming soon" are labels only); revoke `POST …/revoke`; delete with an inline confirmation (`DELETE`); a published card opens its public thread. Nothing leaves without the user's explicit, previewed confirmation.
- **Commons** (`Features/Commons/CommonsView.swift`): `GET /api/commons/feed?page=N`; "Load more...", pull to refresh, "Nothing public yet." / "Error loading the commons."; a card shows "<n> response(s)" and opens the thread.

**Android:** the same.

### 3.33 Design system

**iPhone** (`DesignSystem/Tokens.swift`, `Typography.swift`, `Theme.swift`, `Components/*`; product rule in `docs/LOORE-ESSENCE.md`: nothing in the app counts or scores the user)
- Colours, dark / light: bgDeep `#0E0D0B` / `#F5EFE4`; surface `#181714` / `#EBE4D6`; card `#211F1B` / `#E0D8C7`; input `#151311` / `#FBF7EE`; text `#EDE8DD` / `#1F1C17`, `#A89F91` / `#5D564A`, `#736B5F` / `#857C6C`; accent `#C4956A` / `#A87547`; border `#302C27` / `#CDC4B1`; plus success, error, warning and info pairs (`Tokens.swift:28-82`).
- Fonts: Cormorant Garamond (variable) for headings and page titles; Outfit (static TTFs) for interface text, with a synthetic slant for italics; the system serif for reading text; a monospace font for code. Sizes are the web's rem × 16 (body 14.7, page title 32); everything scales with the user's text size.
- Spacing 4, 8, 12, 16, 24, 32, 48 (page gutter 24); radii 6, 8, 10, 12, 16. Motion: reveal 0.9 s on cubic-bezier(0.22, 1, 0.36, 1), fade-in rising 28 pt, quick 0.25 s; reduced motion respected.
- Components: buttons (outline, primary, filled, link, quiet), choice buttons, cards, page header, eyebrow, tag, pill, pill switch, spinner, text field style, dialog card, toasts (3 s by default, tap to dismiss, announced to screen readers), spend-cap banner, flow layout, glyphs (logo, craft icon, X logo).

**Android**
- The same tokens in a `LooreTheme` (`MaterialTheme` with Loore's colour scheme; no dynamic colour). Light / dark as on the iPhone (§3.28).
- The same font files in `res/font` (SIL OFL; licence files included). Compose's synthetic italic for Outfit. Reading text: the system serif (`FontFamily.Serif`), as the iPhone uses its system serif. Sizes in `sp`, so they follow the user's font size.
- Edge to edge (required for apps that target Android 15 and later): content respects the system bar and keyboard insets.
- Toasts are the design system's own component above the mini-player, not Android's system toasts, which cannot be styled or placed.
- Accessibility: content descriptions on every icon button (the iPhone's VoiceOver labels), toasts announced with a live region, the largest font scale checked.

---

## 4. Where Android differs from the iPhone, and why

| # | Area | iPhone | Android | Why |
|---|---|---|---|---|
| 1 | Lock-screen recording controls | Live Activity (iOS 18) with Pause, Resume, Stop, Record, Glean; Now Playing as fallback | the audio service's notification with the same phases, texts and actions | platform: Android's equivalent of a Live Activity is the ongoing notification of a foreground service |
| 2 | Staying alive during a Voice turn | the audio background mode; the thinking cue keeps the app running between Stop and the first reply audio | a foreground service (microphone and media playback) for the conversation, a partial wake lock while waiting | platform |
| 3 | Thinking cue setting "Off" | shows "Without it, iOS may pause Loore while you wait…" | no warning | the cue no longer keeps the app running on Android (row 2) |
| 4 | End of the conversation's lock-screen controls | the system removes the Live Activity after some hours, or when the user leaves Voice | the notification ends when the user leaves Voice, signs out, or after 30 idle minutes in the background | Android's and Google Play's rules for foreground services expect a service to stop once its task is done |
| 5 | Back | swipe from the edge pops; sheets swipe down | system Back (gesture or button), predictive back | platform |
| 6 | After the system ends the app in the background | starts at Home | the tab stacks come back and each screen reloads | platform convention |
| 7 | Sign-in links | always pasted | may open the app directly (App Links), paste kept | platform; needs a server file and Peter's yes (§10 item 3) |
| 8 | Sign-in screen footnote on one-time links | shown | left out | it no longer matches the server since #703 |
| 9 | Sharing files out | temporary file deleted when the share sheet closes | deleted at the next launch, at sign-out, or when replaced | Android does not report when the receiving app has finished |
| 10 | Picked files (import, audio upload) | read in place | copied to the app's cache first | cloud-provider files on Android can fail when read a second time |
| 11 | Bluetooth headset microphone | audio session mode and preferred input | `setCommunicationDevice` while recording, cleared at Stop | platform; same behaviour (#423) |
| 12 | Hardware keyboard shortcuts | ⌘↩ (send), ⌘K (search) | Ctrl+Enter, Ctrl+K | platform |
| 13 | Notification channels | — | "Voice conversation" and "Recording alerts" | platform (Android 8 and later require channels) |

Everything else (screens, texts, server calls and their order, limits, timers, refusals, sounds) is the same.

---

## 5. What the server needs

List only; this design changes no server code. None of these is needed before M7, and the app works without all but the first.

1. **App Links** (only if Peter agrees, §10 item 3): serve `https://loore.org/.well-known/assetlinks.json` (and on staging), `Content-Type: application/json`, with no redirect, listing the package name and the SHA-256 fingerprints of the signing certificates that may open the link: Google Play's app signing key, and the upload or debug key for builds installed by cable. Android verifies it at install time; without it, sign-in links keep opening in the browser and the paste path works as on the iPhone.
2. **Telling the Android app apart** (`backend/utils/client_platform.py`): the server knows `ios` and `web`, from the `X-Loore-Client` header or the User-Agent (`Mozilla/…` → web; `CFNetwork/… Darwin/…` → ios). Android's OkHttp sends neither, so its requests count as "unknown": nothing breaks, but GitHub issues filed from Android get no platform label. Needed: accept `android` in `CLIENTS`, add it to `PLATFORM_LABELS` in `backend/utils/github.py`, and create the `android` label in the repository. The app sends `X-Loore-Client: android` from M1. (WebViews inside the app send a `Mozilla/…` User-Agent and count as web, which is right for those pages.)
3. **Cookies and sessions:** nothing. The app handles the `session` and `remember_token` cookies the way the iPhone app does: taken from `Set-Cookie` (including the 302 of `/auth/magic-link/verify`, with redirects off), sent back on every request, the sign-in dropped after the cookie's expiry. Native requests send no `Origin`, so CORS does not apply. Local development sets `SESSION_COOKIE_SECURE=false`, which OkHttp's cookie handling respects over `http://`.
4. **Recording:** nothing. The Android app sends the same `audio/mp4` chunks with its own `device_key` (one per install), and the server joins any standard fragmented MP4.
5. **Media and speech:** nothing. ExoPlayer reads the container from the data. The spoken-refusal clips and the approval announcement are served as they are.
6. **Payments:** nothing; the Android app, like the iPhone app, offers no way to buy.
7. **Google Play's account-deletion rule:** Play asks apps that create accounts for a web address where a person can ask to delete their account. The web Account page's delete section (#269) is that place; if Play requires a page reachable without signing in, it is a short static page. To be checked when the Play listing is made (§10 item 1).

---

## 6. Project layout, SDK levels, dependencies

```
android/
  settings.gradle.kts
  build.gradle.kts
  gradle/libs.versions.toml      # every dependency pinned to an exact version
  gradle/wrapper/
  README.md                      # build, run, install on a phone, Play internal testing, debug extras, device checklist
  PROGRESS.md                    # milestone log, differences from the iPhone app, known gaps
  core/                          # Kotlin/JVM library: no Android imports
    src/main/kotlin/org/loore/core/
      model/                     # payloads, dates, open enums, tree decoding
      net/                       # ApiPath, ApiError, SSE parser, poller (OkHttp is a JVM library)
      auth/                      # MagicLink parsing, session record
      markdown/                  # marker split, GFM parse, autolinks, edits, link and image rules, render tree
      text/                      # proposal parser, intentions parser, line diff, spend-cap and date texts
      audio/                     # Fmp4Writer, SegmentPackager, ChunkQueueMath, recording limits, upload rules
      voice/                     # VoiceTurnController, VoiceBackend interface, spoken refusal codes
    src/test/kotlin/…            # the ported unit tests (fast, no emulator)
  app/                           # the Android application
    src/main/
      AndroidManifest.xml
      kotlin/org/loore/app/
        MainActivity.kt, LooreApplication.kt
        state/                   # AppState, environments, preferences (DataStore), SessionStore (Keystore)
        net/                     # OkHttpClient, cookie jar, interceptors, connectivity
        audio/                   # AudioService, AudioCenter, Recorder, AacEncoder, AudioRouting, ToneSynth,
                                 # ChunkQueuePlayer (ExoPlayer), ListenAloud, ChunkUploader, UploadWorker, DictationController
        designsystem/            # theme, tokens, typography, components
        navigation/              # tabs, back stacks, AppRoute handling, NodePrefetch
        feature/                 # home, voice, write, thread, log, search, glean, references, workspace,
                                 # account, more, onboarding, updates, importdata, share, commons, web
      res/                       # fonts, the icon, strings, network security config, file paths, data extraction rules
    src/debug/                   # cleartext config, debug extras
    src/test/                    # Robolectric tests for Android-bound code
    src/androidTest/             # Compose UI flows against the local backend (not in CI)
  scripts/check_terms_text.py    # Terms text == TermsModal.js (or a shared script with a target argument)
```

- **Two Gradle modules.** `:core` holds everything that can be tested on the JVM (the iPhone's unit tests mostly test such code), so the test suite runs in seconds without an emulator. `:app` holds everything that touches Android. Split `:app` further only if build times require it.
- **Package name** `org.loore.app` (the iPhone's bundle id; open for review in §11). Version 0.1.0, version code 1, raised for every upload.
- **minSdk 31** (Android 12; open for review in §11). The reasons: `AudioManager.setCommunicationDevice` (API 31) gives one way to use a Bluetooth headset's microphone (older versions need the deprecated `startBluetoothSco` path, a second code path to build and test on old devices); Android 12 introduced the rules for starting foreground services from the background, so the app is designed and tested against one set of rules; `isClientSilenced()` (API 29) and notification channels (API 26) are then always present.
- **compileSdk and targetSdk:** the newest stable API level when M1 starts; Google Play requires a recent target for every upload, so it moves with each yearly Android release.
- **Toolchain:** the newest stable Android Gradle Plugin and Kotlin at M1; JDK 17; Kotlin 2 with the Compose compiler plugin.
- **Dependencies:**
  - Kotlin standard library, kotlinx.coroutines (core, android), kotlinx.serialization (JSON).
  - AndroidX: core-ktx, activity-compose, lifecycle (runtime, viewmodel, process), navigation-compose, the Compose BOM (ui, foundation, material3), datastore-preferences, work-runtime-ktx, browser (Custom Tabs).
  - Media3: exoplayer, session, datasource-okhttp.
  - OkHttp.
  - commonmark-java with ext-gfm-tables, ext-gfm-strikethrough, ext-task-list-items.
  - Coil (images in markdown) with its OkHttp network module.
  - Tests: JUnit 4, kotlinx-coroutines-test, OkHttp MockWebServer, Robolectric, Compose ui-test-junit4, AndroidX test runner.
  - No Firebase, analytics, crash reporting or advertising SDKs. ZIP reading, the Keystore and text-to-speech come from the platform.
- **Signing:** debug builds use the debug key. Release builds are signed with an upload key that is never committed; its path and passwords are supplied locally at build time. Google Play re-signs with the app signing key it holds (Play App Signing).
- **`.gitignore`:** `android/.gradle/`, `android/**/build/`, `android/local.properties`, `*.jks`, `*.keystore`.

---

## 7. Testing

**Unit tests** mirror the iPhone's (`ios/LooreTests/`, about 860 test functions). Port each test file with the code it tests; where a test checks a port of a web test (`*.test.js`), port the cases one to one. JSON fixtures are read from `ios/LooreTests/Fixtures/` directly (a Gradle test-resources path), so both apps test against the same captured payloads.

| iPhone test file | Android test (module) |
|---|---|
| `AppTests` (routes, link parsing, tokens, Terms text) | `AppRouteTest`, `TermsTextTest` (core) |
| `AuthTests`, `SignInDestinationTests`, `InviteMismatchTests` | `MagicLinkTest` (core), `SessionStoreTest` (app, Robolectric) |
| `NetworkingTests`, `SSETests` | `ApiPathTest`, `ApiErrorTest`, `SseParserTest`, `PollerTest` (core, MockWebServer) |
| `DateTests`, `ModelDecodingTests` | `LooreDateTest`, `ModelDecodingTest` (+ the 3,000-level tree), `SpendCapTextTest` (core) |
| `MarkdownTests`, `MarkdownSafetyTests`, `MarkdownLinkedImagesTests`, `SelectableTextTests` | `MarkdownParserTest`, `MarkdownSafetyTest`, `LinkedImagesTest`, `ContentSegmentsTest`, `NodeLinksTest`, `MarkdownEditsTest` (core) |
| `ProposalParserTests`, `IntentionsParserTests`, `LineDiffTests`, `ReferenceUtilsTests`, `WorkspaceUtilsTests` | the same names (core) |
| `RecordingFormatTests`, `RecordingLimitsTests` | `Fmp4WriterTest` (boxes; chunk 0 = init + first segment; later chunks without `ftyp`; continuous `tfdt`; the server's init rule), `RecordingLimitsTest` (core) |
| `AudioQueueAndUploadTests` | `ChunkQueueMathTest`, `ChunkUploaderTest` (order, retries, answer table, degraded mode, `total_chunks`), `ToneSynthTest` (core) |
| `RecordingRouteTests`, `MicrophoneSourceTests` | `AudioRoutingTest` (app, Robolectric with fakes) |
| `VoiceTurnTests` (110 tests), `VoiceAIBlockTests`, `VoiceApprovalTests`, `VoiceLimitAndApprovalTests`, `VoiceRefusalSpeechTests`, `ListenFailureTests`, `SpeakerAIUsageTests` | the same names against `VoiceTurnController` with a fake `VoiceBackend` (core) |
| `WritingTests`, `NodePrefetchTests`, `ThreadLengthTests`, `SharedThreadTests`, `ToolApprovalTests` | the same names on the view models (app, Robolectric where needed) |
| `GleanTests`, `PickVerdictTests`, `HomeCardModeTests`, `SearchAccessTests` | the same names |
| `FeaturePagesTests`, `ImportPrivacyTests`, `ArtifactDeletionTests`, `AccountDeletionTests`, `AccountBlockedTests`, `SubscriptionTests`, `SpendCapUntilPaymentTests`, `XApiNoTrainTests` | the same names |

**UI flows** (`app/src/androidTest`, Compose UI tests on an emulator against the local backend, not in CI, like `ios/LooreUITests/`): sign-in with a pasted link; tabs; a thread; writing with drafts; Text mode send and discard (#425 repro, with `ios/scripts/delay_proxy.py`); node opening without a loading page (#447 repro with the delay proxy); artifacts delete menu; select and copy; the Home card switches; a Voice turn from the debug audio file (billed, run on purpose, as on the iPhone). Inputs through instrumentation arguments (`-e loore.magicLink …`), as the iPhone's `TEST_RUNNER_` variables. Same rules as the iPhone: test user 5 only on the local backend; billed steps only on purpose and with the cheapest model; delete what the test creates.

**Only a real phone can test** (the checklist goes into `android/README.md`, adapted from `ios/README.md` "Only a real iPhone can test"): a locked Voice turn whose reply plays by itself; Pause, Resume, Stop and Record from the lock screen; thinking cue levels; Bluetooth headphones (recording through the headset, the reply over A2DP, the headset switched off mid-recording); a phone call during a recording; another app taking the microphone; headphones unplugged during a reply; a 60-minute recording; uploads after the app is closed (airplane mode during a recording, then the recovery banner); the 30-minute idle end; Doze (a locked phone left still for an hour, then a Voice turn); dictation with the phone locked; listen-aloud controls on the lock screen. Devices: one phone on the newest Android and one on the minimum version, one of them a Samsung. Emoji, the largest font size and TalkBack on Home, Voice, a thread, Log, Profile.

**Staging checks**: the iPhone's list in `ios/README.md` "Check on staging" (streaming Voice TTS, Commons, a running profile build, Glean picks, real X sign-in, YouTube references, the default-updated prompt banner, Updates with a real changelog entry, spend cap).

---

## 8. CI

A new workflow `.github/workflows/android.yml`, independent of `ci.yml` and the deploy workflows, like `ios.yml`:

```yaml
name: Android
on:
  pull_request:
    paths:
      - "android/**"
      - ".github/workflows/android.yml"
      - "ios/LooreTests/Fixtures/**"          # shared JSON fixtures
      - "frontend/src/components/TermsModal.js"
  workflow_dispatch:
concurrency:
  group: android-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
permissions:
  contents: read
jobs:
  build-and-test:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4        # temurin, 17
      - uses: gradle/actions/setup-gradle@v4
      - run: python3 android/scripts/check_terms_text.py
      - working-directory: android
        run: ./gradlew --no-daemon :core:test :app:testDebugUnitTest :app:assembleDebug :app:lintDebug
      - if: failure()
        uses: actions/upload-artifact@v4   # test reports, 7 days
```

- No emulator in CI: unit tests run on the JVM (`:core`) and under Robolectric (`:app`); UI flows need the local backend, as on the iPhone.
- No signing in CI (debug build only).
- **`deploy.yml`:** add `android/**` and `.github/workflows/android.yml` to `paths-ignore`, next to `ios/**`, in the first commit that adds `android/` (or before it): nothing on the server reads `android/`, and a push that touches only the Android app must not restart the server's workers. Update the comment there and the "Deploy to Production" paragraph in `CLAUDE.md`.

---

## 9. Build order

Each milestone ends with: a clean build, unit tests green, the screens it added checked on an emulator against the local backend next to the iPhone app (or the web), `android/PROGRESS.md` updated (done, differences, known gaps, how to test), commits pushed. Milestones 1–4 need no microphone; the spike M1b is the exception, kept small so that the reason for the app is proven before the screens are built.

1. **M1 — Sign in and the Log.** Gradle project, CI workflow, and the `deploy.yml` `paths-ignore` change in the same first commit (§8); environments, chosen in debug builds by the `loore.environment` extra (the other debug extras and the switcher on Account come in M2); design tokens and fonts (dark and light), `ApiClient` with the cookie jar and error mapping, `ApiPath`, tolerant dates, models for the dashboard and the log, `SessionStore`, email-link sign-in by paste (send, paste, verify, error texts), launch with a stored sign-in, logout, a tab bar with placeholders, the Log (paging, pull to refresh). **Test:** unit tests for dates, models (`dashboard.json`, `log.json`), `ApiPath`, magic-link parsing, the session record; on the emulator: sign in as user 5 with `ios/scripts/local_backend.sh magic-link`, see the Log, close and reopen the app (still signed in), log out.
   **M1b — Device spike** (right after M1; small, with a throwaway screen). On a real phone against the local backend: a record tap starts `AudioService`; `AudioRecord` → `MediaCodec` → `Fmp4Writer` → chunks to the local backend → finalize → the transcript appears (the chunk contract of §3.11). Then with the screen off: a recording stopped from the lock screen, and Record from the lock screen starting the next one. **Test:** the transcript is complete and the server's joined file plays to its end; the locked Stop and Record work on the phone. What the spike finds goes into `android/PROGRESS.md` before M5 and M7 build on it.
2. **M2 — Shell, gates and reading a thread.** A thread read-only (ancestors, focal card, replies, with the node model and its iterative tree, `node_detail.json`) and the markdown basics (paragraphs, line breaks, headings, lists, emphasis, links, code); all tabs, `AppRoute` and link handling, NodePrefetch, the launch order and every gate (Terms, Waitlist with starting point, No subscription, Not active, restore offer, unreachable), Updates sheet, time zone, payments and spend-cap banners, toasts, More (theme, craft mode with its dialog, About in Custom Tabs, Admin web view), Account basics, Sign in with X and the cookie web views, the other debug extras and the switcher on Account. **Test:** `local_backend.sh state …` for each gate; route, prefetch and tree-decoding unit tests; open a thread from the Log.
3. **M3 — Threads and writing.** The full thread (actions, pin, delete, edit, checklists), the complete markdown renderer (markers, quotes, node titles, images, selection), writing with server drafts, selectors, model picker, the send decision tree, audio file upload, AI replies (request, SSE, polling, batch, chains, errors, auto-generate), proposals, tool approvals, long and shared threads, Text mode (typed), Write New Entry, Search. **Test:** the ported writing, markdown, proposal and approval tests; UI flows for writing, drafts and the #425/#447 repros.
4. **M4 — Glean and References.** Home cards with the Voice / Text switch (Text path), starting a glean, the gleaning page, verdicts, quote cards, References list and detail, YouTube embed. **Test:** `GleanTests`, `PickVerdictTests`, `HomeCardModeTests`; a glean on staging.
5. **M5 — Recording and dictation.** Builds on the spike (M1b). The recorder, encoder, writer, packager, the upload queue with WorkManager, limits and refusals, the recording log, dictation in the editor (transcription stream, Save audio), recovered audio in the form. **Test:** `Fmp4WriterTest`, `ChunkUploaderTest`; dictation on the emulator from the debug audio file; a 2-minute dictation on a phone.
6. **M6 — Voice mode with the screen on.** `VoiceTurnController` and its tests, the Voice screen, reply audio (TTS stream, queue player, chapters, rates), the thinking cue and sounds, spoken refusals, approvals in Voice, the recovery banner, continue on another device, Glean from Voice, the mini-player, listen-aloud and download. **Test:** the Voice unit tests; a billed Voice turn from the debug audio file on the emulator; a turn on a phone with the screen on.
7. **M7 — Background and lock screen.** `AudioService`, the media session and notification, lock-screen actions (Pause, Resume, Stop, Record, Glean), audio routing and Bluetooth, interruptions, the headset-microphone hold, local notifications, wake lock, the idle end, the app swiped away from Recents, the permission-refused fallback, uploads after the process ends. Prove the locked Voice turn first. **Test:** the device checklist (§7) on two phones.
8. **M8 — Workspace and account pages.** Profile (with the generation watcher), Todo, Artifacts (Intentions, create, delete), version history and diff, Prompts, the full Account page (email, X, settings, deletion and restore), Welcome, Confirm email, prefill consent, Import (all kinds, references, clipper tokens), Export, Share, Commons. **Test:** the ported page tests; UI flows as the iPhone's `M4ScreensUITests`.
9. **M9 — Parity walk and first release.** Walk every iPhone screen against the Android app and close the gaps; TalkBack and font-size pass; release build signed with the upload key; App Links if the server file exists; Google Play internal testing; `android/README.md` complete; the design docs named in `CLAUDE.md` updated.

---

## 10. Needs Peter

Each item: the situation, the recommendation, and what happens if Peter says nothing.

1. **A Google Play developer account, and how testers get the app.**
   Situation: without a Play Console account the app can be installed only by cable (`adb install`) or by sending the APK file, which testers must allow in their phone's settings. With an account: internal testing reaches up to 100 testers through the Play Store, usually within minutes of an upload. Google asks for a one-time registration fee and identity verification; at the time of writing, a new personal account must run a closed test with at least 12 testers for 14 days before it may publish to everyone (internal testing does not need this). Before the first release Google also asks for the app's declarations: privacy policy address, data safety form, content rating, target audience, the foreground service types (microphone, media playback) with a description of each, and the account-deletion address (§5 item 7). The builder drafts the answers in `android/README.md`; **Peter approves the "Data safety" answers and the privacy-policy address before the first submission**, because they are public statements about which data the app sends where, including to AI providers.
   Recommendation: a Google Play developer account and internal testing for the alpha; whether it is a personal or an organisation account is Peter's choice.
   If Peter says nothing: M1–M8 are built and tested on emulators and on phones by cable; no tester can install the app; App Links wait (they need the Play signing key's fingerprint).

2. **Phones for the device checklist.**
   Situation: the emulator cannot lock like a phone, has no Bluetooth headset, no calls and no manufacturer battery rules; the spike after M1 and M7 need real phones. Samsung phones manage background apps differently from Google's Pixel phones.
   Recommendation: one recent Pixel-class phone for the builder's checklist runs (Peter's or a bought test phone), and at least one alpha tester with a Samsung phone.
   If Peter says nothing: the spike and M7 run on the emulator only, and the lock-screen, Bluetooth and call items wait for a tester with an Android phone.

3. **Sign-in links that open the app (App Links).**
   Situation: on the iPhone every sign-in link is copied from Mail and pasted. On Android, if the server serves `/.well-known/assetlinks.json` (§5 item 1), tapping the sign-in link in a mail app opens the Android app and signs in. Consequences: on a phone with the app installed, every sign-in link opens the app, also when the person wanted to sign in on loore.org in the phone's browser (they can still paste the link into the browser); when the app is already signed in it asks first. Wording candidates: (a) "You're already signed in. Use this link to sign in again?" (Sign in / Cancel); (b) "Sign in with this link? If it is for another account, you leave the one you're in now." (Sign in / Cancel). Recommended: (a). The same server file kind would also allow the iPhone to do this later (`docs/IOS-APP-DESIGN.md` §14.1); not part of this plan.
   Recommendation: yes, for the sign-in link only, with paste kept.
   If Peter says nothing: paste only, as on the iPhone; the App Links code stays off until the server file exists.

---

## 11. Decisions made in this document

Listed for review; each can be changed before or during the build.

1. Kotlin, Jetpack Compose, one Activity, no third-party SDKs beyond the libraries in §6 (§1, §6).
2. Two Gradle modules, `:core` (JVM) and `:app` (§6).
3. An own fragmented-MP4 writer instead of a platform or Media3 muxer, so segments are cut on demand and the bytes match the server's contract (§3.11).
4. One foreground service of type microphone and media playback for the whole Voice conversation, started at the record tap; the notification replaces the Live Activity and Now Playing (§3.18).
5. The thinking cue stays with the same three settings and default; its Off warning goes; a wake lock keeps polling going (§3.16, §3.18).
6. The user pause keeps the microphone open with the samples dropped, as on the iPhone (§3.11).
7. WorkManager for uploads that outlive the service or the process (§3.12).
8. Session cookies encrypted with a Keystore key in a file excluded from backup; not `EncryptedSharedPreferences` (deprecated) (§3.6).
9. App Links only for the sign-in link, if Peter agrees; paste kept (§3.4, §10 item 3).
10. Files picked for import or upload are copied to the cache first (§3.22, §3.31).
11. Shared files deleted at the next launch, at sign-out or when replaced (§3.31).
12. The iPhone's JSON fixtures shared from `ios/LooreTests/Fixtures/` rather than copied (§7).
13. No emulator in CI; UI flows run locally against the local backend (§7, §8).
14. The app sends `X-Loore-Client: android` from M1, ahead of the server change (§5 item 2).
15. The system serif for reading text, as on the iPhone (§3.33).
16. No request to be exempted from battery optimisation (§3.18).
17. Phone layout only; tablets and foldables get the phone layout, and no device is excluded (§0).
18. **Package name `org.loore.app`**, the same as the iPhone's bundle id (§6). Google Play never lets an app change its package name after the first upload, so it must be settled before the first upload in M9.
19. **Oldest supported Android: Android 12 (API 31)** (§6). A higher minimum removes code paths and device testing; a lower one reaches more phones. API 31 gives one way to route a Bluetooth headset's microphone and one set of rules for starting the microphone service; Android 10 (API 29) would add the older Bluetooth path and older service rules to build and test. The builder checks the current share of each version in Android Studio's distribution chart at M1 and reports it.

---

## 12. Heuristics introduced

Every number this document adds that the iPhone app does not already use. Numbers copied from the iPhone app or the server (timers, limits, retry delays, sizes, sound levels) are not repeated here; they are named in §3 with their Swift files.

| Number | Where | Why this value |
|---|---|---|
| minSdk 31 (Android 12) | §6, §11 item 19 | one Bluetooth routing API and one set of foreground-service rules |
| 30 minutes idle in the background before the conversation's notification ends | §3.18 | long enough to answer a reply after a pause; short enough that an idle microphone service does not stay for hours |
| 10-minute wake-lock timeout per wait | §3.18 | the iPhone's thinking-cue limit per wait, reused |
| 30 s first backoff, exponential, for WorkManager uploads | §3.12 | WorkManager's default; uploads are not urgent once the user has stopped |
| 3,000 levels in the deep-tree decoding test | §3.21, §7 | well past any thread seen; enough to fail on recursive decoding |
| Two notification channels, low and high importance | §3.19 | one silent ongoing notification; alerts that show and sound over other apps and stay silent while Loore is in front, as on the iPhone |
| JDK 17, CI timeout 30 min | §6, §8 | current Android Gradle Plugin versions need at least JDK 17; 30 min is above a full build and test run |
| Two test phones (newest and minimum Android), one Samsung | §7, §10 item 2 | covers both ends of the supported range and the main manufacturer difference |

---

## Appendix A: routes

Every route the iPhone app calls, with its method (from `Loore/Core/Networking/APIPaths.swift` and the call sites). The Android app calls the same. `/auth/*` routes are never followed through redirects. `{base}` is `/api/nodes/<id>`, `/api/profile/<id>` or `/api/external/items/<id>`.

**Auth:** `POST /auth/magic-link/send` · `GET /auth/magic-link/verify?token=` · `GET /auth/logout` · `GET /auth/login?next=/profile` (web view) · `GET /auth/x/connect` (web view) · `POST /auth/invite-choice/continue` · `POST /auth/invite-choice/cancel`

**Account:** `GET /api/dashboard/` (`?profile=0` at launch) · `PUT /api/dashboard/user` · `GET /api/dashboard/starting-point` · `PATCH /api/dashboard/timezone` · `POST`/`DELETE /api/dashboard/email` · `POST /api/dashboard/email/confirm` · `DELETE /api/dashboard/email/pending` · `DELETE /api/dashboard/x` · `POST /api/terms/accept` · `POST /api/account/delete` · `POST /api/account/delete/confirm` · `GET`/`POST /api/account/restore` · `POST /api/account/restore/decline`

**Updates:** `GET /api/updates` · `POST /api/updates/changelog/<id>/read|skip` · `POST /api/updates/notifications/<id>/read|skip` · `GET /api/updates/polls/<id>` · `POST /api/updates/polls/<id>/draft` · `PUT /api/updates/polls/<id>/response` · `POST /api/updates/polls/<id>/send` · `POST /api/updates/polls/<id>/decline`

**Tool approvals and Voice clips:** `GET /api/tool-approvals?node=` · `POST /api/tool-approvals/<id>/approve` · `POST /api/tool-approvals/<id>/reject` · `GET /api/voice/approval-announcement?node=` · `GET /api/voice/refusal/<code>.mp3`

**Nodes:** `POST /api/nodes/` (JSON, or multipart `audio_file`) · `GET /api/nodes/models` · `GET /api/nodes/default-model` · `GET /api/nodes/titles?ids=` · `GET`/`PUT`/`DELETE /api/nodes/<id>` · `POST /api/nodes/<id>/llm` · `GET /api/nodes/<id>/llm-status` · `GET /api/nodes/<id>/delete-impact` · `PUT /api/nodes/<id>/thread-name` · `GET /api/nodes/<id>/resolve-quotes` · `GET /api/nodes/<id>/suggested-model` · `POST`/`DELETE /api/nodes/<id>/pin` · `GET /api/nodes/<id>/transcription-status` · `POST /api/nodes/upload/init` · `POST /api/nodes/upload/chunk` · `POST /api/nodes/upload/finalize` · `POST /api/nodes/upload/cleanup`

**Audio and speech:** `GET {base}/audio` · `GET /api/nodes/<id>/audio-chunks` · `GET /api/nodes/<id>/audio-download?format=original|mp3` · `POST {base}/tts` · `GET /api/nodes/<id>/tts-status` · `GET {base}/tts-chapters` · `GET /media/…` (supports ranges)

**Glean:** `POST /api/read/from-node/<id>` · `GET /api/nodes/<id>/feed-picks` · `POST /api/nodes/<id>/feed-picks/read` · `POST /api/nodes/<id>/feed-picks/<item>/verdict`

**References and sources:** `GET /api/external/items` · `GET`/`PUT`/`DELETE /api/external/items/<id>` · `POST`/`DELETE /api/external/items/<id>/read` · `POST /api/external/items/<id>/feedback` · `GET /api/external/twitter/status` · `POST /api/external/twitter/sync` · `GET /api/external/twitter/connect` (web view) · `POST /api/external/community-archive/fetch` · `POST /api/external/bookmarks/import` · `GET`/`POST /api/external/tokens` · `DELETE /api/external/tokens/<id>`

**Proposal accepts:** `POST /api/todo/apply-draft` · `POST /api/github/create-issue` · `POST /api/feedback/submit` · `POST /api/share/save-proposal`

**Log and search:** `GET /api/log` · `GET /api/search` · `GET /api/search/semantic`

**Drafts, recording, Voice, Text mode:** `GET`/`POST`/`DELETE /api/drafts/` · `GET /api/drafts/interrupted` · `POST /api/drafts/streaming/init` · `POST /api/drafts/streaming/<sid>/audio-chunk` (multipart) · `GET /api/drafts/streaming/<sid>/status` · `POST /api/drafts/streaming/<sid>/finalize` · `POST /api/drafts/streaming/<sid>/transcribe-remaining` · `POST /api/drafts/streaming/<sid>/save-as-node` · `DELETE /api/drafts/streaming/<sid>/discard` · `POST /api/drafts/streaming/<sid>/release` · `POST /api/drafts/streaming/<sid>/claim` · `POST /api/voice/` · `POST /api/voice/from-node/<id>` · `GET /api/voice/availability?parent=` · `POST /api/voice/timing` · `GET /api/voice/timing/clock` · `POST /api/textmode/start` · `POST /api/textmode/from-node/<id>`

**Workspace:** `GET`/`POST /api/profile/` · `PUT /api/profile/<id>` · `GET /api/profile/versions` · `GET /api/profile/versions/<id>` · `POST /api/profile/revert/<id>` · `GET /api/export/profile-progress?task_id=` · `GET`/`PUT`/`PATCH /api/todo/` · `GET /api/todo/versions` · `GET /api/todo/versions/<id>` · `POST /api/todo/revert/<id>` · `GET /api/artifacts/` · `GET`/`PUT`/`DELETE /api/artifacts/<kind>` · `POST /api/artifacts/<kind>/viewed` · `GET /api/artifacts/<kind>/versions` · `GET /api/artifacts/versions/<id>` · `POST /api/artifacts/<kind>/revert/<id>` · `GET /api/prompts/` · `GET`/`PUT /api/prompts/<key>` · `GET /api/prompts/<key>/versions` · `GET /api/prompts/<key>/versions/<id>` · `GET /api/prompts/<key>/default` · `POST /api/prompts/<key>/revert/<id>` · `POST /api/prompts/<key>/revert-to-default` · `POST /api/prompts/<key>/acknowledge-default`

**Share and Commons:** `GET`/`POST /api/share` · `PATCH`/`DELETE /api/share/<id>` · `POST /api/share/<id>/publish` · `POST /api/share/<id>/revoke` · `GET /api/commons/feed?page=` · `GET /api/commons/permalink/<user>/<slug>` · `GET /api/export/threads`

**Import:** `POST /api/import/analyze` · `POST /api/import/confirm` · `POST /api/import/claude/analyze` · `POST /api/import/claude/confirm` · `POST /api/import/chatgpt/analyze` · `POST /api/import/chatgpt/confirm` · `POST /api/import/twitter/analyze` · `POST /api/import/twitter/confirm` · `GET /api/import/status/<task_id>` · `POST /api/import/profile-rebuild`

**Server-sent events:** `GET /api/sse/nodes/<id>/llm-stream` · `GET /api/sse/nodes/<id>/tts-stream[?last_chunk=]` · `GET /api/sse/profiles/<id>/tts-stream` · `GET /api/sse/items/<id>/tts-stream` · `GET /api/sse/drafts/<sid>/transcription-stream?device_key=`
