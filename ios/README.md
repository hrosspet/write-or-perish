# Loore for iPhone

A native SwiftUI app with the same features as the desktop web app, against the
same backend. Why native: on a locked iPhone, Safari cannot start a reply's audio
by itself; the app keeps its audio session alive from the record tap to the end of
the reply. The design is in [`docs/IOS-APP-DESIGN.md`](../docs/IOS-APP-DESIGN.md);
the milestone log, the parity table, deviations from the web and known gaps are in
[`PROGRESS.md`](PROGRESS.md).

Contents: [Requirements](#requirements) · [Simulator](#build-and-run-in-the-simulator) ·
[Install on your iPhone](#install-on-your-iphone-free-apple-id) · [Signing in](#signing-in) ·
[Backends](#backends) · [Debug launch arguments](#debug-launch-arguments) · [Tests](#tests) ·
[Device checklist](#only-a-real-iphone-can-test) · [Staging checklist](#check-on-staging) · [Fonts](#fonts)

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

In the simulator a Debug build talks to the local Docker backend (`make dev`:
`http://localhost:5010`). Sign in as the test user with the helpers under
[Backends](#backends).

## Install on your iPhone (free Apple ID)

A free Apple ID is enough. It gives a "Personal Team" whose builds run for 7 days
(then press Run again) and allows three such apps on a phone at a time. A paid
membership lifts the 7-day limit and adds TestFlight and push notifications; the app
needs neither.

1. **Add your Apple ID to Xcode.** Xcode → Settings → Accounts → **+** → Apple ID →
   sign in. A team named "<your name> (Personal Team)" appears.
2. **Find your team id.** `cd ios && xcodegen && open Loore.xcodeproj`, select the
   **Loore** target → **Signing & Capabilities** → Team: pick your Personal Team. Then
   **Build Settings** → search "Development Team": the 10-character value is your team
   id. (XcodeGen overwrites this choice the next time it runs; step 3 keeps it.)
3. **Save your signing settings** in `ios/Config/Signing.local.xcconfig` (git-ignored,
   survives `xcodegen`):
   ```
   DEVELOPMENT_TEAM = ABCDE12345
   // Only if Xcode says the bundle id is not available:
   // LOORE_BUNDLE_ID = org.loore.app.yourname
   ```
   Run `xcodegen` again and reopen the project.
4. **Connect the iPhone** by cable, unlock it and tap **Trust** on "Trust This
   Computer?". In Xcode pick the iPhone as the run destination (top bar).
5. **Turn on Developer Mode** (once): on the iPhone, Settings → Privacy & Security →
   **Developer Mode** → on, then restart and confirm. The switch appears only after
   Xcode has seen the phone.
6. **Choose the build.** For everyday use build **Release** (production backend, no
   debug tools): Product → Scheme → Edit Scheme… → Run → Build Configuration →
   **Release**. Keep **Debug** for the device checklist when you want the debug tools
   (on a phone it also starts on production; see [Backends](#backends)).
7. **Press Run.** The first launch stops at "Untrusted Developer": on the iPhone,
   Settings → General → **VPN & Device Management** → your Apple ID → **Trust**, then
   open Loore from the home screen.
8. **Allow the microphone** at the first recording and **notifications** when asked
   (they say "Recording paused — tap to resume" after an interruption). At the first
   voice recording the lock screen asks **"Allow Live Activities from Loore?"**: tap
   Allow, or the recording controls fall back to the Now Playing ones.

The app has a widget extension (`LooreLiveActivity`, the voice Live Activity) signed
with the same team; its bundle id is the app's plus `.LiveActivity`, and Xcode creates
it on the first Run.

After 7 days the app stops opening: connect the phone and press Run again (nothing is
lost; your writing lives on the server). After the first cable install Xcode can also
reach the phone over the same Wi-Fi.

## Signing in

The backend only knows cookies, and email sign-in links open the web. So:

1. In the app: **Sign in with Email**, enter your address, **Send Sign-in Link**.
2. In Mail on the phone: touch and hold the sign-in button → **Copy Link**.
3. Back in the app: paste it (the paste button, or into the field) → **Sign in**.

A link that *creates* an account works once: if it was opened in Safari first,
send a new one. Links last 15 minutes. A link minted for another page (the Activate
& Welcome email's `/welcome`, an email confirmation) opens that page once you are
in. **Sign in with X** runs X's login inside the app.

The sign-in lasts 30 days, as on the web, then you sign in again.

## Backends

| Build | Simulator | iPhone |
|---|---|---|
| Release | Production (`loore.org`) | Production |
| Debug | Local (`localhost:5010`) | Production |

In a Debug build, switch backends with More → Account → touch and hold the version
line (Local / Staging / Production; switching signs out). Release builds have no
switcher and ignore every launch argument.

**Local backend (development).** The simulator reaches the Mac's `localhost`.
Helpers for the **test user only** (user 5, `seowriter`; other local users hold real
data, so never sign in as them):

```sh
ios/scripts/local_backend.sh magic-link            # a fresh sign-in link to paste
ios/scripts/local_backend.sh magic-link /welcome   # … that lands on a page
ios/scripts/local_backend.sh session-cookie        # for -LooreSessionCookie
ios/scripts/local_backend.sh state terms_old       # see the script for test states
xcrun simctl pbcopy booted <<< "$(ios/scripts/local_backend.sh magic-link)"   # onto the simulator's clipboard
```

A phone on the same Wi-Fi can use the local backend too: launch a Debug build with
`-loore.debug.localBackendURL http://your-mac.local:5010 -loore.debug.localFrontendURL http://your-mac.local:3001`
(Edit Scheme → Run → Arguments), switch to Local, and allow Local Network access when
iOS asks. The local backend runs with streaming voice TTS off.

**Staging** (`staging.loore.org`): Debug build → switch to Staging → sign in with a
link mailed by staging. Staging's database is recreated on every deploy, so the
account has to exist (and be approved) there first.

## Debug launch arguments

Debug builds only (Edit Scheme → Run → Arguments, or `xcrun simctl launch`). In a
Release build they are compiled out.

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
| `-LooreDebugListenNode <id>` | play a node's audio in the global player at launch (the speaker icon's path; billed if the node has no audio yet) |
| `-LooreDebugImportFile <path>` + `-LooreDebugImportKind markdown\|claude\|chatgpt\|twitter` | Import page: a "Debug: import …" button that imports that file instead of opening the file picker |

## Tests

```sh
cd ios && xcodegen
xcodebuild -project Loore.xcodeproj -scheme Loore \
  -destination 'platform=iOS Simulator,name=iPhone 17' test      # 414 unit tests
python3 ios/scripts/check_terms_text.py                          # Terms text == TermsModal.js
```

CI (`.github/workflows/ios.yml`) runs both on pull requests that touch `ios/**`.

### UI tests (local backend)

Not run in CI: they need the Docker backend and the test user. Inputs go through
`TEST_RUNNER_` environment variables; a test whose input is missing skips. Run one
class with `-only-testing:LooreUITests/<Class>`:

```sh
cd ios
TEST_RUNNER_LOORE_MAGIC_LINK="$(scripts/local_backend.sh magic-link)" \
TEST_RUNNER_LOORE_SESSION_COOKIE="$(scripts/local_backend.sh session-cookie)" \
TEST_RUNNER_LOORE_SCREENSHOT_DIR=/tmp/loore-shots \
xcodebuild -project Loore.xcodeproj -scheme LooreUITests \
  -destination 'platform=iOS Simulator,name=iPhone 17' test
```

| Class | What it walks | Extra inputs |
|---|---|---|
| `SmokeFlowsUITests` (M1) | pasted magic link → tabs; More, craft dialog, light mode, Account; Terms gate; Updates sheet | `LOORE_EXPECT=terms` / `updates` with `state terms_old` / `add_notification` first; then `state restore del_notifications` |
| `ThreadWritingUITests`, `WritingFlowUITests`, `M2ScreensUITests` (M2) | Log, threads, kebabs, writing, drafts, proposals, search | `LOORE_THREAD_IDS`, `LOORE_SHARE_NODE`, `LOORE_DRAFT_NODE`, `LOORE_REPLY_NODE`; billed steps only with `LOORE_ALLOW_BILLED=1` |
| `M4ScreensUITests` (M4) | Todo, Profile, artifacts, references, prompts, Account, Confirm email, import, Share, Welcome | `LOORE_IMPORT_DIR`, `LOORE_KEEP_SHARE_IDS`, `LOORE_PICKER_FILE` (a markdown zip in the simulator's Files → On My iPhone, for the real file picker); a test user with no todo or profile; afterwards `state m4_cleanup` |
| `M5ParityUITests` (M5) | signed-out About link, a sign-in link landing on Welcome, a permalink opening the native thread, a toast above the mini-player | `LOORE_WELCOME_LINK` (`magic-link /welcome`), `LOORE_PERMALINK` (a live `/@user/slug` of the test user), `LOORE_LISTEN_NODE` (a node whose audio already exists) |
| `VoiceUITests`, `VoiceWiringUITests` (M3) | a voice turn, Continue; speaker, download, dictation, Voice Mode from a thread | billed, see below |
| `NodeOpeningUITests` (#447) | Text → Send (auto-generate off), then the same entry from the Log: each time the screen stays with the spinner and the thread opens with no "Loading node..." page | `LOORE_BACKEND_URL=http://localhost:5099` with `python3 scripts/delay_proxy.py 5099 0.8` running (node GETs held 0.8 s; without a delay the spinner is too short to see). The test user's sent entries stay in the Log |
| `TextModeDraftUITests` (#425, #427) | Reflect home → Text → Send (auto-generate off) or Discard → back → Text again: the text must not come back as the draft | Send right after typing: `LOORE_BACKEND_URL=http://localhost:5099` with `python3 scripts/delay_proxy.py` running (production-like latency; without it the race cannot fail locally). Send or Discard next to a dead Voice session: `state clear_top_drafts dead_voice_session` before each test. The test user's sent entries stay in the Log |

Notes: the M2 tests cancel every dialog and delete what they create; craft mode is
switched on through More and back off (if a run stops halfway, switch it off again).
The auto-generate path uses the test user's *preferred* model, as the web does, so
pick the cheapest one before a billed run. M4 imports run with AI usage None, so no
profile update starts; the backend has no delete for profiles, todos or artifacts,
which is what `state m4_cleanup` is for. M4's import test needs `LOORE_IMPORT_DIR`
holding `notes.zip` (two `.md` files containing "M4 import test"),
`chatgpt-renamed.zip` (a ChatGPT conversations array under another name) and
`notazip.zip`. No test opens the Commons tab locally: it lists other users' public
posts.

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
recording (PROGRESS.md, "Backend findings"). Recording with the real microphone in
the simulator makes macOS ask for microphone access for Simulator.

## Only a real iPhone can test

The simulator cannot lock, has no Bluetooth or phone calls, never suspends the app
the way a phone does, and draws emoji as "?" boxes. Voice turns are billed
(transcription, reply, TTS): use short recordings. Before each item: the app on the
phone (Release, or Debug for the debug tools), signed in, voice mode enabled,
Account → Voice → "Sound while Loore thinks" on Soft.

**Voice and audio (the reason for the app)**
- [ ] **Locked phone, reply by itself.** Reflect → Voice → record ~10 s → press
      the side button to lock → stop with ■ on the Loore Live Activity. Expected: a
      soft low swell while it thinks (the activity says "Thinking…", Now Playing
      "Voice…"), then the reply plays without touching the phone; Now Playing shows
      "Voice" with play/pause and ±10 s, the activity "Loore is replying" with a mic button.
- [ ] **Locked phone, pause / resume / stop (#397).** Record, lock. The Live Activity
      shows "Recording m:ss" with ‖ and ■, and no Now Playing controls. ‖ → "Paused
      m:ss" with ▶ and ■; ▶ → the clock runs again and the transcript later holds both
      parts; ■ → the reply plays by itself. Repeat the pause a minute long.
- [ ] **Record a reply from the lock screen (#397).** While the reply plays (or after
      it ends, "Reply finished"), tap the mic on the Live Activity: the reply stops and a
      new recording starts without unlocking ("Starting…", then the clock); ■ sends it
      into the same thread. Then unlock and leave Voice: the activity goes away.
- [ ] **Lock-screen Record edge cases (#397).** (a) After two lock-screen turns,
      check the second transcript holds the whole recording (iOS requires a Live
      Activity while an intent records; the app reuses the conversation's one).
      (b) During a recording started from the lock screen, swipe the activity away:
      does recording continue (Now Playing controls appear) or does iOS stop the
      microphone? Note which. (c) Airplane mode, then Record on the activity: the
      reply keeps playing and a notification says you're offline. (d) Force-quit
      Loore while the activity shows "Reply finished", then tap its mic: the card
      goes and a notification says Loore was closed.
- [ ] **Thinking cue volumes.** Account → Voice → Very soft: quieter, same flow. Off
      (the warning appears): repeat the locked turn; silence while thinking, iOS may
      suspend the app and the reply may need a tap after unlocking (the app catches
      up when it comes back). Set it back to Soft.
- [ ] **AirPods.** Connect AirPods, record (mic over Bluetooth HFP), stop. The
      reply should play in good (A2DP) quality, not telephone quality, also while
      locked. If it does not play while locked, note it: the fallback is to stay in
      `.playAndRecord` after Stop (`AudioSessionController.switchToPlaybackAfterRecording`).
- [ ] **Phone call during a recording.** Record, call the phone from another one,
      decline or take the call. Expected: recording pauses, a chime (maybe only after
      the call), a notification "Recording paused — tap to resume", the red message on
      the Voice screen, and the Live Activity "Recording paused · a call or another app
      took the microphone"; Resume (on screen, or ▶ on the activity with the phone still
      locked) continues the same recording; the transcript contains both parts. If ▶ on
      the activity cannot restart the microphone, a notification says so.
- [ ] **Siri / another app takes the mic.** Same as the call: pause + notification.
- [ ] **Lock-screen controls in each phase.** Recording (Live Activity ‖ / ▶ and ■;
      with Live Activities off in Settings → Loore: Now Playing play/pause/⏭), thinking
      (Now Playing shows "Voice…" with every button greyed out; ideally the ±10 s
      layout rather than previous/next: note which), playback (play/pause,
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
- [ ] **Bluetooth headphones on a walk (#423).** Record several voice turns of a few
      minutes with the phone locked in a pocket: the transcripts are complete, and
      the recording logs (below) show the input staying on `BluetoothHFP`. Then
      switch the headphones off mid-recording: the alert plays from the phone, the
      lock screen shows Paused, the notification says so, and Resume continues on
      the phone's mic; the transcript has both parts.

**Recording logs.** Each recording writes a plain-text log on the phone (route
changes, interruptions, microphone restarts, chunks, input level once a second; no
audio or words). Copy them from a build installed from Xcode, with the iPhone
connected (`xcrun devicectl list devices` gives the device id; the bundle id is
yours from `Signing.local.xcconfig`):

```sh
xcrun devicectl device copy from --device <device id> \
  --domain-type appDataContainer --domain-identifier <bundle id> \
  --source "Library/Application Support/RecordingLogs" --destination ./recording-logs
```
- [ ] **Dictation.** In a thread's reply box: Record, lock the phone for 30 s,
      unlock, stop: the transcript lands in the box; "Save audio" shares an `.m4a`.
- [ ] **Listen aloud.** A node's speaker icon plays in the mini-player above the tab
      bar; lock the phone: lock-screen controls work; the mini-player's Stop keeps
      it visible, ✕ closes it.

**Everything else**
- [ ] Sign in by pasting a link from Mail; quit and reopen: still signed in.
- [ ] Sign in with X (needs a real X account).
- [ ] Emoji in entries, replies and the Updates sheet render.
- [ ] VoiceOver: walk Home, Voice, a thread (open a Log card, reply, kebab), Log,
      Profile; every button says what it does.
- [ ] Larger Text (Settings → Accessibility → Display & Text Size) at the largest
      size: Home, Voice, a thread, Log, Profile, Account have no clipped controls.
- [ ] A Release build reaches production and has no "touch and hold" switcher on
      Account's version line.

## Check on staging

Things the local test user has no data for, or that only staging and production
run. Debug build → switch to Staging (see [Backends](#backends)).

- [ ] **Streaming voice TTS** (`STREAMING_VOICE_TTS` is on in staging and production,
      off locally): a voice turn starts speaking while the reply is still being
      written; chapters appear with placeholder titles that are renamed when the reply
      completes; a locked turn still plays by itself.
- [ ] **Commons**: the tab lists public posts, "Load more..." pages, a card opens the
      thread; Share → publish a test draft → it appears in Commons and on My public
      page; a `/@user/slug` link in an entry opens the native thread; then revoke it.
- [ ] **A running profile build**: start one (an import with AI usage Chat or Train,
      or an admin "build profile"): the Profile page shows the progress line
      ("… · n%" or "Chunk n of ~N"), the toast arrives when it finishes, the new
      version is in history.
- [ ] **Read-reply picks** (admin account): Home → Read, and Read further in a read
      thread: the window line, the picks with their quote bubbles, read marks and
      Good/Bad verdicts, "Mark all as read", and the reply box under a read reply.
- [ ] **Real X OAuth**: Account → Connect X → X's login → back with "Connected as
      @…"; Disconnect X. Import → X Bookmarks: Connect X, then Sync bookmarks; the
      references count rises.
- [ ] **YouTube references**: a saved YouTube link shows the player on the
      reference page ("Show stored text" toggles the text).
- [ ] **Default-updated prompt banner** (after a deploy that changes a default
      prompt the user had edited): Prompts shows the dot; the prompt page offers View
      new default / Accept / Dismiss.
- [ ] **Updates sheet** with a real changelog entry (markdown body, internal links).
- [ ] **Spend cap** with a capped test account: the "LIMIT REACHED" banner on a 402,
      and recording refused up front with the toast.

## Fonts

Cormorant Garamond (variable TTFs from the Google Fonts repository) and Outfit
(static TTFs from the upstream Outfitio/Outfit-Fonts repository), both under the
SIL Open Font License 1.1; the licence files are in `Loore/Resources/Fonts/`.
