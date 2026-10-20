import XCTest

/// Peter's repros, against the local backend as the test user: Reflect home →
/// Text → type → Send (auto-generate off: no reply) or Discard → back → Text
/// again. The text must not come back as the draft.
///
/// Two causes, two setups:
/// - #425, the autosave in flight when Send deletes the draft: Send right after
///   typing, through `scripts/delay_proxy.py` (`TEST_RUNNER_LOORE_BACKEND_URL`).
/// - #427, a Voice recording left before its first chunk: run
///   `scripts/local_backend.sh state clear_top_drafts dead_voice_session` first.
final class TextModeDraftUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }

    override func setUp() {
        continueAfterFailure = false
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

    private func textInput(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        let field = app.textFields[id]
        return field.exists ? field : app.textViews[id]
    }

    /// Signed in as the test user, auto-generate off (no reply). With
    /// `TEST_RUNNER_LOORE_BACKEND_URL`, the app talks to that backend (the
    /// delaying proxy, or a branch's backend) instead of :5010.
    private func launch() throws -> XCUIApplication {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreResetState", "YES", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", "dark", "-loore_auto_generate", "NO"]
        if let backend = env["LOORE_BACKEND_URL"], !backend.isEmpty {
            app.launchArguments += ["-loore.debug.localBackendURL", backend]
        }
        app.launch()
        return app
    }

    func testSentEntryDoesNotComeBackAsTheDraft() throws {
        try sendAndReopen(pauseBeforeSend: true)
    }

    /// Send tapped right after the last keystroke: the autosave is still
    /// waiting in its 1 s debounce. With production latency (run through a
    /// delaying proxy, `TEST_RUNNER_LOORE_BACKEND_URL`) it fires while the
    /// send is on its way.
    func testSentRightAfterTypingDoesNotComeBackAsTheDraft() throws {
        try sendAndReopen(pauseBeforeSend: false)
    }

    /// Type, let the autosave run, Discard, go back and open Text mode again:
    /// the discarded text must not come back.
    func testDiscardedDraftDoesNotComeBack() throws {
        let app = try launch()

        let textCard = app.descendants(matching: .any)["home.text"].firstMatch
        XCTAssertTrue(textCard.waitForExistence(timeout: 20), "Reflect home did not show")
        textCard.tap()
        let field = textInput(app, "nodeForm.text.new")
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        sleep(2)
        let entry = "Discard repro \(Int(Date().timeIntervalSince1970))"
        field.tap()
        field.typeText(entry)
        sleep(3)
        snapshot("tmd-d1-typed")
        let discard = app.buttons["nodeForm.discardDraft"]
        XCTAssertTrue(discard.waitForExistence(timeout: 5))
        discard.tap()
        sleep(2)
        snapshot("tmd-d2-discarded")

        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(textCard.waitForExistence(timeout: 10), "did not get back to Reflect home")
        textCard.tap()
        let again = textInput(app, "nodeForm.text.new")
        XCTAssertTrue(again.waitForExistence(timeout: 10))
        sleep(3)
        snapshot("tmd-d3-text-mode-again")
        let shown = (again.value as? String) ?? ""
        XCTAssertFalse(shown.contains(entry), "the discarded text came back: \(shown)")
    }

    private func sendAndReopen(pauseBeforeSend: Bool) throws {
        let app = try launch()

        let textCard = app.descendants(matching: .any)["home.text"].firstMatch
        XCTAssertTrue(textCard.waitForExistence(timeout: 20), "Reflect home did not show")
        textCard.tap()
        let field = textInput(app, "nodeForm.text.new")
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        sleep(1)
        let entry = "Draft repro \(Int(Date().timeIntervalSince1970))"
        field.tap()
        field.typeText(entry)
        if pauseBeforeSend {
            sleep(2)
            snapshot("tmd-01-typed")
        }
        app.buttons["nodeForm.send.new"].tap()
        let focal = app.descendants(matching: .any)["thread.focal"].firstMatch
        XCTAssertTrue(focal.waitForExistence(timeout: 20), "did not land on the new entry")
        sleep(2)
        snapshot("tmd-02-thread")

        // Back to Reflect home (thread → Text mode → home).
        app.navigationBars.buttons.element(boundBy: 0).tap()
        sleep(2)
        snapshot("tmd-03-back-on-text-mode")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(textCard.waitForExistence(timeout: 10), "did not get back to Reflect home")
        sleep(1)
        snapshot("tmd-04-home")

        textCard.tap()
        let again = textInput(app, "nodeForm.text.new")
        XCTAssertTrue(again.waitForExistence(timeout: 10))
        sleep(3)
        snapshot("tmd-05-text-mode-again")
        let shown = (again.value as? String) ?? ""
        XCTAssertFalse(shown.contains(entry), "the sent entry came back as the draft: \(shown)")
        XCTAssertFalse(app.buttons["nodeForm.discardDraft"].exists, "a draft is offered for discarding")
    }
}
