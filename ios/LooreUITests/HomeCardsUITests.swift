import XCTest

/// The home cards of a user who gleans (Peter, 2026-10-13): a tap on a card's
/// Voice / Text switch only picks the mode, a tap on the card opens it, and each
/// card remembers its own mode across launches. Needs a signed-in user with
/// Glean (`TEST_RUNNER_LOORE_SESSION_COOKIE`; `TEST_RUNNER_LOORE_BACKEND_URL` for
/// a backend other than :5010). Skips for a user without Glean. Nothing is sent:
/// the test only opens Voice and Text mode.
final class HomeCardsUITests: XCTestCase {
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

    /// `reset` forgets the remembered modes (and the other preferences) first.
    private func launch(reset: Bool, theme: String = "dark") throws -> XCUIApplication {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", theme]
        if reset { app.launchArguments += ["-LooreResetState", "YES"] }
        if let backend = env["LOORE_BACKEND_URL"], !backend.isEmpty {
            app.launchArguments += ["-loore.debug.localBackendURL", backend]
        }
        app.launch()
        let reflect = app.buttons["home.reflect"]
        if !reflect.waitForExistence(timeout: 20) {
            if app.descendants(matching: .any)["home.text"].exists {
                throw XCTSkip("the signed-in user does not glean: the home has today's mode cards")
            }
            XCTFail("the home did not show")
        }
        sleep(1) // the cards' entrance
        return app
    }

    private func modeSwitch(_ app: XCUIApplication, _ purpose: String) -> XCUIElement {
        app.descendants(matching: .any)["home.\(purpose).mode"].firstMatch
    }

    /// Taps a point of the switch: `dx` 0.25 is the Voice half, 0.75 the Text half.
    private func tapSwitch(_ app: XCUIApplication, _ purpose: String, dx: CGFloat, dy: CGFloat = 0.5) {
        let element = modeSwitch(app, purpose)
        XCTAssertTrue(element.waitForExistence(timeout: 5), "no switch on the \(purpose) card")
        element.coordinate(withNormalizedOffset: CGVector(dx: dx, dy: dy)).tap()
    }

    private func label(_ app: XCUIApplication, _ purpose: String) -> String {
        app.buttons["home.\(purpose)"].label
    }

    /// Nothing opened: still on the home, no Voice or Text screen.
    private func assertStillHome(_ app: XCUIApplication, _ message: String) {
        sleep(1)
        XCTAssertFalse(app.descendants(matching: .any)["nodeForm.text.new"].exists, "Text mode opened: \(message)")
        XCTAssertFalse(app.descendants(matching: .any)["voice.record"].exists, "Voice opened: \(message)")
        XCTAssertTrue(app.buttons["home.reflect"].isHittable, "left the home: \(message)")
    }

    func testTheSwitchPicksAndTheCardOpens() throws {
        var app = try launch(reset: true)
        XCTAssertEqual(label(app, "reflect"), "Reflect by voice", "Voice until the user picks")
        XCTAssertEqual(label(app, "glean"), "Glean by voice")
        snapshot("home-cards-1-voice")

        // The switch selects and opens nothing, wherever on it the tap lands
        // (the middle of a half, and the thin edge inside its border).
        tapSwitch(app, "reflect", dx: 0.75)
        assertStillHome(app, "a tap on Reflect's Text half")
        XCTAssertEqual(label(app, "reflect"), "Reflect in text")
        XCTAssertEqual(label(app, "glean"), "Glean by voice", "Glean keeps its own mode")
        tapSwitch(app, "reflect", dx: 0.25, dy: 0.04)
        assertStillHome(app, "a tap on the top edge of Reflect's Voice half")
        XCTAssertEqual(label(app, "reflect"), "Reflect by voice")
        tapSwitch(app, "reflect", dx: 0.98)
        assertStillHome(app, "a tap on the right end of Reflect's switch")
        XCTAssertEqual(label(app, "reflect"), "Reflect in text")
        snapshot("home-cards-2-reflect-text")

        // The card opens the chosen mode.
        app.buttons["home.reflect"].tap()
        XCTAssertTrue(app.descendants(matching: .any)["nodeForm.text.new"].waitForExistence(timeout: 10),
                      "Reflect in text did not open Text mode")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(app.buttons["home.reflect"].waitForExistence(timeout: 10), "did not get back to the home")

        // A tap on the card's title (words over the card's button) opens it too.
        app.buttons["home.glean"].coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.25)).tap()
        XCTAssertTrue(app.descendants(matching: .any)["voice.record"].waitForExistence(timeout: 10),
                      "Glean by voice did not open Voice")
        snapshot("home-cards-3-glean-voice-opened")

        // Each card remembers its own mode across launches.
        app.terminate()
        app = try launch(reset: false)
        XCTAssertEqual(label(app, "reflect"), "Reflect in text")
        XCTAssertEqual(label(app, "glean"), "Glean by voice")
        tapSwitch(app, "glean", dx: 0.75)
        assertStillHome(app, "a tap on Glean's Text half")
        app.terminate()
        app = try launch(reset: false)
        XCTAssertEqual(label(app, "reflect"), "Reflect in text")
        XCTAssertEqual(label(app, "glean"), "Glean in text")
        app.terminate()
    }

    /// The home in both modes, dark and light (`TEST_RUNNER_LOORE_SCREENSHOT_DIR`).
    func testScreenshotsInBothThemes() throws {
        for theme in ["dark", "light"] {
            let app = try launch(reset: true, theme: theme)
            snapshot("home-cards-\(theme)-voice")
            tapSwitch(app, "reflect", dx: 0.75)
            tapSwitch(app, "glean", dx: 0.75)
            sleep(1)
            snapshot("home-cards-\(theme)-text")
            app.terminate()
        }
    }
}
