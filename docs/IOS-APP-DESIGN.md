# Loore iOS app — design

**Date:** 2026-09-30
**Branch:** `ios-app`
**Status:** built (M1–M5 on `ios-app`, PR #384, 2026-10-01; awaiting device testing and a staging pass). It was approved to build with Peter choosing "run straight through": the decisions below were made without a review checkpoint and are listed in §15 for review in the PR. What differs from this design is in §16 "As built"; every difference from the web is in `ios/PROGRESS.md`.

This document says what the native iPhone app is, how it is built, and in what order. What the web app does today is described in five detailed maps under `ios/docs/web-app-map/` (A–E, written 2026-09-30 from `origin/main` `2774ab2`). This document refers to them by section, e.g. "C §8.3" = map C, section 8.3.

**Precedence rule for implementers.** For how the web app *behaves*, the web code is the truth, then the maps, then this doc. For what the app *should do*, this doc wins; where it is silent, copy the web app. Record every intentional difference from the web app in `ios/PROGRESS.md` under "Deviations".

---

## 0. Goal and non-goals

**Goal.** A native iPhone app with feature parity with the desktop web app (loore.org), against the unchanged backend. "Parity" means a logged-in user can do on the phone everything they can do on desktop, except for the few surfaces listed as web views in §6.

**Why native.** The web app runs into iOS Safari's rules, not into bugs: a reply's audio cannot start by itself while the phone is locked (Safari blocks `play()` without a tap, and suspends a page that has no audio playing). A native app with the `audio` background mode can keep its audio session alive from the record tap to the end of the reply and start the reply's audio itself (§9).

**Non-goals for this PR.**
- No backend changes. Backend changes the app would benefit from are listed in §14 as proposals.
- iPad layout, widgets, Live Activities, push notifications, a Share Extension (the iOS counterpart of the Chrome clipper), offline mode. The code should not block any of them.
- App Store submission. The build targets Peter's own iPhone through Xcode (free Apple ID; a paid membership only adds TestFlight and push).

---

## 1. Stack

- **SwiftUI**, iOS **17.0** minimum (Observation `@Observable`, `AVAudioApplication`, `NavigationStack`), iPhone only, portrait + landscape.
- **Swift 6 toolchain (Xcode 26), Swift 5 language mode**, with explicit `@MainActor` on UI types. Reason: Swift 6 strict concurrency and Xcode 26's default main-actor isolation make audio render/tap callbacks (which run on real-time threads) crash or fail to compile in ways that cost agents hours; Swift 5 mode keeps the audio code straightforward.
- **Dependencies (Swift Package Manager), kept to two:**
  - `swiftlang/swift-markdown` (cmark-gfm parser) — the app renders the AST itself (§8).
  - `weichsel/ZIPFoundation` — streaming zip reading for imports (§10).
  - No networking, DI, or UI frameworks. `URLSession`, `AVFoundation`, `MediaPlayer`, `WebKit`, `SafariServices`, `UserNotifications`, `Security` (Keychain) from the SDK.
- **Why not Capacitor or React Native.** Capacitor would reuse the web UI, but WebKit suspends a web view's JavaScript in the background, so the locked-phone problem stays. React Native needs every screen rewritten anyway, plus a native audio module for the hard part; it adds a runtime without removing work.

---

## 2. Repository layout and project setup

```
ios/
  project.yml                 # XcodeGen spec (the .xcodeproj is generated, git-ignored)
  README.md                   # build/run/install-on-iPhone instructions for Peter
  PROGRESS.md                 # milestone hand-off log + "Deviations" + "Known gaps"
  docs/web-app-map/           # maps A–E (reference, not maintained after the port)
  Loore/
    App/                      # LooreApp, AppState, root navigation, environment config
    Core/
      Networking/             # APIClient, endpoints, SSE client, poller, error mapping
      Auth/                   # AuthService, CookieVault (Keychain), WebLoginView
      Models/                 # Codable models, tolerant date decoding, enums
      Stores/                 # @Observable stores shared across screens
      Util/
    DesignSystem/             # tokens, fonts, reusable components (cards, pills, dialogs, toasts)
    Markdown/                 # marker pre-split, AST → SwiftUI renderer, checklist toggling
    Audio/                    # session, recorder, segment uploader, voice turn machine, player, cue, Now Playing
    Features/
      Home/ Voice/ Write/ Log/ Thread/ Proposals/ Profile/ Todo/ Artifacts/
      References/ Prompts/ Account/ Import/ Share/ Commons/ Search/ Onboarding/ Updates/
    Resources/                # Assets.xcassets (icon, colors), fonts (OFL), terms text, sounds
  LooreTests/                 # unit tests (XCTest)
  LooreUITests/               # a few XCUITest smoke flows
.github/workflows/ios.yml     # build + unit tests on macOS runners for PRs touching ios/**
```

- **XcodeGen** (`brew install xcodegen`; `cd ios && xcodegen`). Sources are included by directory, so adding a file never edits a project file, and parallel milestones do not conflict on `project.pbxproj`.
- Bundle id `org.loore.app`, display name **Loore**, version 0.1.0. Signing: automatic, `DEVELOPMENT_TEAM` left empty in `project.yml`; Peter sets his team once in Xcode (documented in `ios/README.md`).
- `Info.plist`: `UIBackgroundModes = [audio]`; `NSMicrophoneUsageDescription` ("Loore records your voice so it can transcribe what you say and reply."); `NSAppTransportSecurity.NSAllowsLocalNetworking = true` (local dev over http; production and staging are https).
- **App icon** from `frontend/public/loore-logo.png` / `apple-touch-icon.png` (single 1024 px asset). **Fonts**: Cormorant Garamond and Outfit, bundled from the Google Fonts repository (SIL OFL; include the licence files).
- **Environments**: `Production` (`https://loore.org`), `Staging` (`https://staging.loore.org`), `Local` (`http://localhost:5010`, the Docker dev backend; the simulator reaches the Mac's localhost). Release builds default to Production; Debug builds default to Local. A hidden environment switcher (Account screen, long-press on the version label) exists in Debug builds only. Switching environment signs out.
- **CI**: `.github/workflows/ios.yml` runs on pull requests that touch `ios/**`: install XcodeGen, generate, `xcodebuild build-for-testing` + unit tests on an iPhone simulator. The repo is public, so macOS runner minutes are free. It must not touch `ci.yml` or the deploy workflows.

---

## 3. Architecture

- **App state.** One `AppState` (`@Observable`, `@MainActor`) injected through the SwiftUI environment: current user (the `GET /api/dashboard/` payload), auth phase, environment, craft mode, theme, spend-capped flag, the shared audio player, toasts, and the cross-screen signals the web sends as `window` events (A §4.4, E §0.2 — e.g. "todo changed", "profile generation started", "node created"). Feature screens own their own view models.
- **Capabilities.** The ~10 user fields that decide what a user sees (A §8.3: `approved`, `is_admin`, `plan`/`has_voice_mode`, `craft_mode`, `share_v1_enabled`, `voice_mode_enabled`, `spend_blocked`, `terms_up_to_date`, …) are read through one `UserCapabilities` value, not checked ad hoc in views.
- **APIClient** (`actor` or `@MainActor final class`; implementer's choice): `async throws` methods on top of one `URLSession` with an in-memory cookie jar (the Keychain copy in §4.3 is the only persistence; `HTTPCookieStorage.shared` would write the `remember_token` to a backed-up plaintext file); JSON encoding/decoding with a tolerant date strategy (B §0: microsecond fractions, `Z` or `+00:00`, date-only); canonical trailing slashes (B §0); `X-Timezone` header on every request; `Cache-Control: no-cache` on polls. Errors map to one `APIError` enum: `.unauthorized` (401 → sign-out), `.notApproved` (403 with the approval message), `.spendCap(message)` (402 `monthly_spend_limit_reached`), `.server(status, code?, message?)`, `.transport`. Check `Content-Type` before decoding error bodies (some 404/403 are HTML, B §8.9). Never follow redirects on non-`/api` paths (B §0, §3.5).
- **Models.** `Codable` structs with optionals wherever the backend can omit a field; unknown enum cases decode to `.unknown(String)`. Node has several JSON shapes (B §5.2): model them as separate structs (`ThreadNode`, `LogCard`, …) rather than one struct with everything optional.
- **Polling helper** mirroring `useAsyncTaskPolling` (B §6.1): interval, request timeout, give-up time, immediate re-poll when the app returns to the foreground.
- **SSE client** on `URLSession.bytes(for:)`: parses `event:`/`data:` lines, treats three missed heartbeats (45 s) as a stall, reconnects with `?last_chunk=` where the endpoint supports it, handles `event: error` and `event: close`, and checks the response `Content-Type` (the TTS stream answers JSON in some states, C §5.4).
- **Caching and data at rest.** Journal content is private. Use a memory-only `URLCache` for API responses (no disk cache). Anything the app writes to disk (drafts pending upload, recorded segments, the upload queue) goes in Application Support with `FileProtectionType.completeUntilFirstUserAuthentication` (the upload queue must be writable while the phone is locked) and is deleted once the server has it. No analytics, no third-party SDKs, no logging of content (log ids, lengths and statuses only).

---

## 4. Auth

Details and the evidence for each step are in B §3–§4.

1. **Email magic link, pasted (primary).** The app posts `POST /auth/magic-link/send {email}`, then shows "Paste the link from the email" with a Paste button (accepts the full URL or the bare token). It calls `GET /auth/magic-link/verify?token=…` with redirect-following disabled, reads the 302's `Location` (`/login?error=…` → the web's error messages, B §3.2; anything else = success), and keeps the `Set-Cookie` cookies. Then `GET /api/dashboard/`.
   Note in the UI copy that a **sign-up** link works once: a new user who opened it in Safari needs a new link.
2. **Sign in with X** in a `WKWebView` sheet (`/auth/login?next=/profile`). When the web view navigates to the frontend origin after login, cancel the navigation, copy `session` and `remember_token` from `WKHTTPCookieStore` into the app's cookie jar, close the sheet (B §4.1 option B).
3. **Cookie persistence.** `CookieVault` stores `session` and `remember_token` in the Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`) and restores them into `HTTPCookieStorage` at launch. It **honours the cookie's own expiry** (30 days after login, same as the browser): the app does not re-inject an expired `remember_token`, even though the server would accept it (B §3.1). Re-login is then once a month, as on the web. Proposal §14.2 removes that friction server-side.
4. **Logout.** `GET /auth/logout` with redirects disabled; clear the Keychain, `HTTPCookieStorage`, the `WKWebsiteDataStore` cookies and every in-memory store, regardless of the response.
5. **Gating after sign-in** (B §3.6), in order: not approved → waitlist screen (AlphaThankYou: leave/confirm email, prefill consent card); terms out of date → blocking Terms screen (text copied verbatim from `TermsModal.js`, accept = `POST /api/terms/accept`); then the app; once per launch `GET /api/updates` → Updates sheet if anything is unread; `spend_blocked` → spend-capped state; device time zone ≠ `user.timezone` → `PATCH /api/dashboard/timezone` (approved users only).
6. **Email confirmation** (`/confirm-email?token=…` links): paste the link on the Account screen → `POST /api/dashboard/email/confirm {token}`.
7. **Local dev login** (for agents and Peter): mint a magic link inside the backend container (CLAUDE.md "Login without SMTP"), paste it into the simulator (`xcrun simctl pbcopy booted`). Debug builds also accept a launch argument `-LooreSessionCookie <value>` (see §12).

---

## 5. Navigation

The web NavBar has the same items on phone and desktop (A §2.1). The app maps it to a bottom tab bar, the iOS equivalent of that top bar:

| Tab | Web equivalent | Contents |
|---|---|---|
| **Reflect** | "Reflect" → `/` (Home) | Greeting, "What's on your mind?", cards: Voice, Text, Share (flag), Read (admin). Pushes Voice / Write / Thread. |
| **Artifacts** | "Artifacts" → `/profile` | Profile, with the `ArtifactsNav` row to Todo and each artifact kind (E §1). |
| **Log** | "Log" → `/log` | Log list; search button (the web's ⌘K and magnifiers, E §3); pushes Thread. |
| **Commons** | "Commons" (only `share_v1_enabled`) | Commons feed. Hidden otherwise. |
| **More** | the ⋮ menu | Import data, References (web has no nav entry; E §4), Account, My public page (flag), Light mode toggle, Craft mode toggle (+ craft block: Write new entry, Export data, Prompts), Admin (web view, `is_admin`), About (web views), Logout. |

- The **global audio player** is a mini-player docked above the tab bar (the web's mobile floating card, C §6.2) on every screen except Voice, which has its own player.
- **Thread** (`/node/:id`) is pushed on whichever tab opened it. In-markdown links to `https://loore.org/node/<id>` (and relative `/node/<id>`) open in-app; other loore.org paths open the matching native screen when there is one, else an in-app Safari view; external links open in Safari.
- Touch equivalents for desktop-only interactions (A §2.3): kebab menus always visible (the web already does this on `hover: none` devices), `⌘↩` submit → a Send button plus hardware-keyboard shortcut, `Esc` → Cancel buttons and swipe-to-dismiss, `⌘`-click → long-press context menu "Open in new window" is dropped.

---

## 6. Screen inventory: native, web view, or not ported

| Web route (A §1) | App |
|---|---|
| `/` Home, `/voice`, `/textmode`, `/log`, `/node/:id` (member) | **Native** (M2, M3) |
| `/profile`, `/todo`, `/artifacts[/:kind]`, `/references[/:id]`, `/prompts[/:key]`, `/account`, `/import`, `/share`, `/commons`, `/welcome`, `/confirm-email`, `/alpha-thank-you`, Search, Updates modal, Terms modal | **Native** (M4, onboarding pieces in M1) |
| `/admin` | **Web view** (`SFSafariViewController` does not share the app's cookies, so use a `WKWebView` with the session cookies injected) |
| `/landing`, `/vision`, `/why-loore`, `/how-to`, `/@user`, `/@user/:slug`, public thread pages | **Web view** (`SFSafariViewController`, public pages need no cookie) |
| `/login` | Native sign-in screen (§4) |
| Connect X (`/auth/x/connect`), X bookmarks connect (`/api/external/twitter/connect`) | **Web view** with cookies injected; detect the final frontend URL and read its `x_login=` / `x_connect=` outcome (B §4.1 C) |
| Chrome clipper token card (Import page) | Native (it only creates/lists tokens); the clipper itself stays a desktop feature |

---

## 7. Design system

- Tokens come from A §5 (exact hex values for dark and light, typography, radii, spacing, motion). Implement them once in `DesignSystem/Tokens.swift` and colour assets; views never use literal colours or font names.
- Dark (amber on near-black) by default, light when the user turns it on (`ThemeContext` semantics, A §4.3; the web stores the choice per device — do the same with `@AppStorage`). Follow the system setting until the user chooses.
- Serif (Cormorant Garamond) for headings and page titles, Outfit for body and UI; Dynamic Type supported by scaling from the web sizes (use `relativeTo:`).
- Motion: slow fades (A §5.4). Respect Reduce Motion.
- The look carries a product rule (`docs/LOORE-ESSENCE.md`): nothing here counts or scores the user. No badges with counts on tabs, no streaks, no progress rings, no "dashboard" layouts. Generous spacing, calm copy, the web's exact wording.

---

## 8. Thread, writing, markdown, replies (M2)

Everything here is specified in map D; the decisions:

- **Thread** (`GET /api/nodes/<id>`, D §1): ancestors, focal card (3 px accent left border), footer, action row, inline reply form, children tree (recursive, 20 px indent with a left rule when siblings > 1). The payload is the whole subtree; render lazily (`LazyVStack`) and do not re-fetch the subtree on every change.
- **Markdown**: pre-split the content on the web's markers first (`{quote:N}`, `{quote_ext:N}`, `{user_*}`, D §3.1), exactly as `MarkdownBody` does, then parse each prose segment with swift-markdown (GFM: tables, strikethrough, task lists, autolinks) and render the AST with SwiftUI views. **Single newlines render as line breaks** (the web uses remark-breaks). Code blocks with horizontal scroll. Node links render as the target's title (`GET /api/nodes/titles?ids=`). One renderer is used everywhere (thread, profile, todo, artifacts, references, updates).
- **Checklists** (owner only, D §3.5): toggling finds the line by the same stripped-text matching the web uses. Port the web's text-extraction function and its tests so both produce identical strings.
- **Proposals** (D §6): port the parser from `ProposalInline.js` and **port its jest tests to XCTest one-to-one** (including the #378 "no Completed section" case). Four accept endpoints; no reject button (parity).
- **Writing** (D §4): server drafts (`/api/drafts/`, 1 s debounce + 3 s flush), privacy and AI-usage selectors and model picker behind craft mode, submit decision tree as in `NodeForm.handleSubmit` (D §4.5), edit, delete (with the web's dialog options), rename thread (Log only). Dictation into the text editor uses the recorder from §9 (text-mode path: finalize without the `Voice` label, `save-as-node`), so the recorder must be usable outside voice mode; in M2 the mic button may be present but disabled until M3 lands.
- **Replies** (D §5): request `POST /api/nodes/<id>/llm`, navigate to the reply's thread with an "awaiting" state, stream text over `llm-stream` SSE, poll `llm-status` every 2 s as the source of truth, continuation chains, the web's error handling (spend cap, refusals, failure → back to the parent). Auto-generate setting as on the web (per device, default on).
- Copy the web quirks listed in D §10 unless they are plain bugs; list each choice in `PROGRESS.md`.

---

## 9. Voice and audio (M3) — the reason for the app

Map C is the specification; §8 of it is the native design. The decisions:

### 9.1 Audio session
- Voice turn: `.playAndRecord`, mode `.videoChat`, options `[.allowBluetoothHFP, .allowBluetoothA2DP, .defaultToSpeaker]`, activated at the record tap (foreground) — what Safari sets when the web app opens the mic. The headset's mic is set as the preferred input. (Mode `.default` was the first choice; it let iOS move the input to the phone's own mic mid-recording while Bluetooth headphones kept playing, #423. The reply's volume is unaffected: it plays after the switch to `.playback`.) **Do not deactivate until the turn's audio is finished** — a non-mixable session cannot be re-activated from the background.
- If a headset's mic goes away mid-recording anyway, the recording pauses (alert, toast, notification) and waits for Resume; the microphone keeps running with its samples dropped, so Resume works from the lock screen (#423).
- Each recording writes a log on the phone (`RecordingLog`: route changes, interruptions, microphone restarts, chunks, input level once a second; no audio, no words), read over the cable (`ios/README.md`).
- At Stop, switch the still-active session to `.playback` / `.spokenAudio` so replies play over Bluetooth A2DP rather than narrowband HFP (C §8.2); back to `.playAndRecord` at the next record tap. Device-test this while locked (§13.3); if it breaks background playback, stay in `.playAndRecord` and note it.
- Listen-aloud outside voice mode: `.playback`, `.spokenAudio`. Deactivate with `.notifyOthersOnDeactivation` when playback ends.
- `setPrefersNoInterruptionsFromSystemAlerts(true)` while recording.

### 9.2 Recording and upload
- `AVAudioEngine` input tap → `AVAudioConverter` to a fixed format (48 kHz mono) → `AVAssetWriter(contentType: .mpeg4Movie)` with `outputFileTypeProfile = .mpeg4AppleHLS`, AAC 64 kbps, `preferredOutputSegmentInterval` 15 s (or `.indefinite` + `flushSegment()` every 15 s and on pause/interruption/stop), delegate `didOutputSegmentData`.
- **Chunk contract (C §2.1):** chunk 0 = the initialization segment **concatenated with** the first media segment; chunks 1…N = each media segment alone; timestamps continuous across user pauses (compute PTS from the sample count). A new writer after an interruption may send an `ftyp`-first chunk (the server opens a subsession). Upload `mime_type=audio/mp4`.
- **Build this first in M3 and prove it end to end against the local backend** (a 30–60 s recording → `finalize` → transcript appears) before building anything on top. If the server rejects the segments and no writer configuration fixes it, stop and report; that is the one place a backend change could become necessary.
- Uploads: write each chunk to disk first; upload chunk 0 before any other; foreground `URLSession` with the web's retry schedule (4 retries, 2/4/8/16 s); on failure or backgrounding, also enqueue a background `URLSession` upload (survives suspension; the server dedupes by index). Persist the queue so a relaunch finishes it. `finalize` only after every chunk is acknowledged; if some are given up, send `total_chunks` = the count actually stored (otherwise the server waits 10 minutes, C §10.4). Treat `400 not in recording state` on a finalize retry as success.
- After `finalize`, **poll `GET /api/drafts/streaming/<sid>/status`** (~1 s) for `llm_node_id` / `warning`; do not use the draft SSE in voice mode (it deletes the draft when it sends `all_complete`, C §10.3). Never call `/status` while still recording (#320).

### 9.3 Reply audio
- TTS attach rule exactly as C §8.1 ("TTS attach rule"): attach `tts-stream` as soon as the reply id is known and TTS is pending/processing; handle the JSON answer; `POST /tts` once when the node completes without `all_complete`; follow `continuation_node_id` into the same queue; close on `failed`; handle `cancelled` as terminal (the web gets stuck there).
- **Player**: one `ChunkQueuePlayer` (`AVQueuePlayer` + durations array for cumulative time, chapters, seek across chunks, rate 1/1.25/1.5/2 with `.timeDomain` pitch), shared by voice mode and the global mini-player (C §8.4 option A). Dedupe by `(node_id, chunk_index)` and URL.
- **Send the session cookies with every media request** (`AVURLAssetHTTPCookiesKey`, and `URLSession` for downloads).
- Original recordings: `.mp4` originals are served as `application/octet-stream` → use `AVURLAssetOverrideMIMETypeKey: "audio/mp4"`; WebM/Opus originals (desktop recordings) → play the MP3 from `audio-download?format=mp3` (C §10.7).

### 9.4 Staying alive between Stop and the first chunk
Chosen design (C §8.3): an **audible, soft "thinking" cue**, looping, started *before* the mic engine stops and whenever the queue drains while TTS is still generating (including an interim → continuation wait); stopped when the next chunk plays. This keeps the audio session running under the `audio` background mode, so networking continues while the phone is locked, and it tells a user with the phone in a pocket that the reply is coming.
- The cue is generated in code (no bundled recording), low and calm: a slow soft pulse, around −30 dBFS, no melody. It must be audible (a silent keepalive risks App Review rejection under 2.5.4 and is not what we want to rely on).
- Account → Voice: cue volume (Soft / Very soft / Off). Choosing Off shows: "Without it, iOS may pause Loore while you wait, and the reply will need a tap when your phone is locked."
- Safety nets: `beginBackgroundTask` around finalize; on foreground, reconcile from REST (`/status`, `llm-status`, `tts-status`, `tts-stream?last_chunk=`) like the web's #242 loop.

### 9.5 Lock screen, interruptions, timing
- Now Playing and remote commands per phase exactly as the web's Media Session mapping (C §6.5, §8.1): recording (play = resume, pause = pause, next = stop and send), thinking ("Voice…", next = cancel), playback (play/pause/skip ±10 s/seek/rate, chapters in the title).
- Changed after the first device test (#397): while recording, the controls are a **Live Activity** (Pause/Resume, Stop; widget extension `LooreLiveActivity`, iOS 18) and Now Playing is cleared; during the reply the activity adds a Record button that starts the next turn from the lock screen (`AudioRecordingIntent`). The Now Playing recording mapping above is the fallback where no activity shows. While thinking, Now Playing has no active command (no lock-screen cancel). Details in `ios/PROGRESS.md` "Field test fixes".
- Interruptions (`interruptionNotification`, route changes, media-services reset) as C §8.1; on a mic interruption flush the segment, pause, play the web's chime when possible and post a **local notification** "Recording paused — tap to resume" (no backend needed).
- A 59-minute warning (chime + local notification) in voice mode too (the web has it only in text mode, #243).
- Send the timing marks (`POST /api/voice/timing`, C §4.6) with the same stage names; `autoplay_blocked` never occurs.
- Recovery banner for interrupted sessions (`GET /api/drafts/interrupted`, continue/discard) as on the web.
- Cancel keeps the web's behaviour: stops local audio and streams; the server keeps generating.

---

## 10. Feature pages (M4)

Map E is the specification (per-page endpoints, states, copy). Decisions:
- **Import**: native. Document picker (`fileImporter`) for the zip/markdown archives; read `conversations.json` etc. with ZIPFoundation **streaming**, never loading a multi-GB zip into memory; then the same analyze/confirm calls as the web. The web sends the whole content in one synchronous confirm request (nginx: 60 s, 200 MB, E §15): show the same limits and errors the web shows; do not invent chunking the backend does not support.
- **Export data** (craft): download `GET /api/export/threads` to a temp file and open the share sheet.
- **Todo**: saves overwrite the whole list with no version check (E §15). Re-fetch the todo right before a save and after any "todo changed" signal to shrink the stale-overwrite window; keep the web's text-based item matching.
- **Search**: keep the 300 ms debounce (every semantic search is billed). Snippets arrive as HTML with `<mark>`: convert `<mark>` to highlighted runs, strip other tags.
- **Share / Commons**: behind `share_v1_enabled`, as on the web. Nothing leaves without the user's explicit, previewed confirmation — copy the web's confirmation steps exactly.
- **Version history with diff** (artifacts): port `utils/diff.js` and its tests.
- **Prompts** (craft): native list and detail editor.
- **Account**: native, including the Voice settings added in §9.4 and, in Debug builds, the environment switcher.

---

## 11. Parity rules

- Same endpoints, same payloads, same order of calls, same copy. When the web does two things for one action (e.g. a toast and a navigation), the app does both.
- Hover-only affordances become always-visible or long-press; keyboard shortcuts become buttons (hardware-keyboard shortcuts optional).
- A web quirk listed in the maps (A §7, D §10, E §15, C §10) is copied unless it is clearly a bug. Fixing a bug client-side is allowed only when it does not change what the server stores; list it under "Deviations".
- Anything that would change what the server stores, what leaves a user's private space, or how AI usage is gated must behave exactly as on the web.

---

## 12. Testing

**Unit tests (XCTest), required:** tolerant date decoding; every model against JSON fixtures captured from the local backend (with test-user data only); SSE line parser; proposal parser (ported jest cases); markdown marker pre-split and checklist line matching (ported cases); diff; intentions/references/nodeLinks/spendCap/aiUsage utils (port the matching `utils/*.test.js`); fMP4 segment packaging (chunk 0 = init + first segment, later chunks have no `ftyp`); voice turn state machine transitions with a fake API; the chunk queue's cumulative-time and seek maths.

**Simulator runs against the local Docker backend** (`http://localhost:5010`):
- Sign in **only as the test user 5 (`seowriter`)**. Other local users hold real corpora (including Peter's); do not open, screenshot, or fetch their content. Fixtures and screenshots come from user 5 only.
- The local backend has real Anthropic and OpenAI keys: every reply, TTS and transcription is billed. Keep billed actions to a handful per milestone, pick the cheapest model in the model picker for tests, and keep test recordings short.
- Debug-only launch arguments (`#if DEBUG`, never in Release): `-LooreEnvironment local|staging|production`, `-LooreSessionCookie <session cookie value>`, `-LooreRoute /node/123` (open a screen directly), `-LooreDebugAudioFile <path>` (feed an audio file into the recorder pipeline instead of the mic, so the whole upload → transcription → reply → TTS loop runs in the simulator).
- Visual check: `xcrun simctl io booted screenshot` of each screen next to a screenshot of the same web screen. For the web side use **headless Chrome with a throwaway profile** pointed at `http://localhost:3001` with a session cookie for user 5 (signed in the backend container with `app.session_interface.get_signing_serializer(app).dumps({"_user_id": "5", "_fresh": True})`). Do not use Peter's Chrome window or the Chrome extension. Screenshots stay in the scratch directory; they are not committed.
- A few XCUITest smoke flows: sign-in gate with a pasted link, open a thread, write an entry, open each tab.

**Only a real iPhone can test** (checklist for Peter in `ios/README.md`): locked-phone voice turn with the reply starting by itself; AirPods (HFP recording → A2DP playback switch); a phone call during recording; lock-screen controls in each phase; a 60-minute recording; background upload after the app is killed; cue volume settings.

---

## 13. Milestones

Each milestone is one agent session on the `ios-app` branch (M3 and M4 in parallel on their own branches, merged in M5). Every milestone ends with: a clean simulator build, unit tests green, screenshots of every screen it added compared with the web, `ios/PROGRESS.md` updated (done / deviations / known gaps / how to test), commits pushed.

**M1 — Foundation.** XcodeGen project, CI workflow, environments, design tokens and fonts, APIClient + errors + tolerant dates, SSE client, poller, core models, AuthService + CookieVault + magic-link paste + X web login, gating (waitlist, Terms, Updates sheet, time zone), tab shell with placeholder screens, More menu (theme, craft toggle with its dialog, logout, web views for Admin/About), debug launch arguments, `ios/README.md`. Open the PR as a **draft**.

**M2 — Thread and writing.** Markdown renderer, Home, Log (cursor paging, rename/delete), Thread (full layout, bubbles, kebab actions, footer, pin, children tree), writing (NodeForm parity, drafts, craft controls, model picker), LLM replies (request, SSE, polling, chains, errors), proposals (parser + card + accept flows), Write-new-entry modal, Search.

**M3 — Voice and audio** (branch `ios-app-voice`). Recorder + chunk contract proven first; uploader; voice turn state machine; reply audio; thinking cue; Now Playing; interruptions; recovery banner; global mini-player; speaker icon + download on nodes, profiles and references; dictation into the text editor.

**M4 — Feature pages** (branch `ios-app-pages`). Profile, Todo, Artifacts + Intentions + version history, References list/detail + feedback + read picks, Prompts, Account, Import, Share, Commons, Welcome, Confirm email, prefill consent, profile-generation watcher, export.

**M5 — Integration and parity.** Merge M3 and M4 into `ios-app`; walk every route in the web inventory (A §1) against the app and close gaps; accessibility pass (VoiceOver labels, Dynamic Type); finish `ios/README.md` (including the device-test checklist); update the design docs named in `CLAUDE.md`; mark the PR ready for review.

---

## 14. Proposed backend follow-ups (not in this PR)

1. **Universal links**: serve `/.well-known/apple-app-site-association` (static file + nginx rule) so tapping the mailed magic link opens the app, no paste (B §4.2).
2. **`REMEMBER_COOKIE_REFRESH_EACH_REQUEST = True`**: a sliding 30-day sign-in, so active app users are not signed out monthly.
3. **Content type for `.mp4` media** (currently `application/octet-stream`, C §10.7).
4. **Push notifications** ("Your reply is ready") as a fallback when iOS suspends the app anyway; needs APNs and a paid Apple Developer membership.
5. A short numeric sign-in code as an alternative to pasting the link (B §4.2).

---

## 15. Decisions made without a review checkpoint

1. SwiftUI, iOS 17+, iPhone only, Swift 5 language mode (§1).
2. Tab bar mapping of the web NavBar, with References under More (§5).
3. Admin and marketing/public pages as web views; Connect X and X bookmarks connect as cookie-injected web views (§6).
4. Sign-in by pasting the magic link, X login in a web view; the app honours the 30-day cookie expiry instead of keeping users signed in indefinitely (§4).
5. The audible thinking cue with a volume setting (§9.4) — the main user-visible behaviour that has no web counterpart.
6. Reply audio over A2DP by switching the session category at Stop (§9.1), subject to device testing.
7. Memory-only HTTP cache and file protection for anything on disk (§3).
8. Markdown via swift-markdown with an in-app renderer (§8).

---

## 16. As built (2026-10-01)

The app follows this design; the significant differences:

1. **Recording (§9.2).** AVFoundation refuses to encode with `.indefinite` segments, so the app encodes AAC itself (`AVAudioConverter`) and the `AVAssetWriter` runs in passthrough, flushing a segment every 15 s of audio and on pause or interruption. The chunks are exactly the contract in §9.2 (proven against the local backend before anything else was built). A user's pause keeps the same writer, so it does not open a server subsession as the web's resume does.
2. **Environments (§2).** Debug builds default to Local only in the simulator; on a phone they start on Production (a phone cannot reach the Mac's `localhost`). Release builds compile out the switcher and every launch argument.
3. **Voice turn (§9).** The microphone permission is asked before `init` (no orphan drafts on denial); a `cancelled` reply ends the turn; a draft started in desktop Chrome (WebM) cannot be continued natively and says so; the Voice screen's proposal card is the compact one.
4. **Navigation (§5, §6).** `/@user/slug` permalinks open the native thread for a signed-in member (resolved through `GET /api/commons/permalink`), as the web does; Commons cards open the thread by id. A pasted sign-in link opens its landing page (`/welcome`, `/confirm-email`). Search opens from the Log and References magnifiers (⌘K there with a hardware keyboard), not app-wide. Logout asks for confirmation.
5. **Feature pages (§10).** Version history is a sheet (list, then detail) instead of a side drawer; Profile, Todo and artifacts switch in place under the bubble row; Import refuses files over 200 MB before uploading and explains a confirm that outlives nginx's 60 s.
6. **Design system (§7).** Colours are code-defined dynamic colours rather than asset-catalog sets; Outfit ships as the upstream static TTFs (the variable file's instances have no PostScript names iOS can select).
7. **Not built:** the admin-only `SemanticNeighbors` rail and read rerun controls in the thread (admins can use the web for those).

Verification so far is the simulator against the local backend (334 unit tests, XCUITest flows, side-by-side screenshots with the web). The device checklist and the staging checklist are in `ios/README.md`.
