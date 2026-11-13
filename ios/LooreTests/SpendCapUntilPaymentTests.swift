import XCTest
@testable import Loore

/// Payments (#438): the trial's usage ceiling and an alpha tester's free month's
/// lift with the first payment, not when the trial ends and not with the month
/// (Peter, 2026-11-13: "pls fix this in a followup"). The server says so
/// (`lifts_with_payment` on the 402, `spend_cap_lifts_with_payment` on the user
/// and on a saved entry's answer); the banner and the toasts the app writes
/// itself then say the first payment, and still never where or how to pay.
final class SpendCapUntilPaymentTests: XCTestCase {
    private let trial402 = #"{"error":"monthly_spend_limit_reached","message":"You've reached the usage limit of your trial. It lifts with your first payment.","lifts_on":null,"lifts_with_payment":true}"#
    private let monthly402 = #"{"error":"monthly_spend_limit_reached","message":"You've reached your monthly usage limit. It resets at the start of next month.","lifts_on":"2026-12-01"}"#

    private func map(_ body: String) -> APIError {
        APIError.from(status: 402, contentType: "application/json", data: Data(body.utf8))
    }

    override func tearDown() {
        SpendCap.liftsWithPayment = false
        APIError.paymentsEnabled = false
        super.tearDown()
    }

    func testTheTrials402SaysTheFirstPayment() {
        APIError.paymentsEnabled = true
        XCTAssertEqual(map(trial402),
                       .spendCap(message: "You've reached the usage limit of your trial. It lifts with your first payment."))
        XCTAssertTrue(SpendCap.liftsWithPayment)
        XCTAssertEqual(SpendCap.toastMessage(.record),
                       "You've reached your usage limit, so you can't start a new recording until your first payment.")
        XCTAssertEqual(SpendCap.toastMessage(.glean),
                       "You've reached your usage limit, so you can't glean until your first payment.")
        XCTAssertEqual(APIError.spendCapFallbackMessage,
                       "You've reached your usage limit. It lifts with your first payment.")
        for text in [SpendCap.toastMessage(.upload), APIError.spendCapFallbackMessage] {
            XCTAssertFalse(text.contains("monthly"), text)
            XCTAssertFalse(text.contains("resets"), text)
            for word in ["subscribe", "Settings", "card", "web", "$"] {
                XCTAssertFalse(text.contains(word), text)
            }
        }
    }

    /// Control: a later 402 for the month's cap (after the first payment) names
    /// its reset again.
    func testAMonthly402KeepsTheReset() {
        APIError.paymentsEnabled = true
        _ = map(trial402)
        _ = map(monthly402)
        XCTAssertFalse(SpendCap.liftsWithPayment)
        XCTAssertTrue(SpendCap.toastMessage(.record).hasPrefix("You've reached your monthly usage limit"))
        XCTAssertEqual(APIError.spendCapFallbackMessage, APIError.paidSpendCapMessage)
    }

    func testTheUserAndTheAnswersCarryIt() throws {
        let capped = try decode(CurrentUser.self, #"""
        {"id": 1, "spend_blocked": true, "spend_cap_lifts_on": null, "spend_cap_lifts_with_payment": true}
        """#)
        XCTAssertTrue(capped.spendCapLiftsWithPayment)
        let monthly = try decode(CurrentUser.self, #"{"id": 1, "spend_blocked": true, "spend_cap_lifts_on": "2026-12-01"}"#)
        XCTAssertFalse(monthly.spendCapLiftsWithPayment)
        let saved = try decode(SaveAsNodeResponse.self,
                               #"{"id": 3, "spend_capped": true, "spend_cap_lifts_with_payment": true}"#)
        XCTAssertTrue(saved.spendCapLiftsWithPayment)
        let started = try decode(TextmodeStartResponse.self,
                                 #"{"user_node_id": 7, "spend_capped": true, "spend_cap_lifts_with_payment": true}"#)
        XCTAssertTrue(started.spendCapLiftsWithPayment)
        let plain = try decode(TextmodeStartResponse.self, #"{"user_node_id": 7, "spend_capped": true}"#)
        XCTAssertFalse(plain.spendCapLiftsWithPayment)
    }

    /// A saved entry whose reply the cap skipped raises the banner with the
    /// answer's cap; a press refused in the app keeps what is known.
    @MainActor
    func testTheBannerOfASavedEntry() {
        let app = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(),
                           defaults: UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!)
        app.notifySpendBlocked(liftsWithPayment: true)
        XCTAssertTrue(app.spendCapped)
        XCTAssertEqual(app.spendCapBannerMessage, SpendCap.untilPaymentMessage)
        app.notifySpendBlocked()
        XCTAssertTrue(SpendCap.liftsWithPayment)
    }
}
