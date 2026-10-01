import XCTest
import UIKit

/// M4 feature pages against the local backend (not in CI). None of these
/// flows bill an AI provider. They create test content as the test user and
/// leave it for the runner to remove (the backend has no delete for profiles,
/// todos or artifacts): see `ios/README.md` "M4 flows".
/// `TEST_RUNNER_LOORE_IMPORT_DIR` holds the tiny archives for the import flow.
final class M4ScreensUITests: XCTestCase {
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

    private func launch(route: String?, extra: [String] = []) throws -> XCUIApplication {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreResetState", "YES", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", "dark"] + extra
        if let route { app.launchArguments += ["-LooreRoute", route] }
        app.launch()
        return app
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    /// Replaces the text of a field or editor: cursor to the end, delete it all, type.
    private func replaceText(_ app: XCUIApplication, in field: XCUIElement, with text: String) {
        field.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.97)).tap()
        sleep(1)
        let count = ((field.value as? String) ?? "").count + 5
        field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: count))
        field.typeText(text)
    }

    // MARK: Todo

    func testTodoCreateTickAddEditHistoryRevert() throws {
        let app = try launch(route: "/todo")
        let create = app.buttons["todo.create"]
        XCTAssertTrue(create.waitForExistence(timeout: 20), "expects the test user to have no todo yet")
        create.tap()
        let editor = element(app, "todo.editor")
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        snapshot("m4-todo-create-template")
        replaceText(app, in: editor, with: "## Today\n\n- [ ] water the plants\n- [ ] sharpen **pencils**\n  - [ ] the red one\n\n## Upcoming\n\n- [ ] call the [plumber](https://example.com)\n- Errands\n\n## Completed recently\n")
        app.buttons["doc.save"].tap()
        XCTAssertTrue(app.buttons["todo.check.water the plants"].waitForExistence(timeout: 10))
        snapshot("m4-todo-list")

        app.buttons["todo.check.water the plants"].tap()
        sleep(2)
        app.buttons["Show 1 sub-items"].firstMatch.tap()
        sleep(1)
        snapshot("m4-todo-ticked-expanded")

        app.buttons["todo.quickAdd"].tap()
        let quick = app.textFields["todo.quickAddField"]
        XCTAssertTrue(quick.waitForExistence(timeout: 3))
        quick.typeText("buy bread\n")
        sleep(2)
        app.buttons["Add an item below"].firstMatch.tap()
        let row = app.textFields["todo.newItem"]
        XCTAssertTrue(row.waitForExistence(timeout: 3))
        row.typeText("fill the can\n")
        sleep(2)
        snapshot("m4-todo-added")

        app.buttons["doc.versionChip"].tap()
        let editor2 = element(app, "todo.editor")
        XCTAssertTrue(editor2.waitForExistence(timeout: 5))
        editor2.tap()
        app.typeKey(.downArrow, modifierFlags: .command)
        editor2.typeText("- [ ] sorted the drawer\n")
        app.buttons["doc.save"].tap()
        sleep(2)
        snapshot("m4-todo-v2")

        app.buttons["doc.history"].tap()
        XCTAssertTrue(element(app, "history.v2").waitForExistence(timeout: 5))
        snapshot("m4-todo-history")
        element(app, "history.v2").tap()
        XCTAssertTrue(element(app, "history.diff").waitForExistence(timeout: 5))
        snapshot("m4-todo-history-diff")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        element(app, "history.v1").tap()
        XCTAssertTrue(app.buttons["history.revert"].waitForExistence(timeout: 5))
        snapshot("m4-todo-history-v1")
        app.buttons["history.revert"].tap()
        sleep(3)
        snapshot("m4-todo-reverted")
    }

    // MARK: Profile

    func testProfileWriteEditHistory() throws {
        let app = try launch(route: "/profile")
        let write = app.buttons["profile.write"]
        XCTAssertTrue(write.waitForExistence(timeout: 20), "expects the test user to have no profile yet")
        write.tap()
        let editor = element(app, "profile.editor")
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        editor.tap()
        editor.typeText("## Working on\n\nA field guide to **rivers** and glaciers.\n\n- walks along the river\n- notes on ice\n")
        app.buttons["doc.save"].tap()
        XCTAssertTrue(element(app, "profile.content").waitForExistence(timeout: 10))
        sleep(1)
        snapshot("m4-profile-v1")

        app.buttons["doc.versionChip"].tap()
        let editor2 = element(app, "profile.editor")
        XCTAssertTrue(editor2.waitForExistence(timeout: 5))
        editor2.tap()
        app.typeKey(.downArrow, modifierFlags: .command)
        editor2.typeText("\nAlso: a small garden.")
        snapshot("m4-profile-editing")
        app.buttons["doc.save"].tap()
        sleep(2)
        snapshot("m4-profile-edited")

        app.buttons["doc.history"].tap()
        XCTAssertTrue(element(app, "history.v1").waitForExistence(timeout: 5))
        element(app, "history.v1").tap()
        sleep(2)
        snapshot("m4-profile-history-v1")
    }

    // MARK: Artifacts

    func testArtifactCreateEditHistoryAndUnsavedGuard() throws {
        let app = try launch(route: "/artifacts?create=1")
        let kind = app.textFields["artifact.kind"]
        XCTAssertTrue(kind.waitForExistence(timeout: 20))
        kind.tap()
        kind.typeText("M4 Test")
        XCTAssertEqual(kind.value as? String, "m4-test", "the name is sanitised as typed")
        app.textFields["artifact.description"].tap()
        app.textFields["artifact.description"].typeText("A test artifact from the app")
        let editor = element(app, "artifact.editor")
        editor.tap()
        editor.typeText("First line\nSecond line\n")
        snapshot("m4-artifact-create")
        app.buttons["doc.save"].tap()
        XCTAssertTrue(element(app, "artifact.content").waitForExistence(timeout: 10))
        sleep(1)
        snapshot("m4-artifact-v1")

        app.buttons["doc.versionChip"].tap()
        let editor2 = element(app, "artifact.editor")
        XCTAssertTrue(editor2.waitForExistence(timeout: 5))
        replaceText(app, in: editor2, with: "First line\nSecond line, changed\nThird line\n")
        app.buttons["artifactsNav.Memory"].tap()
        XCTAssertTrue(app.buttons["artifact.leave"].waitForExistence(timeout: 5))
        snapshot("m4-artifact-unsaved")
        app.buttons["Cancel"].firstMatch.tap()
        sleep(1)
        app.buttons["doc.save"].tap()
        sleep(3)
        app.buttons["doc.history"].tap()
        XCTAssertTrue(element(app, "history.v2").waitForExistence(timeout: 5))
        element(app, "history.v2").tap()
        XCTAssertTrue(element(app, "history.diff").waitForExistence(timeout: 5))
        snapshot("m4-artifact-diff")
        app.buttons["Full text"].tap()
        sleep(1)
        snapshot("m4-artifact-fulltext")
    }

    // MARK: References

    func testReferencesListDetailReadToggleAndDialogs() throws {
        let itemId = env["LOORE_REFERENCE_ID"] ?? "1032"
        let app = try launch(route: "/references")
        let card = element(app, "reference.card.\(itemId)")
        XCTAssertTrue(card.waitForExistence(timeout: 20))
        snapshot("m4-references")
        card.tap()
        let toggle = app.buttons["reference.readToggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 10))
        sleep(6) // the tweet embed
        snapshot("m4-reference-detail")
        let before = toggle.label
        toggle.tap()
        sleep(2)
        snapshot("m4-reference-read-toggled")
        app.buttons["reference.readToggle"].tap()
        sleep(2)
        XCTAssertEqual(app.buttons["reference.readToggle"].label, before, "toggled back")

        app.buttons["More actions"].firstMatch.tap()
        app.buttons["Edit"].tap()
        XCTAssertTrue(element(app, "referenceEdit.content").waitForExistence(timeout: 5))
        snapshot("m4-reference-edit")
        app.buttons["Cancel"].tap()
        sleep(1)
        app.buttons["More actions"].firstMatch.tap()
        app.buttons["Delete"].tap()
        XCTAssertTrue(app.buttons["reference.confirmDelete"].waitForExistence(timeout: 5))
        snapshot("m4-reference-delete-dialog")
        app.buttons["Cancel"].firstMatch.tap()
        sleep(1)
    }

    // MARK: Prompts

    func testPromptHistoryAgainstTheDefault() throws {
        let app = try launch(route: "/prompts")
        let row = app.buttons["prompt.row.voice"]
        XCTAssertTrue(row.waitForExistence(timeout: 20))
        row.tap()
        XCTAssertTrue(element(app, "prompt.content").waitForExistence(timeout: 10))
        app.buttons["doc.history"].tap()
        XCTAssertTrue(element(app, "history.v0").waitForExistence(timeout: 5))
        snapshot("m4-prompt-history")
        element(app, "history.v1").tap()
        sleep(3)
        snapshot("m4-prompt-history-v1")
    }

    // MARK: Account

    func testAccountValidationSettingsAndConnectX() throws {
        let app = try launch(route: "/account")
        let username = app.textFields["account.username"]
        XCTAssertTrue(username.waitForExistence(timeout: 20))
        // Double tap selects the one-word username; the invalid name never leaves the app.
        username.doubleTap()
        username.typeText("bad name!")
        app.buttons["Save"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts["Only letters, numbers, and underscores allowed."].waitForExistence(timeout: 3))
        snapshot("m4-account-username-error")

        app.buttons["account.connectX"].tap()
        sleep(6)
        snapshot("m4-account-connect-x")
        // Locally X OAuth ends at once on `/account?x_login=…`, which closes the sheet.
        if app.buttons["Cancel"].firstMatch.exists { app.buttons["Cancel"].firstMatch.tap() }
        sleep(1)

        app.swipeUp()
        sleep(1)
        snapshot("m4-account-settings")
        app.swipeUp()
        sleep(1)
        snapshot("m4-account-voice")
    }

    func testConfirmEmailWithAStaleToken() throws {
        let app = try launch(route: "/confirm-email?token=m4-stale-token-0000000000000000")
        XCTAssertTrue(app.staticTexts["Not confirmed"].waitForExistence(timeout: 20))
        snapshot("m4-confirm-email-invalid")
    }

    // MARK: Import

    func testImportMarkdownThenAgainThenChatGPTAnalyze() throws {
        let dir = try XCTUnwrap(env["LOORE_IMPORT_DIR"], "set TEST_RUNNER_LOORE_IMPORT_DIR")
        var app = try launch(route: "/import", extra: ["-LooreDebugImportFile", "\(dir)/notes.zip",
                                                       "-LooreDebugImportKind", "markdown"])
        XCTAssertTrue(app.buttons["import.debugFile"].waitForExistence(timeout: 20))
        app.buttons["import.debugFile"].tap()
        XCTAssertTrue(app.buttons["import.confirm"].waitForExistence(timeout: 20))
        snapshot("m4-import-confirm-markdown")
        app.buttons["import.confirm"].tap()
        XCTAssertTrue(element(app, "import.finished").waitForExistence(timeout: 30))
        snapshot("m4-import-finished")
        app.buttons["import.ok"].tap()
        sleep(2)
        app.buttons["import.debugFile"].tap()
        XCTAssertTrue(app.buttons["import.confirm"].waitForExistence(timeout: 20))
        app.buttons["import.confirm"].tap()
        XCTAssertTrue(element(app, "import.finished").waitForExistence(timeout: 30))
        snapshot("m4-import-finished-again")
        app.buttons["import.ok"].tap()
        app.terminate()

        app = try launch(route: "/import", extra: ["-LooreDebugImportFile", "\(dir)/chatgpt-renamed.zip",
                                                   "-LooreDebugImportKind", "chatgpt"])
        XCTAssertTrue(app.buttons["import.debugFile"].waitForExistence(timeout: 20))
        app.buttons["import.debugFile"].tap()
        XCTAssertTrue(app.buttons["import.confirm"].waitForExistence(timeout: 20))
        snapshot("m4-import-confirm-chatgpt")
        app.buttons["import.cancel"].tap()
        app.terminate()

        app = try launch(route: "/import", extra: ["-LooreDebugImportFile", "\(dir)/notazip.zip",
                                                   "-LooreDebugImportKind", "claude"])
        XCTAssertTrue(app.buttons["import.debugFile"].waitForExistence(timeout: 20))
        app.buttons["import.debugFile"].tap()
        XCTAssertTrue(element(app, "import.error").waitForExistence(timeout: 10))
        snapshot("m4-import-error-notazip")
    }

    // MARK: Share

    func testShareDraftPublishRevokeDelete() throws {
        let app = try launch(route: "/share")
        let new = app.buttons["share.new"]
        XCTAssertTrue(new.waitForExistence(timeout: 20))
        new.tap()
        let editor = element(app, "share.editor")
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        editor.tap()
        editor.typeText("M4 test share: rivers carry what they cannot keep.")
        app.buttons["doc.save"].tap()
        sleep(3)
        snapshot("m4-share-draft")
        let publish = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'share.publish.'")).firstMatch
        XCTAssertTrue(publish.waitForExistence(timeout: 5))
        publish.tap()
        XCTAssertTrue(app.buttons["share.publishConfirm"].waitForExistence(timeout: 5))
        snapshot("m4-share-publish-confirm")
        app.buttons["share.publishConfirm"].tap()
        sleep(3)
        snapshot("m4-share-published")
        app.buttons["Revoke"].firstMatch.tap()
        sleep(3)
        snapshot("m4-share-revoked")
        // The revoked group is last: its Delete is the last one (never a pre-existing share).
        let deletes = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH 'share.delete.'")).allElementsBoundByIndex
        let delete = try XCTUnwrap(deletes.last)
        let keep = (env["LOORE_KEEP_SHARE_IDS"] ?? "").split(separator: ",").map { "share.delete.\($0)" }
        XCTAssertFalse(keep.contains(delete.identifier), "must not delete a pre-existing share")
        delete.tap()
        XCTAssertTrue(app.buttons["share.deleteConfirm"].waitForExistence(timeout: 5))
        snapshot("m4-share-delete-confirm")
        app.buttons["share.deleteConfirm"].tap()
        sleep(2)
    }

    // MARK: Welcome

    func testWelcomeImportSheet() throws {
        let app = try launch(route: "/welcome")
        let importButton = app.buttons["welcome.import"]
        XCTAssertTrue(importButton.waitForExistence(timeout: 20))
        app.swipeUp()
        sleep(1)
        snapshot("m4-welcome-lower")
        importButton.tap()
        XCTAssertTrue(app.buttons["import.markdown"].waitForExistence(timeout: 5))
        snapshot("m4-welcome-import-sheet")
    }
}
