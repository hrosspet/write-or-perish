import Foundation

/// Tolerant parser for the timestamp strings the backend sends (map B §0).
///
/// Accepted forms:
/// - `2026-09-30T12:34:56.123456Z` (Python `isoformat()` + "Z", microseconds when non-zero)
/// - `2026-09-30T12:34:56Z` (no fraction when the microseconds are zero)
/// - `2026-09-30T12:34:56+00:00` / `-05:00` / `+0530` (timezone-aware columns)
/// - `2026-09-30T12:34:56` (no zone marker: UTC, as `utils/date.js` assumes)
/// - `2026-09-30 12:34:56` (space separator)
/// - `2026-09-30` (date only, e.g. changelog dates: midnight UTC)
///
/// Hand-written instead of `ISO8601DateFormatter` because that formatter does not
/// reliably accept six fractional digits or a missing zone on every iOS version.
enum LooreDate {
    private static let utcCalendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        return calendar
    }()

    static func parse(_ raw: String) -> Date? {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        let chars = Array(s.utf8)
        var i = 0

        func digits(_ count: Int) -> Int? {
            guard i + count <= chars.count else { return nil }
            var value = 0
            for k in 0..<count {
                let c = chars[i + k]
                guard c >= 48 && c <= 57 else { return nil }
                value = value * 10 + Int(c - 48)
            }
            i += count
            return value
        }
        func expect(_ c: UInt8) -> Bool {
            guard i < chars.count, chars[i] == c else { return false }
            i += 1
            return true
        }

        guard let year = digits(4), expect(UInt8(ascii: "-")),
              let month = digits(2), expect(UInt8(ascii: "-")),
              let day = digits(2) else { return nil }
        guard (1...12).contains(month), (1...31).contains(day) else { return nil }

        var hour = 0, minute = 0, second = 0
        var fraction: Double = 0
        var offsetSeconds = 0

        if i < chars.count {
            guard chars[i] == UInt8(ascii: "T") || chars[i] == UInt8(ascii: "t") || chars[i] == UInt8(ascii: " ") else {
                return nil
            }
            i += 1
            guard let h = digits(2), expect(UInt8(ascii: ":")), let m = digits(2) else { return nil }
            hour = h
            minute = m
            if expect(UInt8(ascii: ":")) {
                guard let sec = digits(2) else { return nil }
                second = sec
                if i < chars.count, chars[i] == UInt8(ascii: ".") || chars[i] == UInt8(ascii: ",") {
                    i += 1
                    var scale = 0.1
                    var sawDigit = false
                    while i < chars.count, chars[i] >= 48, chars[i] <= 57 {
                        fraction += Double(chars[i] - 48) * scale
                        scale /= 10
                        sawDigit = true
                        i += 1
                    }
                    guard sawDigit else { return nil }
                }
            }
            guard hour <= 24, minute <= 59, second <= 60 else { return nil }
            if i < chars.count {
                let c = chars[i]
                if c == UInt8(ascii: "Z") || c == UInt8(ascii: "z") {
                    i += 1
                } else if c == UInt8(ascii: "+") || c == UInt8(ascii: "-") {
                    let sign = c == UInt8(ascii: "-") ? -1 : 1
                    i += 1
                    guard let oh = digits(2) else { return nil }
                    _ = expect(UInt8(ascii: ":"))
                    let om = digits(2) ?? 0
                    offsetSeconds = sign * (oh * 3600 + om * 60)
                } else {
                    return nil
                }
            }
        }
        guard i == chars.count else { return nil }

        var components = DateComponents()
        components.year = year
        components.month = month
        components.day = day
        components.hour = hour
        components.minute = minute
        components.second = second
        guard let base = utcCalendar.date(from: components) else { return nil }
        return base.addingTimeInterval(fraction - Double(offsetSeconds))
    }

    /// The backend's own format (`iso_utc`): UTC with a "Z" and microseconds.
    /// Used when the app has to send a timestamp back (rare) and in tests.
    static func isoString(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.calendar = utcCalendar
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS'Z'"
        return formatter.string(from: date)
    }
}

extension JSONDecoder.DateDecodingStrategy {
    /// Decodes every backend timestamp shape (see `LooreDate`).
    static var looreTolerant: JSONDecoder.DateDecodingStrategy {
        .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            guard let date = LooreDate.parse(raw) else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Unrecognised timestamp format: \(raw.prefix(40))"
                )
            }
            return date
        }
    }
}
