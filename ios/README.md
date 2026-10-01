# Loore for iPhone

A native SwiftUI app with the same features as the desktop web app, against the
same backend. The design is in [`docs/IOS-APP-DESIGN.md`](../docs/IOS-APP-DESIGN.md);
the milestone log, deviations from the web and known gaps are in
[`PROGRESS.md`](PROGRESS.md).

## Requirements

- macOS with **Xcode 26** (iOS 26 SDK; the app runs on iOS 17 and later).
- **XcodeGen**: `brew install xcodegen`. The Xcode project is generated from
  `ios/project.yml` and is not committed.

If `xcode-select` points at the Command Line Tools, prefix Xcode commands with
`export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer;` or run
`sudo xcode-select -s /Applications/Xcode.app` once.

## Build and run in the simulator

```sh
cd ios
xcodegen                 # after every pull that adds or removes files
open Loore.xcodeproj     # then pick an iPhone simulator and press Run
```

Command line:

```sh
cd ios && xcodegen
xcodebuild -project Loore.xcodeproj -scheme Loore \
  -destination 'platform=iOS Simulator,name=iPhone 17' build
```

In the simulator a Debug build talks to the local Docker backend
(`make dev`: `http://localhost:5010`). On a phone a Debug build talks to
production. Switch backends in a Debug build: More → Account → touch and hold
the version line.

## Install on your iPhone (free Apple ID)

1. Xcode → Settings → Accounts → **+** → Apple ID. Sign in; a "Personal Team" appears.
2. Find your team id: Xcode → Settings → Accounts → select the team (the 10-character id),
   or leave step 3 out and pick the team in the target's Signing tab each time.
3. Create `ios/Config/Signing.local.xcconfig` (git-ignored, survives `xcodegen`):
   ```
   DEVELOPMENT_TEAM = ABCDE12345
   // Only if Xcode says the bundle id is taken:
   // LOORE_BUNDLE_ID = org.loore.app.peter
   ```
4. `cd ios && xcodegen && open Loore.xcodeproj`.
5. Connect the iPhone by cable (or the same Wi-Fi after the first pairing), pick it as
   the run destination, press Run. The first time:
   - iPhone: Settings → Privacy & Security → **Developer Mode** → on, restart.
   - iPhone: Settings → General → VPN & Device Management → your Apple ID → **Trust**.
6. For everyday use, build Release (production backend, no debug tools):
   Product → Scheme → Edit Scheme → Run → Build Configuration → **Release**.

A free Apple ID's signature lasts **7 days**: after that the app does not open
until you press Run again from Xcode. A paid developer membership lifts this and
adds TestFlight.

## Signing in

The backend only knows cookies, and email sign-in links open the web. So:

1. In the app: **Sign in with Email**, enter your address, **Send Sign-in Link**.
2. In Mail on the phone: touch and hold the sign-in button → **Copy Link**.
3. Back in the app: paste it (the paste button, or into the field) → **Sign in**.

A link that *creates* an account works once: if it was opened in Safari first,
send a new one. Links last 15 minutes. **Sign in with X** runs X's login inside
the app.

The sign-in lasts 30 days, as on the web, then you sign in again.

### Local backend (development)

The simulator reaches the Mac's `localhost`. Helpers for the **test user only**
(user 5, `seowriter`; other local users hold real data):

```sh
ios/scripts/local_backend.sh magic-link       # a fresh sign-in link to paste
ios/scripts/local_backend.sh session-cookie   # for -LooreSessionCookie
ios/scripts/local_backend.sh state terms_old  # see the script for test states
xcrun simctl pbcopy booted <<< "$(ios/scripts/local_backend.sh magic-link)"   # onto the simulator's clipboard
```

A phone on the same Wi-Fi can use the local backend too: set
`loore.debug.localBackendURL` / `loore.debug.localFrontendURL` (e.g.
`http://Peters-Mac.local:5010` and `:3001`) with `-loore.debug.localBackendURL <url>`
launch arguments, and allow Local Network access when iOS asks.

## Debug launch arguments

Debug builds only (Edit Scheme → Run → Arguments, or `xcrun simctl launch`):

| Argument | Effect |
|---|---|
| `-LooreEnvironment local\|staging\|production` | backend for this launch |
| `-LooreSessionCookie <value>` | inject a Flask `session` cookie (skips sign-in) |
| `-LooreRoute /node/123` | open a screen after sign-in (any web path) |
| `-LooreTheme light\|dark` | force the theme for this launch |
| `-LooreResetState YES` | forget stored cookies and preferences |
| `-LooreSkipUpdates YES` | don't show the Updates sheet |
| `-LooreDebugAudioFile <path>` | feed an audio file to the recorder instead of the mic (from M3) |

## Tests

```sh
cd ios && xcodegen
xcodebuild -project Loore.xcodeproj -scheme Loore \
  -destination 'platform=iOS Simulator,name=iPhone 17' test      # unit tests
python3 ios/scripts/check_terms_text.py                          # Terms text == TermsModal.js
```

CI (`.github/workflows/ios.yml`) runs both on pull requests that touch `ios/**`.

### UI smoke tests (local backend)

Not run in CI: they need the Docker backend. Inputs go through `TEST_RUNNER_`
environment variables:

```sh
cd ios
TEST_RUNNER_LOORE_MAGIC_LINK="$(scripts/local_backend.sh magic-link)" \
TEST_RUNNER_LOORE_SESSION_COOKIE="$(scripts/local_backend.sh session-cookie)" \
TEST_RUNNER_LOORE_SCREENSHOT_DIR=/tmp/loore-shots \
xcodebuild -project Loore.xcodeproj -scheme LooreUITests \
  -destination 'platform=iOS Simulator,name=iPhone 17' test
```

`testTermsGateAccept` and `testUpdatesSheetTakeALookThenGotIt` also need
`TEST_RUNNER_LOORE_EXPECT=terms` / `updates` and the matching `state` first
(`terms_old`, `add_notification`); restore with `state restore del_notifications`.

M2 flows (thread, writing, Log, search, proposals) are in `ThreadWritingUITests`,
`WritingFlowUITests` and `M2ScreensUITests` (run one class with
`-only-testing:LooreUITests/<Class>`). They cancel every dialog and delete what
they create. Steps that bill the backend's AI keys run only with
`TEST_RUNNER_LOORE_ALLOW_BILLED=1`: pick the cheapest model, and note that the
auto-generate path uses the test user's *preferred* model (as the web does).
Some take node ids of the test user: `TEST_RUNNER_LOORE_THREAD_IDS`,
`TEST_RUNNER_LOORE_SHARE_NODE`, `TEST_RUNNER_LOORE_DRAFT_NODE`,
`TEST_RUNNER_LOORE_REPLY_NODE`. Craft mode is switched on through More and
back off at the end; if a run stops halfway, switch it off again (More → Craft mode).

## Only a real iPhone can test

For M3 onwards (design doc §12):

- [ ] A voice turn with the phone locked: the reply starts playing by itself.
- [ ] AirPods: recording over HFP, the reply over A2DP.
- [ ] A phone call during a recording.
- [ ] Lock-screen controls in each phase (recording, thinking, playback).
- [ ] A 60-minute recording.
- [ ] Background upload finishing after the app was killed.
- [ ] Thinking-cue volume settings (Soft / Very soft / Off).
- [ ] Sign in with X (needs a real X account).

## Fonts

Cormorant Garamond (variable TTFs from the Google Fonts repository) and Outfit
(static TTFs from the upstream Outfitio/Outfit-Fonts repository), both under the
SIL Open Font License 1.1; the licence files are in `Loore/Resources/Fonts/`.
