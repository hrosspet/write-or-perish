import XCTest
@testable import Loore

/// Payments (#438) in the app: Settings shows the subscription's state and
/// nothing to buy; a reply refused for an account without AI replies shows
/// the server's message; the spend-cap fallback drops "free alpha" only
/// while payments are on. Without `payments_enabled` nothing changes.
final class SubscriptionTests: XCTestCase {
    override func tearDown() {
        APIError.paymentsEnabled = false
        super.tearDown()
    }

    private func map(_ status: Int, _ body: String) -> APIError {
        APIError.from(status: status, contentType: "application/json", data: Data(body.utf8))
    }

    func testAUserWithoutPaymentsDecodesAsBefore() throws {
        let user = try decode(CurrentUser.self, #"{"id": 1, "plan": "alpha"}"#)
        XCTAssertFalse(user.paymentsEnabled)
        XCTAssertNil(user.subscription)
        XCTAssertFalse(user.aiRepliesPaused)
    }

    func testTheSubscriptionDecodes() throws {
        let user = try decode(CurrentUser.self, #"""
        {"id": 1, "plan": "free", "payments_enabled": true,
         "subscription": {"level": "standard", "status": "trialing",
           "current_period_end": "2026-11-26T10:00:00Z", "trial_end": "2026-11-26T10:00:00Z",
           "cancel_at_period_end": false, "plan_set_by_hand": false, "ai_replies": true,
           "trial_available": false, "trial_days": 14, "has_customer": true}}
        """#)
        XCTAssertTrue(user.paymentsEnabled)
        let sub = try XCTUnwrap(user.subscription)
        XCTAssertEqual(sub.level, "standard")
        XCTAssertEqual(sub.status, "trialing")
        XCTAssertTrue(sub.aiReplies)
        XCTAssertFalse(user.aiRepliesPaused)
        XCTAssertEqual(sub.statusText(plan: "free"), "Standard trial until November 26, 2026.")
    }

    func testAnEndedSubscriptionPausesReplies() throws {
        let user = try decode(CurrentUser.self, #"""
        {"id": 1, "plan": "free", "payments_enabled": true,
         "subscription": {"status": "ended", "level": "standard", "ai_replies": false}}
        """#)
        XCTAssertTrue(user.aiRepliesPaused)
        XCTAssertTrue(user.subscription!.statusText(plan: "free").hasPrefix("Ended."))
    }

    func testStatusTextsOfferNothingToBuy() {
        let end = ISO8601DateFormatter().date(from: "2027-01-05T00:00:00Z")!
        let states = [
            SubscriptionState(level: "supporter", status: "active", currentPeriodEnd: end),
            SubscriptionState(level: "standard", status: "active", currentPeriodEnd: end, cancelAtPeriodEnd: true),
            SubscriptionState(level: "discounted", status: "past_due", currentPeriodEnd: end),
            SubscriptionState(status: "ended", aiReplies: false),
            SubscriptionState(status: "none", aiReplies: false),
            SubscriptionState(planSetByHand: true),
        ]
        let texts = states.map { $0.statusText(plan: "alpha") }
        XCTAssertEqual(texts[0], "Supporter. Renews on January 5, 2027.")
        XCTAssertEqual(texts[1], "Standard, cancelled. It ends on January 5, 2027.")
        XCTAssertEqual(texts[4], "No subscription.")
        XCTAssertEqual(texts[5], "Included with your plan (Alpha).")
        // A live subscription on a plan set by hand still shows: it renews.
        XCTAssertEqual(SubscriptionState(level: "standard", status: "active", currentPeriodEnd: end,
                                         planSetByHand: true).statusText(plan: "alpha"),
                       "Standard. Renews on January 5, 2027.")
        for text in texts {
            let lower = text.lowercased()
            for word in ["subscribe", "buy", "price", "$", "usd", "http", "loore.org", "web", "stripe", "manage"] {
                XCTAssertFalse(lower.contains(word), "\(text) mentions \(word)")
            }
        }
    }

    func testA402SubscriptionRequired() {
        let message = "Loore doesn't reply on an account without a subscription. Your writing is saved."
        let error = map(402, #"{"error": "subscription_required", "message": "\#(message)"}"#)
        XCTAssertEqual(error, .subscriptionRequired(message: message))
        XCTAssertEqual(error.status, 402)
        XCTAssertEqual(error.serverMessage, message)
        XCTAssertEqual(error.errorDescription, message)
        XCTAssertEqual(map(402, #"{"error": "subscription_required"}"#),
                       .subscriptionRequired(message: APIError.defaultSubscriptionMessage))
        XCTAssertFalse(APIError.defaultSubscriptionMessage.lowercased().contains("subscribe"))
    }

    func testTheSpendCapFallbackFollowsPayments() {
        APIError.paymentsEnabled = false
        XCTAssertEqual(map(402, #"{"error": "monthly_spend_limit_reached"}"#),
                       .spendCap(message: APIError.defaultSpendCapMessage))
        XCTAssertTrue(APIError.spendCapFallbackMessage.contains("free alpha"))
        APIError.paymentsEnabled = true
        XCTAssertEqual(map(402, #"{"error": "monthly_spend_limit_reached"}"#),
                       .spendCap(message: APIError.paidSpendCapMessage))
        XCTAssertFalse(APIError.spendCapFallbackMessage.contains("free alpha"))
    }

    func testASavedEntryWithASkippedReply() throws {
        let answer = try decode(TextmodeStartResponse.self, #"""
        {"user_node_id": 7, "conversation_id": 6, "subscription_required": true,
         "subscription_message": "Your subscription has ended, so Loore doesn't reply. Your writing is saved."}
        """#)
        XCTAssertNil(answer.llmNodeId)
        XCTAssertEqual(answer.subscriptionNotice,
                       "Your subscription has ended, so Loore doesn't reply. Your writing is saved.")
        let plain = try decode(TextmodeStartResponse.self, #"{"user_node_id": 7, "llm_node_id": 8}"#)
        XCTAssertNil(plain.subscriptionNotice)
        let saved = try decode(SaveAsNodeResponse.self, #"{"id": 3, "subscription_required": true}"#)
        XCTAssertEqual(saved.subscriptionNotice, APIError.defaultSubscriptionMessage)
    }

    @MainActor
    func testTheAppStateFollowsTheUser() throws {
        let app = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(),
                           defaults: UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!)
        let paused = try decode(CurrentUser.self, #"""
        {"id": 1, "plan": "free", "payments_enabled": true, "subscription": {"status": "none", "ai_replies": false}}
        """#)
        app.replaceUser(paused)
        XCTAssertTrue(app.subscriptionRequired)
        XCTAssertTrue(APIError.paymentsEnabled)
        app.handle(.subscriptionRequired(message: "Why."))
        XCTAssertEqual(app.subscriptionBannerMessage, "Why.")
        let alpha = try decode(CurrentUser.self, #"{"id": 1, "plan": "alpha"}"#)
        app.replaceUser(alpha)
        XCTAssertFalse(app.subscriptionRequired)
        XCTAssertFalse(APIError.paymentsEnabled)
    }

    /// Peter, 2026-11-12: "people shouldn't be let in until they have a
    /// subscription". Only accounts in state "none" get NoSubscriptionView.
    func testWhoIsNotLetIn() throws {
        func user(_ subscription: String, payments: Bool = true, approved: Bool = true) throws -> CurrentUser {
            try decode(CurrentUser.self, #"""
            {"id": 1, "plan": "free", "approved": \#(approved), "payments_enabled": \#(payments),
             "subscription": \#(subscription)}
            """#)
        }
        XCTAssertTrue(try user(#"{"state": "none", "status": "none", "ai_replies": false}"#).notLetIn)
        // The first payment failed: never paid, so not let in either.
        XCTAssertTrue(try user(#"{"state": "none", "status": "past_due", "ai_replies": false}"#).notLetIn)
        XCTAssertFalse(try user(#"{"state": "active", "status": "trialing"}"#).notLetIn)
        XCTAssertFalse(try user(#"{"state": "lapsed", "status": "ended", "ai_replies": false}"#).notLetIn)
        XCTAssertFalse(try user(#"{"state": "set_by_hand", "status": "none", "plan_set_by_hand": true}"#).notLetIn)
        XCTAssertFalse(try user(#"{"state": "none", "status": "none"}"#, payments: false).notLetIn)
        XCTAssertFalse(try user(#"{"state": "none", "status": "none"}"#, approved: false).notLetIn)
        // An older server sends no state: nobody is stopped.
        XCTAssertFalse(try user(#"{"status": "none", "ai_replies": false}"#).notLetIn)
        XCTAssertFalse(try decode(CurrentUser.self, #"{"id": 1, "plan": "free", "approved": true}"#).notLetIn)
    }

    func testTheScreenWithoutASubscriptionOffersNothingToBuy() {
        let texts = [NoSubscriptionView.message, AppState.paymentFailedMessage,
                     SubscriptionState(level: "standard", status: "past_due").statusText(plan: "free")]
        for text in texts {
            let lower = text.lowercased()
            for word in ["subscribe", "buy", "price", "$", "usd", "http", "loore.org", "web", "stripe",
                         "manage", "settings", "card"] {
                XCTAssertFalse(lower.contains(word), "\(text) mentions \(word)")
            }
        }
    }

    @MainActor
    func testAFailedPaymentIsSaidOnLoad() throws {
        let app = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(),
                           defaults: UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!)
        let failed = try decode(CurrentUser.self, #"""
        {"id": 1, "plan": "free", "approved": true, "payments_enabled": true,
         "subscription": {"state": "lapsed", "status": "past_due", "level": "standard", "ai_replies": false}}
        """#)
        XCTAssertFalse(failed.notLetIn)
        app.replaceUser(failed)
        XCTAssertEqual(app.subscriptionBannerMessage, AppState.paymentFailedMessage)
        XCTAssertTrue(app.subscriptionRequired)
        // Payments off: no banner.
        let off = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(),
                           defaults: UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!)
        off.replaceUser(try decode(CurrentUser.self, #"{"id": 1, "plan": "free", "approved": true}"#))
        XCTAssertNil(off.subscriptionBannerMessage)
    }
}
