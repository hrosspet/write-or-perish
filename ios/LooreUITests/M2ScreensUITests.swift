import XCTest
import UIKit

/// More M2 screens against the local backend (not in CI): craft-mode forms,
/// search with a query (one billed embedding), light theme, proposal cards.
/// Billed steps run only with `TEST_RUNNER_LOORE_ALLOW_BILLED=1`.
/// `TEST_RUNNER_LOORE_SHARE_NODE` names a test-user node holding a `:::share` fence.
final class M2ScreensUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private var billed: Bool { env["LOORE_ALLOW_BILLED"] == "1" }

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

    private func launch(route: String?, theme: String = "dark", extra: [String] = []) throws -> XCUIApplication {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreResetState", "YES", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", theme] + extra
        if let route { app.launchArguments += ["-LooreRoute", route] }
        app.launch()
        return app
    }

    private func setCraft(_ app: XCUIApplication, on: Bool) {
        app.tabBars.buttons["More"].tap()
        let row = app.buttons["more.craftMode"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        let isOn = (row.value as? String) == "On"
        guard isOn != on else { return }
        row.tap()
        let turnOn = app.buttons["craft.turnOn"]
        if on, turnOn.waitForExistence(timeout: 3) { turnOn.tap() }
        sleep(2)
    }

    func testCraftModeForms() throws {
        let app = try launch(route: "/node/201071", extra: ["-loore_auto_generate", "NO"])
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        setCraft(app, on: true)
        snapshot("m2c-more-craft")
        app.buttons["more.writeNew"].tap()
        sleep(2)
        snapshot("m2c-write-new")
        app.buttons["Close"].tap()
        sleep(1)
        app.tabBars.buttons["Home"].tap()
        sleep(2)
        snapshot("m2c-thread-top")
        app.swipeUp()
        sleep(1)
        snapshot("m2c-thread-llm-bar")
        app.navigationBars.buttons.firstMatch.tap()
        sleep(1)
        app.buttons["home.text"].tap()
        sleep(2)
        snapshot("m2c-textmode")
        setCraft(app, on: false)
        snapshot("m2c-craft-off")
    }

    func testSearchWithAQuery() throws {
        let app = try launch(route: "/log")
        XCTAssertTrue(app.buttons["Search your entries"].waitForExistence(timeout: 20))
        app.buttons["Search your entries"].tap()
        let field = app.textFields["search.field"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.typeText("rivers")
        sleep(4)
        snapshot("m2s-results")
        let first = app.buttons.matching(NSPredicate(format: "label CONTAINS[c] 'river'")).firstMatch
        if first.exists {
            first.tap()
            sleep(3)
            snapshot("m2s-opened")
        }
    }

    func testLightTheme() throws {
        let app = try launch(route: "/node/201105", theme: "light")
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        sleep(2)
        snapshot("m2l-thread-201105")
        app.swipeUp()
        app.swipeUp()
        sleep(1)
        snapshot("m2l-thread-201105-proposal")
        app.tabBars.buttons["Log"].tap()
        sleep(2)
        snapshot("m2l-log")
    }

    func testProposalAccepts() throws {
        // A superseded todo proposal: Apply answers 404, shown in the card (free).
        var app = try launch(route: "/node/201105")
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        let apply = app.buttons["Apply changes to my Todo"]
        for _ in 0..<4 where !apply.isHittable { app.swipeUp() }
        sleep(3)
        apply.tap()
        sleep(2)
        snapshot("m2p-apply-superseded")
        app.terminate()

        // Save a user-authored :::share block as a private draft (free).
        if let shareNode = env["LOORE_SHARE_NODE"], !shareNode.isEmpty {
            app = try launch(route: "/node/\(shareNode)")
            XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
            sleep(1)
            snapshot("m2p-share-card")
            app.buttons["Save to shares"].tap()
            sleep(2)
            snapshot("m2p-share-saved")
            app.terminate()
        }

        guard billed else { return }
        // Tick / untick a proposal row, then apply it (one billed merge on the reply's model).
        app = try launch(route: "/node/201121")
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        let apply2 = app.buttons["Apply changes to my Todo"]
        for _ in 0..<4 where !apply2.isHittable { app.swipeUp() }
        sleep(1)
        snapshot("m2p-todo-card")
        let row = app.buttons.matching(NSPredicate(format: "value == 'Not done'")).firstMatch
        if row.exists {
            row.tap()
            sleep(2)
            snapshot("m2p-todo-ticked")
            let done = app.buttons.matching(NSPredicate(format: "value == 'Done'")).firstMatch
            if done.exists { done.tap(); sleep(2); snapshot("m2p-todo-unticked") }
        }
        for _ in 0..<4 where !apply2.isHittable { app.swipeUp() }
        sleep(3) // let the scroll settle: a tap during deceleration only stops it
        apply2.tap()
        sleep(1)
        snapshot("m2p-todo-started")
        // A long thread's accessibility tree is slow to query: just wait for the merge.
        sleep(30)
        snapshot("m2p-todo-applied")
    }

    /// The inline form's server draft survives a relaunch; "Discard draft" clears it.
    /// Also opens an ancestor's Reply modal. Writes only a draft (deleted at the end).
    /// `TEST_RUNNER_LOORE_DRAFT_NODE`: a short node of the test user to type under.
    func testInlineDraftRestoresAndDiscards() throws {
        var app = try launch(route: "/node/201076")
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        app.buttons.matching(NSPredicate(format: "label == 'More actions'")).firstMatch.tap()
        app.buttons["Reply"].firstMatch.tap()
        sleep(2)
        snapshot("m2r-reply-modal")
        app.terminate()

        let node = env["LOORE_DRAFT_NODE"].flatMap { $0.isEmpty ? nil : $0 } ?? "201185"
        app = try launch(route: "/node/\(node)", extra: ["-loore_auto_generate", "NO"])
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        sleep(2)
        let field = app.textViews["nodeForm.text.inline"].exists ? app.textViews["nodeForm.text.inline"]
            : app.textFields["nodeForm.text.inline"]
        field.tap()
        field.typeText("A draft that should come back.")
        sleep(5) // 1 s debounce + save
        snapshot("m2r-draft-saved")
        app.terminate()

        app = try launch(route: "/node/\(node)")
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        let discard = app.buttons["Discard draft"]
        XCTAssertTrue(discard.waitForExistence(timeout: 10))
        sleep(1)
        snapshot("m2r-draft-restored")
        discard.tap()
        sleep(2)
        snapshot("m2r-draft-discarded")
    }

    /// Review M5: text typed less than the 1 s debounce before a tab switch is
    /// saved (leaving the screen used to cancel the pending save). Writes only a
    /// draft, discarded at the end. `TEST_RUNNER_LOORE_DRAFT_NODE` as above.
    func testDraftTypedRightBeforeATabSwitchIsSaved() throws {
        let node = try XCTUnwrap(env["LOORE_DRAFT_NODE"].flatMap { $0.isEmpty ? nil : $0 },
                                 "set TEST_RUNNER_LOORE_DRAFT_NODE")
        var app = try launch(route: "/node/\(node)", extra: ["-loore_auto_generate", "NO"])
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        sleep(2)
        let field = app.textViews["nodeForm.text.inline"].exists ? app.textViews["nodeForm.text.inline"]
            : app.textFields["nodeForm.text.inline"]
        field.tap()
        field.typeText("Typed just before leaving.")
        app.tabBars.buttons["Log"].tap()
        sleep(3)
        app.terminate()

        app = try launch(route: "/node/\(node)")
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        let discard = app.buttons["Discard draft"]
        XCTAssertTrue(discard.waitForExistence(timeout: 10), "the draft was not saved")
        snapshot("m5-draft-after-tab-switch")
        discard.tap()
        sleep(2)
    }

    /// One billed inline reply with auto-generate on: lands on the new entry,
    /// hands off to its pending reply, watches it. `TEST_RUNNER_LOORE_REPLY_NODE`
    /// is the test-user reply to answer under.
    func testInlineReplyAutoGenerates() throws {
        guard billed, let node = env["LOORE_REPLY_NODE"], !node.isEmpty else {
            throw XCTSkip("set TEST_RUNNER_LOORE_ALLOW_BILLED=1 and TEST_RUNNER_LOORE_REPLY_NODE")
        }
        let app = try launch(route: "/node/\(node)", extra: ["-loore_auto_generate", "YES"])
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        sleep(2)
        let field = app.textViews["nodeForm.text.inline"].exists ? app.textViews["nodeForm.text.inline"]
            : app.textFields["nodeForm.text.inline"]
        for _ in 0..<4 where !field.isHittable { app.swipeUp() }
        sleep(2)
        field.tap()
        field.typeText("One more short sentence about that river, please.")
        app.buttons["nodeForm.send.inline"].tap()
        for i in 0..<10 {
            sleep(1)
            snapshot(String(format: "m2i-reply-%02d", i))
        }
        sleep(12)
        snapshot("m2i-reply-done")
        app.navigationBars.buttons.firstMatch.tap()
        sleep(2)
        snapshot("m2i-back-on-entry")
    }

    /// Diagnostic: tick / untick on a superseded proposal, then Apply (answers 404, free).
    func testApplyAfterTicksOnASupersededProposal() throws {
        let app = try launch(route: "/node/201105")
        XCTAssertTrue(app.descendants(matching: .any)["thread.focal"].firstMatch.waitForExistence(timeout: 20))
        let apply = app.buttons["Apply changes to my Todo"]
        for _ in 0..<4 where !apply.isHittable { app.swipeUp() }
        sleep(3)
        let row = app.buttons.matching(NSPredicate(format: "value == 'Not done'")).firstMatch
        row.tap()
        sleep(2)
        let done = app.buttons.matching(NSPredicate(format: "value == 'Done'")).firstMatch
        done.tap()
        sleep(2)
        snapshot("m2d-before-apply")
        sleep(2)
        apply.tap()
        sleep(3)
        snapshot("m2d-after-apply")
    }

    /// One billed Text-mode reply watched while it streams.
    func testTextModeReplyStreams() throws {
        guard billed else { throw XCTSkip("set TEST_RUNNER_LOORE_ALLOW_BILLED=1") }
        let app = try launch(route: "/textmode", extra: ["-loore_auto_generate", "YES"])
        let fields = app.textViews["nodeForm.text.new"].exists ? app.textViews["nodeForm.text.new"]
            : app.textFields["nodeForm.text.new"]
        XCTAssertTrue(fields.waitForExistence(timeout: 20))
        fields.tap()
        fields.typeText("M2 stream test. In about 150 words, describe a river at dawn. No tools needed.")
        app.buttons["nodeForm.send.new"].tap()
        for i in 0..<16 {
            sleep(1)
            snapshot(String(format: "m2t-stream-%02d", i))
        }
        sleep(15)
        snapshot("m2t-stream-done")
    }
}
