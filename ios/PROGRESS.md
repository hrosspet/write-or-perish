# iOS app: progress and hand-off

The spec is [`docs/IOS-APP-DESIGN.md`](../docs/IOS-APP-DESIGN.md) (§13 milestones).
The web app's behaviour is mapped in `ios/docs/web-app-map/A–E`. Precedence: for
how the web behaves, the web code wins; for what the app should do, the design doc.
Build, install and the device / staging checklists are in [`README.md`](README.md).

| Milestone | Branch | Status |
|---|---|---|
| M1 Foundation | `ios-app` | **done** (2026-10-01) |
| M2 Thread and writing | `ios-app` | **done** (2026-10-01) |
| M3 Voice and audio | `ios-app-voice`, merged into `ios-app` (`d69bd0b`) | **done** in the simulator (2026-10-01); locked-phone behaviour needs the device checklist |
| M4 Feature pages | `ios-app` (built after M3 was merged) | **done** (2026-10-01) |
| M5 Integration and parity | `ios-app` | **done** in the simulator (2026-10-01); PR ready for review |

Not yet done anywhere: a run on a real iPhone (README "Only a real iPhone can test") and a
pass on staging (README "Check on staging").

---

## How to work on this (read first)

**Environment**
- Worktree `.claude/worktrees/ios-app`, branch `ios-app`. PR #384 "Native iPhone app (SwiftUI)".
- Xcode 26.3 is at `/Applications/Xcode.app` but `xcode-select` points at the CLT and sudo
  is unavailable: prefix every Xcode command with
  `export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer;`.
- `simctl` commands that boot or install need the sandbox disabled in the agent shell.
- If `xcrun simctl list runtimes` is empty although `xcrun simctl runtime list` shows the
  iOS 26.3 disk image "Ready", run `xcrun simctl runtime scan-and-mount` (sandbox off) once.
- Simulators used: M1 and M4 "Loore iPhone 17" (`CDAD3013-…`), M2 "Loore M2 iPhone 17"
  (`0A4086FA-…`), M3 "Loore Voice iPhone 17" (`F23E45D8-…`), M5 "Loore M5 iPhone 17"
  (`AB8027B7-…`). Use your own; create one with
  `xcrun simctl create "<name>" com.apple.CoreSimulator.SimDeviceType.iPhone-17 com.apple.CoreSimulator.SimRuntime.iOS-26-3`.
- This simulator runtime draws **every emoji as a "?" box**, even in the system font (checked with
  an `ImageRenderer` test); it is the runtime, not the app. Check emoji on a device.
- User 5 is shared with other agents: its `preferred_model` and `craft_mode` can change
  under you. Check `GET /api/dashboard/` before a billed step (the auto-generate path sends the
  preferred model, as on the web); restore anything you switch. For voice runs M3 set
  `preferred_model` to `gpt-6-luna` (the cheapest) and back to `claude-opus-5.5` afterwards.

**Build and test**
```sh
cd ios && xcodegen          # always after adding/removing files
xcodebuild -project Loore.xcodeproj -scheme Loore -destination 'platform=iOS Simulator,id=<UDID>' \
  -derivedDataPath build/DerivedData test          # unit tests (one skipped: the format-probe helper)
python3 ios/scripts/check_terms_text.py            # from the repo root
```
`build/` is git-ignored. Install + launch for screenshots:
`xcrun simctl install <UDID> build/DerivedData/Build/Products/Debug-iphonesimulator/Loore.app`,
`xcrun simctl launch <UDID> org.loore.app -LooreEnvironment local -LooreResetState YES -LooreSessionCookie "$(ios/scripts/local_backend.sh session-cookie)" -LooreTheme dark -LooreSkipUpdates YES`,
`xcrun simctl io <UDID> screenshot <scratch>/x.png`. UI test classes and their inputs: README "UI tests".

**Signing in on the simulator (test user 5 `seowriter` only)**
- Fastest: `-LooreSessionCookie "$(ios/scripts/local_backend.sh session-cookie)"`.
- The real flow: `ios/scripts/local_backend.sh magic-link` → paste in the app
  (Sign in → "I already have a sign-in link"), or run the UI smoke test
  `testSignInWithPastedMagicLink`.
- Test states (terms out of date, unapproved, an unread notification):
  `ios/scripts/local_backend.sh state …`; always finish with `state restore del_notifications`.
- User 5 has an email address, is approved, plan alpha, `share_v1_enabled` true, not admin, no X
  login. Its content is test text (rivers, glaciers).
- Web reference screenshots: headless Chrome with a throwaway profile and the same session
  cookie on `localhost:3001` (driven over CDP with a small Node script, kept out of the repo).

**A voice turn without a microphone**
- `-LooreDebugAudioFile <clip>` (make one with
  `say -o clip.m4a --file-format=m4af --data-format=aac "…"`) plays the file into the recorder at
  real time; its end acts as Stop. Debug-file recordings use a `.playback` session, because in the
  simulator a `.playAndRecord` session opens the Mac's microphone and blocks on macOS's consent
  prompt.
- Every voice turn is billed (transcription + reply + TTS). Use a `parent` node: a text entry saved
  elsewhere as the same user deletes the user's top-level draft, which can be a live voice recording
  (see "Backend findings").

**Where things are**
```
ios/project.yml                 XcodeGen spec (packages: swift-markdown 0.7.3, ZIPFoundation 0.9.20)
ios/Config/Signing.xcconfig     team id / bundle id (+ git-ignored Signing.local.xcconfig)
ios/Loore/App/                  LooreApp, AppState, RootView (gating), MainTabView (tabs, RouteDestination),
                                Router, AppRoute (web path → route), AppEnvironment, LaunchOptions
ios/Loore/Core/Networking/      APIClient (+APIRequest, MultipartFormData), APIError, APIPaths, SSE, Poller
ios/Loore/Core/Auth/            AuthService, CookieVault, KeychainStore, MagicLink, WebLoginView (X)
ios/Loore/Core/Models/          LooreDate, JSONValue, OpenEnum (+enums), User, Nodes, Replies, Drafts,
                                Updates, StreamEvents, DecodingHelpers, Workspace, References, ShareCommons
ios/Loore/Core/Util/            DateFormatting (date.js port), SpendCap (spendCap.js port), LineDiff
                                (+VersionDiff; diff.js port), VersionLabels
ios/Loore/DesignSystem/         Tokens (colours, spacing, radii, motion, FadeIn), Typography, Theme,
                                Components/ (Buttons, Surfaces, Feedback [dialogs, toasts, pill switch,
                                spend banner], Glyphs [SVG paths, logo, craft icon, X logo])
ios/Loore/Markdown/             the one markdown renderer — MarkdownView (+MarkdownStyle presets,
                                ChecklistActions), MarkdownModel (swift-markdown → render tree, GFM autolinks,
                                HTML as code, list tightness), MarkdownEdits (utils/markdown.js port), JSRegex,
                                ContentSegments + QuotedContentView (quote/artifact markers, quote bubbles),
                                NodeLinks (+NodeTitleStore on AppState.nodeTitles)
ios/Loore/Audio/                M3: Recording/ (SegmentPackager + MP4Boxes, FMP4SegmentWriter + AACEncoder +
                                PCMConverter, AudioSources [mic, Debug file], VoiceRecorder, ChunkUploader),
                                Voice/ (VoiceTurnController, VoiceTurnDependencies + LiveVoiceBackend),
                                Playback/ (ChunkQueuePlayer + ChunkQueueMath, Sounds [cue, chimes],
                                ListenAloud [+AudioDownloader]), Session/ (AudioSessionController,
                                NowPlayingController), Dictation/ (DictationController), AudioCenter
                                (owns all of it as AppState.audio; LooreAppDelegate for background sessions)
ios/Loore/Features/Home/        HomeView (+Reflect / Glean cards, #475), PlaceholderScreen (only routes that never push)
ios/Loore/Features/Onboarding/  SignIn, Terms + TermsText, Waitlist + PrefillConsentCard, WelcomeView,
                                ConfirmEmailView (+model, paste sheet)
ios/Loore/Features/Updates/     UpdatesSheet
ios/Loore/Features/More/        MoreView (the ⋮ menu), CraftModeDialog, ShareSheet
ios/Loore/Features/Thread/      ThreadView/ThreadModel/ThreadSheets (+NodeAudioControls), Bubble (BubbleView,
                                BubbleData, BubblePreview, KebabMenu, NodeFooterView), ModelPicker (+ModelCatalog)
ios/Loore/Features/Write/       NodeFormView/NodeFormModel, DraftAutosaver, PrivacySelector (+SelectField,
                                FieldLabel, LabeledPillToggle), WritingDialogs, TextModeView (+WriteNewEntrySheet)
ios/Loore/Features/Proposals/   ProposalParser (ProposalInline.js port), ProposalCard (compact)
ios/Loore/Features/Log/         LogView
ios/Loore/Features/Search/      SearchView (+SearchSnippet, SearchResult)
ios/Loore/Features/Voice/       M3: VoiceView (+VoiceVisuals), MiniPlayerView, VoiceSettingsSection,
                                NodeAudioButtons (SpeakerButton, DownloadAudioButton), StreamingMicButton
ios/Loore/Features/Workspace/   WorkspaceView (Artifacts tab root; document switched in place), ArtifactsNavRow,
                                doc header pieces, VersionHistorySheet (+DiffRowsView), WorkspaceStores
                                (ArtifactsStore, ProfileGenerationWatcher)
ios/Loore/Features/{Profile,Todo,Artifacts}/  ProfilePage (+SourceMix), TodoPage + TodoModel (+TodoSections),
                                ArtifactsPage + IntentionsView (+IntentionsParser, ArtifactKinds)
ios/Loore/Features/References/  ReferencesView, ReferenceDetailView (+edit sheet), Embeds (tweet, YouTube),
                                ReadReplyViews (FeedPicks, ReadWindowLine, ReadReplyTail), ReferenceUtils
ios/Loore/Features/Prompts/     PromptsView, PromptDetailView
ios/Loore/Features/Account/     AccountView, AccountModel (Debug environment switcher, Voice section)
ios/Loore/Features/Import/      ImportView (+ExternalImportSection, NewTokenDialog), ImportDataSection,
                                ImportModel, ImportFiles (ZIPFoundation streaming, multipart files)
ios/Loore/Features/{Share,Commons}/  ShareView, CommonsView (+CommonsModel)
ios/Loore/Features/Web/         SafariView, AuthenticatedWebView, CookieWebFlowSheet (Connect X, X bookmarks)
ios/LooreTests/                 unit tests + Fixtures/ (captured from user 5, trimmed, email scrubbed)
ios/LooreUITests/               flows against the local backend (not in CI)
ios/scripts/                    check_terms_text.py, local_backend.sh
```

**Conventions**
- Paths: add every endpoint to `APIPath` (it owns the trailing-slash rule; `APIPath.canonical`).
- Calls: `try await app.api.get/post/put/patch/delete(path, json:)` returning a `Decodable`;
  `EmptyResponse` when the answer is ignored; `api.fireAndForget` for the web's `.catch(() => {})`.
  Status polls: `get(..., poll: true)` (no-cache, 10 s timeout) inside `Poller.run/runStatus`.
  Large uploads: `api.upload(_:fromFile:)` with a body written by `ImportFiles.multipartBody`.
- Errors: catch `APIError`; `.userMessage(fallback:)` gives the server's words. 401/402/403-not-approved
  are handled globally (sign-out, spend banner, re-gate) — call sites just stop.
- SSE: `app.sse.subscribe(path:resumeQuery:)` → `SSEMessage`; decode events with
  `LLMStreamEvent(e)`, `TTSStreamEvent(e)`, `TranscriptionStreamEvent(e)`; `LastChunkTracker` for `?last_chunk=`.
  A `.response(status, body)` message means the endpoint answered JSON instead of streaming.
- Models: explicit `CodingKeys`, `c.tolerant(...)` for every non-id field, open enums
  (`AIUsage.off` is the wire value "none"). Never use `.convertFromSnakeCase` (it rewrites
  dictionary keys such as artifact kinds and source names).
- Navigation: routes are `AppRoute`; `app.open(route)` / `app.openLink(link)`. A route's screen is its case
  in `RouteDestination` (MainTabView.swift). `app.open(.profile / .todo / .artifacts(kind:) / .newArtifact)`
  selects `router.workspace` and pops the Artifacts tab (web `ArtifactsNav`); a workspace route pushed
  elsewhere shows a standalone workspace (`WorkspaceView(pinned:)`).
- UI: tokens only (`LooreColor`, `LooreFont`, `LooreSpacing`, `LooreRadius`, `LooreMotion`);
  cards `.looreCard()`, page titles `PageHeader`, dialogs `.looreDialog(isPresented:)` +
  `LooreDialogCard` + `ChoiceButton`, toasts `app.toasts.show(text, duration:)`,
  `app.notifySpendBlocked()` before refusing a cost action, pills `LoorePill`, tags `LooreTag`.
  Shared building blocks: `RenameThreadDialog`, `NodeFormSheet`, `FieldLabel`/`SelectField`,
  `NetworkStatus`, `NodeTitleStore`, `InlineArtifactSection.formatTokens`, `JSRegex`.
- Chained dialogs: present them through **one** `.looreDialog` whose content switches (two
  full-screen covers cannot dismiss and present in the same update; the second one is dropped).
- Work a view starts on appear that must not be cancelled by a re-render goes in its own `Task`,
  not `.task(id:)` (a cancelled `URLSession` call is a silent failure).
- Markdown anywhere: `MarkdownView(markdown:style:checklist:onLink:)`; node content with quote and
  artifact markers: `QuotedContentView`. Pick or derive a `MarkdownStyle` preset (`.focal`, `.bubble`,
  `.quote`, `.proposal`, `.changelog`); `flowText` makes soft breaks spaces (authored docs only).
  Checklists: `ChecklistActions` + `MarkdownEdits.toggleCheckbox / insertItemAfter / appendItemToSection`.
- Text matching for list edits: `MarkdownEdits` (same strings as the web). Port JS regexes with `JSRegex`
  (UTF-16, ASCII `\w`), lengths with `.jsLength`, cuts with `.jsPrefix`.
- Italics in Outfit: `LooreFont.sansOblique` (Outfit has no italic face; `.italic()` does nothing).
- Cross-screen signals: `app.signals.post(.todoChanged)` etc. (`artifactsChanged`, `profileGenerationStarted`,
  `nodeCreated`, `logChanged`, `referencesChanged`); observe the counters with `.onChange`.
  Stores on `AppState`: `audio`, `nodeTitles`, `artifacts`, `profileWatcher`.
- Any versioned document: a `VersionHistorySource` (title, versions, content, revert) and `VersionHistorySheet`.
- Audio: everything goes through `app.audio` (`AudioCenter`). Listen-aloud: `SpeakerButton(target: .node /
  .profile / .item, content:, onTtsGenerated:)` + `DownloadAudioButton`; dictation: `StreamingMicButton`
  driving `NodeFormModel.dictationStarted/Transcript/Finished/Failed`.
- Signed-in web flows that end on a frontend page: `CookieWebFlowSheet(startURL:onLanding:)`.
- A container with `.accessibilityIdentifier` needs `.accessibilityElement(children: .contain)` first, or
  SwiftUI copies the identifier onto every child and UI tests cannot find the buttons inside.
- Test identifiers: `thread.focal`, `thread.focalKebab`, `thread.llmResponse`, `nodeForm.text.<new|inline|edit>`,
  `nodeForm.send.<…>`, `search.field`, `more.writeNew`, `home.reflect.voice` / `home.glean.text` (…), `voice.glean`,
  `thread.glean`, `tab.reflect` (the Home tab), `more.*`.
- Per-device preferences use the web's localStorage names (`DefaultsKey`).
- No content in logs (ids, statuses, byte counts only).

---

## Done in M1 (foundation)

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
- Tab shell with Home (greeting, Voice/Text/Share cards).
- More: Import · Admin (web view, admins) · Account · My public page (flag) · References ·
  Light mode · Craft mode (+ dialog, toast, glow, persistence) · Write new entry · Export
  data (share sheet) · Prompts · About (Safari views) · Logout.
- Debug launch arguments (`LaunchOptions`), `ios/README.md`, this file.

**Verified in M1** (iPhone 17, iOS 26.3, local Docker backend, user 5)
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
- No billed calls.

## Done in M2 (thread and writing)

- **Markdown renderer** (design §8, map D §3): swift-markdown parse (no smart punctuation) → render
  tree → SwiftUI. GFM tables (horizontal scroll, column alignment), strikethrough, task lists,
  autolink literals (swift-markdown does not attach cmark's autolink extension: `https://`, `www.`,
  emails, GFM trailing-punctuation rules), raw HTML as code / comments dropped, headings with the
  web's sizes and collapsing margins, blockquotes, nested lists with tight/loose detection, code
  blocks (Menlo, horizontal scroll), rules, images. Single newlines break lines (web `pre-wrap`);
  in tight list items they are spaces (web `white-space: normal` li). Marker pre-split exactly as
  `QuotedContent` (`{quote:N}`, `{quote_ext:N}`, `{user_*}`, guidance markers). Node links show the
  node's title (`GET /nodes/titles`, coalesced per run-loop turn, cached, failures not cached).
  Owner checklists: round boxes, toggle by the web's stripped-text matching, "+" insert below.
  Used in the thread, expanded bubbles, quote cards, artifact sections, proposal cards, the
  Updates sheet and every M4 page.
- **Home**: admin-only Read card (`POST /api/read/start`).
- **Log**: cursor paging (auto-load at the end, "Load more...", retry), dedupe, Rename thread,
  Delete thread, search button (⌘K), pull to refresh; refreshes after creates/deletes.
- **Thread**: header with Voice Mode (→ `/voice`) and Auto-generate (craft); admin Read /
  Read further; ancestors; focal card (system-prompt header, pending "Thinking"/"Processing" with
  pulsing dots, streamed text, proposals, "Actions taken" with every tool label); footer with pin
  rules and titles; LLM Response + model picker (craft / public threads); in-progress line;
  inline reply form (read-reply override); reply tree with sibling indentation; kebabs
  (Reply/Edit/Delete); prompt-edit confirm; Edit Text and Reply sheets; delete with the
  orphaned-prompt follow-up and the web's landing rules; scroll to the focal node once.
- **Replies**: `POST /nodes/<id>/llm`, `?awaitLlm=` hand-off (entry → reply, consumed so back does
  not repeat it), `llm-stream` SSE text, `llm-status` polling every 2 s (15 s batch), in-place
  completion, continuation chains, failure toast + back to the entry, cancelled reads, request
  errors (402 silent), auto-generate rules (chain check turns it off with the toast; public threads
  force it off; editing a user node starts a new branch).
- **Writing** (`NodeForm`): server drafts (1 s debounce, 3 s flush, "Saving..." / "Draft saved …",
  Discard draft, restore on reopen, recovered-audio transcription), craft privacy / AI-usage
  selects (public parent locks privacy), remembered choices, Write New Entry toggles, the submit
  decision tree (content required → regenerate audio → apply to replies → 100,000-character cap with
  split dialog or error → public reply consent) and every send path (custom submit, agentic
  `/textmode/start`, recorded `save-as-node`, edit `PUT`, audio upload ≤10 MB and chunked, plain
  `POST /nodes/` + Write New Entry auto-generate), paste over the cap, offline state, ⌘↩.
- **Text mode** (`/textmode`) and **Write New Entry** (More → craft) with the web's routing.
- **Proposals**: parser ported with all jest cases; compact card (todo rows with tick/untick and "+",
  priority, note, GitHub issue, feedback, shares with copy and per-block save, preferences line),
  the four accepts (todo apply + polling, issue, feedback, share save), state from `tool_calls_meta`.
- **Search** sheet: 300 ms debounce, semantic (admins: keyword + paging), date range, `<mark>`
  highlights, node and reference rows.

**Verified in M2** (simulator "Loore M2 iPhone 17", local Docker backend, user 5)
- Unit tests: 197 pass (M1's 106 + 91): `ProposalParserTests` (ProposalInline.test.js one-to-one),
  `MarkdownEditsTests` (markdown.test.js), `MarkdownParserTests` (MarkdownBody.test.js and
  MarkdownBody.stable.test.js #321 equivalents, autolinks, tables, tightness), `BubblePreviewTests`
  (Bubble.test.js), `NodeLinksTests` (nodeLinks.test.js), `ModelPickerTests` (ModelSelector.test.js
  `pickerOptions` + default rules), `ContentSegmenterTests`, `NodeFormModelTests` (decision tree on
  a stubbed backend), `ThreadModelTests` (reply rules, hand-off, completion, continuation, failure),
  `SearchSnippetTests`.
- UI flows (screenshots in the session scratch dir, compared with headless-Chrome shots of the web at
  402×874): Log (cards, kebab, rename and delete dialogs), search (empty, dates, a "rivers" query and
  opening a result), threads with a system prompt and artifact chips, proposal cards (tick/untick
  saved; Apply on a superseded proposal shows the server's 404 text), ancestors, reply tree,
  tombstone; kebab/Edit/Reply/Delete; a Text-mode entry with a checklist (tick and "+" saved), edit,
  delete with "Delete the system prompt too?"; craft mode on/off (selectors, Upload, LLM Response
  bar, model picker with "More models…", Write New Entry); draft saved, restored after relaunch,
  discarded; light theme; a temporary entry with every markdown construct and a node link; quote
  bubbles (`{quote:N}` and an inaccessible one); a `:::share` block saved as a private draft.
- Billed calls: **3 LLM replies** (two on GPT-6 Luna; one auto-generate reply went to Opus 5.5 because
  the shared user's preferred model had been switched back during the run) plus one semantic search
  embedding. Test entries, the share draft and craft mode were cleaned up / restored.
- Bugs found and fixed by these runs: chained dialogs dropped the follow-up (one presenter now);
  the inline form's draft load was cancelled by a re-render (draft not restored); forms kept their
  craft-off controls after craft mode was switched on; bubble footers squeezed the author away
  next to wide tags; italics had no effect in Outfit.

## Done in M3 (voice and audio)

Spec: design doc §9, map C (§8 is the native design).

**Recording format, proven first.** `AVAudioConverter` (AAC-LC 64 kbps, 48 kHz mono) →
`AVAssetWriter(contentType: .mpeg4Movie)`, `outputFileTypeProfile = .mpeg4AppleHLS`, segment interval
`.indefinite` in passthrough, `flushSegment()` every 15 s of audio and on pause/interruption.
Chunk 0 = initialization segment + first media segment. Evidence (local backend, 2026-10-01): chunk 0
was 41 829 bytes with top-level boxes `ftyp`(28) `moov`(584) `moof` `mdat`, and the server's own
`extract_mp4_init_segment` returned a 612-byte init; chunks 1–2 were bare `moof`+`mdat`. The server's
`concat_fragmented_media` remux gave one AAC 48 kHz mono file (14.9 s of the 15.0 s clip); a batch
starting at chunk 1 with the persisted init decoded too. Upload: three chunks `202 stored`, a duplicate
`200 Chunk already uploaded`; finalize `202`; `/status` → `completed`, 3/3 chunks, transcript word for word.

`ios/Loore/Audio/`
- `Recording/SegmentPackager.swift`: chunk contract; `MP4Boxes` (box walker, the server's chunk-0 rule,
  the `ftyp` subsession test). `Recording/FMP4SegmentWriter.swift`: `AACEncoder` + passthrough fMP4
  writer + `PCMConverter` (any input → 48 kHz mono Int16). `Recording/AudioSources.swift`:
  `MicrophoneSource` (AVAudioEngine tap, restarts after a configuration change), Debug `AudioFileSource`.
- `Recording/VoiceRecorder.swift`: source → converter → writer → upload queue; pause keeps capture
  running (drops samples); interruption flushes and holds; a failed writer is replaced on resume (its
  first chunk opens a server subsession).
- `Recording/ChunkUploader.swift`: persisted queue in Application Support
  (`completeUntilFirstUserAuthentication`), chunk 0 first, the web's 2/4/8/16 s retries, background
  `URLSession` copy on give-up and on backgrounding, resumed after a relaunch (`AppState.didLoad` →
  `audio.didSignIn()`), `init_parse_failed` fatal, `total_chunks` = stored count when chunks were
  given up (plus the chunks a resumed session already had).
- `Voice/VoiceTurnController.swift`: the turn state machine (C §8.5): init (402 → toast), record, stop
  (cue first, then mic), finalize (400 "not in recording state" = success), `/status` every 1 s (never
  the draft SSE, never before finalize), empty transcript / warning / legacy `POST /api/voice`,
  llm-status every 1.5 s, the TTS attach rule, `POST /tts` once on completion, JSON stream answers,
  chains into one queue with chapters (placeholder `…` renamed from `all_complete.preview`), drains
  (cue), REST recovery (#242: trigger watchdog 20 s, reconcile 7 s + foreground, 3 s catch-up, 60 s
  net), `cancelled` terminal, cancel/continue, 59-minute warning, interruptions (chime, 24 h toast,
  local notification), timing marks (`VoiceTiming` sends them with the clock offset).
- `Playback/ChunkQueuePlayer.swift` + `ChunkQueueMath.swift`: `AVQueuePlayer` queue, cumulative time,
  seek across chunks, chapters, rates 1/1.25/1.5/2 (time-domain pitch), duration correction, dedupe by
  URL, cookies on every media request (`AVURLAssetHTTPCookiesKey`), `.mp4` MIME override.
- `Playback/Sounds.swift`: the thinking cue (G3+D4 swell, 4 s loop, −24 dBFS file; Soft ≈ −30,
  Very soft ≈ −38 dBFS) and the web's chimes, generated in code, played on a private queue; the cue
  player is prepared at the record tap.
- `Playback/ListenAloud.swift`: the SpeakerIcon logic (recording first, chunked recordings as a queue,
  TTS + chapters, else `POST /tts` + stream) and `AudioDownloader` (DownloadAudioIcon logic, web file names).
- `Session/AudioSessionController.swift`: `.playAndRecord` at the record tap, `.playback`/`.spokenAudio`
  after Stop and for listening, `setPrefersNoInterruptionsFromSystemAlerts` while recording,
  interruption / route / media-reset events. `Session/NowPlayingController.swift`: lock screen per phase
  (recording, thinking, playback) with remote commands.
- `Dictation/DictationController.swift`: recording into a writing form (no label, draft SSE for live
  text, finalize, `/status` after finalize).

`ios/Loore/Features/Voice/`: `VoiceView` (VoicePage parity: ready/recording/Thinking/playback, ECG and
waveform, recording timer, interruption text, error dot, offline notice, player with ±10 s, progress bar
with chapter ticks, chapter list with Roman numerals, proposal card, Continue, Text Mode, recovery
banner), `MiniPlayerView` (global player above the tab bar, hidden on Voice), `VoiceSettingsSection`
(Account → Voice), `NodeAudioButtons` (`SpeakerButton`, `DownloadAudioButton`), `StreamingMicButton`
(dictation button with the web's states). M2's hooks were filled: `NodeAudioControls` (thread footer)
and `DictationButton` (writing form).

**Verified in M3** (simulator "Loore Voice iPhone 17", local backend, user 5)
- Unit tests: 240 with M2's (one skipped). New: fMP4 packaging and a real encode (chunk 0 = init + first
  segment, later chunks without `ftyp`, durations, flush on pause); the turn state machine against a
  fake API (19 cases: happy path incl. cue-before-mic order and timing marks, continue, chains, drains,
  JSON stream answer, POST /tts 200, empty transcript, warning, spend cap, mic failure, reply failure,
  fatal upload, given-up chunks, retried finalize, cancel ignores late events, interruption, 59-minute
  warning, resumed draft, WebM draft); queue maths; uploader (order, cookies, retries, offline degrade,
  fatal, duplicate, relaunch); sounds; lock-screen command mapping.
- Turn 1 (`VoiceUITests`, reply under a test-user node): recording → Thinking → the reply played without
  a tap (`Pause`, 0:08 after 8 s) → end 0:13/0:13 → Continue. Turns 2 and 3 (Continue): the app log
  shows `thinking cue on` at Stop and `thinking cue off` when the first chunk played (6.3 s and 8.4 s
  later); both replies started by themselves. Server timing (Stop → playing): 9.7 s, 6.3 s, 8.4 s;
  transcription 1.7–2.8 s; first chunk ready → playing 0.05–0.27 s.
- `VoiceWiringUITests`: thread footer speaker → mini-player plays the reply; download → share sheet
  with `node-<id>-tts.mp3`; "Voice Mode" on a thread reply → Voice screen plays it; dictation from the
  debug file into a reply form (279-character transcript in the editor; draft discarded).
- Recovery banner ("Unfinished Voice recording", Continue / Discard) for a real interrupted draft;
  Account → Voice with the Off warning.
- Billed calls: 3 full voice turns (transcription + `gpt-6-luna` reply + TTS) and 2 transcription-only
  calls (the format probe, the dictation test). One TTS was replayed from the existing file.

## Done in M4 (feature pages)

Every route classed CORE or SECONDARY in map A §1 now has a native screen; `PlaceholderScreen` is only
reached by routes that never push (admin, waitlist, web pages).
- **Workspace** (Artifacts tab): ArtifactsNav bubbles (store refreshed on `artifactsChanged`), Profile (meta line
  with source mix, empty state, edit with the regenerate-audio question, `PUT` in place / `POST` first, history
  with revert, generation indicator fed by the app-wide `ProfileGenerationWatcher`, listen-aloud speaker), Todo
  (sections, nested items collapsed, tick, row "+", quick-add to Today, raw edit, history, revert; every in-place
  save re-fetches first), Artifacts (built-in and custom kinds, synthetic unknown kind, create form with the slug
  sanitiser and validation, description required, unsaved-changes guard, `viewed` ping, Intentions view, history).
- **Version history**: list → detail sheet; line diff with word refinement, folded unchanged runs, Changes/Full
  text (heavy rewrites open in Full), "Initial version", revert; prompts' file default as v0.
- **References**: list (cards with reference footer and tags, page paging, Open source, Delete), detail (title
  with speaker, tweet embed via widgets.js, YouTube nocookie embed, stored text toggle, footer and tags,
  surfacing line, Good/Bad verdict, read toggle, Edit sheet with the cap and regenerate-audio question, Delete).
  In the thread: legacy FeedPicks, ReadWindowLine and the ReadReplyTail.
- **Prompts** (craft): list with the "default updated" dot, detail with edit (monospace), the default-updated
  banner (view / accept / dismiss), history.
- **Account**: username (validation), email (one request in flight, pending notice, resend / new link, cancel,
  remove with X, "Paste the link" for the confirmation), X (Connect in a signed-in web view with the outcome
  messages, Disconnect), plan, settings that save on change (model, privacy, external references, public sharing,
  AI usage, craft mode), AI Preferences link, M3's Voice section, version line with the Debug switcher, anchors.
- **Confirm email** (pasted link, from Account or the waitlist) with every outcome; **Welcome** (hero, first
  entry → Write New Entry, prefill consent card, import sheet, How-to link, closing lines).
- **Import**: the four archive importers (file picker; Claude/ChatGPT `conversations.json` streamed out of the
  zip with ZIPFoundation, the largest JSON array as fallback; Markdown/Twitter zips streamed into the multipart
  body), analyze → confirm dialogs (privacy Private, AI None), 409 restore-or-skip, Twitter task polling with
  progress, Import Finished (then the user is reloaded and the profile watcher starts when a build was handed
  off), the web's errors; Import References (Community Archive fetch + count polling, X bookmarks sync / connect /
  reconnect, bookmarks JSON, Chrome clipper tokens with the one-time token dialog).
- **Share** (drafts, publish with the inline "Where should this go?" confirmation, revoke, edit, delete) and
  **Commons** (feed, click-to-load paging, cards open the thread), both behind `share_v1_enabled`.
- Ports: `diff.js`, `intentions.js`, `references.js`, `artifactKinds.js`, `parseTodoSections`,
  `formatSourceMix`, the version labels (with their jest cases; 2,491 extra checks against the JS outputs were
  run once in a scratch harness).
- Fixes to earlier screens found on the way: collapsed card bodies collapse whitespace like the web (Log too);
  dialog cards keep narrow content left-aligned.

**Verified in M4** (simulator "Loore iPhone 17", local Docker backend, user 5)
- Unit tests: 332 pass (M3's 240 + 92): LineDiff/Intentions/ReferenceUtils/WorkspaceUtils (jest ports), Account
  (AccountPage.test.js), ConfirmEmail (ConfirmEmailPage.test.js), ProfileGenerationWatcher (its test.js +
  outcome rules), ReadReply + FeedPicks (their test.js), TodoModel (re-fetch before PATCH keeps the AI's item,
  quick-add, revert + toast), Import (zip reading, multipart, confirm bodies, 409 retry, ChatGPT errors,
  results), payload decoding and workspace routes, fixture renders of Commons cards and the Intentions view.
- Side by side with headless-Chrome shots of the web at 402×874 (scratch dir): Profile and Todo (empty and
  filled), Memory, Intentions (empty), new artifact form, References list and a tweet reference, Prompts and a
  prompt, Account, Import, Share, Welcome, Confirm email, Todo history drawer vs sheet.
- `M4ScreensUITests` (all pass): Todo create → tick → expand → quick-add → row add → edit (v2) → history diff →
  revert (v3); Profile write → edit → history; artifact create (slug sanitised) → edit → unsaved-changes dialog
  → diff / full text; reference embed → read toggle on/off → edit and delete dialogs (cancelled); prompt history
  vs the file default; Account username validation, Connect X round trip (local backend lands on
  `x_login=cancelled` at once); stale confirmation token → "Not confirmed"; markdown import (2 imported), the same
  archive again (0 imported, 2 skipped, note), a ChatGPT zip without a `conversations.json` name (1
  conversation found), not-a-zip error; Share draft → publish → revoke → delete; Welcome import sheet.
- Light theme checked on Account, Intentions and a reference (the tweet embed follows the theme).
- No billed calls (imports ran with AI usage None, which starts no profile update; no speaker taps). User 5
  restored: shares, reference marks, settings unchanged; test todo/profile/artifact/notes removed with
  `local_backend.sh state m4_cleanup`.

## Done in M5 (integration and parity)

- Folded the voice hand-off (`PROGRESS-voice.md`) into this file.
- **Parity walk** (table below) and the gaps it closed:
  - `/@user/slug` permalinks open the native thread for a signed-in member (`GET /api/commons/permalink/…`,
    web `PermalinkRoute`); anything that does not resolve opens the public web page as before.
  - A pasted sign-in link opens its landing page (the link's `next_url`: `/welcome` from the Activate & Welcome
    email, `/confirm-email?token=…`, …) once the user is in, as the web lands there.
  - The sign-in screen has the web NavBar's signed-out About links (Why Loore, Vision, How To).
  - Toasts and the spend-cap banner sit above the mini-player while it shows (web `--floating-player-offset`).
  - Audio generated from a thread's speaker marks the node as having audio, so a later edit asks whether to
    regenerate it (web `onTtsGenerated`).
  - `SmokeFlowsUITests` no longer opens the Commons tab (locally it lists other users' public posts).
- **Publish scan**: the web-app maps and the design doc no longer describe server-side gaps found while mapping;
  personal names removed from the README and the signing example.
- **Docs**: `ios/README.md` finished (install on an iPhone with a free Apple ID step by step, backends, every UI
  test class and its inputs, the device checklist and a staging checklist); `docs/IOS-APP-DESIGN.md` marked built
  with an "As built" section (§16); the app added to `docs/FOUR-FEATURE-ECOSYSTEM.md`, `docs/TECHNICAL-ROADMAP.md`
  and `docs/ACCELERATED-DEVELOPMENT-ROADMAP.md` (A.37) as in progress, awaiting device testing.
- **Release safety**: a Release build compiles for the simulator and for devices (`CODE_SIGNING_ALLOWED=NO`).
  `LaunchOptions` has no fields and no parser outside `#if DEBUG`, so every launch argument
  (`-LooreSessionCookie`, `-LooreRoute`, `-LooreEnvironment`, `-LooreTheme`, `-LooreResetState`,
  `-LooreSkipUpdates`, `-LooreDebugAudioFile`, `-LooreDebugListenNode`, `-LooreDebugVoiceAutoStart`,
  `-LooreDebugImportFile`), the session-cookie injection, the environment switcher (and `switchEnvironment`),
  the local-backend URL overrides, the debug audio-file source and the test hooks (`useForTesting`) are compiled
  out; `strings` on both Release binaries finds none of the argument names. Release always resolves to
  Production; the Local and Staging cases remain as unreachable enum values (their hosts are also needed to
  recognise loore.org links). A Release build launched with `-LooreSessionCookie … -LooreRoute /log` stays on
  the sign-in screen.
- **Accessibility pass** (an audit of every view, then fixes):
  - VoiceOver: every icon-only control already had a label (about 40); added the missing ones: the search
    date fields ("From date" / "To date"), the editors (Profile, Todo, artifact, prompt, share, reference text),
    the username field, the dictation button ("Record" / "Stop recording" with the elapsed time / "Resume
    recording"), the Voice screen's error dot ("Something went wrong."), "audio still generating" on both
    players' time labels, the current chapter in the mini-player, "Actions taken" with expanded/collapsed,
    ✓/✗ tool results as "Succeeded"/"Failed", quick-add's open/close state, checkbox rows as toggles. Hidden:
    decorative glyphs and symbols inside labelled buttons (search magnifier, chevrons, keyboard/mic/book icons,
    "→" arrows). Actions: external quote cards ("Open original post") and published share cards ("Open public
    thread"). Toasts and "Copied" are announced.
  - Dynamic Type: `FlowLayout` wraps a child wider than its row instead of letting it run off-screen (the
    ArtifactsNav bubbles, title rows); `AdaptiveStack` (HStack that becomes a VStack at accessibility sizes)
    for the thread header, LLM Response / Read rows, Account's username and email rows and the spend-cap banner;
    the node footer puts the author above a wrapping row of date, replies and icons; the mini-player puts time
    and progress under its buttons; the writing form's buttons wrap; Voice's "Text Mode" scrolls with the page,
    its recovery buttons stack, chapter titles get three lines; Voice Mode / Auto-generate buttons grow instead
    of clipping; monospaced text (`LooreFont.mono`) and the navigation-bar fonts scale (bars capped).
  - Reduce Motion: the pulsing dot stays steady; toasts, the spend-cap banner and the mini-player fade without
    sliding; the thread's scroll to the focal node and Account/Import anchor scrolls jump instead of animating.
    (`FadeIn`, the dialog scale-in, ECG, waveform, pulsing dots/text and the craft glow already respected it.)

**Verified in M5** (simulator "Loore M5 iPhone 17", local Docker backend, user 5)
- Unit tests: 334 pass, one skipped (M4's 332 + the landing path, the speaker's audio flag; permalink parsing
  joined the route tests).
- `M5ParityUITests` (all pass): signed-out "Vision" opens the web page; a sign-in link minted with `/welcome`
  lands on Welcome; `/@seowriter/<slug>` opens the native thread; a toast sits above the playing mini-player
  (craft mode switched on for the toast and back off).
- Large text: Home, Voice, Thread, Log, Profile, Account and the mini-player screenshotted at Accessibility XL
  and XXXL (scratch dir): no clipped controls or horizontal overflow after the fixes (Account overflowed before).
- UI tests re-run after the accessibility changes (all pass): `M5ParityUITests`, `SmokeFlowsUITests`
  (session-cookie launch + More), `ThreadWritingUITests` (Log, threads render, kebabs), `M2ScreensUITests`
  (craft forms, light theme), `M4ScreensUITests` (Todo, Profile, Account, prompt history, references; the
  reference card is now tapped on its title, since its centre can land on the footer's source link),
  `VoiceWiringUITests` (speaker + download on a reply with stored audio). Afterwards `state m4_cleanup`; user 5's
  settings unchanged. No billed calls in M5.
- Web-view routes opened with `-LooreRoute`: `/admin` (authenticated web view, signed in; the server refuses the
  data to a non-admin), `/vision`, `/@seowriter` (Safari views), a permalink whose node is gone (falls back to the
  web page).

### Parity walk

Every route of map A §1, every global UI piece of A §4 and every NavBar / ⋮ item of A §2.1, against design
doc §6. **parity** = same behaviour (a web view where §6 says so); **deviation** = intentional difference
(listed under "Deviations"); **gap** = missing. "Unseen" items are built but need data the local test user lacks
(see "Known gaps").

**Routes (map A §1)**

| # | Web route | App screen | Status | Note |
|---|---|---|---|---|
| 1 | `/` Home | Home tab, `HomeView` | parity | with Glean (#475): Reflect and Glean cards (Voice, Text each), Share (flag) below; without: Voice, Text, Share (flag); cards stack on a phone |
| 2 | `/landing` | signed out: native sign-in; links: Safari view | deviation | no marketing page before sign-in (design §6: web view when linked) |
| 3 | `/login` | `SignInView` | deviation | link pasted back (design §4); default landing is Reflect, not `/profile`; a link's `next_url` is followed (M5) |
| 4–6 | `/vision`, `/why-loore`, `/how-to` | Safari view (More → About, sign-in screen, waitlist ⋯) | parity | checked M5 |
| 7 | `/alpha-thank-you` | `WaitlistView` (gate for unapproved users) | parity | in-app links to it are ignored; email form not exercised |
| 8 | `/confirm-email` | `ConfirmEmailView` | deviation | the link is pasted (Account, waitlist) or followed from a sign-in link |
| 9 | `/welcome` | `WelcomeView` pushed on Reflect | parity | reached from the Activate & Welcome link since M5 |
| 10 | `/voice` | `VoiceView` | deviation | thinking cue, mic permission first, `cancelled` ends the turn (Deviations, M3); device checklist |
| 11 | `/textmode` | `TextModeView` | parity | |
| 12 | `/profile` | Artifacts tab → Profile | parity | running-build indicator unseen (staging) |
| 13 | `/todo` | Artifacts tab → Todo | deviation | re-fetch before every save (design §10) |
| 14 | `/log` | Log tab | deviation | refreshes on change and pull-to-refresh, not per visit |
| 15–17 | `/feed`, `/dashboard`, `/dashboard/:u` | Log, Profile, `/@u` web view | parity | legacy redirects |
| 18–19 | `/prompts`, `/prompts/:key` | `PromptsView`, `PromptDetailView` (More, craft) | parity | default-updated banner unseen (staging) |
| 20 | `/import` | `ImportView` | deviation | native file picker, 200 MB pre-check, 504 wording (Deviations, M4) |
| 21–22 | `/references`, `/references/:id` | `ReferencesView` (More), `ReferenceDetailView` | deviation | listed in More (web has no entry); embeds in small web views; YouTube unseen |
| 23–25 | `/ai-preferences`, `/artifacts`, `/artifacts/:kind`, `?create=1` | Artifacts tab documents | parity | documents switch in place (Deviations, M4) |
| 26 | `/share` | `ShareView` | parity | "Not available." without the flag |
| 27 | `/commons` | Commons tab (flag) | deviation | cards open the thread by id; never opened locally (staging) |
| 28 | `/account` | `AccountView` | deviation | Connect X in a cookie web view; paste the confirmation link; real X OAuth unseen |
| 28a | `/confirm-account-deletion` | `ConfirmAccountDeletionView` (paste sheet on Account) | deviation | the link is pasted, as for `/confirm-email` (#269) |
| 28b | `/account-restore`, `/account-deleted` | `AccountRestoreView`, `AccountDeletedView` (root screens) | parity | the restore question follows a sign-in by link or X (#269) |
| 29 | `/node/:id` (member) | `ThreadView` | deviation | iOS menus, narrower indents (Deviations, M2); visitors sign in first |
| 29a | `/node/:id` admin extras | — | **gap** | `SemanticNeighbors` rail and read rerun controls not built (admin only) |
| 30 | `/admin` | authenticated web view (More, admins) | parity | opened M5 as a non-admin (signed in, data refused); unseen as an admin |
| 31 | `/@username` | Safari view (More → My public page) | parity | the page is public; Safari has no session |
| 32 | `/@user/:slug` | member: `ThreadView` (M5); else Safari view | parity | checked M5 |
| 33 | `*` | Reflect | parity | |
| — | Connect X, X bookmarks connect | `CookieWebFlowSheet` | parity | design §6 web views; local landing checked M4, real OAuth on staging |

**Global UI (map A §4)**

| Piece | App | Status | Note |
|---|---|---|---|
| Terms modal | `TermsView` over everything | parity | text checked by `check_terms_text.py` |
| Updates modal | `UpdatesSheet` | deviation | bottom sheet; poll "Draft with AI" not exercised (billed) |
| Search modal | `SearchView` sheet | deviation | from the Log and References magnifiers (⌘K there); no app-wide ⌘K |
| NodeFormModal (Write New Entry, Edit, Reply, Edit Reference) | sheets | deviation | swipe down closes; the draft keeps the text |
| Craft mode dialog | `CraftModeDialog` | deviation | copy says "in More" |
| New token dialog | `NewTokenDialog` | deviation | "Tap" for "Click" |
| Delete, Rename, Regenerate audio, Apply to replies, Public reply, Split content, Unsaved changes dialogs | `.looreDialog` cards | parity | one presenter per dialog chain |
| Version history drawer | `VersionHistorySheet` | deviation | sheet with list → detail |
| Admin refusal, X lookup dialogs | admin web view | parity | |
| Toasts | `ToastStack` | parity | above the mini-player (M5); announced to VoiceOver (M5) |
| Spend-cap banner | `SpendCapBanner` | parity | unseen live (no capped user); unit-tested |
| Offline banner | Voice screen, writing form | parity | |
| Recovery banner | Voice screen | parity | Continue unit-tested only |
| Global audio player | `MiniPlayerView` above the tab bar | deviation | the phone card, plus the chapter menu |
| Profile generation watcher | `ProfileGenerationWatcher` on `AppState` | parity | unit-tested; a real build on staging |
| ProtectedRoute | `RootView` gating | parity | |
| User / Theme / Toast / Audio contexts | `AppState`, `ThemeManager`, `ToastCenter`, `AudioCenter` | parity | theme follows the system until chosen |
| Cross-screen signals, CSS offsets, module caches | `AppSignals`, measured mini-player height, stores | parity | offset added M5 |
| Per-device preferences | `DefaultsKey` (web names) | parity | |

**NavBar and ⋮ menu (map A §2.1)**

| Item | App | Status | Note |
|---|---|---|---|
| Brand, Reflect, Artifacts, Log, Commons | bottom tab bar | deviation | design §5; Commons only with the flag; no counts |
| About ▾ (signed out) | links under the sign-in card | parity | M5 |
| Login | sign-in screen | parity | |
| ⋮ | More tab | deviation | design §5 |
| Import data, Admin, Account, My public page | More rows | parity | |
| References | More row | deviation | not in the web menu |
| Light mode, Craft mode (+ dialog, toast, glow) | More rows | parity | |
| Write new entry, Export data, Prompts (craft) | More rows | parity | export via the share sheet |
| About: Why Loore, Vision, How To | More rows | parity | |
| Logout | More row | deviation | asks for confirmation |

Totals (a range of routes counts as one row): routes 15 parity · 11 deviation · 1 gap; global UI 12 parity ·
7 deviation; NavBar/⋮ 6 parity · 4 deviation. Overall **33 parity, 22 deviation, 1 gap** (56 rows).

---

## Review fixes (audio)

PR #384 review, voice/audio findings (branch `ios-fix-audio`, merged into `ios-app`). Each commit adds the unit
test that would have caught it; 355 unit tests pass (one skipped). No billed calls.

| Finding | Fix | Commit |
|---|---|---|
| B1 chunk left out at finalize; chunk 0 lost = no transcript | chunk 0 never given up while recording, nothing sent before it; one more attempt per missing chunk at Stop; still missing → no finalize, chunks stay queued, the user is told | `2e21622` |
| M9 `POST /tts` while the stream is attached | skipped for streamed replies; a 200 after chunks reconnects the stream; errors non-fatal while attached | `c2886c2` |
| M13 60 s net abandoned the reply | net removed (deviation listed) | `0be0430` |
| M12 cue loops indefinitely | turn ends at the llm-status deadline or after 2 min without an answer; cue capped at 10 min per wait (Stop → first chunk, or a drain), not per turn; pause in a drain stops the cue, play restarts it | `36ffb28`, per-wait cap in the commit after `7cf99cd` |
| M11 "next" during Stop cancelled before finalize | next is a no-op in `.stopping`; `switchToPlayback()` after the generation guard | `568220c` |
| M10 route re-applied on return to Voice | `VoiceRouteParameters` applies `parent`/`resume` once per screen | `d343192` |
| M15 failed mic restart was silent | one retry after 0.5 s, then an interruption (Resume, chime, toast, notification) in voice and dictation | `77f18cc` |
| M14 WebM did not play; failures silent | WebM recordings play the server's MP3 (seen on a local WebM node); failed items toast once and are skipped | `1bb8128` |
| M2 private files stayed on disk | dictation audio in memory until "Save audio"; share files deleted on dismiss; `tmp/` swept at launch and sign-out; sign-out deletes the upload queue and cancels its background tasks; fatal sessions keep no audio; Discard forgets local chunks | `9b3f426` |

**New heuristics (flagged):** `Timings.llmErrorGiveUp` = 2 min without an llm-status answer ends the turn;
`Timings.cueCap` = 10 min of thinking cue per wait (a per-turn total would silence the cue on long turns and let iOS suspend a locked app mid-reply); `MicrophoneSource.restartRetryDelay` = 0.5 s.

**Not changed, related:** the Minor "Resume numbering" finding (resumed chunks start at `chunk_count`) matters
more now, because B1 sends a recording with missing chunks to the recovery banner instead of finalizing it.
Sign-out does not warn about unsent chunks (the review's optional logout warning was not built).

---

## Deviations from the web

**Sign-in, shell and global UI (M1)**
- **Magic link is pasted into the app** (design §4): extra "I already have a sign-in link"
  step and paste field, plus a note that sign-up links work once. After sign-in the app opens Reflect (the
  web's default target is `/profile`); a link minted with another landing page (`/welcome`, `/confirm-email`)
  opens that page (M5).
- **More is a tab/screen**, not a dropdown; it also lists **References** (the web has no entry).
  Menu rows use `text-secondary` instead of the dropdown's `text-muted` for legibility on a full screen.
- **Logout asks for confirmation** (the web logs out on click): re-signing in on a phone
  means fetching a new email link.
- **Craft dialog copy** says "in More" where the web says "in the ⋮ menu" (no ⋮ menu in the app).
- **Terms screen**: same text; the card scrolls inside a dimmed backdrop like the web modal.
  Bold runs are Outfit 400 in `text-primary` (the web's `<strong>` on a 300 body).
- **Updates sheet** is a bottom sheet (half height for a single notification) instead of a
  centred card; external links open in Safari.
- **Home**: cards stack vertically on a phone (as the web does at that width).
- **Tab icons**: the Reflect tab uses the Loore mark; others are outline SF Symbols. No counts or badges.
- **Debug builds on a device default to Production** (simulator: Local); the design says Debug → Local.
- **Colours are code-defined dynamic `UIColor`s** (`Tokens.swift`) instead of asset-catalog colour
  sets (the launch colour and AccentColor are in the catalog).
- **Fonts**: Outfit comes from the upstream project's static TTFs, not the Google Fonts variable
  file (its named instances have no PostScript names, so iOS cannot select weights).

**Markdown, thread and writing (M2)**
- **Markdown**: `{quote:N}` markers show a "Loading quote…" chip until quotes load (the web shows
  the raw marker); because content is always split at markers, markdown never spans a marker
  (the web's final rendering, without its stateful-regex quirk). Footnotes are not parsed
  (swift-markdown has no footnote extension). Images sit on their own rows inside a paragraph;
  only Loore's own media (`/media/…`) loads by itself, any other image is an "Image from <host>"
  placeholder that loads in place when tapped (the web opens it in a new tab, #441).
  Links to deleted or inaccessible nodes are muted, non-tappable text ("[Node deleted]" /
  "[Node inaccessible]"; the web keeps them as links). External links open in an in-app Safari
  view and `mailto:` in Mail; links with any other scheme are plain text (`Router.externalHandling`,
  #442). The checklist "+" is always shown at 55 % opacity (the web shows it on hover).
- **Thread**: kebabs are iOS menus (Delete in the system's destructive red); the focal section is
  indented 8 pt (web 20 px) and each reply-tree level 24 pt (web 32 px) to fit a phone; the
  navigation title is the web's tab title but hidden (the page has its "Thread" heading; the title
  shows in the back button's menu); the delete dialog stays up while the orphaned-prompt check runs
  and swaps to the follow-up in place; the edit form opens 0.45 s after the prompt-edit confirm
  closes. Kept web quirks (D §10.9): expanded bubbles show raw quote markers; `llm-status.warnings`
  are not shown; auto-generate sends `preferred_model` when no picker is visible; the auto-generate
  check ignores the new reply's own AI usage; a split entry's reply goes under the head; clearing
  the text does not clear the server draft; a pin error replaces the page.
- **Model picker**: a popover list (the web's custom listbox): featured models, "More models…"
  expanding in place to the grouped list.
- **Writing**: selects are iOS menus; the text field grows with its content (3–12 lines inline,
  at least 40 % of the screen in Text mode). Paste over the cap is detected as a jump of more than
  one character past 100,000 (SwiftUI has no paste event). Write New Entry, Reply and Edit Text are
  sheets (swipe down closes; the draft keeps the text). Forms are rebuilt when craft mode changes.
- **Log**: refreshes after entries are created or deleted and on pull-to-refresh, not on every
  visit (the tab stays alive); "Delete thread" is a destructive menu item.
- **Search**: a sheet from the Log's magnifier (⌘K with a hardware keyboard); dates are optional
  date pickers ("yyyy-mm-dd" until set).

**Voice and audio (M3)**
- **Encoding**: AVFoundation refuses to encode with `.indefinite` segments (-11875 "only supports
  passthrough", HLS and CMAF profiles alike), so the app encodes AAC itself and the writer runs in
  passthrough. Same output format.
- **Pause**: timestamps stay continuous and the writer is kept, so a native pause does not open a server
  subsession (the web's resume does). After a system interruption the same writer continues; only a
  media-services reset starts a new one (ftyp chunk → subsession).
- **Lock-screen recording controls** are a Live Activity (Pause/Resume, Stop), not the Media Session
  mapping (play / pause / next = stop); see "Field test fixes" below. The Now Playing mapping remains the
  fallback without a Live Activity.
- **Thinking cue**: audible cue between Stop and the first chunk and whenever the queue drains while TTS
  is generating (design §9.4; no web counterpart), with Account → Voice → Soft / Very soft / Off.
- **Mic permission** is asked before `init` (the web asks after init and discards the draft on denial):
  no orphan drafts. Notification permission is asked at the first record tap, before recording starts.
- **`cancelled`** reply status ends the turn (the web stays on "Thinking...").
- **No 60 s safety net**: the web switches to its player 60 s after completion when no chunk came (for its
  autoplay block); the app keeps "Thinking…", the stream, reconcile and the cue until audio arrives.
- **A WebM draft** (started in desktop Chrome) cannot be continued natively: a toast says so instead of
  the web's `mime_mismatch` failure.
- **Offline stop**: after one chunk used up its retries, later chunks get one attempt each and go to the
  background session (the web retries each in full). Chunk 0 is never given up while recording and nothing
  is sent before it. At Stop every missing chunk gets one more attempt; if any is still missing the
  recording is not finalized (the web finalizes with what arrived): the chunks stay queued on the phone and
  the draft stays on the server for the recovery banner.
- **Dictation "Save audio"** appears once the first 15 s chunk exists (the web keeps an in-memory partial
  from the start) and is named `.m4a` (the web names MP4 audio `.webm`).
- **Proposal card** on the Voice screen is M2's compact card (the web uses `roomy`).
- **Leaving Voice**: popping the screen ends the conversation (web unmount); switching tabs keeps it running.
- **59-minute warning** in voice mode too (the web has it only in text mode, #243).
- Speaker icon title falls back to "Node N" (parity) because nodes pass no heading.

**Feature pages (M4)**
- **Workspace**: Profile, Todo and artifacts switch in place under the bubbles (no back step between
  documents, as with tabs); the version drawer is a sheet with a list and a detail page; revert failures toast
  "Couldn't revert." (web: console only); an artifact kind is validated against the backend's slug rule before
  Save and a save error shows the server's reason (web: console only); the todo row "+" is always shown at 55 %.
- **Todo saves** (design §10): tick / row add / quick-add re-fetch the todo and apply the same text-keyed
  edit to the fresh content before `PATCH`; the list re-fetches after "todo changed" and on return to the app.
  Edit-mode Save (`PUT`) writes a new version as typed (the replaced version stays in history).
- **References**: embeds run in small web views (widgets.js with a 15 s give-up → stored text; YouTube
  nocookie iframe with the frontend origin as referrer); the verdict/read row sits under the surfacing line, as
  the web wraps it at phone width; Edit is a sheet.
- **Account**: Connect X and X bookmarks connect run in a cookie-seeded web view that closes on the frontend
  landing; then the user reloads (Account) or the card refreshes (Import). "Paste the link" for an email
  confirmation is added to the pending-email notice and the waitlist (design §4.6); the confirmation shows in a
  sheet. "Sign out and use the other account" signs out (the link is pasted again after signing in). The default
  privacy menu has no disabled "Circles (coming soon)" row. The model picker is M2's full-width control.
- **Import**: native file picker; a JSON candidate counts as an array when its first non-blank byte is `[`
  and its last `]` (not parsed whole); files above 200 MB are refused before upload ("This file is larger than
  200 MB, the most Loore accepts in one upload."; ChatGPT keeps the web's 413 text); a confirm that hits nginx's
  60 s (504/502/timeout) says the import may still finish and to check the Log before importing again; a lost
  Twitter task says "check your Log" instead of "reload the page"; after "Import Finished" the app reloads the
  user and starts the profile watcher when the confirm handed a build off (the web reloads the page). "Click"
  copy became "Tap" in the token dialog.
- **Commons**: cards open the thread by node id (map E §8.2 iOS note), not the permalink.

---

## Known gaps

**Needs a real iPhone** (README "Only a real iPhone can test")
- Locked-phone voice turns, lock-screen controls, interruptions (calls, Siri), background uploads after the
  app is killed, AirPods (HFP → A2DP switch), the 59-minute mark, cue volumes.
- Emoji (this simulator runtime draws every emoji as a "?" box).
- A VoiceOver walk-through: labels were checked in code and through XCUITest queries only. In particular, Log /
  thread / reference cards are containers whose children VoiceOver reads; opening one relies on a double-tap
  reaching the card's tap gesture.
- Sign in with X is built but untested (needs X credentials and a real X account); it runs in a
  non-persistent `WKWebView` and ends when the web view reaches the frontend origin.

**Needs staging or data the local test user lacks** (README "Check on staging")
- Commons was never opened as the test user locally (it lists other users' public posts): checked with a
  fixture render and decoding tests only.
- Streaming voice TTS: the local backend runs with `STREAMING_VOICE_TTS` off, so turns used batch TTS
  (attach while `pending`, chunks after completion). The streaming path (`tts_streaming: true`, placeholder
  chapters) is unit-tested only; the flag is on in staging and production.
- No data for: read replies (FeedPicks, ReadWindowLine, ReadReplyTail are unit-tested only), a YouTube clip,
  an updated default prompt (banner), a running profile build (indicator; the watcher is unit-tested), filled
  intentions (fixture render only), `{quote_ext:N}` bubbles in a thread.
- Real X OAuth (Connect X; only the immediate local landing was seen), X bookmarks connect and sync.
- Admin: the web view is built but unseen as an admin (user 5 is not one; admin pages show other users'
  data). Not built: the admin rerun controls and the `SemanticNeighbors` rail.
- Glean (#475): built against #473/#474's API and seen in the simulator against canned answers only (the
  local backend runs `main`); the Voice glean, the Live Activity's Glean button and the ⋯ menu entry need
  the device and staging checklists.

**Not run against the backend** (billed, or would send mail / file issues)
- Audio file upload and chunked upload, recovered-audio drafts, the todo apply *success* path (a billed merge),
  Create issue and Send feedback, continuation chains and within-turn voice chains (unit-tested only), the
  recovery banner's Continue against the server (unit-tested), spend-cap and offline states (the 402 mapping and
  event are unit-tested), poll items in the Updates sheet ("Draft with AI" is billed).
- Sending / resending / cancelling an email change (would mail a link), the waitlist email form, Community
  Archive fetch and bookmarks JSON import (they rebuild the billed references digest), Claude and Twitter
  archive confirms, clipper token create/revoke, prompt edit / accept default, reference edit save,
  listen-aloud on profiles and references (billed TTS).

**Behaviour gaps**
- Listen-aloud has none of the Voice page's REST recovery (parity with the web).
- A dictation form that goes away mid-recording stops capture but leaves the audio session active until the
  next audio action.
- Big archives: the zip is read in a streaming way, but the analyze answer (all conversations) is held in
  memory to post it back, as the web does; a 200 MB `conversations.json` needs several hundred MB of RAM.
- The Updates sheet has no persistent "What's new" entry (map E §9 suggestion; not built).
- A long thread's accessibility tree makes XCUITest queries slow; on one long proposal thread a tap on
  "Apply changes to my Todo" did not register in the UI test (the same steps work on a shorter proposal).
- The Updates sheet's changelog bodies go through `MarkdownView` (`flowText`); not seen on screen with a real
  unread changelog entry.

---

## Backend findings (no backend changes made)

1. **`DELETE /api/drafts/` without `parent_id` deletes the user's first top-level draft, which can be a live
   voice recording.** Seen twice in M3 testing: another client (the M2 agent testing the writing form as the
   same user) saved and discarded a top-level text draft 8 s after this app's voice `init`; the voice draft was
   deleted, chunk uploads got 404 and finalize 404. In production this is a web tab or a second device
   submitting a new entry while a top-level voice recording runs (related to #320). Suggested fix: exclude
   drafts with a `session_id` in `delete_draft` (and in `save_draft`'s lookup).
2. Proposed follow-ups that would help the app (universal links, sliding sign-in, `.mp4` content type, push,
   numeric sign-in code): design doc §14.

## Record-keeping

- XCUITest flows need the local backend and are not in CI.
- `M4ScreensUITests` assume a test user with no todo/profile; clean up with `state m4_cleanup`.
- The first two M1 commits carry their `Co-Authored-By`/`Claude-Session` lines mid-message, and the M3 merge
  commit `73e60d7` (made with `--no-edit`) has none: not rewritten, per the no-amend rule.

## Review fixes (app)

Findings from the PR #384 review (comment 5924901081) outside `Audio/` and `Features/Voice/`
(the audio findings are fixed on a separate branch). One commit per finding; unit tests 334 → 351.

| Finding | Commit | Fix | Tests |
|---|---|---|---|
| B2 todo edits overwrite each other | `34f670c` | `TodoModel` chains each edit's re-fetch + `PATCH` after the previous one; shown = server's latest + edits in flight; a failure reverts only itself | two quick ticks, quick-add + tick, failed tick + good tick |
| M1 `remember_token` in a backed-up cookie file | `d3bb5ed` | `APIClient` on an ephemeral config with an in-memory jar; `CookieVault` is the only persistence; Set-Cookie answers notify `AuthService` (`APIClient.cookiesChanged`); `HTTPCookieStorage.shared` emptied at launch and sign-out and set to refuse cookies; the uploader's `cookieHeader` is set from `AppState` to read the app jar | jar not shared, shared jar cleared, uploader header, vault follows Set-Cookie; magic-link UI test + relaunch in the simulator, no token in `Library/Cookies` |
| M3 import never starts from the real picker | `6790333` | importer kind kept in `ImportModel.picking`, picker driven by `showingPicker` | SwiftUI order replayed; UI test `testImportThroughTheSystemFilePicker` (`LOORE_PICKER_FILE`) |
| M4 profile save overwrites a generated version | `69cae62` | edit records its base version; a newer latest at Save asks: save as new version (`POST /profile`) / discard / keep editing | `ProfilePage.saveTarget`; profile + todo UI flows |
| M5 draft autosave stops off screen | `dd82e30` | `onDisappear` saves what is pending, `onAppear` restarts the interval, `.background` saves inside a background task; `flush()` waits for a save in flight | three `DraftAutosaverTests`; UI test `testDraftTypedRightBeforeATabSwitchIsSaved` |
| M6 threads 256+ levels deep fail to decode | `78f0e8a` | `NodeTreeDecoding`: iterative scan cuts each node's `children` out, decodes one flat array, rebuilds bottom-up; used by `ThreadModel` and the Reply form (`APIClient.nodeDetail`) | 300-level chain, flat = nested on fixtures, deep `ThreadModel` load |
| M7 picked file size read before access | `830c424` | size read inside security-scoped access (`NodeFormModel.pickedFileSize`) | call order with fakes; real temp file. Needs a device check |
| M8 stuck Twitter import traps the user | `e94ba1e` | "Close" while polling (import continues on the server); stop with the check-your-Log message after **10 min without progress (new heuristic, `ImportModel.twitterStallLimit`)** | stall stop; close stops polling |
| M16 non-http(s) link crashes | `79fdae2` | `Router.externalHandling`: http(s) → Safari view, `mailto:` → system, anything else ignored | handling table; schemeless `.external` |

Notes:
- M1: the background upload session still names `HTTPCookieStorage.shared` (`ChunkUploader`, under `Audio/`); the
  jar is empty and refuses cookies, so nothing is written. Setting `httpShouldSetCookies = false` and
  `httpCookieAcceptPolicy = .never` there is left to the audio branch.
- M6: the Reply form keeps the web's fallback (account defaults) when the parent fetch fails for another reason. The
  server builds the nested answer recursively too (`serialize_node_recursive`); its depth limit was not checked.

## AI usage "none": no Voice mode, no speech (companion to the backend PR)

The backend change "Never generate AI replies for content marked 'none'; Voice mode explains instead" (branch
`fix/no-replies-for-none`) refuses a reply wherever the node replied to, or a node above it, is not chat/train.
The app follows it. 390 unit tests pass (one skipped), 17 of them new (`VoiceAIBlockTests`,
`SpeakerAIUsageTests`, five in `VoiceTurnTests`). No billed calls.

- **Voice screen block** (`Audio/Voice/VoiceAIBlock.swift`, `VoiceView`): instead of the record button, "Voice mode
  needs AI" and why, when a fresh conversation's account Default AI usage is None (read from the user), or when
  the thread the route continues (`parent`, else `resume`) is not AI-readable (`GET /api/voice/availability?parent=`).
  No answer from that route (an older server, offline) is not a block. The route's `parent` / `resume` are applied
  only once Voice mode is open, so a blocked thread's reply is not resumed. Buttons: "Account settings" (Account
  scrolled to the new `ai-usage` anchor on the Default AI usage row) and, for a thread, "Back to the thread" (pops
  when a thread is under Voice, else the thread replaces Voice). The check runs again on every return while
  blocked. The copy is the web's, word for word (`testCopyMatchesTheWeb`).
- **`ai_usage_none` from the server** (403, `code`, `scope` account/thread): `streaming/init` refused → the turn
  goes back to ready with the block (no draft, the mic never opens, no toast or red dot); the legacy `POST
  /api/voice` refused → the same; the thread's Voice Mode button refused by `voice/from-node` → opens the Voice
  screen on that thread, which shows the block (the web navigates to `/voice?parent=<id>`).
- **Voice Mode button** shows on every owned, non-public thread, also on 'none' nodes (the block lives on the
  Voice screen); Read and Auto-generate keep the AI-usage rule.
- **Finalize of a Voice draft with no reply allowed** (setting changed mid-recording): the server saves the
  transcript as an entry and answers `warning`; the app already toasts it and returns to ready.
- **Speaker icon** (`SpeakerButton.speechOff`): off for any node whose AI usage is not chat/train, model replies
  included (the server dropped the reply exemption in `speech_allowed`), and for a profile version whose
  `ai_usage` the server sends. The dashboard's `latest_profile` does not send `ai_usage` today, so the profile
  icon stays on there (parity with the web, whose icon gates nodes only).

## Field test fixes (#397, #398; 2026-10-02)

From Peter's first run on his iPhone. 395 unit tests pass (one skipped), 5 of them new.

**#397 Lock screen while recording and replying**
- **Live Activity** (`ios/LooreLiveActivity/`, a widget extension; state and intents in `ios/Shared/`
  compiled into both targets; app side `Audio/Session/VoiceLiveActivity.swift`). One activity per
  conversation: requested at a record tap, updated on phase changes from `AudioCenter.refreshNowPlaying`,
  ended by `VoiceTurnController.tearDown` (Voice screen closed, sign-out) and at launch (leftovers).
  Recording: "Recording" + a clock that runs by itself, ‖ and ■. Paused / interrupted: ▶ and ■. Thinking:
  no buttons. Replying and finished: a mic button = Record a reply (the Voice screen's Continue; stops the
  reply). Between turns (a cancelled or failed one): Record.
- **Why not Now Playing**: its slots are previous / play-pause / next, so Stop could only be "next track"
  beside a greyed-out "previous", and its commands cannot start the microphone from the background. The
  activity's Resume and Record buttons are `AudioRecordingIntent`s (iOS 18), which may start it; Pause and
  Stop are `LiveActivityIntent`s. All run in the app's process (`VoiceActivityCommands`).
- While the activity shows a recording, Now Playing is cleared; it returns for thinking and playback, where
  play/pause and ±10 s stay. Without an activity (iOS 17, Live Activities off, swiped away) the old Now
  Playing recording controls stand in. AirPods presses no longer pause a recording while the activity shows
  (they were Now Playing commands).
- **Resume**: `MicrophoneSource.resume()` does nothing when the engine still runs (a user pause only drops
  samples). Before, it called `prepare()` and `start()` on the running engine; the likely reason the lock
  screen's play did not resume (unconfirmed: the device log was not available). A failed resume now also
  posts a notification ("The recording could not resume"), withdrawn when a resume works; the error is logged.
- **Checked in the simulator** (iPhone 11 size, debug audio file, phone locked through XCUITest): record →
  lock → ‖ → ▶ → ■ → the reply plays with the activity's mic → mic → a second recording → ■ → second reply →
  leave Voice → the activity is gone. The simulator never suspends the app and cannot take the microphone
  away, so the background-microphone rules, calls and the 59-minute mark need the device checklist (the
  interruption and 59-minute paths are unit-tested: `testInterruptionPausesAlertsAndNotifies`,
  `testLongRecordingWarningAt59Minutes`).
- **Known**: after a reply played, iOS keeps an empty "Not Playing" platter above the activity during the
  next recording (the last audio app's platter; clearing the info and removing the command handlers did not
  remove it in the simulator). Dictation in the writing form has no lock-screen controls (unchanged).

- **Review fixes** (review comment on #384, M1, M2, m1): a lock-screen Record that cannot run now
  notifies instead of failing unseen: offline (checked before the reply is stopped), spending limit, AI
  usage None, a start that fails (the notification repeats its toast), and a tap on an activity left
  after iOS ended the app. For that last case, iOS launches the app in the background and runs the intent
  before SwiftUI creates `AppState`, so `VoiceActivityCommands.run` handles it with no handler set (seen in
  the simulator: app killed, Pause on the leftover card → the card goes and the notification request is
  added; this simulator never granted notifications, so the banner itself was not seen). The card shows "Starting…" (no clock, no buttons) until the microphone is on,
  so its clock no longer runs ahead. The wait for the microphone is `AudioCenter.lockScreenStartWait`
  (15 s, a heuristic). Open from that review: m2 (the "could not resume" notification stays after Stop),
  m3 (a `startSession` slower than the wait), m4 (device checks, in the README checklist), m5 (the trash
  icon sits next to Send and discards without confirmation, as on the web).

- **Second device round (2026-10-02)**: the card's buttons ask for the passcode on a locked phone, then
  run (Now Playing's do not). The intents already compile to "always allowed"; marking them
  `AudioPlaybackIntent` as well changed nothing (reverted). Not reproducible in the simulator, which has
  no passcode. With Face ID it is a glance. Thinking: no lock-screen command (the web's next = cancel
  read as "next track"); "next track" has a handler only for the fallback recording row. Also fixed: a new
  Voice screen is a new conversation (it had replied to the last, since deleted, thread and ignored
  `resume` after a finished turn); a cancel unloads the last reply (the lock screen had replayed it);
  Voice Mode on a thread and Text Mode open in the current tab above the thread (Voice always went to
  Reflect's stack, so Back led into Reflect's history).

**#398 Writing form and model picker on a narrow phone**
- Send keeps its words; Discard draft (trash), Record (mic), Save audio, Upload (paperclip), Resume and
  Stop & save are icons with spoken labels (`ButtonIcon`, `.looreIcon`); the recording clock and "Retry"
  stay words. The row is a `FlowLayout`, so it wraps instead of squeezing labels.
- The model picker next to LLM Response / Read is as wide as the model's name (the web's inline button),
  not 200 pt.

**#269 Account deletion (merges after the backend PR #464)**
- Account → "Delete my account" (shown when the server sends `account_deletion`): the web's dialog text in a
  sheet, the username typed, "Delete my account" / "Email me the confirmation link" and "Keep my account".
  An X-only account is scheduled at once and lands on "Your account is scheduled for deletion". An email
  account gets the link; the section then explains how to copy it from the email and offers "Paste the link"
  ("I already have a confirmation link" stays available). The pasted link opens the web's confirm question
  in the app's session (`POST /api/account/delete/confirm`). The waitlist screen shows the same section
  (#464 lets an unapproved account through to the deletion and restore routes).
- A sign-in into an account in its grace period (magic link landing, or the X web view landing on
  `/account-restore`) shows the native restore question instead of loading the user: "Restore my account"
  signs in as after any sign-in (Reflect, the server's `next` is not followed); "Keep it deleted" and
  "Back to Loore" return to sign-in. 401s from the signed-out session change nothing on these screens.
- Checked in the simulator against #464's backend on sqlite: X-only deletion, email deletion through the
  pasted link, restore after a magic-link sign-in, keep it deleted, a waitlisted X account deleting itself
  from the waitlist screen. Not seen: the X sign-in landing on the
  restore question (needs real X), and a sign-in surviving a relaunch (the unsigned simulator build keeps
  nothing in the Keychain, for any sign-in).
