import XCTest
@testable import Loore

/// The in-app web views that carry the session (Admin, "Open on the web") stay
/// on Loore's own host; a link to another site goes to the browser.
final class SessionWebViewTests: XCTestCase {
    private let hosts = AppEnvironment.production.sessionHosts

    private func leaves(_ string: String, inFrame: Bool = false) -> Bool {
        AuthenticatedWebView.leaves(URL(string: string)!, inFrame: inFrame, hosts: hosts)
    }

    func testTheSessionHostsAreTheEnvironmentsOwn() {
        XCTAssertEqual(AppEnvironment.production.sessionHosts, ["loore.org"])
        XCTAssertEqual(AppEnvironment.staging.sessionHosts, ["staging.loore.org"])
    }

    func testLooresOwnPagesStayInTheView() {
        XCTAssertFalse(leaves("https://loore.org/admin"))
        XCTAssertFalse(leaves("https://loore.org/account?tab=1#top"))
        XCTAssertFalse(leaves("https://LOORE.org/admin"))
    }

    func testAnotherSiteLeavesTheView() {
        XCTAssertTrue(leaves("https://example.com/"))
        XCTAssertTrue(leaves("http://example.com/page"))
        XCTAssertTrue(leaves("https://staging.loore.org/admin"), "another environment's host")
        XCTAssertTrue(leaves("https://loore.org.example.com/admin"))
        XCTAssertTrue(leaves("https://example.com/?next=loore.org"))
        XCTAssertTrue(leaves("https://loore.org@example.com/"))
    }

    func testFramesInsideAPageAndOtherSchemesAreLeftAlone() {
        XCTAssertFalse(leaves("https://www.youtube-nocookie.com/embed/abc", inFrame: true))
        XCTAssertFalse(leaves("about:blank"))
        XCTAssertFalse(leaves("blob:https://loore.org/1234"))
    }
}
