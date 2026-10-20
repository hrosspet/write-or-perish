import XCTest
@testable import Loore

final class LooreDateTests: XCTestCase {
    private func utc(_ y: Int, _ mo: Int, _ d: Int, _ h: Int = 0, _ mi: Int = 0, _ s: Int = 0) -> Date {
        var c = DateComponents()
        (c.year, c.month, c.day, c.hour, c.minute, c.second) = (y, mo, d, h, mi, s)
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        return cal.date(from: c)!
    }

    func testMicrosecondsWithZ() throws {
        let date = try XCTUnwrap(LooreDate.parse("2026-09-30T12:34:56.123456Z"))
        XCTAssertEqual(date.timeIntervalSince1970, utc(2026, 9, 30, 12, 34, 56).timeIntervalSince1970 + 0.123456, accuracy: 1e-6)
    }

    func testNoFractionWithZ() {
        XCTAssertEqual(LooreDate.parse("2026-09-30T12:34:56Z"), utc(2026, 9, 30, 12, 34, 56))
    }

    func testOffsetForms() {
        let expected = utc(2026, 9, 30, 12, 34, 56)
        XCTAssertEqual(LooreDate.parse("2026-09-30T12:34:56+00:00"), expected)
        XCTAssertEqual(LooreDate.parse("2026-09-30T14:34:56+02:00"), expected)
        XCTAssertEqual(LooreDate.parse("2026-09-30T07:34:56-05:00"), expected)
        XCTAssertEqual(LooreDate.parse("2026-09-30T18:04:56+0530"), expected)
        let withFraction = LooreDate.parse("2026-09-30T12:34:56.5+00:00")!
        XCTAssertEqual(withFraction.timeIntervalSince(expected), 0.5, accuracy: 1e-9)
    }

    func testNoZoneIsUTC() {
        // `batch_submitted_at` and the log cursor carry no zone marker; utils/date.js assumes UTC.
        XCTAssertEqual(LooreDate.parse("2026-09-30T12:34:56"), utc(2026, 9, 30, 12, 34, 56))
        XCTAssertEqual(LooreDate.parse("2026-09-30T12:34:56.000001")!.timeIntervalSince(utc(2026, 9, 30, 12, 34, 56)),
                       0.000001, accuracy: 1e-7)
        XCTAssertEqual(LooreDate.parse("2026-09-30 12:34:56"), utc(2026, 9, 30, 12, 34, 56))
        XCTAssertEqual(LooreDate.parse("2026-09-30T12:34"), utc(2026, 9, 30, 12, 34))
    }

    func testDateOnly() {
        XCTAssertEqual(LooreDate.parse("2026-09-28"), utc(2026, 9, 28))
    }

    func testRejectsGarbage() {
        for bad in ["", "yesterday", "2026-13-01", "2026-09-30T25:00:00Z", "2026-09-30T12:34:56Q", "2026/09/30", "2026-09-30T12:34:56."] {
            XCTAssertNil(LooreDate.parse(bad), bad)
        }
    }

    func testDecoderStrategyInModels() throws {
        struct Box: Decodable { var a: Date; var b: Date?; var c: Date? }
        let box = try decode(Box.self, #"{"a": "2026-09-30T12:34:56.123456Z", "b": null, "c": "2026-09-28"}"#)
        XCTAssertEqual(box.a.timeIntervalSince(utc(2026, 9, 30, 12, 34, 56)), 0.123456, accuracy: 1e-6)
        XCTAssertNil(box.b)
        XCTAssertEqual(box.c, utc(2026, 9, 28))
    }

    func testDecoderRejectsBadTimestampWithClearError() {
        struct Box: Decodable { var a: Date }
        XCTAssertThrowsError(try decode(Box.self, #"{"a": "soon"}"#))
    }

    func testIsoStringRoundTrip() {
        let date = utc(2026, 1, 2, 3, 4, 5)
        XCTAssertEqual(LooreDate.parse(LooreDate.isoString(date)), date)
    }
}

/// Port of `frontend/src/utils/date.test.js` plus the Home greeting.
final class DateFormattingTests: XCTestCase {
    private let prague = TimeZone(identifier: "Europe/Prague")!

    private func local(_ y: Int, _ mo: Int, _ d: Int, _ h: Int = 12, _ mi: Int = 0, _ s: Int = 0, tz: TimeZone) -> Date {
        var c = DateComponents()
        (c.year, c.month, c.day, c.hour, c.minute, c.second) = (y, mo, d, h, mi, s)
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = tz
        return cal.date(from: c)!
    }

    func testYmdZeroPads() {
        XCTAssertEqual(LooreDateFormat.ymd(local(2026, 1, 5, tz: prague), timeZone: prague), "2026/01/05")
    }

    func testDateMonDYYYYForNonRecent() {
        let date = LooreDate.parse("2024-03-07T12:00:00Z")
        XCTAssertEqual(LooreDateFormat.date(date, relative: false, timeZone: prague), "Mar 7, 2024")
    }

    func testRelativeTodayAndYesterday() {
        let now = local(2026, 9, 30, 15, tz: prague)
        XCTAssertEqual(LooreDateFormat.date(local(2026, 9, 30, 1, tz: prague), now: now, timeZone: prague), "today")
        XCTAssertEqual(LooreDateFormat.date(local(2026, 9, 29, 23, tz: prague), now: now, timeZone: prague), "yesterday")
        XCTAssertEqual(LooreDateFormat.date(local(2026, 9, 28, 23, tz: prague), now: now, timeZone: prague), "Sep 28, 2026")
    }

    func testDateTimeWithoutSeconds() {
        XCTAssertEqual(LooreDateFormat.dateTime(local(2026, 9, 12, 9, 5, 30, tz: prague), timeZone: prague), "2026/09/12 09:05")
    }

    func testDateTimeRendersInLocalZone() {
        // A UTC timestamp shows in the viewer's zone (the web's reason for appending "Z").
        let date = LooreDate.parse("2026-09-12T22:30:00Z")
        XCTAssertEqual(LooreDateFormat.dateTime(date, timeZone: prague), "2026/09/13 00:30")
    }

    func testEmptyInputReturnsFallback() {
        XCTAssertEqual(LooreDateFormat.date(nil, fallback: "default"), "default")
        XCTAssertEqual(LooreDateFormat.dateTime(nil), "")
    }

    func testGreetingByHour() {
        XCTAssertEqual(LooreDateFormat.greeting(at: local(2026, 9, 30, 7, tz: prague), timeZone: prague), "Good morning")
        XCTAssertEqual(LooreDateFormat.greeting(at: local(2026, 9, 30, 12, tz: prague), timeZone: prague), "Good afternoon")
        XCTAssertEqual(LooreDateFormat.greeting(at: local(2026, 9, 30, 18, tz: prague), timeZone: prague), "Good evening")
    }
}

/// Port of `frontend/src/utils/spendCap.test.js`.
final class SpendCapTests: XCTestCase {
    private func utc(_ y: Int, _ mo: Int, _ d: Int, _ h: Int = 0, _ mi: Int = 0) -> Date {
        var c = DateComponents()
        (c.year, c.month, c.day, c.hour, c.minute) = (y, mo, d, h, mi)
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        return cal.date(from: c)!
    }

    func testResetDateIsFirstOfNextUTCMonth() {
        XCTAssertEqual(SpendCap.resetDateText(now: utc(2026, 9, 30, 23, 30)), "October 1")
        XCTAssertEqual(SpendCap.resetDateText(now: utc(2026, 12, 15)), "January 1")
        XCTAssertEqual(SpendCap.resetDate(now: utc(2026, 12, 15)), utc(2027, 1, 1))
    }

    func testToastNamesTheRefusedAction() {
        let now = utc(2026, 9, 23)
        XCTAssertTrue(SpendCap.toastMessage(.record, now: now).contains("start a new recording"))
        XCTAssertTrue(SpendCap.toastMessage(.upload, now: now).contains("upload audio"))
        XCTAssertEqual(SpendCap.toastMessage(.record, now: now),
                       "You've reached your monthly usage limit, so you can't start a new recording until it resets on October 1.")
    }

    func testRecognisesOnlyTheSpendCap402() {
        let capped = APIError.from(status: 402, contentType: "application/json",
                                   data: Data(#"{"error":"monthly_spend_limit_reached","message":"m"}"#.utf8))
        XCTAssertTrue(SpendCap.isSpendCapError(capped))
        let other402 = APIError.from(status: 402, contentType: "application/json", data: Data(#"{"error":"something_else"}"#.utf8))
        XCTAssertFalse(SpendCap.isSpendCapError(other402))
        let bad400 = APIError.from(status: 400, contentType: "application/json",
                                   data: Data(#"{"error":"monthly_spend_limit_reached"}"#.utf8))
        XCTAssertFalse(SpendCap.isSpendCapError(bad400))
        XCTAssertFalse(SpendCap.isSpendCapError(URLError(.notConnectedToInternet)))
    }
}
