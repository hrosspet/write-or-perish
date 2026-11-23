import XCTest
@testable import Loore

final class BetaReleaseTests: XCTestCase {
    private var calendar: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC")!
        return c
    }

    private func day(_ y: Int, _ m: Int, _ d: Int, _ h: Int = 0, _ mi: Int = 0) -> Date {
        calendar.date(from: DateComponents(year: y, month: m, day: d, hour: h, minute: mi))!
    }

    func testSentenceShownBeforeTheDate() {
        XCTAssertEqual(BetaRelease.opensSentence(now: day(2026, 12, 20, 23, 59), calendar: calendar),
                       "The Beta opens on 21 December.")
        XCTAssertEqual(BetaRelease.opensSentence(now: day(2026, 11, 23), calendar: calendar),
                       "The Beta opens on 21 December.")
    }

    func testSentenceGoneFromTheDateOn() {
        XCTAssertNil(BetaRelease.opensSentence(now: day(2026, 12, 21), calendar: calendar))
        XCTAssertNil(BetaRelease.opensSentence(now: day(2027, 3, 1), calendar: calendar))
    }

    func testWaitingTextSaysBetaAndKeepsTheRest() {
        let before = BetaRelease.waitingText(now: day(2026, 11, 23), calendar: calendar)
        XCTAssertTrue(before.hasPrefix("Thank you for signing up for the Loore Beta."))
        XCTAssertTrue(before.hasSuffix("as they begin. The Beta opens on 21 December."))
        let after = BetaRelease.waitingText(now: day(2026, 12, 21), calendar: calendar)
        XCTAssertEqual(after, BetaRelease.waitingIntro)
        XCTAssertFalse(after.contains("Alpha"))
    }
}
