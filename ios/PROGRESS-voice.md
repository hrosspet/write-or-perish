# M3 Voice and audio: progress and hand-off

Branch `ios-app-voice` (from `ios-app` at M1, with `origin/ios-app` M2 merged on
2026-10-01, last at `b8417c7`). M5 folds this file into `PROGRESS.md`. Spec: design doc §9, map C
(§8 is the native design).

**Status: done** in the simulator; the locked-phone behaviour, Bluetooth, calls
and real background suspension need the device checklist in `ios/README.md`.

## How to work on it

- Simulator used: "Loore Voice iPhone 17" (iPhone 17, iOS 26.3),
  `F23E45D8-25E5-4A27-A31E-31149C9D540A`. Prefix Xcode commands with
  `export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer;`.
- Unit tests: `cd ios && xcodegen && xcodebuild … -scheme Loore test` (240 tests
  with M2's, one skipped: the format-probe helper).
- A whole voice turn without a microphone: `-LooreDebugAudioFile <clip>` (make one
  with `say -o clip.m4a --file-format=m4af --data-format=aac "…"`). The file plays
  into the recorder at real time; its end acts as Stop. UI tests (billed, local
  backend): `VoiceUITests` (turn, optional Continue), `VoiceWiringUITests`
  (speaker + mini-player + download on a thread; dictation into a reply form;
  "Voice Mode" from a thread). Commands in `ios/README.md`.
- Test user only (user 5). Its `preferred_model` was set to `gpt-6-luna` (the
  cheapest model) for the runs and set back to `claude-opus-5.5` at the end.

## Recording format: proven

`AVAudioConverter` (AAC-LC 64 kbps, 48 kHz mono) → `AVAssetWriter(contentType:
.mpeg4Movie)`, `outputFileTypeProfile = .mpeg4AppleHLS`, segment interval
`.indefinite` in passthrough, `flushSegment()` every 15 s of audio and on
pause/interruption. Chunk 0 = initialization segment + first media segment.

Evidence (local backend, 2026-10-01):
- Chunk 0: 41 829 bytes, top-level boxes `ftyp`(28) `moov`(584) `moof` `mdat`; the
  server's own `extract_mp4_init_segment` returns a 612-byte init. Chunks 1–2: bare
  `moof`+`mdat`, no `ftyp`. The server's `concat_fragmented_media` remux gives one
  AAC 48 kHz mono file (14.9 s of the 15.0 s clip); a batch starting at chunk 1 with
  the persisted init decodes too.
- Upload: three chunks `202 stored`, a duplicate `200 Chunk already uploaded`;
  finalize `202`; `/status` → `completed`, 3/3 chunks, transcript word for word.
- Full turns from the simulator (see below): finalize 202 → `/status` → reply node
  → TTS → playback.

## Done

`ios/Loore/Audio/`
- `Recording/SegmentPackager.swift`: chunk contract; `MP4Boxes` (box walker, the
  server's chunk-0 rule, the `ftyp` subsession test).
- `Recording/FMP4SegmentWriter.swift`: `AACEncoder` + passthrough fMP4 writer +
  `PCMConverter` (any input → 48 kHz mono Int16).
- `Recording/AudioSources.swift`: `MicrophoneSource` (AVAudioEngine tap, restarts
  after a configuration change), Debug `AudioFileSource`.
- `Recording/VoiceRecorder.swift`: source → converter → writer → upload queue;
  pause keeps capture running (drops samples); interruption flushes and holds;
  a failed writer is replaced on resume (its first chunk opens a server subsession).
- `Recording/ChunkUploader.swift`: persisted queue in Application Support
  (`completeUntilFirstUserAuthentication`), chunk 0 first, the web's 2/4/8/16 s
  retries, background `URLSession` copy on give-up and on backgrounding, resumed
  after a relaunch (`AppState.didLoad` → `audio.didSignIn()`), `init_parse_failed`
  fatal, `total_chunks` = stored count when chunks were given up (plus the chunks
  a resumed session already had).
- `Voice/VoiceTurnController.swift`: the turn state machine (C §8.5): init (402 →
  toast), record, stop (cue first, then mic), finalize (400 "not in recording
  state" = success), `/status` every 1 s (never the draft SSE, never before
  finalize), empty transcript / warning / legacy `POST /api/voice`, llm-status
  every 1.5 s, the TTS attach rule, `POST /tts` once on completion, JSON stream
  answers, chains into one queue with chapters (placeholder `…` renamed from
  `all_complete.preview`), drains (cue), REST recovery (#242: trigger watchdog
  20 s, reconcile 7 s + foreground, 3 s catch-up, 60 s net), `cancelled` terminal,
  cancel/continue, 59-minute warning, interruptions (chime, 24 h toast, local
  notification), timing marks. `VoiceTiming` sends them with the clock offset.
- `Voice/VoiceTurnDependencies.swift`: protocols + `LiveVoiceBackend`.
- `Playback/ChunkQueuePlayer.swift` + `ChunkQueueMath.swift`: `AVQueuePlayer` queue,
  cumulative time, seek across chunks, chapters, rates 1/1.25/1.5/2 (time-domain
  pitch), duration correction, dedupe by URL, cookies on every media request
  (`AVURLAssetHTTPCookiesKey`), `.mp4` MIME override.
- `Playback/Sounds.swift`: the thinking cue (G3+D4 swell, 4 s loop, −24 dBFS
  file; Soft ≈ −30, Very soft ≈ −38 dBFS) and the web's chimes, generated in code,
  played on a private queue; the cue player is prepared at the record tap.
- `Playback/ListenAloud.swift`: the SpeakerIcon logic (recording first, chunked
  recordings as a queue, TTS + chapters, else `POST /tts` + stream) and
  `AudioDownloader` (DownloadAudioIcon logic, web file names).
- `Session/AudioSessionController.swift`: `.playAndRecord` at the record tap,
  `.playback`/`.spokenAudio` after Stop and for listening,
  `setPrefersNoInterruptionsFromSystemAlerts` while recording, interruption /
  route / media-reset events. `Session/NowPlayingController.swift`: lock screen
  per phase (recording, thinking, playback) with remote commands.
- `Dictation/DictationController.swift`: recording into a writing form (no label,
  draft SSE for live text, finalize, `/status` after finalize).
- `AudioCenter.swift`: owns all of it (`AppState.audio`); routes interruptions;
  the background-session app delegate (`LooreAppDelegate`).

`ios/Loore/Features/Voice/`: `VoiceView` (VoicePage parity: ready/recording/
Thinking/playback, ECG and waveform, recording timer, interruption text, error dot,
offline notice, player with ±10 s, progress bar with chapter ticks, chapter list
with Roman numerals, proposal card, Continue, Text Mode, recovery banner),
`MiniPlayerView` (global player above the tab bar, hidden on Voice),
`VoiceSettingsSection` (Account → Voice), `NodeAudioButtons` (`SpeakerButton`,
`DownloadAudioButton`), `StreamingMicButton` (dictation button with the web's states).

Shared-file edits: `AppState` (`audio`, `didSignIn`, sign-out), `MainTabView`
(`.voice` route, mini-player inset), `LooreApp` (app delegate adaptor),
`AccountView` (one line), `LaunchOptions` (docs). M2 hooks filled:
`NodeAudioControls` (ThreadSheets.swift), `DictationButton` (NodeFormView.swift).

## Verified

- Unit tests (new): fMP4 packaging and a real encode (chunk 0 = init + first
  segment, later chunks without `ftyp`, durations, flush on pause); the turn state
  machine against a fake API (19 cases: happy path incl. cue-before-mic order and
  timing marks, continue, chains, drains, JSON stream answer, POST /tts 200, empty
  transcript, warning, spend cap, mic failure, reply failure, fatal upload,
  given-up chunks, retried finalize, cancel ignores late events, interruption,
  59-minute warning, resumed draft, WebM draft); queue maths; uploader (order,
  cookies, retries, offline degrade, fatal, duplicate, relaunch); sounds; lock-screen
  command mapping.
- Simulator + local backend (user 5), with screenshots in the scratch directory:
  - Turn 1 (`VoiceUITests`, reply under node 201104): recording → Thinking → the
    reply played without a tap (`Pause`, 0:08 after 8 s) → end 0:13/0:13 → Continue.
  - Turns 2 and 3 (Continue): app log shows `thinking cue on` at Stop and
    `thinking cue off` when the first chunk played (6.3 s and 8.4 s later); both
    replies started by themselves. Server timing (Stop → playing): 9.7 s, 6.3 s,
    8.4 s; transcription 1.7–2.8 s; first chunk ready → playing 0.05–0.27 s.
  - Thread footer: speaker → mini-player plays the reply; download → share sheet
    with `node-201178-tts.mp3`; "Voice Mode" on a thread reply → Voice screen plays it.
  - Dictation from the debug file into a reply form: transcript (279 characters) in
    the editor; draft discarded.
  - Recovery banner ("Unfinished Voice recording", Continue / Discard) for a real
    interrupted draft; Account → Voice with the Off warning.

Billed calls: 3 full voice turns (transcription + `gpt-6-luna` reply + TTS), plus
2 transcription-only calls (the format probe, the dictation test). One TTS was
replayed from the existing file (not billed).

## Deviations

- **Encoding**: AVFoundation refuses to encode with `.indefinite` segments
  (-11875 "only supports passthrough", HLS and CMAF profiles alike), so the app
  encodes AAC itself and the writer runs in passthrough. Same output format.
- **Pause**: timestamps stay continuous and the writer is kept, so a native pause
  does not open a server subsession (the web's resume does). After a system
  interruption the same writer continues; only a media-services reset starts a new
  one (ftyp chunk → subsession).
- **Lock-screen pause title** is "Paused m:ss" (the web adds "— play, then unlock";
  natively the mic restarts from the lock screen while the session is alive).
- **Cue**: audible thinking cue between Stop and the first chunk and whenever the
  queue drains while TTS is generating (design §9.4; no web counterpart).
- **Mic permission** is asked before `init` (the web asks after init and discards
  the draft on denial): no orphan drafts. Notification permission is asked at the
  first record tap, before recording starts.
- **`cancelled`** reply status ends the turn (the web stays on "Thinking...").
- **A WebM draft** (started in desktop Chrome) cannot be continued natively: a toast
  says so instead of the web's `mime_mismatch` failure.
- **Debug-file recordings** use a `.playback` session: in the simulator a
  `.playAndRecord` session opens the Mac's microphone and blocks on macOS's consent
  prompt (this hung the app once, see Known gaps).
- **Offline stop**: after one chunk used up its retries, later chunks get one
  attempt each and go to the background session (the web retries each in full).
- **Dictation "Save audio"** appears once the first 15 s chunk exists (the web keeps
  an in-memory partial from the start) and is named `.m4a` (the web names MP4 audio
  `.webm`).
- **Proposal card** on the Voice screen is M2's compact card (the web uses `roomy`).
- **Leaving Voice**: popping the screen ends the conversation (web unmount); switching
  tabs keeps it running.
- Speaker icon title falls back to "Node N" (parity) because nodes pass no heading.

## Known gaps

- Not exercised live: a within-turn chain (needs a tool call), the recovery banner's
  Continue against the server (its draft was deleted by the collision below; the
  flow is unit-tested and the `prior + new` total is tested), lock-screen controls,
  interruptions, background uploads, AirPods, the 59-minute mark (all on the device
  checklist).
- The local backend runs with `STREAMING_VOICE_TTS` off, so turns used batch TTS
  (attach while `pending`, chunks after completion). The streaming path
  (`tts_streaming: true`, placeholder chapters) is unit-tested only; check it on
  staging, where the flag is on.
- `NodeAudioControls.onTtsGenerated` is not connected to `ThreadModel` (the web sets
  `has_tts` on the node so a later edit offers "Regenerate audio").
- Speaker icons on Profile and saved-reference pages: those screens are M4's.
- Listen-aloud has none of the Voice page's REST recovery (parity with the web).
- A dictation form that goes away mid-recording stops capture but leaves the audio
  session active until the next audio action.

## Backend findings (no backend changes made)

1. **`DELETE /api/drafts/` without `parent_id` deletes the user's first top-level
   draft, which can be a live voice recording.** Seen twice in testing: another
   client (the M2 agent testing the writing form as the same user) saved and
   discarded a top-level text draft 8 s after this app's voice `init`; the voice
   draft was deleted, chunk uploads got 404 and finalize 404. On production this is
   a web tab or a second device submitting a new entry while a top-level voice
   recording runs (related to #320). Suggest excluding drafts with a `session_id`
   in `delete_draft` (and `save_draft`'s lookup).
2. The Simulator's microphone prompt: with the real mic path in the simulator, macOS
   asks for microphone access for "Simulator". A prompt may still be on Peter's Mac
   from 2026-10-01 01:26 (either answer is fine; the debug path no longer needs it).

## For M5

1. Merge this branch into `ios-app` (M2 is already merged here; re-merge if M2 moved on).
   `AppState` / `MainTabView` conflicts are "keep both" additions.
2. Fold this file into `PROGRESS.md` (Done / Deviations / Known gaps) and the device
   checklist is already in `ios/README.md`.
3. M4's Profile and Reference pages: add `SpeakerButton(target: .profile(id) /
   .item(id), content: …)` where the web has `SpeakerIcon`. M4's native Account page
   must keep `VoiceSettingsSection()`.
4. Connect `NodeAudioControls.onTtsGenerated` to a `ThreadModel` method that sets
   `node.hasTTS = true`.
5. Design docs (`CLAUDE.md` list): mark M3 done.
6. Report finding 1 to Peter (backend follow-up).
7. Commit `73e60d7` (the second merge of `origin/ios-app`) lacks the
   `Co-Authored-By`/`Claude-Session` lines (made with `--no-edit`; not rewritten,
   per the no-amend rule).
