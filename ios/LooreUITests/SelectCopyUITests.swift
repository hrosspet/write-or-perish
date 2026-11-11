import XCTest
import UIKit

/// Selecting and copying reading text (Peter, 2026-11-10: "currently it's not
/// possible to select text in the app and copy it") against a local backend
/// (not in CI). The test makes its own thread through the API, an entry and a
/// markdown reply under it, and deletes both at the end; nothing is billed.
///
/// What it walks: a long press on a word of the reply selects it; dragging the
/// end handle into the next paragraph widens the selection across paragraphs;
/// Copy puts exactly that text on the pasteboard; the text is not editable (no
/// keyboard); a link in the text still opens on a tap; a tap outside the text
/// clears the selection; in an expanded reply card a 0.7 s press selects and a
/// 0.4 s press on the text still opens the reply; the profile's text can be
/// selected too (skipped without a profile).
///
/// Inputs: `TEST_RUNNER_LOORE_SESSION_COOKIE`, `TEST_RUNNER_LOORE_BACKEND_URL`
/// (default `http://localhost:5010`), `TEST_RUNNER_LOORE_SCREENSHOT_DIR`. The
/// coordinates assume an iPhone 17 at the default text size.
final class SelectCopyUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }

    static let entry = "Select and copy check\n\nAn entry for the selection test. Its reply is below."
    static let reply = """
        The first paragraph of the reply has **bold words**, *italic words* and a link to \
        [the example site](https://example.com).

        A second paragraph, so a selection can cross from one paragraph into the next.

        ## A heading further down

        - A bullet about rivers
        - A bullet about mountains
        """

    private var created: [Int] = []

    override func setUp() {
        continueAfterFailure = false
    }

    override func tearDownWithError() throws {
        // Children first: the entry's delete would keep a reply with a deleted parent.
        for id in created.reversed() { _ = try? call("DELETE", "/api/nodes/\(id)") }
        created = []
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

    @discardableResult
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

    /// The test's entry and reply: (entry id, reply id).
    private func seed() throws -> (Int, Int) {
        let (s1, root) = try call("POST", "/api/nodes/", json: ["content": Self.entry])
        let rootId = try XCTUnwrap(root["id"] as? Int, "POST entry: \(s1)")
        created.append(rootId)
        let (s2, child) = try call("POST", "/api/nodes/", json: ["content": Self.reply, "parent_id": rootId])
        let replyId = try XCTUnwrap(child["id"] as? Int, "POST reply: \(s2)")
        created.append(replyId)
        return (rootId, replyId)
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

    /// The selectable text (a text view) whose text starts with `prefix`.
    private func text(_ app: XCUIApplication, startingWith prefix: String) -> XCUIElement {
        app.textViews.matching(NSPredicate(format: "value BEGINSWITH %@", prefix)).firstMatch
    }

    private func at(_ element: XCUIElement, _ x: CGFloat, _ y: CGFloat) -> XCUICoordinate {
        element.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: x, dy: y))
    }

    /// What Copy put on the pasteboard, read back as a user would: pasted into
    /// the Log's search field (a fresh launch; the search only reads). The test
    /// runner itself may not read another app's pasteboard. A one-line field
    /// turns newlines into spaces.
    private func pasted() throws -> String {
        let app = try launch(route: "/log")
        XCTAssertTrue(app.buttons["Search your entries"].waitForExistence(timeout: 20), "no Log")
        app.buttons["Search your entries"].tap()
        let field = app.textFields["search.field"]
        XCTAssertTrue(field.waitForExistence(timeout: 5), "no search field")
        field.tap()
        sleep(1)
        field.press(forDuration: 1.0)
        let paste = app.menuItems["Paste"]
        XCTAssertTrue(paste.waitForExistence(timeout: 5), "no Paste in the search field")
        paste.tap()
        sleep(1)
        snapshot("select-pasted")
        return field.value as? String ?? ""
    }

    // MARK: The flows

    func testSelectPartOfAReplyAcrossParagraphsAndCopy() throws {
        let (_, replyId) = try seed()
        let app = try launch(route: "/node/\(replyId)")
        let reply = text(app, startingWith: "The first paragraph")
        XCTAssertTrue(reply.waitForExistence(timeout: 20), "the reply did not load")
        sleep(2) // the scroll to the focal card

        // A long press on "first" selects the word and shows the menu.
        at(reply, 46, 10).press(forDuration: 1.0)
        XCTAssertTrue(app.menuItems["Copy"].waitForExistence(timeout: 5), "no Copy after a long press")
        XCTAssertEqual(app.keyboards.count, 0, "the reading text opened a keyboard")
        snapshot("select-01-word")

        // The end handle (its knob under the word), dragged into the second
        // paragraph's second line; the finger stays as far below the text as it
        // started below it.
        at(reply, 58, 26).press(forDuration: 0.3, thenDragTo: at(reply, 60, 118), withVelocity: .slow,
                                 thenHoldForDuration: 0.5)
        XCTAssertTrue(app.menuItems["Copy"].waitForExistence(timeout: 5), "no Copy after dragging the handle")
        snapshot("select-02-across-paragraphs")
        app.menuItems["Copy"].tap()
        sleep(1)

        let copied = try pasted()
        XCTAssertTrue(copied.hasPrefix("first paragraph"), copied)
        XCTAssertTrue(copied.contains("the example site. A"), "the selection did not cross into the next paragraph: \(copied)")
        XCTAssertFalse(copied.contains("The first"), "more than the selection was copied: \(copied)")
        XCTAssertFalse(copied.contains("heading"), "more than the selection was copied: \(copied)")
    }

    func testTapOutsideClearsTheSelectionAndLinksStillOpen() throws {
        let (_, replyId) = try seed()
        let app = try launch(route: "/node/\(replyId)")
        let reply = text(app, startingWith: "The first paragraph")
        XCTAssertTrue(reply.waitForExistence(timeout: 20), "the reply did not load")
        sleep(2)

        at(reply, 46, 10).press(forDuration: 1.0)
        XCTAssertTrue(app.menuItems["Copy"].waitForExistence(timeout: 5))
        // The focal card's padding, above the text.
        at(reply, 150, -16).tap()
        sleep(1)
        XCTAssertFalse(app.menuItems["Copy"].exists, "the menu stayed after a tap outside")
        snapshot("select-04-cleared")

        let link = reply.links["the example site"]
        XCTAssertTrue(link.waitForExistence(timeout: 5), "the link is not a link")
        link.tap()
        // The in-app Safari view (the router's external route), not a selection.
        XCTAssertTrue(app.webViews.firstMatch.waitForExistence(timeout: 10)
                      || app.otherElements["TopBrowserBar"].waitForExistence(timeout: 2), "the link did not open")
        XCTAssertFalse(app.menuItems["Copy"].exists, "the tap on the link selected text")
        snapshot("select-05-link-opened")
    }

    func testTapOnAnExpandedReplyCardStillOpensIt() throws {
        let (rootId, _) = try seed()
        let app = try launch(route: "/node/\(rootId)")
        let expand = app.buttons["Expand preview"].firstMatch
        XCTAssertTrue(expand.waitForExistence(timeout: 20), "the reply card has no expand button")
        expand.tap()
        let card = text(app, startingWith: "The first paragraph")
        XCTAssertTrue(card.waitForExistence(timeout: 5), "the expanded card shows no text")
        sleep(1)
        app.swipeUp() // the card above the tab bar
        sleep(1)
        snapshot("select-06a-card-expanded")

        // A long press selects in the card too, after its usual delay: a 0.7 s hold
        // is enough although the card's own tap waits up to 0.45 s.
        at(card, 46, 12).press(forDuration: 0.7)
        XCTAssertTrue(app.menuItems["Copy"].waitForExistence(timeout: 5), "no Copy after a 0.7 s press in the card")
        snapshot("select-06-card-selection")
        // A tap only clears the selection.
        at(card, 150, 12).tap()
        sleep(1)
        XCTAssertTrue(app.buttons["Collapse preview"].exists, "the tap that cleared the selection opened the card")
        XCTAssertFalse(app.menuItems["Copy"].exists, "the tap did not clear the selection")
        // A slow press (0.4 s, under the long press) opens the reply, as on the card's padding.
        at(card, 150, 12).press(forDuration: 0.4)
        // The reply's own page: its text is the focal card, no longer an expanded card.
        let collapse = app.buttons["Collapse preview"]
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline && collapse.exists { usleep(200_000) }
        sleep(1)
        snapshot("select-07-card-opened")
        XCTAssertFalse(collapse.exists, "a 0.4 s press on the card's text did not open the reply")
        XCTAssertFalse(app.menuItems["Copy"].exists, "a 0.4 s press selected text")
        XCTAssertTrue(text(app, startingWith: "The first paragraph").exists, "the reply's page shows no reply")
    }

    func testProfileTextCanBeSelected() throws {
        let app = try launch(route: "/profile")
        let content = app.descendants(matching: .any)["profile.content"].firstMatch
        guard content.waitForExistence(timeout: 20) else { throw XCTSkip("the test user has no profile") }
        let first = content.textViews.firstMatch
        XCTAssertTrue(first.waitForExistence(timeout: 5), "the profile's text is not selectable text")
        sleep(1)
        // Hold on a word, then keep the finger down and drag: the selection follows it.
        at(first, 40, 12).press(forDuration: 1.0, thenDragTo: at(first, 160, 90), withVelocity: .slow,
                                thenHoldForDuration: 0.5)
        XCTAssertTrue(app.menuItems["Copy"].waitForExistence(timeout: 5), "no Copy on the profile")
        snapshot("select-08-profile")
        app.menuItems["Copy"].tap()
        sleep(1)
        let profile = (first.value as? String ?? "").replacingOccurrences(of: "\n", with: " ")
        let copied = try pasted()
        XCTAssertFalse(copied.isEmpty)
        XCTAssertTrue(profile.contains(copied), "copied text is not from the profile: \(copied)")
    }
}
