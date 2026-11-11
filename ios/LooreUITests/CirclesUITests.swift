import XCTest

/// Circles, the screens that need taps (2026-11-11): the add-person dialog, the
/// picker in the writing form, the "discussion leading up to this node" toggle,
/// the readers popover, the who-will-see dialogs for a typed reply and an AI
/// reply, "Only me", a shared prompt for a reader, and the Updates item.
///
/// Runs against a scratch backend with fake users only (never the Docker stack):
/// `TEST_RUNNER_LOORE_E2E_DIR` holds `cookies.json` (session cookies keyed
/// `anna`, `ben`, `chidi`) and `ids.json` (`entry`, `agentic_turn`, `prompt`);
/// `TEST_RUNNER_LOORE_BACKEND_URL` is that backend. Model calls are stubbed
/// there, so the AI replies cost nothing. Skips without the directory.
/// Screenshots also go to `TEST_RUNNER_LOORE_SCREENSHOT_DIR`.
///
/// The seed: anna has the circle "Close friends" (ben in it, one entry shared);
/// `entry` is anna's, shared with ben and chidi; `agentic_turn` is anna's,
/// shared with ben together with the discussion above it; `prompt` is the
/// shared prompt of that thread; ben has a profile and no memory.
final class CirclesUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }

    override func setUp() {
        continueAfterFailure = true
    }

    private func snapshot(_ name: String) {
        let shot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: shot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let dir = env["LOORE_SCREENSHOT_DIR"], !dir.isEmpty {
            try? shot.pngRepresentation.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
    }

    private func json(_ file: String) throws -> [String: Any] {
        guard let dir = env["LOORE_E2E_DIR"], !dir.isEmpty else {
            throw XCTSkip("set TEST_RUNNER_LOORE_E2E_DIR (cookies.json and ids.json of the scratch backend)")
        }
        let data = try Data(contentsOf: URL(fileURLWithPath: dir).appendingPathComponent(file))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func id(_ key: String) throws -> Int { try XCTUnwrap(try json("ids.json")[key] as? Int, "no id \(key)") }

    private func launch(as user: String, route: String?, skipUpdates: Bool = true,
                        autoGenerateOff: Bool = false) throws -> XCUIApplication {
        let cookie = try XCTUnwrap(try json("cookies.json")[user] as? String, "no cookie for \(user)")
        let backend = try XCTUnwrap(env["LOORE_BACKEND_URL"].flatMap { $0.isEmpty ? nil : $0 },
                                    "set TEST_RUNNER_LOORE_BACKEND_URL (the scratch backend, not :5010)")
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreResetState", "YES", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", skipUpdates ? "YES" : "NO", "-LooreTheme", "dark",
                                "-loore.debug.localBackendURL", backend]
        if let frontend = env["LOORE_FRONTEND_URL"], !frontend.isEmpty {
            app.launchArguments += ["-loore.debug.localFrontendURL", frontend]
        }
        if let route { app.launchArguments += ["-LooreRoute", route] }
        // On one's own node LLM Response shows with auto-generate off (as on main).
        if autoGenerateOff { app.launchArguments += ["-loore_auto_generate", "NO"] }
        app.launch()
        return app
    }

    private func element(_ app: XCUIApplication, _ identifier: String) -> XCUIElement {
        app.descendants(matching: .any)[identifier].firstMatch
    }

    /// Any element whose label contains the text.
    private func text(_ app: XCUIApplication, containing part: String) -> XCUIElement {
        app.descendants(matching: .any).matching(NSPredicate(format: "label CONTAINS %@", part)).firstMatch
    }

    private func waitForFocal(_ app: XCUIApplication) {
        XCTAssertTrue(element(app, "thread.focal").waitForExistence(timeout: 20), "the thread did not load")
        sleep(2)
    }

    /// Scrolls up the page until the element can be tapped.
    private func bringIntoView(_ app: XCUIApplication, _ target: XCUIElement, swipes: Int = 6) {
        for _ in 0..<swipes where !(target.exists && target.isHittable) {
            app.swipeUp(velocity: .slow)
        }
    }

    // MARK: 1. Your circles

    func test1AddAPersonRenameAndNewCircle() throws {
        let app = try launch(as: "anna", route: "/circles")
        XCTAssertTrue(app.buttons["circles.new"].waitForExistence(timeout: 20), "the circles did not load")
        // An only circle shows open; one of several is opened with "Open".
        let open = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'circles.open.'")).firstMatch
        if open.exists { open.tap() }
        let field = element(app, "circles.addName")
        XCTAssertTrue(field.waitForExistence(timeout: 5), "no circle is open")
        field.tap()
        field.typeText("chidi")
        app.buttons["circles.add"].tap()
        let confirm = app.buttons["circles.addConfirm"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10), "the add dialog did not open")
        XCTAssertEqual(confirm.label, "Add @chidi", "the emphasized button adds for new entries")
        XCTAssertTrue(app.buttons["circles.alsoEarlier"].exists, "one entry is shared: the earlier entries are offered")
        XCTAssertEqual(app.buttons["circles.alsoEarlier"].label, "Also show earlier entries")
        XCTAssertTrue(text(app, containing: "stays closed to @chidi unless you choose").exists)
        snapshot("10-anna-add-person-dialog")
        app.buttons["Cancel"].firstMatch.tap()
        sleep(1)

        // The simulator's first keyboard shows a typing tip over it; then the keyboard goes.
        let tip = app.buttons["Continue"]
        if tip.exists { tip.tap() }
        if app.keyboards.firstMatch.exists { app.swipeDown() }
        sleep(1)
        let rename = element(app, "circles.renameOrDelete")
        XCTAssertTrue(rename.waitForExistence(timeout: 5))
        rename.tap()
        XCTAssertTrue(app.buttons["circles.delete"].waitForExistence(timeout: 5), "no rename-or-delete form")
        XCTAssertTrue(app.buttons["Save name"].exists)
        sleep(1)
        snapshot("11-anna-rename-or-delete")
        rename.tap() // the form is in the card: the same button closes it
        sleep(1)

        let new = app.buttons["circles.new"]
        bringIntoView(app, new)
        XCTAssertTrue(new.exists, "no New circle button")
        new.tap()
        XCTAssertTrue(element(app, "circles.newName").waitForExistence(timeout: 5), "no new-circle form")
        sleep(1)
        snapshot("12-anna-new-circle")
    }

    // MARK: 2. The picker in the writing form

    func test2ThePickerNamesTheReadersBeforeSharing() throws {
        let app = try launch(as: "anna", route: "/textmode")
        let circles = app.buttons["privacy.choice.circles"]
        XCTAssertTrue(circles.waitForExistence(timeout: 20), "no \"Who can read this\" in the new entry form")
        bringIntoView(app, circles)
        circles.tap()
        let chip = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'circles.chip.'")).firstMatch
        XCTAssertTrue(chip.waitForExistence(timeout: 10), "the circles did not load in the picker")
        bringIntoView(app, chip)
        chip.tap()
        let sentence = element(app, "circles.sentence")
        // dana joined Close friends with "Only new entries": a new entry is hers to read.
        let named = NSPredicate(format: "label == %@", "@ben and @dana can read this entry and reply to it. Nobody else can.")
        wait(for: [expectation(for: named, evaluatedWith: sentence)], timeout: 10)
        bringIntoView(app, sentence)
        let send = element(app, "nodeForm.send.new")
        XCTAssertEqual(send.label, "Share with Close friends")
        snapshot("13-anna-picker-readers-sentence")
        app.swipeUp(velocity: .slow)
        sleep(1)
        snapshot("14-anna-picker-share-button")
    }

    // MARK: 3. The discussion above an entry

    func test3TheEditFormShowsTheDiscussionToggleOn() throws {
        let app = try launch(as: "anna", route: "/node/\(try id("agentic_turn"))")
        waitForFocal(app)
        element(app, "thread.focalKebab").tap()
        app.buttons["Edit"].firstMatch.tap()
        let toggle = element(app, "circles.sharePath")
        XCTAssertTrue(toggle.waitForExistence(timeout: 15), "no \"Share also the discussion…\" toggle")
        bringIntoView(app, toggle)
        XCTAssertEqual(toggle.label, "Share also the discussion leading up to this node?")
        XCTAssertEqual(toggle.value as? String, "On", "the path above this entry is shared")
        snapshot("15-anna-edit-discussion-toggle")
        app.swipeUp(velocity: .slow)
        sleep(1)
        snapshot("16-anna-edit-discussion-note")
    }

    // MARK: 4. A reader: who reads the entry, and who sees a reply

    func test4ReadersPopoverAndTheReplyDialog() throws {
        let app = try launch(as: "ben", route: "/node/\(try id("entry"))")
        waitForFocal(app)
        XCTAssertTrue(element(app, "thread.sharedBanner").exists, "no banner over the shared entry")
        let readers = element(app, "thread.readers")
        bringIntoView(app, readers)
        readers.tap()
        XCTAssertTrue(text(app, containing: "@anna, the author").waitForExistence(timeout: 5), "the readers list did not open")
        XCTAssertTrue(app.staticTexts["@chidi"].exists)
        XCTAssertTrue(app.staticTexts["You"].exists)
        snapshot("17-ben-readers-popover")
        // A tap outside closes the popover.
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.12)).tap()
        sleep(1)

        let field = element(app, "nodeForm.text.inline")
        bringIntoView(app, field)
        field.tap()
        field.typeText("That makes sense to me.")
        let send = element(app, "nodeForm.send.inline")
        bringIntoView(app, send)
        send.tap()
        let confirm = app.buttons["circles.replyConfirm"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10), "the who-will-see dialog did not open")
        XCTAssertTrue(app.staticTexts["Send this reply?"].exists)
        XCTAssertTrue(app.staticTexts["@anna and @chidi will see it, with your name."].exists)
        XCTAssertEqual(confirm.label, "Send reply")
        snapshot("18-ben-reply-dialog")
        app.buttons["Cancel"].firstMatch.tap()
        sleep(1)
        // Nothing was sent; the typed text is discarded so no draft stays behind.
        let discard = element(app, "nodeForm.discardDraft")
        if discard.exists && discard.isHittable { discard.tap() }
    }

    // MARK: 5. An AI reply in a shared thread

    func test5AnAIReplySharedThenOnlyMe() throws {
        let turn = try id("agentic_turn")
        var app = try launch(as: "ben", route: "/node/\(turn)")
        waitForFocal(app)
        let llm = app.buttons["thread.llmResponse"]
        bringIntoView(app, llm)
        XCTAssertTrue(llm.exists, "no LLM Response control for a reader on someone else's node")
        XCTAssertTrue(element(app, "thread.aiReplyPrivacy.shared").exists, "no \"AI reply:\" choice")
        snapshot("19-ben-ai-reply-control")
        llm.tap()
        let confirm = app.buttons["circles.replyConfirm"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10), "the AI dialog did not open")
        XCTAssertTrue(app.staticTexts["Generate an AI reply?"].exists)
        XCTAssertTrue(text(app, containing: "@anna will see it, marked with your name.").exists)
        XCTAssertTrue(text(app, containing: "Your AI reads your profile").exists, "the artifacts sentence is missing")
        XCTAssertTrue(text(app, containing: "and could share private information from them.").exists)
        XCTAssertEqual(confirm.label, "Generate")
        XCTAssertTrue(confirm.isHittable, "Generate is off the screen")
        snapshot("20-ben-ai-dialog")
        confirm.tap()
        // The reply opens on its own page; the stub answers at once.
        XCTAssertTrue(text(app, containing: "Stub reply from").waitForExistence(timeout: 30), "no AI reply appeared")
        let label = text(app, containing: "via @ben")
        XCTAssertTrue(label.waitForExistence(timeout: 10), "no label on the AI reply")
        XCTAssertTrue(label.label.contains("GPT-6 Luna via @ben"), "the label reads \(label.label)")
        sleep(2)
        snapshot("21-ben-ai-reply-shared")

        // Only me in a thread whose shared prompt reads ben's data: the dialog in
        // its private variant, then a private reply.
        app = try launch(as: "ben", route: "/node/\(turn)")
        waitForFocal(app)
        let onlyMe = element(app, "thread.aiReplyPrivacy.onlyMe")
        bringIntoView(app, onlyMe)
        onlyMe.tap()
        XCTAssertTrue(onlyMe.isSelected, "Only me was not chosen")
        snapshot("22-ben-ai-reply-only-me-chosen")
        app.buttons["thread.llmResponse"].tap()
        let privateConfirm = app.buttons["circles.replyConfirm"]
        XCTAssertTrue(privateConfirm.waitForExistence(timeout: 10), "the private AI dialog did not open")
        XCTAssertTrue(app.staticTexts["Generate an AI reply?"].exists)
        XCTAssertTrue(text(app, containing: "Only you will see it. Your AI reads your profile").exists)
        XCTAssertTrue(text(app, containing: "for this reply, as the prompt shared in this thread asks.").exists)
        XCTAssertFalse(text(app, containing: "will see it, marked with your name").exists, "no names for a private reply")
        XCTAssertFalse(text(app, containing: "could share private information").exists)
        XCTAssertTrue(privateConfirm.isHittable, "Generate is off the screen")
        snapshot("22b-ben-ai-dialog-only-me")
        privateConfirm.tap()
        XCTAssertTrue(text(app, containing: "Stub reply from").waitForExistence(timeout: 30), "no private AI reply appeared")
        XCTAssertFalse(element(app, "thread.readers").exists, "a private reply has no readers")
        sleep(2)
        snapshot("23-ben-ai-reply-only-me")
        // `-LooreResetState` forgets the choice: the next launch is back to shared.
        app = try launch(as: "ben", route: "/node/\(turn)")
        waitForFocal(app)
        let shared = element(app, "thread.aiReplyPrivacy.shared")
        bringIntoView(app, shared)
        XCTAssertTrue(shared.isSelected, "-LooreResetState should forget \"Only me\"")
    }

    // MARK: 6. The AI dialog where nothing of the requester's is read

    func test6TheAIDialogWithoutTheArtifactsSentence() throws {
        let app = try launch(as: "ben", route: "/node/\(try id("entry"))")
        waitForFocal(app)
        let llm = app.buttons["thread.llmResponse"]
        bringIntoView(app, llm)
        llm.tap()
        let confirm = app.buttons["circles.replyConfirm"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10), "the AI dialog did not open")
        XCTAssertTrue(app.staticTexts["@anna and @chidi will see it, marked with your name."].exists)
        XCTAssertFalse(text(app, containing: "Your AI reads your").exists, "nothing of ben's is read here")
        snapshot("24-ben-ai-dialog-no-artifacts")
        app.buttons["Cancel"].firstMatch.tap()
        sleep(2)
        // Cancel generates nothing: the page is still the entry, its control at rest.
        XCTAssertTrue(element(app, "thread.sharedBanner").exists, "Cancel left the entry's page")
        XCTAssertEqual(app.buttons["thread.llmResponse"].label, "LLM Response")
    }

    // MARK: 7. A shared prompt, for a reader

    func test7ASharedPromptEmbedsTheReadersOwnArtifacts() throws {
        let app = try launch(as: "ben", route: "/node/\(try id("prompt"))")
        waitForFocal(app)
        snapshot("25-ben-shared-prompt-top")
        XCTAssertFalse(element(app, "thread.focalKebab").exists, "a reader gets no Edit or Delete on the prompt")
        // ben's profile is a card that opens to his own text.
        let profile = app.buttons.matching(NSPredicate(format: "label CONTAINS 'User Profile'")).firstMatch
        bringIntoView(app, profile, swipes: 30)
        XCTAssertTrue(profile.exists, "no profile card in the prompt")
        XCTAssertFalse(profile.label.contains(" v"), "no version for a reader: \(profile.label)")
        profile.tap()
        XCTAssertTrue(text(app, containing: "BENS-PROFILE").waitForExistence(timeout: 5), "ben's own profile is not embedded")
        snapshot("26-ben-shared-prompt-own-profile")
        // He has no memory: the placeholder itself.
        let memory = app.staticTexts["{user_memory}"]
        bringIntoView(app, memory, swipes: 30)
        XCTAssertTrue(memory.exists, "a missing artifact should show as its placeholder")
        snapshot("27-ben-shared-prompt-placeholders")
    }

    // MARK: 9. A reply under one's own Only-me AI reply

    func test9AReplyUnderOnesOwnPrivateAIReplyIsOnlyMe() throws {
        let app = try launch(as: "ben", route: "/node/\(try id("bens_private_ai"))")
        waitForFocal(app)
        let note = text(app, containing: "You are replying under a node only you can read")
        bringIntoView(app, note, swipes: 10)
        XCTAssertTrue(note.exists, "the reply form does not say the reply is Only me")
        XCTAssertFalse(app.buttons["privacy.choice.circles"].exists, "Circles is offered where the server refuses it")
        XCTAssertFalse(app.buttons["privacy.choice.public"].exists, "Everyone is offered where the server refuses it")
        sleep(1)
        snapshot("29-ben-reply-under-own-private-ai")
    }

    // MARK: 10–13. Second review: Only-me branches, the sentence before sharing

    /// An AI reply under one's own Only-me AI reply in a thread whose prompt
    /// reads the member's data: asked about in the private wording, then made.
    private func onlyMeAIReply(on key: String, shot: String) throws {
        let app = try launch(as: "ben", route: "/node/\(try id(key))", autoGenerateOff: true)
        waitForFocal(app)
        let llm = app.buttons["thread.llmResponse"]
        bringIntoView(app, llm)
        XCTAssertTrue(llm.exists, "no LLM Response under ben's own Only-me node")
        llm.tap()
        let confirm = app.buttons["circles.replyConfirm"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 10), "the private AI dialog did not open")
        XCTAssertTrue(app.staticTexts["Generate an AI reply?"].exists)
        XCTAssertTrue(text(app, containing: "Only you will see it. Your AI reads your profile").exists)
        XCTAssertFalse(text(app, containing: "will see it, marked with your name").exists)
        snapshot("\(shot)a-dialog")
        confirm.tap()
        XCTAssertTrue(text(app, containing: "Stub reply from").waitForExistence(timeout: 30), "no AI reply was made")
        XCTAssertFalse(element(app, "thread.readers").exists, "an Only-me reply has no readers")
        sleep(2)
        snapshot("\(shot)b-reply")
    }

    func test10AnAIReplyUnderOnesOwnOnlyMeAIReply() throws {
        try onlyMeAIReply(on: "bens_agentic_private_ai", shot: "30")
    }

    func test11AnAIReplyInOnesOnlyMeBranch() throws {
        try onlyMeAIReply(on: "bens_private_note", shot: "31")
    }

    func test12TheEditSentenceNamesOnlyWhoCanReadTheEntry() throws {
        let app = try launch(as: "anna", route: "/node/\(try id("entry"))")
        waitForFocal(app)
        element(app, "thread.focalKebab").tap()
        app.buttons["Edit"].firstMatch.tap()
        let sentence = element(app, "circles.sentence")
        XCTAssertTrue(sentence.waitForExistence(timeout: 15), "no readers sentence in the edit form")
        bringIntoView(app, sentence)
        let named = NSPredicate(format: "label == %@", "@ben and @chidi can read this entry and reply to it. Nobody else can.")
        wait(for: [expectation(for: named, evaluatedWith: sentence)], timeout: 10)
        XCTAssertFalse(sentence.label.contains("@dana"), "dana was added after the entry was shared: she does not read it")
        snapshot("32-anna-edit-sentence")
    }

    func test13ANameTheSentenceDidNotNameStopsTheShare() throws {
        let app = try launch(as: "anna", route: "/textmode")
        let editor = element(app, "nodeForm.text.new")
        XCTAssertTrue(editor.waitForExistence(timeout: 20), "no new entry form")
        editor.tap()
        editor.typeText("An entry for the people the sentence names.")
        let circles = app.buttons["privacy.choice.circles"]
        bringIntoView(app, circles)
        circles.tap()
        let chip = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'circles.chip.'")).firstMatch
        XCTAssertTrue(chip.waitForExistence(timeout: 10))
        bringIntoView(app, chip)
        chip.tap()
        let sentence = element(app, "circles.sentence")
        let before = NSPredicate(format: "label == %@", "@ben and @dana can read this entry and reply to it. Nobody else can.")
        wait(for: [expectation(for: before, evaluatedWith: sentence)], timeout: 10)
        let names = element(app, "circles.names")
        bringIntoView(app, names)
        names.tap()
        names.typeText("@chidi")      // not finished: the sentence does not name chidi yet
        let send = element(app, "nodeForm.send.new")
        bringIntoView(app, send)
        send.tap()
        let stopped = text(app, containing: "Who can read this has changed. Check the names above, then share again.")
        XCTAssertTrue(stopped.waitForExistence(timeout: 10), "a reader the sentence did not name was shared with")
        let after = NSPredicate(format: "label == %@",
                                "@ben, @chidi and @dana can read this entry and reply to it. Nobody else can.")
        wait(for: [expectation(for: after, evaluatedWith: sentence)], timeout: 10)
        sleep(1)
        snapshot("33-anna-share-stopped")
        // Nothing was saved: the draft is discarded so no entry or draft stays.
        let discard = element(app, "nodeForm.discardDraft")
        if discard.exists && discard.isHittable { discard.tap() }
    }

    // MARK: 8. The note about a shared entry

    func test8TheUpdatesSheetHasTheShareNote() throws {
        let app = try launch(as: "ben", route: nil, skipUpdates: false)
        let read = app.buttons.matching(identifier: "updates.circleRead").firstMatch
        XCTAssertTrue(read.waitForExistence(timeout: 20), "no share note in the Updates sheet")
        XCTAssertEqual(read.label, "Read it")
        XCTAssertEqual(app.buttons.matching(identifier: "updates.circleHide").firstMatch.label, "Don't show @anna's entries")
        XCTAssertTrue(app.staticTexts["@anna is not told what you choose."].firstMatch.exists)
        sleep(1)
        snapshot("28-ben-updates-share-note")
    }
}
