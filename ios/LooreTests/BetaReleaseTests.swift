import XCTest
@testable import Loore

final class BetaReleaseTests: XCTestCase {
    func testWaitingTextSaysBetaAndNamesNoDate() {
        let text = BetaRelease.waitingText
        XCTAssertTrue(text.hasPrefix("Thank you for signing up for the Loore Beta."))
        XCTAssertTrue(text.hasSuffix("they deserve as they begin."))
        XCTAssertFalse(text.contains("Alpha"))
        XCTAssertFalse(text.contains("opens on"))
        XCTAssertFalse(text.contains("December"))
    }
}
