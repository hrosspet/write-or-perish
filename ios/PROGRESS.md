# iOS app: progress and hand-off

The spec is [`docs/IOS-APP-DESIGN.md`](../docs/IOS-APP-DESIGN.md) (§13 milestones).
The web app's behaviour is mapped in `ios/docs/web-app-map/A–E`. Precedence: for
how the web behaves, the web code wins; for what the app should do, the design doc.

| Milestone | Branch | Status |
|---|---|---|
| M1 Foundation | `ios-app` | **done** (2026-10-01) |
| M2 Thread and writing | `ios-app` | **done** (2026-10-01) |
| M3 Voice and audio | `ios-app-voice` | in progress (started in parallel with M2) |
| M4 Feature pages | `ios-app-pages` | next, parallel with M3 |
| M5 Integration and parity | `ios-app` | last |

---

## How to work on this (read first)

**Environment**
- Worktree `.claude/worktrees/ios-app`, branch `ios-app`. Draft PR "Native iPhone app (SwiftUI)".
- Xcode 26.3 is at `/Applications/Xcode.app` but `xcode-select` points at the CLT and sudo
  is unavailable: prefix every Xcode command with
  `export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer;`.
- `simctl` commands that boot or install need the sandbox disabled in the agent shell.
- If `xcrun simctl list runtimes` is empty although `xcrun simctl runtime list` shows the
  iOS 26.3 disk image "Ready", run `xcrun simctl runtime scan-and-mount` (sandbox off) once.
- Simulators: M1 used "Loore iPhone 17" (`CDAD3013-…`), M2 "Loore M2 iPhone 17"
  (`0A4086FA-7D51-4ACF-ADDF-C90377075FEE`), M3 "Loore Voice iPhone 17". Use your own; create one with
  `xcrun simctl create "<name>" com.apple.CoreSimulator.SimDeviceType.iPhone-17 com.apple.CoreSimulator.SimRuntime.iOS-26-3`.
- This simulator runtime draws **every emoji as a "?" box**, even in the system font (checked with
  an `ImageRenderer` test); it is the runtime, not the app. Check emoji on a device.
- User 5 is shared with the other milestone agents: its `preferred_model` and `craft_mode` can change
  under you. Check `GET /api/dashboard/` before a billed step (the auto-generate path sends the
  preferred model, as on the web); restore anything you switch.

**Build and test**
```sh
cd ios && xcodegen          # always after adding/removing files
xcodebuild -project Loore.xcodeproj -scheme Loore -destination 'platform=iOS Simulator,id=<UDID>' \
  -derivedDataPath build/DerivedData test          # 197 unit tests, ~5 s of test time
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
ios/Loore/Markdown/             M2: the one markdown renderer — MarkdownView (+MarkdownStyle presets,
                                ChecklistActions), MarkdownModel (swift-markdown → render tree, GFM autolinks,
                                HTML as code, list tightness), MarkdownEdits (utils/markdown.js port), JSRegex,
                                ContentSegments + QuotedContentView (quote/artifact markers, quote bubbles),
                                NodeLinks (+NodeTitleStore on AppState.nodeTitles)
ios/Loore/Features/Thread/      M2: ThreadView/ThreadModel/ThreadSheets, Bubble (BubbleView, BubbleData,
                                BubblePreview, KebabMenu, NodeFooterView), ModelPicker (+ModelCatalog),
                                NodeAudioControls (M3 hook)
ios/Loore/Features/Write/       M2: NodeFormView/NodeFormModel (+DictationButton, M3 hook), DraftAutosaver,
                                PrivacySelector (+SelectField, FieldLabel, LabeledPillToggle), WritingDialogs,
                                TextModeView (+WriteNewEntrySheet)
ios/Loore/Features/Proposals/   M2: ProposalParser (ProposalInline.js port), ProposalCard (compact)
ios/Loore/Features/Log/         M2: LogView
ios/Loore/Features/Search/      M2: SearchView (+SearchSnippet, SearchResult)
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

**Conventions M2 added**
- Markdown anywhere: `MarkdownView(markdown:style:checklist:onLink:)`; node content with quote and
  artifact markers: `QuotedContentView`. Pick or derive a `MarkdownStyle` preset (`.focal`, `.bubble`,
  `.quote`, `.proposal`, `.changelog`); `flowText` makes soft breaks spaces (authored docs only).
- Text matching for list edits: `MarkdownEdits` (same strings as the web). Port JS regexes with `JSRegex`
  (UTF-16, ASCII `\w`), lengths with `.jsLength`, cuts with `.jsPrefix`.
- Italics in Outfit: `LooreFont.sansOblique` (Outfit has no italic face; `.italic()` does nothing).
- Chained dialogs: present them through **one** `.looreDialog` whose content switches (two
  full-screen covers cannot dismiss and present in the same update; the second one is dropped).
- Work a view starts on appear that must not be cancelled by a re-render goes in its own `Task`,
  not `.task(id:)` (a cancelled `URLSession` call is a silent failure).
- Test identifiers: `thread.focal`, `thread.focalKebab`, `thread.llmResponse`, `nodeForm.text.<new|inline|edit>`,
  `nodeForm.send.<…>`, `search.field`, `more.writeNew`, `home.read`.

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

## Done in M2

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
  Used in the thread, expanded bubbles, quote cards, artifact sections, proposal cards and the
  Updates sheet (`SimpleMarkdownText` deleted).
- **Home**: admin-only Read card (`POST /api/read/start`).
- **Log**: cursor paging (auto-load at the end, "Load more...", retry), dedupe, Rename thread,
  Delete thread, search button (⌘K), pull to refresh; refreshes after creates/deletes.
- **Thread**: header with Voice Mode (→ `/voice`, M3) and Auto-generate (craft); admin Read /
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
- **M3 hooks**: `NodeAudioControls` (focal footer speaker/download, `ThreadSheets.swift`) and
  `DictationButton` (shown, disabled) + `NodeFormModel.dictationStarted/Transcript/Finished/Failed`.

### Verified in M2 (simulator "Loore M2 iPhone 17", local Docker backend, user 5)
- Unit tests: 197 pass (M1's 106 + 91): `ProposalParserTests` (ProposalInline.test.js one-to-one),
  `MarkdownEditsTests` (markdown.test.js), `MarkdownParserTests` (MarkdownBody.test.js and
  MarkdownBody.stable.test.js #321 equivalents, autolinks, tables, tightness), `BubblePreviewTests`
  (Bubble.test.js), `NodeLinksTests` (nodeLinks.test.js), `ModelPickerTests` (ModelSelector.test.js
  `pickerOptions` + default rules), `ContentSegmenterTests`, `NodeFormModelTests` (decision tree on
  a stubbed backend), `ThreadModelTests` (reply rules, hand-off, completion, continuation, failure),
  `SearchSnippetTests`.
- UI flows (screenshots in the session scratch dir, compared with headless-Chrome shots of the web at
  402×874): Log (cards, kebab, rename and delete dialogs), search (empty, dates, a "rivers" query and
  opening a result), threads 201102 (system prompt with artifact chips), 201105/201121 (proposal
  cards; tick/untick saved; Apply on a superseded proposal shows the server's 404 text), 201071/
  201076 (ancestors, reply tree), 201000 (tombstone), 200961; kebab/Edit/Reply/Delete; a Text-mode
  entry with a checklist (tick and "+" saved), edit, delete with "Delete the system prompt too?";
  craft mode on/off (selectors, Upload, LLM Response bar, model picker with "More models…", Write
  New Entry); draft saved, restored after relaunch, discarded; light theme; a temporary entry with
  every markdown construct and a node link; quote bubbles (`{quote:N}` and an inaccessible one);
  a `:::share` block saved as a private draft.
- Billed calls: **3 LLM replies** — LLM Response on GPT-6 Luna (watched to completion), a Text-mode
  reply on GPT-6 Luna (watched streaming: partial text + dots → final), an inline reply with
  auto-generate (went to Opus 5.5: the shared user's preferred model had been switched back during
  the run) — plus one semantic search embedding. Test entries, the share draft and craft mode were
  cleaned up / restored.
- Bugs found and fixed by these runs: chained dialogs dropped the follow-up (one presenter now);
  the inline form's draft load was cancelled by a re-render (draft not restored); forms kept their
  craft-off controls after craft mode was switched on; bubble footers squeezed the author away
  next to wide tags; italics had no effect in Outfit.

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
- **Markdown (M2)**: `{quote:N}` markers show a "Loading quote…" chip until quotes load (the web shows
  the raw marker); because content is always split at markers, markdown never spans a marker
  (the web's final rendering, without its stateful-regex quirk). Footnotes are not parsed
  (swift-markdown has no footnote extension). Images sit on their own rows inside a paragraph.
  Links to deleted or inaccessible nodes are muted, non-tappable text ("[Node deleted]" /
  "[Node inaccessible]"; the web keeps them as links). External links open in an in-app Safari
  view; `mailto:` and other schemes go to the system. The checklist "+" is always shown at 55 %
  opacity (the web shows it on hover).
- **Thread (M2)**: kebabs are iOS menus (Delete in the system's destructive red); the focal section is
  indented 8 pt (web 20 px) and each reply-tree level 24 pt (web 32 px) to fit a phone; the
  navigation title is the web's tab title but hidden (the page has its "Thread" heading; the title
  shows in the back button's menu); the delete dialog stays up while the orphaned-prompt check runs
  and swaps to the follow-up in place; the edit form opens 0.45 s after the prompt-edit confirm
  closes. Kept web quirks (D §10.9): expanded bubbles show raw quote markers; `llm-status.warnings`
  are not shown; auto-generate sends `preferred_model` when no picker is visible; the auto-generate
  check ignores the new reply's own AI usage; a split entry's reply goes under the head; clearing
  the text does not clear the server draft; a pin error replaces the page.
- **Model picker (M2)**: a popover list (the web's custom listbox): featured models, "More models…"
  expanding in place to the grouped list.
- **Writing (M2)**: selects are iOS menus; the text field grows with its content (3–12 lines inline,
  at least 40 % of the screen in Text mode). Paste over the cap is detected as a jump of more than
  one character past 100,000 (SwiftUI has no paste event). Write New Entry, Reply and Edit Text are
  sheets (swipe down closes; the draft keeps the text). Forms are rebuilt when craft mode changes.
- **Log (M2)**: refreshes after entries are created or deleted and on pull-to-refresh, not on every
  visit (the tab stays alive); "Delete thread" is a destructive menu item.
- **Search (M2)**: a sheet from the Log's magnifier (⌘K with a hardware keyboard); dates are optional
  date pickers ("yyyy-mm-dd" until set).

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
- Not built in M1 (by plan): voice, feature pages (M2 built thread, writing, markdown, replies, search).

## Known gaps (after M2)

- **Emoji** render as "?" boxes in this simulator runtime (system font included); unverified on a device.
- **Admin read feature** (map D §5.9): the Home Read card and the thread's Read / Read further buttons
  (with the read-model picker) are built but untested (user 5 is not an admin). Not built: legacy
  `FeedPicks`, `ReadReplyTail` ("Mark all as read"), the admin rerun controls, `ReadWindowLine`,
  the `SemanticNeighbors` rail.
- **Not exercised against the backend**: audio file upload and chunked upload (billed transcription;
  no file in the simulator), recovered-audio drafts, the todo apply *success* path (a billed merge on
  the reply's Opus model; the 404 path is verified), Create issue and Send feedback (would file a
  real issue / message), continuation chains (only unit-tested; cannot be forced), spend-cap and
  offline states, `{quote_ext:N}` bubbles (user 5 has no saved references).
- The Updates sheet's changelog bodies now go through `MarkdownView` (`flowText`); not re-seen on
  screen in M2 (user 5 has no unread changelog entry).
- A long thread's accessibility tree makes XCUITest queries slow; on 201121 a tap on "Apply changes
  to my Todo" did not register in the UI test (the same steps work on a shorter proposal).
- Voice Mode in the thread posts `/voice/from-node` (billed when it starts a reply) and opens the
  Voice placeholder until M3.

## Notes for M3 (voice and audio)
- Speaker / download on nodes: replace `NodeAudioControls` (`Features/Thread/ThreadSheets.swift`);
  it receives the node id, content, public flag, AI usage and `hasTTS` (refresh the thread's
  `node.hasTTS` after generating, as the web's `onTtsGenerated`).
- Dictation: replace `DictationButton` (`Features/Write/NodeFormView.swift`) and drive the model's
  `dictationStarted()`, `dictationTranscript(_:)`, `dictationFinished(sessionId:transcript:)`,
  `dictationFailed(_:spendCapped:)`; Send then uses `save-as-node` (already implemented, incl. the
  Text-mode and read-reply overrides). The button must honour `model.audioDisabledReason` and the
  spend-cap pre-check (`model.uploadPressed()` shows the pattern for Upload).
- `ProposalCard` is the compact variant only; the Voice page's roomy variant needs a size parameter
  (labels "GitHub Issue Proposal", "Create GitHub Issue", "Feedback for the Team", "Save to your
  shares", pulsing AI dot). The parser and accept flows are shared.
- `ThreadModel.startVoice()` opens `.voice(parentId:resumeLLMId:)` exactly as the web navigates.

## Notes for M4 (feature pages)
- Render every markdown body with `MarkdownView` (Profile, Todo, artifacts, references, prompts);
  `MarkdownStyle` takes the page's font size/weight/colour. Todo checklists: pass `ChecklistActions`
  and use `MarkdownEdits.toggleCheckbox / insertItemAfter / appendItemToSection` (Todo quick-add)
  so lines match the web's. A single-line inline variant (`MarkdownBody inline`) is not built:
  use `InlineText` or add it.
- References list: `BubbleView` has no footer/tag slots yet (the web's `footer` / `tag` props);
  add them. `DeleteConfirmDialog` needs the `reference` mode ("Delete reference?").
  `ReferenceFeedbackControl` (Good/Bad quote) is in `Markdown/QuotedContentView.swift`; move it if
  the reference page wants it elsewhere. `SearchView(scope: .external)` is the References search.
- Account's preferred-model picker: `ModelPicker(nodeId: nil, …)` asks `/nodes/default-model`.
- `RenameThreadDialog`, `NodeFormSheet`, `LooreTag`, `FieldLabel`/`SelectField`, `NetworkStatus`,
  `NodeTitleStore`, `InlineArtifactSection.formatTokens` and `JSRegex` are shared building blocks.
- Version-history diff (`utils/diff.js` + tests) is still to port (M4).

## Notes for M5
- Merging M3/M4 touches the M2 hooks above and `RouteDestination` (`.voice`, M4 routes).
- Update the design docs named in `CLAUDE.md` (M2 did not).
- XCUITest flows need the local backend and are not in CI.
- The first two M1 commits carry their `Co-Authored-By`/`Claude-Session` lines mid-message
  (a quoting slip; not rewritten, per the no-amend rule).
