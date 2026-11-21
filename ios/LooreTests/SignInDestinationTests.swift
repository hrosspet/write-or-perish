import XCTest
@testable import Loore

/// After signing in, back to the entry where the sign-in started (#704), on
/// the iPhone. The app has no universal links: a Loore link opens in Safari,
/// never in the app. What lands on an entry here is a sign-in link pasted
/// into the app (requested on the web, where it carries the page): the
/// verify's redirect names the page, and the app opens it once the account
/// can see the app (terms first). Tests marked "control" pass on the base
/// too: they pin what already worked.
@MainActor
final class SignInDestinationTests: StubbedAppTestCase {
    private func user(approved: Bool, terms: Bool) -> String {
        """
        {"user":{"id":5,"username":"seowriter","approved":\(approved),"terms_up_to_date":\(terms),
         "plan":"alpha","craft_mode":false,"timezone":"UTC"}}
        """
    }

    private func serve(approved: Bool, terms: Bool, permalinkNode: Int? = nil) {
        let dashboard = user(approved: approved, terms: terms)
        StubURLProtocol.install { request in
            let path = request.url?.path(percentEncoded: true) ?? ""
            if path.hasPrefix("/api/dashboard") { return .json(200, dashboard) }
            if path == "/api/terms/accept" { return .json(200, #"{"message":"ok"}"#) }
            if path.hasPrefix("/api/commons/permalink/"), let id = permalinkNode {
                return .json(200, #"{"node_id":\#(id)}"#)
            }
            return .json(200, "{}")
        }
    }

    private var openedRoutes: [AppRoute] { app.router.path(for: app.router.selectedTab) }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    func testControlAPastedLinkLandsOnItsEntry() async {
        serve(approved: true, terms: true)
        let landing = MagicLink.landingPath(location: "https://loore.org/node/41#reply-2")
        XCTAssertEqual(landing, "/node/41#reply-2")
        await app.signInCompleted(landing: landing)
        XCTAssertEqual(openedRoutes.last, .thread(id: 41, awaitLLM: nil))
    }

    func testControlAPermalinkLandingOpensItsThread() async {
        serve(approved: true, terms: true, permalinkNode: 41)
        let landing = MagicLink.landingPath(location: "https://loore.org/@ana/a-first-entry#reply-7")
        XCTAssertEqual(landing, "/@ana/a-first-entry#reply-7")
        await app.signInCompleted(landing: landing)
        let opened = await eventually { self.openedRoutes.last == .thread(id: 41, awaitLLM: nil) }
        XCTAssertTrue(opened)
    }

    func testControlTheTermsComeFirst() async throws {
        serve(approved: true, terms: false)
        await app.signInCompleted(landing: "/node/41")
        XCTAssertTrue(openedRoutes.isEmpty, "the terms screen first")
        try await app.acceptTerms()
        XCTAssertEqual(openedRoutes.last, .thread(id: 41, awaitLLM: nil))
    }

    func testAWaitingAccountDropsTheEntry() async {
        serve(approved: false, terms: true)
        await app.signInCompleted(landing: "/node/41")
        XCTAssertTrue(openedRoutes.isEmpty)
        // Approved later in the same run: the entry is not opened out of
        // nowhere, as on the web (the waiting screen dropped it).
        serve(approved: true, terms: true)
        await app.loadUser()
        XCTAssertTrue(openedRoutes.isEmpty)
    }

    func testAnEntryNotSharedSaysSoPlainly() async {
        StubURLProtocol.install { _ in .json(404, #"{"error":"Node not found"}"#) }
        let model = ThreadModel(nodeId: 41, awaitLLM: nil, app: app)
        await model.load()
        XCTAssertEqual(model.pageError,
                       "This entry isn't shared with you. It may be private, or it may have been deleted.")
    }

    func testBackFromXWithoutASignInMatchesTheWeb() {
        XCTAssertEqual(MagicLink.message(for: "x_cancelled"), "Sign in with X was cancelled.")
        XCTAssertEqual(MagicLink.message(for: "x_failed"), "Sign in with X did not finish. Please try again.")
        // The in-app X sign-in reads the code off the sign-in page it lands on.
        let cancelled = URL(string: "https://loore.org/login?error=x_cancelled&returnUrl=%2Fprofile")!
        XCTAssertTrue(WebLoginRouting.isFrontendLanding(cancelled, environment: .production))
        XCTAssertEqual(WebLoginRouting.loginErrorCode(cancelled), "x_cancelled")
    }
}
