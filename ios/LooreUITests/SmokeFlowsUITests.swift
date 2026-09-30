import XCTest
import UIKit

/// Smoke flows against the local Docker backend (not run in CI).
///
/// Inputs come from the environment of `xcodebuild test`, prefixed with
/// `TEST_RUNNER_` (xcodebuild strips the prefix):
/// - `LOORE_MAGIC_LINK`: a freshly minted magic link for the test user.
/// - `LOORE_SESSION_COOKIE`: a Flask `session` cookie signed for the test user.
/// - `LOORE_SCREENSHOT_DIR`: optional; screenshots are also written there as PNGs.
/// Each test skips when its input is missing. See ios/README.md "UI smoke tests".
final class SmokeFlowsUITests: XCTestCase {
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
            let url = URL(fileURLWithPath: dir).appendingPathComponent("\(name).png")
            try? shot.pngRepresentation.write(to: url)
        }
    }

    private func launch(_ extra: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local"] + extra
        app.launch()
        return app
    }

    /// Paste a magic link into the sign-in screen and land on Reflect.
    func testSignInWithPastedMagicLink() throws {
        let link = try XCTUnwrap(env["LOORE_MAGIC_LINK"].flatMap { $0.isEmpty ? nil : $0 },
                                 "set TEST_RUNNER_LOORE_MAGIC_LINK")
        let app = launch(["-LooreResetState", "YES", "-LooreSkipUpdates", "YES"])

        let haveLink = app.buttons["signin.haveLink"]
        XCTAssertTrue(haveLink.waitForExistence(timeout: 15))
        snapshot("ui-01-signin")
        haveLink.tap()

        let field = app.textFields["signin.linkField"].exists ? app.textFields["signin.linkField"] : app.textViews["signin.linkField"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        snapshot("ui-01b-paste-step")
        UIPasteboard.general.string = link
        let paste = app.buttons["Paste"]
        if paste.waitForExistence(timeout: 3) {
            paste.tap()
        } else {
            field.tap()
            field.typeText(link)
            app.buttons["signin.submitLink"].tap()
        }
        snapshot("ui-02-link-pasted")

        let heading = app.staticTexts["What's on your mind?"]
        XCTAssertTrue(heading.waitForExistence(timeout: 20), "did not reach Reflect after sign-in")
        sleep(2)
        snapshot("ui-03-reflect")

        // Walk the tabs.
        for tab in ["Artifacts", "Log", "Commons", "More"] {
            let button = app.tabBars.buttons[tab]
            guard button.exists else { continue }
            button.tap()
            sleep(1)
            snapshot("ui-04-tab-\(tab.lowercased())")
        }
    }

    /// Debug `-LooreSessionCookie` path: straight to Reflect, then the More menu.
    func testSessionCookieLaunchAndMoreMenu() throws {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        let app = launch(["-LooreResetState", "YES", "-LooreSessionCookie", cookie, "-LooreSkipUpdates", "YES"])
        XCTAssertTrue(app.staticTexts["What's on your mind?"].waitForExistence(timeout: 20))
        app.tabBars.buttons["More"].tap()
        let craft = app.buttons["more.craftMode"]
        XCTAssertTrue(craft.waitForExistence(timeout: 5))
        snapshot("ui-10-more")

        // Craft mode: the confirmation dialog, then back off again (restores the user's setting).
        let wasOn = (craft.value as? String) == "On"
        if !wasOn {
            craft.tap()
            let turnOn = app.buttons["craft.turnOn"]
            XCTAssertTrue(turnOn.waitForExistence(timeout: 5))
            snapshot("ui-11-craft-dialog")
            turnOn.tap()
            XCTAssertTrue(app.buttons["more.export"].waitForExistence(timeout: 5))
            sleep(1)
            snapshot("ui-12-craft-on")
            craft.tap() // off again
            XCTAssertTrue(app.buttons["more.export"].waitForNonExistence(timeout: 5))
        }

        // Light mode toggles and back.
        let light = app.buttons["more.lightMode"]
        light.tap()
        sleep(1)
        snapshot("ui-13-light")
        light.tap()

        // Account → version label.
        app.buttons["more.account"].tap()
        XCTAssertTrue(app.staticTexts["account.version"].waitForExistence(timeout: 5))
        snapshot("ui-14-account")
    }
}

extension SmokeFlowsUITests {
    /// Gating: the blocking Terms screen, then "I Agree" → Reflect.
    /// Needs `TEST_RUNNER_LOORE_EXPECT=terms` and the test user's accepted terms
    /// set to an old version in the local database first (see ios/README.md).
    func testTermsGateAccept() throws {
        guard env["LOORE_EXPECT"] == "terms" else { throw XCTSkip("set TEST_RUNNER_LOORE_EXPECT=terms") }
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 })
        let app = launch(["-LooreResetState", "YES", "-LooreSessionCookie", cookie, "-LooreSkipUpdates", "YES",
                          "-LooreTheme", "dark"])
        let agree = app.buttons["terms.agree"]
        XCTAssertTrue(agree.waitForExistence(timeout: 20))
        sleep(1)
        snapshot("ui-20-terms-top")
        for _ in 0..<40 where !agree.isHittable {
            app.swipeUp()
        }
        snapshot("ui-21-terms-bottom")
        agree.tap()
        XCTAssertTrue(app.staticTexts["What's on your mind?"].waitForExistence(timeout: 20))
        sleep(1)
        snapshot("ui-22-after-terms")
    }
}

extension SmokeFlowsUITests {
    /// Updates sheet: "Take a look" closes it and opens the link's screen; on the
    /// next launch the item is still there (a look is a skip), and "Got it" clears it.
    /// Needs `TEST_RUNNER_LOORE_EXPECT=updates` and one unread notification linking
    /// to /log for the test user (see ios/README.md).
    func testUpdatesSheetTakeALookThenGotIt() throws {
        guard env["LOORE_EXPECT"] == "updates" else { throw XCTSkip("set TEST_RUNNER_LOORE_EXPECT=updates") }
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 })
        var app = launch(["-LooreResetState", "YES", "-LooreSessionCookie", cookie, "-LooreTheme", "dark"])
        let look = app.buttons["updates.takeALook"]
        XCTAssertTrue(look.waitForExistence(timeout: 20))
        sleep(1)
        snapshot("ui-30-updates")
        look.tap()
        XCTAssertTrue(app.staticTexts["Log"].waitForExistence(timeout: 5), "the link opens the Log")
        sleep(1)
        snapshot("ui-31-updates-link-opened")

        app.terminate()
        app = launch(["-LooreSessionCookie", cookie, "-LooreTheme", "dark"])
        let gotIt = app.buttons["updates.gotIt"]
        XCTAssertTrue(gotIt.waitForExistence(timeout: 20), "still unread after a look")
        gotIt.tap()
        XCTAssertTrue(gotIt.waitForNonExistence(timeout: 5), "the sheet closes when nothing remains")
    }
}

extension SmokeFlowsUITests {
    /// The web fallback opens the signed-in web app (cookies injected into the
    /// web view), then Logout returns to sign-in and a relaunch stays signed out.
    func testWebFallbackAndLogout() throws {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        var app = launch(["-LooreResetState", "YES", "-LooreSessionCookie", cookie, "-LooreSkipUpdates", "YES",
                          "-LooreTheme", "dark"])
        XCTAssertTrue(app.staticTexts["What's on your mind?"].waitForExistence(timeout: 20))
        app.tabBars.buttons["Log"].tap()
        let openWeb = app.buttons["placeholder.openWeb"]
        XCTAssertTrue(openWeb.waitForExistence(timeout: 5))
        openWeb.tap()
        // The web Log page shows its "Log" heading only when signed in (else it redirects to /login).
        let webHeading = app.webViews.staticTexts["Log"]
        XCTAssertTrue(webHeading.waitForExistence(timeout: 25), "web view did not show the signed-in Log")
        sleep(2)
        snapshot("ui-40-web-fallback-log")
        app.buttons["Done"].tap()

        app.tabBars.buttons["More"].tap()
        app.buttons["more.logout"].tap()
        let confirm = app.sheets.buttons["Logout"].exists ? app.sheets.buttons["Logout"] : app.buttons["Logout"].firstMatch
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        snapshot("ui-41-logout-confirm")
        confirm.tap()
        XCTAssertTrue(app.buttons["signin.haveLink"].waitForExistence(timeout: 15), "not back at sign-in")

        app.terminate()
        app = launch([])
        XCTAssertTrue(app.buttons["signin.haveLink"].waitForExistence(timeout: 15), "signed in again after logout")
    }
}

private extension XCUIElement {
    func waitForNonExistence(timeout: TimeInterval) -> Bool {
        let predicate = NSPredicate(format: "exists == false")
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: self)
        return XCTWaiter.wait(for: [expectation], timeout: timeout) == .completed
    }
}
