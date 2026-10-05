import XCTest
import UIKit

/// M5 parity checks against the local Docker backend (not run in CI). None of
/// them bills an AI provider. Inputs (`TEST_RUNNER_` prefix, see ios/README.md):
/// - `LOORE_SESSION_COOKIE`: a Flask `session` cookie for the test user.
/// - `LOORE_WELCOME_LINK`: a sign-in link minted with the landing path `/welcome`
///   (`scripts/local_backend.sh magic-link /welcome`).
/// - `LOORE_PERMALINK`: a live public permalink of the test user (`/@user/slug`).
/// - `LOORE_LISTEN_NODE`: a test-user node whose TTS already exists (replaying it is not billed).
/// - `LOORE_SCREENSHOT_DIR`: optional; screenshots are also written there.
final class M5ParityUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }

    override func setUp() {
        continueAfterFailure = false
    }

    private func input(_ key: String) throws -> String {
        guard let value = env[key], !value.isEmpty else { throw XCTSkip("set TEST_RUNNER_\(key)") }
        return value
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

    private func launch(_ extra: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreSkipUpdates", "YES", "-LooreTheme", "dark"] + extra
        app.launch()
        return app
    }

    /// Signed out, the web NavBar's About links open the web page (here: Vision).
    func testSignedOutAboutLinkOpensTheWebPage() {
        let app = launch(["-LooreResetState", "YES"])
        let vision = app.buttons["Vision"]
        XCTAssertTrue(vision.waitForExistence(timeout: 15))
        snapshot("m5-01-signin-about")
        vision.tap()
        sleep(4)
        snapshot("m5-02-vision-sheet")
        XCTAssertFalse(app.buttons["signin.haveLink"].isHittable, "the Vision page did not cover the sign-in screen")
    }

    /// A pasted sign-in link whose `next_url` is `/welcome` (the Activate & Welcome
    /// email) lands on Welcome, as the web does.
    func testWelcomeLinkLandsOnWelcome() throws {
        let link = try input("LOORE_WELCOME_LINK")
        let app = launch(["-LooreResetState", "YES"])
        let haveLink = app.buttons["signin.haveLink"]
        XCTAssertTrue(haveLink.waitForExistence(timeout: 15))
        haveLink.tap()
        UIPasteboard.general.string = link
        let paste = app.buttons["Paste"]
        XCTAssertTrue(paste.waitForExistence(timeout: 5))
        paste.tap()
        XCTAssertTrue(app.buttons["welcome.startWriting"].waitForExistence(timeout: 20), "did not land on Welcome")
        sleep(2)
        snapshot("m5-03-welcome-after-link")
    }

    /// `/@user/slug` opens the native thread for a signed-in member (web `PermalinkRoute`).
    func testPermalinkOpensTheNativeThread() throws {
        let cookie = try input("LOORE_SESSION_COOKIE")
        let permalink = try input("LOORE_PERMALINK")
        let app = launch(["-LooreResetState", "YES", "-LooreSessionCookie", cookie, "-LooreRoute", permalink])
        XCTAssertTrue(app.otherElements["thread.focal"].waitForExistence(timeout: 20)
                      || app.descendants(matching: .any)["thread.focal"].waitForExistence(timeout: 5))
        snapshot("m5-04-permalink-thread")
    }

    /// While the mini-player shows, a toast sits above it (web `--floating-player-offset`).
    /// Turns craft mode on (for its toast) and off again.
    func testToastsSitAboveTheMiniPlayer() throws {
        let cookie = try input("LOORE_SESSION_COOKIE")
        let node = try input("LOORE_LISTEN_NODE")
        let app = launch(["-LooreResetState", "YES", "-LooreSessionCookie", cookie, "-LooreDebugListenNode", node])
        // Every tab stack has its own mini-player view: query the first match.
        let playerTitle = app.staticTexts["miniPlayer.title"].firstMatch
        XCTAssertTrue(playerTitle.waitForExistence(timeout: 20), "the mini-player did not appear")
        app.tabBars.buttons["More"].tap()
        let craft = app.buttons["more.craftMode"]
        XCTAssertTrue(craft.waitForExistence(timeout: 5))
        let wasOn = (craft.value as? String) == "On"
        if wasOn { craft.tap() } // off first, so the next tap shows the dialog
        craft.tap()
        let turnOn = app.buttons["craft.turnOn"]
        XCTAssertTrue(turnOn.waitForExistence(timeout: 5))
        turnOn.tap()
        let toast = app.descendants(matching: .any)["toast"].firstMatch
        XCTAssertTrue(toast.waitForExistence(timeout: 5), "no toast")
        XCTAssertEqual(toast.label, "Craft mode on. Its controls carry the sliders icon.")
        sleep(1)
        snapshot("m5-05-toast-above-player")
        let titleTop = app.staticTexts["miniPlayer.title"].firstMatch.frame.minY
        let toastBottom = toast.frame.maxY
        if !wasOn { // restore before asserting, so a failure leaves craft mode as it was
            craft.tap()
            XCTAssertTrue(app.buttons["more.export"].waitForNonExistence(timeout: 5))
        }
        app.buttons["miniPlayer.close"].firstMatch.tap()
        XCTAssertLessThanOrEqual(toastBottom, titleTop, "the toast overlaps the mini-player")
    }
}
