import XCTest
@testable import Loore

/// Payments on (#697; Peter, 2026-11-13): nobody waits for approval, so the
/// only account that is not let in is one the admin blocked. The server sends
/// `account_blocked`; the app shows `AccountNotActiveView`, asks for no terms,
/// and maps the blocked account's 403 like the waiting one's (the user is read
/// again). Without the key nothing changes.
final class AccountBlockedTests: XCTestCase {
    private func map(_ status: Int, _ body: String) -> APIError {
        APIError.from(status: status, contentType: "application/json", data: Data(body.utf8))
    }

    func testControlNoKeyMeansNotBlocked() throws {
        let user = try decode(CurrentUser.self, #"{"id": 1, "plan": "alpha", "approved": false}"#)
        XCTAssertFalse(user.accountBlocked)
    }

    func testTheKeyDecodes() throws {
        let user = try decode(CurrentUser.self, #"""
        {"id": 1, "plan": "free", "approved": false, "payments_enabled": true, "account_blocked": true}
        """#)
        XCTAssertTrue(user.accountBlocked)
        XCTAssertFalse(user.notLetIn)
    }

    func testTheBlocked403IsAnApprovalChange() {
        let body = #"{"error": "This account is not active. Write to info@loore.org.", "code": "account_not_active"}"#
        XCTAssertEqual(map(403, body), .notApproved(message: "This account is not active. Write to info@loore.org."))
        // Control: the waiting answer, and any other 403.
        XCTAssertEqual(map(403, #"{"error":"Your account is not approved. Please wait for approval."}"#),
                       .notApproved(message: "Your account is not approved. Please wait for approval."))
        XCTAssertEqual(map(403, #"{"error":"Not authorized"}"#).status, 403)
        XCTAssertNotEqual(map(403, #"{"error":"Not authorized"}"#),
                          .notApproved(message: "Not authorized"))
    }

    @MainActor
    func testNoTermsForABlockedAccount() throws {
        let app = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(),
                           defaults: UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!)
        app.replaceUser(try decode(CurrentUser.self, #"""
        {"id": 1, "plan": "free", "approved": false, "terms_up_to_date": false,
         "payments_enabled": true, "account_blocked": true}
        """#))
        XCTAssertFalse(app.needsTerms)
        // Control: a waiting account is asked as before.
        app.replaceUser(try decode(CurrentUser.self, #"""
        {"id": 1, "plan": "alpha", "approved": false, "terms_up_to_date": false}
        """#))
        XCTAssertTrue(app.needsTerms)
    }

    func testTheScreenSaysNothingAboutPaying() {
        XCTAssertEqual(AccountNotActiveView.message, "This account is not active. Write to info@loore.org.")
        let lower = AccountNotActiveView.message.lowercased()
        for word in ["subscri", "buy", "price", "pay", "$", "trial", "card", "plan"] {
            XCTAssertFalse(lower.contains(word), "mentions \(word)")
        }
    }
}
