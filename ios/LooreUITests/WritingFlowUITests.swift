import XCTest
import UIKit

/// The writing path end to end against the local backend, as the test user:
/// a Text-mode entry with auto-generate off (no reply), checklist toggle and
/// "+", an edit, then (only with `TEST_RUNNER_LOORE_ALLOW_BILLED=1`) craft
/// mode, the model picker set to GPT-6 Luna and one billed LLM Response
/// watched while it streams; finally the entry and its session are deleted
/// and craft mode is switched back off.
final class WritingFlowUITests: XCTestCase {
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

    func testTextModeEntryChecklistEditAndOptionalReply() throws {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        let billed = env["LOORE_ALLOW_BILLED"] == "1"
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreResetState", "YES", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", "dark", "-LooreRoute", "/textmode",
                                "-loore_auto_generate", "NO"]
        app.launch()

        // 1. A Text-mode entry with a checklist (auto-generate off: no reply).
        XCTAssertTrue(app.staticTexts["What's on your mind?"].waitForExistence(timeout: 20))
        let field = textInput(app, "nodeForm.text.new")
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.typeText("M2 test entry. Reply in one short sentence.\n- [ ] first item\n- [ ] second item")
        sleep(2)
        snapshot("m2w-01-typed")
        app.buttons["nodeForm.send.new"].tap()
        let focal = app.descendants(matching: .any)["thread.focal"].firstMatch
        XCTAssertTrue(focal.waitForExistence(timeout: 20), "did not land on the new entry")
        sleep(2)
        snapshot("m2w-02-entry")

        // 2. Tick the first item; add a third below the second.
        let first = app.buttons["first item"]
        XCTAssertTrue(first.waitForExistence(timeout: 5))
        first.tap()
        sleep(2)
        snapshot("m2w-03-ticked")
        let plus = app.buttons.matching(identifier: "Add an item below")
        if plus.count >= 2 {
            plus.element(boundBy: 1).tap()
            sleep(1)
            app.typeText("third item\n")
            sleep(2)
            snapshot("m2w-04-added")
        }

        // 3. Edit the entry (no generated audio, so no dialog; auto-generate off: no reply).
        app.buttons["thread.focalKebab"].tap()
        app.buttons["Edit"].firstMatch.tap()
        let editField = textInput(app, "nodeForm.text.edit")
        XCTAssertTrue(editField.waitForExistence(timeout: 5))
        sleep(1)
        snapshot("m2w-05-edit")
        editField.tap()
        editField.typeText(" Edited.")
        app.buttons["nodeForm.send.edit"].tap()
        sleep(3)
        snapshot("m2w-06-edited")

        if billed {
            // 4. Craft mode on (More), back to the entry, pick GPT-6 Luna, LLM Response.
            app.tabBars.buttons["More"].tap()
            app.buttons["more.craftMode"].tap()
            let turnOn = app.buttons["craft.turnOn"]
            if turnOn.waitForExistence(timeout: 3) { turnOn.tap() }
            sleep(2)
            app.tabBars.buttons["Home"].tap()
            sleep(2)
            snapshot("m2w-07-craft-thread")
            let picker = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Model:'")).firstMatch
            XCTAssertTrue(picker.waitForExistence(timeout: 10))
            sleep(2)
            picker.tap()
            sleep(1)
            snapshot("m2w-08-picker")
            app.buttons["More models…"].tap()
            sleep(1)
            snapshot("m2w-09-picker-more")
            app.buttons["GPT-6 Luna"].firstMatch.tap()
            sleep(1)
            snapshot("m2w-10-luna")
            app.buttons["thread.llmResponse"].tap()
            for i in 0..<14 {
                sleep(2)
                snapshot(String(format: "m2w-11-reply-%02d", i))
            }
            sleep(10)
            snapshot("m2w-12-reply-done")
            app.navigationBars.buttons.firstMatch.tap()
            sleep(2)
            snapshot("m2w-13-back-on-entry")
        }

        // 5. Delete the entry (and its reply), then its now-empty session.
        let focalKebab = app.buttons["thread.focalKebab"]
        XCTAssertTrue(focalKebab.waitForExistence(timeout: 10))
        focalKebab.tap()
        app.buttons["Delete"].firstMatch.tap()
        sleep(1)
        snapshot("m2w-14-delete")
        let withReplies = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Delete this node and all my replies'")).firstMatch
        if withReplies.exists { withReplies.tap() } else { app.buttons["Delete"].firstMatch.tap() }
        let alsoPrompt = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Also delete the system prompt'")).firstMatch
        if alsoPrompt.waitForExistence(timeout: 5) {
            snapshot("m2w-15-delete-prompt")
            alsoPrompt.tap()
        }
        sleep(3)
        snapshot("m2w-16-after-delete")

        if billed {
            app.tabBars.buttons["More"].tap()
            app.buttons["more.craftMode"].tap()
            sleep(1)
        }
    }
}
