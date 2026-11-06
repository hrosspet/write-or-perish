import XCTest

/// Deleting an artifact (#603) against a local backend (not in CI). A long press
/// on a bubble opens its menu: Edit and Delete for a custom artifact, Edit only for
/// the built-in kinds, the Saved References Digest, Profile and Todo, none for "+".
/// Edit opens the document's edit form; Delete asks, removes the bubble, opens the
/// next one when it was open, and toasts; a link to the deleted artifact says so.
///
/// The test makes its three custom artifacts itself (`PUT`) and deletes them (a soft
/// delete, purged after 30 days); it skips when one of those names is already in use.
/// Inputs: `TEST_RUNNER_LOORE_SESSION_COOKIE`, `TEST_RUNNER_LOORE_BACKEND_URL` (default
/// `http://localhost:5010`), `TEST_RUNNER_LOORE_SCREENSHOT_DIR`. The user should have a
/// profile, a todo list and a Saved References Digest (the digest's bubble is skipped
/// when it is missing).
final class ArtifactDeleteUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }

    private static let custom = [
        ("landlord-case", "Landlord Case", "Notes for the landlord case"),
        ("loore-development-map", "Loore Development Map", "Guide post to all ideas related to the development of Loore"),
        ("winter-solstice-gathering", "Winter Solstice Gathering", "Plans for the solstice gathering"),
    ]

    override func setUp() {
        continueAfterFailure = false
    }

    // MARK: Helpers

    private var cookie: String {
        get throws {
            try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                          "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        }
    }

    private var backend: String {
        env["LOORE_BACKEND_URL"].flatMap { $0.isEmpty ? nil : $0 } ?? "http://localhost:5010"
    }

    /// A call as the signed-in user: (status, JSON object).
    private func call(_ method: String, _ path: String, json: [String: Any]? = nil) throws -> (Int, [String: Any]) {
        var request = URLRequest(url: try XCTUnwrap(URL(string: backend + path)))
        request.httpMethod = method
        request.setValue("session=\(try cookie)", forHTTPHeaderField: "Cookie")
        if let json {
            request.httpBody = try JSONSerialization.data(withJSONObject: json)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        var result: (Int, Data)?
        let done = expectation(description: "\(method) \(path)")
        URLSession.shared.dataTask(with: request) { data, response, _ in
            result = ((response as? HTTPURLResponse)?.statusCode ?? 0, data ?? Data())
            done.fulfill()
        }.resume()
        wait(for: [done], timeout: 15)
        let (status, data) = try XCTUnwrap(result)
        return (status, (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:])
    }

    private func liveKinds() throws -> Set<String> {
        let (status, body) = try call("GET", "/api/artifacts/")
        XCTAssertEqual(status, 200, "the artifact list")
        let rows = body["artifacts"] as? [[String: Any]] ?? []
        return Set(rows.compactMap { $0["created_at"] is String ? $0["kind"] as? String : nil })
    }

    private func seed() throws {
        let live = try liveKinds()
        for (kind, _, _) in Self.custom where live.contains(kind) {
            throw XCTSkip("the user already has an artifact \"\(kind)\", which this test would delete")
        }
        for (kind, title, description) in Self.custom {
            let (status, _) = try call("PUT", "/api/artifacts/\(kind)", json: [
                "title": title, "description": description, "generated_by": "user",
                "content": "## \(title)\n\nSynthetic test content for the delete check.\n\n- [ ] a first item",
            ])
            XCTAssertEqual(status, 200, "PUT \(kind)")
        }
    }

    private func launch(route: String, theme: String = "dark") throws -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreResetState", "YES", "-LooreSessionCookie", try cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", theme, "-LooreRoute", route,
                                "-loore.debug.localBackendURL", backend]
        app.launch()
        return app
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

    private func bubble(_ app: XCUIApplication, _ title: String) -> XCUIElement {
        app.buttons["artifactsNav.\(title)"]
    }

    private func element(_ app: XCUIApplication, _ id: String) -> XCUIElement {
        app.descendants(matching: .any)[id].firstMatch
    }

    /// Long-presses a bubble and waits for its menu (`nil` when none opened).
    @discardableResult
    private func openMenu(_ app: XCUIApplication, _ title: String, expectMenu: Bool = true) -> XCUIElement? {
        let target = bubble(app, title)
        XCTAssertTrue(target.waitForExistence(timeout: 15), "no bubble \(title)")
        target.press(forDuration: 1.0)
        let edit = app.buttons["Edit"]
        if expectMenu {
            XCTAssertTrue(edit.waitForExistence(timeout: 5), "no menu on \(title)")
            sleep(1) // the menu's spring
            return edit
        }
        XCTAssertFalse(edit.waitForExistence(timeout: 2), "\(title) opened a menu")
        return nil
    }

    private func dismissMenu(_ app: XCUIApplication) {
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.85)).tap()
        XCTAssertTrue(app.buttons["Edit"].waitForNonExistence(timeout: 5))
        sleep(1)
    }

    private func toast(_ app: XCUIApplication, _ text: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: "toast")
            .matching(NSPredicate(format: "label == %@", text)).firstMatch
    }

    private func title(_ app: XCUIApplication, is text: String) -> Bool {
        app.staticTexts.matching(NSPredicate(format: "label == %@", text)).firstMatch.waitForExistence(timeout: 5)
    }

    // MARK: The flow

    func testLongPressMenusEditAndDelete() throws {
        try seed()
        var app = try launch(route: "/artifacts/loore-development-map")
        XCTAssertTrue(bubble(app, "Loore Development Map").waitForExistence(timeout: 20))
        sleep(1)
        let hasDigest = bubble(app, "Saved References Digest").exists

        // Menus: Edit and Delete on custom artifacts, Edit only elsewhere, none on "+".
        openMenu(app, "Loore Development Map")
        XCTAssertTrue(app.buttons["Delete"].exists)
        snapshot("01-menu-custom-open")
        dismissMenu(app)

        openMenu(app, "Landlord Case")
        XCTAssertTrue(app.buttons["Delete"].exists)
        snapshot("02-menu-custom-not-open")
        dismissMenu(app)

        openMenu(app, "Memory")
        XCTAssertFalse(app.buttons["Delete"].exists, "a built-in offered Delete")
        snapshot("03-menu-builtin-edit-only")
        dismissMenu(app)

        if hasDigest {
            openMenu(app, "Saved References Digest")
            XCTAssertFalse(app.buttons["Delete"].exists, "the digest offered Delete")
            snapshot("04-menu-digest-edit-only")
            dismissMenu(app)
        }

        // Edit from the menu: Profile, Todo, then a built-in artifact.
        openMenu(app, "Profile")
        XCTAssertFalse(app.buttons["Delete"].exists, "Profile offered Delete")
        snapshot("05-menu-profile-edit-only")
        app.buttons["Edit"].tap()
        XCTAssertTrue(element(app, "profile.editor").waitForExistence(timeout: 10), "Edit did not open the profile's form")
        snapshot("05b-profile-edit-from-menu")
        app.buttons["doc.cancel"].tap()

        openMenu(app, "Todo")
        XCTAssertFalse(app.buttons["Delete"].exists, "Todo offered Delete")
        snapshot("06-menu-todo-edit-only")
        app.buttons["Edit"].tap()
        XCTAssertTrue(element(app, "todo.editor").waitForExistence(timeout: 10), "Edit did not open the todo's form")
        snapshot("07-todo-edit-from-menu")
        app.buttons["doc.cancel"].tap()

        openMenu(app, "Memory")?.tap()
        XCTAssertTrue(element(app, "artifact.editor").waitForExistence(timeout: 10), "Edit did not open Memory's form")
        XCTAssertTrue(title(app, is: "Memory"))
        snapshot("08-memory-edit-from-menu")
        app.buttons["doc.cancel"].tap()

        // "+" has no menu.
        openMenu(app, "+", expectMenu: false)
        if app.buttons["doc.cancel"].waitForExistence(timeout: 3) { app.buttons["doc.cancel"].tap() }

        // The light theme: the lifted bubble on the page colour.
        app.terminate()
        app = try launch(route: "/artifacts/loore-development-map", theme: "light")
        openMenu(app, "Landlord Case")
        snapshot("09-menu-light-not-open")
        dismissMenu(app)
        openMenu(app, "Loore Development Map")
        snapshot("10-menu-light-open")
        dismissMenu(app)
        app.terminate()

        // The confirmation; Cancel leaves everything.
        app = try launch(route: "/artifacts/loore-development-map")
        openMenu(app, "Loore Development Map")
        app.buttons["Delete"].tap()
        let confirm = app.buttons["artifact.confirmDelete"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 5), "Delete did not open the confirmation")
        sleep(1)
        snapshot("11-delete-dialog")
        app.buttons["artifact.cancelDelete"].tap()
        XCTAssertTrue(confirm.waitForNonExistence(timeout: 5))
        XCTAssertTrue(bubble(app, "Loore Development Map").exists)

        // Unsaved edits go with it, and the dialog says so.
        bubble(app, "Winter Solstice Gathering").tap()
        XCTAssertTrue(title(app, is: "Winter Solstice Gathering"))
        app.buttons["doc.versionChip"].tap()
        let editor = element(app, "artifact.editor")
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        editor.tap()
        editor.typeText(" Bring candles.")
        openMenu(app, "Winter Solstice Gathering")
        app.buttons["Delete"].tap()
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "Your unsaved edits go with it.")).firstMatch.exists)
        sleep(1)
        snapshot("12-delete-dialog-unsaved-edits")
        app.buttons["artifact.cancelDelete"].tap()
        XCTAssertTrue(confirm.waitForNonExistence(timeout: 5))
        XCTAssertTrue(editor.exists, "Cancel closed the editor")
        app.buttons["doc.cancel"].tap()

        // Delete the open artifact: the next bubble opens, a toast names it.
        bubble(app, "Loore Development Map").tap()
        XCTAssertTrue(title(app, is: "Loore Development Map"))
        openMenu(app, "Loore Development Map")
        app.buttons["Delete"].tap()
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        confirm.tap()
        XCTAssertTrue(bubble(app, "Loore Development Map").waitForNonExistence(timeout: 10), "the bubble stayed")
        XCTAssertTrue(toast(app, "Loore Development Map deleted").waitForExistence(timeout: 5), "no toast")
        snapshot("13-after-delete-next-opens") // while the toast shows (3 s)
        let next = hasDigest ? "Saved References Digest" : "Winter Solstice Gathering"
        XCTAssertTrue(title(app, is: next), "the next bubble did not open")
        XCTAssertTrue(toast(app, "Loore Development Map deleted").waitForNonExistence(timeout: 6))

        // Delete a bubble that is not open: the open one stays.
        openMenu(app, "Landlord Case")
        app.buttons["Delete"].tap()
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        confirm.tap()
        XCTAssertTrue(bubble(app, "Landlord Case").waitForNonExistence(timeout: 10))
        XCTAssertTrue(toast(app, "Landlord Case deleted").waitForExistence(timeout: 5))
        snapshot("14-after-delete-not-open-stays")
        XCTAssertTrue(title(app, is: next), "the open artifact changed")
        XCTAssertTrue(toast(app, "Landlord Case deleted").waitForNonExistence(timeout: 6))

        // Delete the last bubble while it is open: the previous one opens.
        bubble(app, "Winter Solstice Gathering").tap()
        XCTAssertTrue(title(app, is: "Winter Solstice Gathering"))
        openMenu(app, "Winter Solstice Gathering")
        app.buttons["Delete"].tap()
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        confirm.tap()
        XCTAssertTrue(bubble(app, "Winter Solstice Gathering").waitForNonExistence(timeout: 10))
        XCTAssertTrue(title(app, is: hasDigest ? "Saved References Digest" : "AI Interaction Preferences"),
                      "the previous bubble did not open")
        snapshot("15-after-delete-last-previous-opens")
        app.terminate()

        // A link to a deleted artifact says so.
        app = try launch(route: "/artifacts/loore-development-map")
        XCTAssertTrue(element(app, "artifact.deleted").waitForExistence(timeout: 20), "the deleted link opened an artifact")
        XCTAssertFalse(bubble(app, "Loore Development Map").exists)
        snapshot("16-deleted-link")
        XCTAssertFalse(try liveKinds().contains { kind in Self.custom.contains { $0.0 == kind } })
    }
}
