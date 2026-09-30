import XCTest
import UIKit

/// M2 flows against the local Docker backend (not run in CI): Log, threads,
/// kebab actions and their dialogs, the writing forms, search. They cancel
/// every dialog and never send anything, so they write nothing to the server
/// and make no billed calls. Same `TEST_RUNNER_` inputs as `SmokeFlowsUITests`;
/// `TEST_RUNNER_LOORE_THREAD_IDS` may override the threads visited
/// (comma-separated node ids of the test user).
final class ThreadWritingUITests: XCTestCase {
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

    private func launch(route: String?, theme: String = "dark") throws -> XCUIApplication {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreResetState", "YES", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", theme]
        if let route { app.launchArguments += ["-LooreRoute", route] }
        app.launch()
        return app
    }

    private func waitForFocal(_ app: XCUIApplication) {
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20), "thread did not load")
        sleep(2)
    }

    func testLogCardsRenameDeleteAndSearch() throws {
        let app = try launch(route: "/log")
        XCTAssertTrue(app.staticTexts["Log"].waitForExistence(timeout: 20))
        sleep(2)
        snapshot("m2-log")
        app.swipeUp()
        sleep(1)
        snapshot("m2-log-scrolled")
        app.swipeDown()
        app.swipeDown()
        let kebab = app.buttons["More actions"].firstMatch
        XCTAssertTrue(kebab.waitForExistence(timeout: 5))
        kebab.tap()
        sleep(1)
        snapshot("m2-log-kebab")
        app.buttons["Rename thread"].tap()
        sleep(1)
        snapshot("m2-log-rename")
        app.buttons["Cancel"].tap()
        sleep(1)
        kebab.tap()
        app.buttons["Delete thread"].tap()
        sleep(1)
        snapshot("m2-log-delete")
        app.buttons["Cancel"].tap()
        sleep(1)
        app.buttons["Search your entries"].tap()
        sleep(1)
        snapshot("m2-search-empty")
        app.buttons["Dates"].tap()
        sleep(1)
        snapshot("m2-search-dates")
    }

    func testThreadsRender() throws {
        let ids = (env["LOORE_THREAD_IDS"].flatMap { $0.isEmpty ? nil : $0 } ?? "201102,201105,201071,201076,201000,200961")
            .split(separator: ",").map(String.init)
        for id in ids {
            let app = try launch(route: "/node/\(id)")
            waitForFocal(app)
            snapshot("m2-thread-\(id)")
            app.swipeUp()
            sleep(1)
            snapshot("m2-thread-\(id)-down1")
            app.swipeUp()
            sleep(1)
            snapshot("m2-thread-\(id)-down2")
            app.terminate()
        }
    }

    func testThreadKebabActionsOpenAndCancel() throws {
        let app = try launch(route: "/node/201076")
        waitForFocal(app)
        let focal = app.buttons["thread.focalKebab"]
        let focalFrame = app.descendants(matching: .any)["thread.focal"].firstMatch.frame
        let kebabs = app.buttons.matching(NSPredicate(format: "label == 'More actions'"))
        focal.tap()
        sleep(1)
        snapshot("m2-kebab-focal")
        app.buttons["Edit"].firstMatch.tap()
        sleep(2)
        snapshot("m2-edit-modal")
        app.buttons["Close"].tap()
        sleep(1)
        focal.tap()
        app.buttons["Delete"].firstMatch.tap()
        sleep(1)
        snapshot("m2-delete-dialog")
        app.buttons["Cancel"].tap()
        sleep(1)
        // An ancestor's Reply.
        let first = kebabs.element(boundBy: 0)
        if first.exists && first.frame.midY < focalFrame.minY {
            first.tap()
            app.buttons["Reply"].firstMatch.tap()
            sleep(2)
            snapshot("m2-reply-modal")
            app.buttons["Close"].tap()
        }
        app.swipeUp()
        sleep(1)
        snapshot("m2-inline-form")
    }

    func testTextModeAndWriteNewEntry() throws {
        let app = try launch(route: "/textmode")
        XCTAssertTrue(app.staticTexts["What's on your mind?"].waitForExistence(timeout: 20))
        sleep(2)
        snapshot("m2-textmode")
        let field = app.textFields["nodeForm.text.new"].exists ? app.textFields["nodeForm.text.new"] : app.textViews["nodeForm.text.new"]
        if field.waitForExistence(timeout: 3) {
            field.tap()
            sleep(1)
            snapshot("m2-textmode-keyboard")
        }
    }
}
