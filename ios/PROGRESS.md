# iOS app: progress and hand-off

The spec is [`docs/IOS-APP-DESIGN.md`](../docs/IOS-APP-DESIGN.md) (§13 milestones).
The web app's behaviour is mapped in `ios/docs/web-app-map/A–E`. Precedence: for
how the web behaves, the web code wins; for what the app should do, the design doc.

| Milestone | Branch | Status |
|---|---|---|
| M1 Foundation | `ios-app` | **done** (2026-10-01) |
| M2 Thread and writing | `ios-app` | next |
| M3 Voice and audio | `ios-app-voice` | after M2, parallel with M4 |
| M4 Feature pages | `ios-app-pages` | after M2, parallel with M3 |
| M5 Integration and parity | `ios-app` | last |

---

## How to work on this (read first, M2 agent)

**Environment**
- Worktree `.claude/worktrees/ios-app`, branch `ios-app`. Draft PR "Native iPhone app (SwiftUI)".
- Xcode 26.3 is at `/Applications/Xcode.app` but `xcode-select` points at the CLT and sudo
  is unavailable: prefix every Xcode command with
  `export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer;`.
- `simctl` commands that boot or install need the sandbox disabled in the agent shell.
- If `xcrun simctl list runtimes` is empty although `xcrun simctl runtime list` shows the
  iOS 26.3 disk image "Ready", run `xcrun simctl runtime scan-and-mount` (sandbox off) once.
- Simulator used for M1: "Loore iPhone 17" (iPhone 17, iOS 26.3),
  `CDAD3013-73D9-4F7F-A37B-C376806BC727`. It may be gone later; create one with
  `xcrun simctl create "Loore iPhone 17" com.apple.CoreSimulator.SimDeviceType.iPhone-17 com.apple.CoreSimulator.SimRuntime.iOS-26-3`.

**Build and test**
```sh
cd ios && xcodegen          # always after adding/removing files
xcodebuild -project Loore.xcodeproj -scheme Loore -destination 'platform=iOS Simulator,id=<UDID>' \
  -derivedDataPath build/DerivedData test          # 106 unit tests, ~3 s of test time
python3 ios/scripts/check_terms_text.py            # from the repo root
```
`build/` is git-ignored. Install + launch for screenshots:
`xcrun simctl install <UDID> build/DerivedData/Build/Products/Debug-iphonesimulator/Loore.app`,
`xcrun simctl launch <UDID> org.loore.app -LooreEnvironment local -LooreResetState YES -LooreSessionCookie "$(ios/scripts/local_backend.sh session-cookie)" -LooreTheme dark -LooreSkipUpdates YES`,
`xcrun simctl io <UDID> screenshot <scratch>/x.png`.

**Signing in on the simulator (test user 5 `seowriter` only)**
- Fastest: `-LooreSessionCookie "$(ios/scripts/local_backend.sh session-cookie)"`.
- The real flow: `ios/scripts/local_backend.sh magic-link` → paste in the app
  (Sign in → "I already have a sign-in link"), or run the UI smoke test
  `testSignInWithPastedMagicLink` (README "UI smoke tests").
- Test states (terms out of date, unapproved, an unread notification):
  `ios/scripts/local_backend.sh state …`; always finish with `state restore del_notifications`.
- User 5 has an email address already (not changed by M1), is approved, plan alpha,
  `share_v1_enabled` true, not admin, no X login. Its content is test text (rivers, glaciers).
- Web reference screenshots: headless Chrome with a throwaway profile and the same session
  cookie on `localhost:3001` (M1 drove it over CDP with a small Node script, kept out of the repo).

**Where things are**
```
ios/project.yml                 XcodeGen spec (packages: swift-markdown 0.7.3, ZIPFoundation 0.9.20)
ios/Config/Signing.xcconfig     team id / bundle id (+ git-ignored Signing.local.xcconfig)
ios/Loore/App/                  LooreApp, AppState, RootView (gating), MainTabView (tabs, RouteDestination),
                                Router, AppRoute (web path → route), AppEnvironment, LaunchOptions
ios/Loore/Core/Networking/      APIClient (+APIRequest, MultipartFormData), APIError, APIPaths, SSE, Poller
ios/Loore/Core/Auth/            AuthService, CookieVault, KeychainStore, MagicLink, WebLoginView (X)
ios/Loore/Core/Models/          LooreDate, JSONValue, OpenEnum (+enums), User, Nodes, Replies, Drafts,
                                Updates, StreamEvents, DecodingHelpers
ios/Loore/Core/Util/            DateFormatting (date.js port), SpendCap (spendCap.js port)
ios/Loore/DesignSystem/         Tokens (colours, spacing, radii, motion, FadeIn), Typography, Theme,
                                Components/ (Buttons, Surfaces, Feedback [dialogs, toasts, pill switch,
                                spend banner], Glyphs [SVG paths, logo, craft icon, X logo],
                                SimpleMarkdownText)
ios/Loore/Features/             Onboarding (SignIn, Terms + TermsText, Waitlist + PrefillConsentCard),
                                Updates (UpdatesSheet), Home (HomeView, PlaceholderScreen), More,
                                Account (placeholder + debug environment switcher), Web (Safari, cookie web view)
ios/LooreTests/                 unit tests + Fixtures/ (captured from user 5, trimmed, email scrubbed)
ios/LooreUITests/               smoke flows against the local backend (not in CI)
ios/scripts/                    check_terms_text.py, local_backend.sh
```

**Conventions the next milestones should keep**
- Paths: add every endpoint to `APIPath` (it owns the trailing-slash rule; `APIPath.canonical`).
- Calls: `try await app.api.get/post/put/patch/delete(path, json:)` returning a `Decodable`;
  `EmptyResponse` when the answer is ignored; `api.fireAndForget` for the web's `.catch(() => {})`.
  Status polls: `get(..., poll: true)` (no-cache, 10 s timeout) inside `Poller.run/runStatus`.
- Errors: catch `APIError`; `.userMessage(fallback:)` gives the server's words. 401/402/403-not-approved
  are handled globally (sign-out, spend banner, re-gate) — call sites just stop.
- SSE: `app.sse.subscribe(path:resumeQuery:)` → `SSEMessage`; decode events with
  `LLMStreamEvent(e)`, `TTSStreamEvent(e)`, `TranscriptionStreamEvent(e)`; `LastChunkTracker` for `?last_chunk=`.
  A `.response(status, body)` message means the endpoint answered JSON instead of streaming.
- Models: explicit `CodingKeys`, `c.tolerant(...)` for every non-id field, open enums
  (`AIUsage.off` is the wire value "none"). Never use `.convertFromSnakeCase` (it rewrites
  dictionary keys such as artifact kinds and source names).
- Navigation: routes are `AppRoute`; `app.open(route)` / `app.openLink(link)`. To land a native
  screen, add its case to `RouteDestination` (MainTabView.swift) and stop using `PlaceholderScreen` for it.
  Tab roots are in `MainTabView` (Artifacts → Profile, Log, Commons are placeholders now).
- UI: tokens only (`LooreColor`, `LooreFont`, `LooreSpacing`, `LooreRadius`, `LooreMotion`);
  cards `.looreCard()`, page titles `PageHeader`, dialogs `.looreDialog(isPresented:)` +
  `LooreDialogCard` + `ChoiceButton`, toasts `app.toasts.show(text, duration:)`,
  `app.notifySpendBlocked()` before refusing a cost action, pills `LoorePill`, tags `LooreTag`.
- Cross-screen signals: `app.signals.post(.todoChanged)` etc.; observe the counters with `.onChange`.
- Per-device preferences use the web's localStorage names (`DefaultsKey`).
- No content in logs (ids, statuses, byte counts only).

**Instructions for M2**
1. Replace `SimpleMarkdownText` (Updates sheet) with the swift-markdown renderer (design §8);
   `import Markdown` is already linked.
2. Home: add the admin-only Read card (`POST /api/read/start`, billed — test once at most).
3. Log tab root → native Log (`GET /api/log`, `LogPage`/`LogCard` are ready, fixture `log.json`).
4. Thread (`.thread(id:awaitLLM:)`): `NodeDetail`/`AncestorNode`/`TreeNode` are ready (fixture
   `node_detail.json`); `LLMStatus`, `LLMStreamEvent`, `SSEClient`, `Poller` are ready for replies.
5. Text mode and "Write new entry" (More → craft) currently route to `.textMode` (placeholder).
6. Keep billed calls to a handful; pick the cheapest model in the picker.

---

## Done in M1

- XcodeGen project; Swift 5 mode, iOS 17+, iPhone; Info.plist (audio background mode,
  microphone text, local networking, fonts); app icon from `loore-logo.png`; launch colour.
- CI: `.github/workflows/ios.yml` (XcodeGen → build-for-testing → unit tests on the newest
  available iPhone simulator; also the Terms text check). Independent of `ci.yml`/deploy.
- Environments Local / Staging / Production; Debug-only switcher (Account → hold the version line).
- Design system: tokens for dark and light, bundled fonts with Dynamic Type, reusable components.
- Networking: `APIClient`, `APIError`, tolerant dates, canonical slashes, SSE client, poller.
- Models for the user/dashboard, nodes (3 shapes), log, LLM/TTS/transcription status,
  drafts and recording sessions, voice/text-mode starts, updates, SSE payloads.
- Auth: magic link sent from the app and pasted back; Sign in with X in a web view;
  `CookieVault` in the Keychain honouring the 30-day expiry; logout clears Keychain,
  cookie jar, web-view data and caches.
- Gating: waitlist (AlphaThankYou: email form, pending state, resend, prefill consent card,
  "What happens next", About/Logout menu); blocking Terms (verbatim, checked by script);
  Updates sheet once per launch (notifications, polls with the two-step opt-in, changelog);
  spend-cap flag from `spend_blocked`; time-zone PATCH for approved users.
- Tab shell with Home (greeting, Voice/Text/Share cards) and placeholders that open the web
  page in a cookie-injected web view meanwhile.
- More: Import · Admin (web view, admins) · Account · My public page (flag) · References ·
  Light mode · Craft mode (+ dialog, toast, glow, persistence) · Write new entry · Export
  data (share sheet) · Prompts · About (Safari views) · Logout.
- Debug launch arguments (`LaunchOptions`), `ios/README.md`, this file.

### Verified on the simulator (iPhone 17, iOS 26.3, local Docker backend, user 5)
- Unit tests: 106 pass (dates, date.js/spendCap.js ports, error mapping, paths, API client
  headers/bodies/events, SSE parser and client incl. reconnect/stall/JSON answers, poller,
  model decoding against fixtures, cookie vault, magic-link parsing, web-login routing,
  routes, launch options, fonts, colours, SVG parser, theme, Terms structure).
- UI smoke tests (all pass): pasted magic link → Reflect, then every tab; `-LooreSessionCookie`
  launch → More → craft dialog on/off → light mode → Account; Terms gate shown and accepted
  (DB back at 2.0); Updates sheet "Take a look" (skip + navigate) then "Got it" (read);
  "Open on the web" shows the signed-in web Log (cookie injection); Logout → sign-in, and a
  relaunch stays signed out.
- Manually via `simctl`: waitlist for an unapproved user 5 (no timezone PATCH while
  unapproved), timezone PATCH UTC → Europe/Prague once approved, `-LooreRoute /node/…`.
- Side-by-side with headless-Chrome screenshots of the web: login, Home, ⋮ menu vs More,
  craft dialog, alpha-thank-you.
- The backend accepted the app's cookies everywhere (magic-link 302 cookies, injected session).
- No billed calls were made.

## Deviations from the web

- **Magic link is pasted into the app** (design §4): extra "I already have a sign-in link"
  step and paste field, plus a note that sign-up links work once.
- **More is a tab/screen**, not a dropdown; it also lists **References** (the web has no entry).
  Menu rows use `text-secondary` instead of the dropdown's `text-muted` for legibility on a full screen.
- **Logout asks for confirmation** (the web logs out on click): re-signing in on a phone
  means fetching a new email link.
- **Craft dialog copy** says "in More" where the web says "in the ⋮ menu" (no ⋮ menu in the app).
- **Terms screen**: same text; the card scrolls inside a dimmed backdrop like the web modal.
  Bold runs are Outfit 400 in `text-primary` (the web's `<strong>` on a 300 body).
- **Updates sheet** is a bottom sheet (half height for a single notification) instead of a
  centred card; external links open in Safari.
- **Home**: cards stack vertically on a phone (as the web does at that width); no Read card yet (M2).
- **Tab icons**: the Reflect tab uses the Loore mark; others are outline SF Symbols. No counts or badges.
- **Debug builds on a device default to Production** (simulator: Local); the design says Debug → Local.
- **Colours are code-defined dynamic `UIColor`s** (`Tokens.swift`) instead of asset-catalog colour
  sets (the launch colour and AccentColor are in the catalog).
- **Fonts**: Outfit comes from the upstream project's static TTFs, not the Google Fonts variable
  file (its named instances have no PostScript names, so iOS cannot select weights).
- **Placeholders** offer "Open on the web" (authenticated web view) until each native screen lands.
- **SimpleMarkdownText** renders changelog bodies until the M2 renderer exists (no lists nesting,
  tables or code blocks).

## Known gaps (after M1)

- **Sign in with X** is built but untested (needs X credentials and a real X account); it runs in a
  non-persistent `WKWebView` and ends when the web view reaches the frontend origin.
- Waitlist email form not exercised visually (user 5 already has a confirmed address; M1 did not
  change it). The code path mirrors `AlphaThankYouPage.js` and posts `POST /api/dashboard/email`.
- Spend-cap banner not seen on screen (no capped user locally); the 402 mapping and event are unit-tested.
- Poll items in the Updates sheet not exercised against the backend ("Draft with AI" is billed).
- Admin web view opens `/admin` with the session cookie; not viewed as an admin (user 5 is not one,
  and admin pages show other users' data).
- Email confirmation by pasted `/confirm-email` link (design §4.6) belongs to the Account screen (M4).
- `ProfileGenerationWatcher` (app-wide profile-progress poller) is M4.
- Not built in M1 (by plan): thread, writing, markdown, replies, voice, feature pages, search.
- XCUITest flows need the local backend and are not in CI.
- The first two M1 commits carry their `Co-Authored-By`/`Claude-Session` lines mid-message
  (a quoting slip; not rewritten, per the no-amend rule).
