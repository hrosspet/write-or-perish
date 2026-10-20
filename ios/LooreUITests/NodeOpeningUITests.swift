import XCTest

/// Opening a thread from Text mode and from the Log, against the local backend as
/// the test user, through `scripts/delay_proxy.py <port> 0.8` (node GETs held
/// 0.8 s, `TEST_RUNNER_LOORE_BACKEND_URL`). The screen must stay, with the
/// spinner, until the node is in: no "Loading node..." page in between.
final class NodeOpeningUITests: XCTestCase {
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

    /// Signed in as the test user, auto-generate off (no reply), through the proxy.
    private func launch() throws -> XCUIApplication {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        guard let backend = env["LOORE_BACKEND_URL"], !backend.isEmpty else {
            throw XCTSkip("set TEST_RUNNER_LOORE_BACKEND_URL to delay_proxy.py with a node delay")
        }
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreResetState", "YES", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", "dark", "-loore_auto_generate", "NO",
                                "-loore.debug.localBackendURL", backend]
        app.launch()
        return app
    }

    /// Watches until the thread's focal card shows: was the spinner seen, and the loading page?
    private func watchOpening(_ app: XCUIApplication, _ name: String) -> (spinner: Bool, loadingPage: Bool, opened: Bool) {
        let spinner = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Loading node")).firstMatch
        let loadingPage = app.staticTexts["Loading node..."]
        let focal = app.descendants(matching: .any)["thread.focal"].firstMatch
        var sawSpinner = false
        var sawLoadingPage = false
        let deadline = Date().addingTimeInterval(15)
        while Date() < deadline {
            if !sawSpinner && spinner.exists {
                sawSpinner = true
                snapshot("\(name)-1-spinner")
            }
            if !sawLoadingPage && loadingPage.exists {
                sawLoadingPage = true
                snapshot("\(name)-x-loading-page")
            }
            if focal.exists {
                snapshot("\(name)-2-thread")
                return (sawSpinner, sawLoadingPage, true)
            }
        }
        return (sawSpinner, sawLoadingPage, false)
    }

    func testTextModeAndLogOpenTheThreadWithoutALoadingPage() throws {
        let app = try launch()

        let textCard = app.descendants(matching: .any)["home.text"].firstMatch
        XCTAssertTrue(textCard.waitForExistence(timeout: 20), "Reflect home did not show")
        textCard.tap()
        let field = textInput(app, "nodeForm.text.new")
        XCTAssertTrue(field.waitForExistence(timeout: 10))
        sleep(1)
        let entry = "Opening check \(Int(Date().timeIntervalSince1970))"
        field.tap()
        field.typeText(entry)
        sleep(2)
        app.buttons["nodeForm.send.new"].tap()
        let fromTextMode = watchOpening(app, "nodeopen-textmode")
        XCTAssertTrue(fromTextMode.opened, "the entry did not open")
        XCTAssertTrue(fromTextMode.spinner, "no spinner while the entry loaded")
        XCTAssertFalse(fromTextMode.loadingPage, "a Loading node... page showed")

        // Back to Reflect home (thread → Text mode → home), then the entry from the Log.
        app.navigationBars.buttons.element(boundBy: 0).tap()
        sleep(1)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(textCard.waitForExistence(timeout: 10), "did not get back to Reflect home")
        app.tabBars.buttons["Log"].tap()
        let card = app.staticTexts[entry].firstMatch
        XCTAssertTrue(card.waitForExistence(timeout: 15), "the entry is not in the Log")
        sleep(1)
        card.tap()
        let fromLog = watchOpening(app, "nodeopen-log")
        XCTAssertTrue(fromLog.opened, "the thread did not open from the Log")
        XCTAssertTrue(fromLog.spinner, "no spinner while the thread loaded")
        XCTAssertFalse(fromLog.loadingPage, "a Loading node... page showed")
    }
}
