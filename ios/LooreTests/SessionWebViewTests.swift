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

    // Flows a Loore page starts and that come back to Loore.

    private func leavesWithFlows(_ string: String) -> Bool {
        AuthenticatedWebView.leaves(URL(string: string)!, inFrame: false, hosts: hosts,
                                    flowHosts: SessionWebFlowHosts.all)
    }

    func testTheFlowHostsAreTheFourNamedOnes() {
        XCTAssertEqual(SessionWebFlowHosts.all,
                       ["checkout.stripe.com", "billing.stripe.com", "twitter.com", "x.com"])
    }

    func testSubscribingAndManagingTheSubscriptionStayInTheView() {
        XCTAssertFalse(leavesWithFlows("https://checkout.stripe.com/c/pay/cs_test_a1"))
        XCTAssertFalse(leavesWithFlows("https://billing.stripe.com/p/session/test_1"))
        // And back.
        XCTAssertFalse(leavesWithFlows("https://loore.org/account?subscription=success"))
    }

    func testConnectingXStaysInTheView() {
        XCTAssertFalse(leavesWithFlows("https://twitter.com/i/oauth2/authorize?client_id=a&state=b"))
        XCTAssertFalse(leavesWithFlows("https://x.com/i/oauth2/authorize?client_id=a&state=b"))
        XCTAssertFalse(leavesWithFlows("https://X.com/i/flow/login"))
        XCTAssertFalse(leavesWithFlows("https://loore.org/api/external/twitter/callback?code=c&state=b"))
    }

    func testOnlyThoseExactHostsOverHTTPSStay() {
        XCTAssertTrue(leavesWithFlows("http://checkout.stripe.com/c/pay/cs_test_a1"), "not https")
        XCTAssertTrue(leavesWithFlows("http://x.com/i/oauth2/authorize"), "not https")
        XCTAssertTrue(leavesWithFlows("https://stripe.com/"))
        XCTAssertTrue(leavesWithFlows("https://dashboard.stripe.com/"))
        XCTAssertTrue(leavesWithFlows("https://pay.checkout.stripe.com/"))
        XCTAssertTrue(leavesWithFlows("https://checkout.stripe.com.example.com/"))
        XCTAssertTrue(leavesWithFlows("https://www.x.com/"))
        XCTAssertTrue(leavesWithFlows("https://mobile.twitter.com/"))
        XCTAssertTrue(leavesWithFlows("https://notx.com/"))
        XCTAssertTrue(leavesWithFlows("https://example.com/"))
        // Without the flow hosts, as before.
        XCTAssertTrue(leaves("https://checkout.stripe.com/c/pay/cs_test_a1"))
        XCTAssertTrue(leaves("https://x.com/i/oauth2/authorize"))
    }

    func testFramesInsideAPageAndOtherSchemesAreLeftAlone() {
        XCTAssertFalse(leaves("https://www.youtube-nocookie.com/embed/abc", inFrame: true))
        XCTAssertFalse(leaves("about:blank"))
        XCTAssertFalse(leaves("blob:https://loore.org/1234"))
    }
}
