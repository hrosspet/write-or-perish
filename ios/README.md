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
| `-LooreDebugAudioFile <path>` | feed an audio file to the recorder instead of the mic (voice mode and dictation); the file's end acts as Stop |
| `-LooreDebugVoiceAutoStart YES` | with `-LooreDebugAudioFile` and `-LooreRoute /voice`: start recording at once |
| `-LooreDebugListenNode <id>` | play a node's audio in the global player at launch (the speaker icon's path) |
| `-LooreDebugImportFile <path>` + `-LooreDebugImportKind markdown\|claude\|chatgpt\|twitter` | Import page: a "Debug: import …" button that imports that file instead of opening the file picker |

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

M4 flows (feature pages) are in `M4ScreensUITests`. None of them bills an AI
provider (imports run with AI usage None, so no profile update starts). They
need a test user with no todo and no profile, `TEST_RUNNER_LOORE_IMPORT_DIR`
(a folder with `notes.zip` holding two `.md` files whose text contains
"M4 import test", `chatgpt-renamed.zip` with a ChatGPT conversations array
under another name, and `notazip.zip`), and `TEST_RUNNER_LOORE_KEEP_SHARE_IDS`
(the test user's existing share ids, never deleted). The backend has no delete
for profiles, todos or artifacts: afterwards run
`scripts/local_backend.sh state m4_cleanup`, which removes the test user's
todo and profile rows, the `m4-test` artifact and the imported test notes.

## Only a real iPhone can test

The simulator cannot lock, has no Bluetooth or phone calls, and never suspends
the app the way a phone does. Voice turns are billed (transcription, reply, TTS):
use short recordings. Before each item: a Debug build on the phone (Production
backend by default, or Local over Wi-Fi, see "Local backend"), signed in, voice
mode enabled, Account → Voice → "Sound while Loore thinks" on Soft.

- [ ] **Locked phone, reply by itself.** Reflect → Voice → record ~10 s → press
      the side button to lock → stop from the lock screen (⏭ "next track") or wait
      and stop before locking. Expected: a soft low swell while it thinks
      (lock screen shows "Voice…"), then the reply plays without touching the phone;
      the lock screen shows "Voice" with play/pause and ±10 s.
- [ ] **Locked phone, stop from the lock screen.** Record, lock, then on the lock
      screen: pause (title "Paused m:ss"), play (resumes, "Recording m:ss"),
      ⏭ (stop and send). The reply must play by itself.
- [ ] **Thinking cue off.** Account → Voice → Off (the warning appears). Repeat
      the locked turn. Expected: silence while thinking; iOS may suspend the app,
      and the reply may need a tap after unlocking (the app reconciles on
      foreground). Set it back to Soft. Also try Very soft: quieter, same flow.
- [ ] **AirPods.** Connect AirPods, record (mic over Bluetooth HFP), stop. The
      reply should play in good (A2DP) quality, not telephone quality. If the reply
      does not play while locked with AirPods, note it: the fallback is to stay in
      `.playAndRecord` after Stop (`AudioSessionController.switchToPlaybackAfterRecording`).
- [ ] **Phone call during a recording.** Record, call the phone from another one,
      decline or take the call. Expected: recording pauses, a chime (maybe only after
      the call), a notification "Recording paused — tap to resume", the red message on
      the Voice screen; Resume continues the same recording; the transcript contains
      both parts.
- [ ] **Siri / another app takes the mic.** Same as the call: pause + notification.
- [ ] **Lock-screen controls in each phase.** Recording (play/pause/⏭), thinking
      (only ⏭ = cancel; the server still finishes the reply), playback (play/pause,
      ±10 s, scrubbing, speed from the ⋯ menu where iOS offers it).
- [ ] **60-minute recording.** Record for 59 minutes (phone locked is fine): at
      59:00 a rising two-note chime, a notification and a toast; stop at ~60:00 and
      check the transcript has the whole hour.
- [ ] **Background upload after the app is killed.** Start recording online (the
      first 15 s chunk uploads), turn on airplane mode at ~20 s, record to ~45 s,
      stop. After ~30 s the app says the recording could not be sent and is kept.
      Force-quit Loore, turn airplane mode off, wait a minute (the background
      upload session finishes the queued chunks without the app), open Loore →
      Voice: "Unfinished Voice recording" → Continue → record a few seconds → stop.
      The reply's transcript contains all three parts.
- [ ] **Headphones unplugged / AirPods removed during playback** pause the reply.
- [ ] **Dictation.** In a thread's reply box: Record, lock the phone for 30 s,
      unlock, stop: the transcript lands in the box; "Save audio" shares an `.m4a`.
- [ ] **Listen aloud.** A node's speaker icon plays in the mini-player above the tab
      bar; lock the phone: lock-screen controls work; the mini-player's Stop keeps
      it visible, ✕ closes it.
- [ ] Sign in with X (needs a real X account).

### Voice UI tests in the simulator (billed, run on purpose)

```sh
cd ios
say -o /tmp/clip.m4a --file-format=m4af --data-format=aac "A short test recording about the river."
TEST_RUNNER_LOORE_SESSION_COOKIE="$(scripts/local_backend.sh session-cookie)" \
TEST_RUNNER_LOORE_AUDIO_FILE=/tmp/clip.m4a \
TEST_RUNNER_LOORE_VOICE_ROUTE="/voice?parent=<a test-user node>" \
xcodebuild -project Loore.xcodeproj -scheme LooreUITests \
  -destination 'platform=iOS Simulator,name=iPhone 17' test -only-testing:LooreUITests/VoiceUITests
```

Use a `parent` while other agents or people use the same test user: a text entry
saved elsewhere deletes the user's top-level draft, which can be a live voice
recording (see PROGRESS-voice.md, "Backend findings"). Recording with the real
microphone in the simulator makes macOS ask for microphone access for Simulator.

## Fonts

Cormorant Garamond (variable TTFs from the Google Fonts repository) and Outfit
(static TTFs from the upstream Outfitio/Outfit-Fonts repository), both under the
SIL Open Font License 1.1; the licence files are in `Loore/Resources/Fonts/`.
