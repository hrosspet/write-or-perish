import Foundation

/// Ports of `frontend/src/utils/date.js`: the web's two date styles.
/// - Date-only surfaces: "today" / "yesterday" / "Sep 12, 2026".
/// - Date+time surfaces (node footers, Log cards): "yyyy/mm/dd HH:MM", local, 24-hour.
enum LooreDateFormat {
    /// "yyyy/mm/dd" in `timeZone` (local by default).
    static func ymd(_ date: Date, timeZone: TimeZone = .current) -> String {
        let c = calendar(timeZone).dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d/%02d/%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    /// `formatDate(iso, {relative})`: "today", "yesterday", else "Mon D, YYYY" (en-US).
    static func date(_ date: Date?, relative: Bool = true, fallback: String = "",
                     now: Date = Date(), timeZone: TimeZone = .current) -> String {
        guard let date else { return fallback }
        if relative {
            let cal = calendar(timeZone)
            if cal.isDate(date, inSameDayAs: now) { return "today" }
            if let yesterday = cal.date(byAdding: .day, value: -1, to: now), cal.isDate(date, inSameDayAs: yesterday) {
                return "yesterday"
            }
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US")
        formatter.timeZone = timeZone
        formatter.dateFormat = "MMM d, yyyy"
        return formatter.string(from: date)
    }

    /// `formatDateTime(iso)`: "yyyy/mm/dd HH:MM" local time, no seconds.
    static func dateTime(_ date: Date?, fallback: String = "", timeZone: TimeZone = .current) -> String {
        guard let date else { return fallback }
        let c = calendar(timeZone).dateComponents([.hour, .minute], from: date)
        return "\(ymd(date, timeZone: timeZone)) " + String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }

    /// Home greeting by local hour (`HomePage.js`): <12 morning, <18 afternoon, else evening.
    static func greeting(at date: Date = Date(), timeZone: TimeZone = .current) -> String {
        let hour = calendar(timeZone).component(.hour, from: date)
        if hour < 12 { return "Good morning" }
        if hour < 18 { return "Good afternoon" }
        return "Good evening"
    }

    private static func calendar(_ timeZone: TimeZone) -> Calendar {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        return cal
    }
}

/// Ports of `frontend/src/utils/spendCap.js` (per-user monthly spend cap, #85).
enum SpendCap {
    /// The cap resets on the first day of the next month in UTC (the server's month).
    static func resetDate(now: Date = Date()) -> Date {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        let c = cal.dateComponents([.year, .month], from: now)
        var next = DateComponents()
        next.year = c.year
        next.month = (c.month ?? 1) + 1
        next.day = 1
        return cal.date(from: next) ?? now
    }

    /// "October 1" (month long, day numeric, UTC), like `toLocaleDateString(undefined, {month:'long', day:'numeric', timeZone:'UTC'})`.
    static func resetDateText(now: Date = Date(), locale: Locale = Locale(identifier: "en_US")) -> String {
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.setLocalizedDateFormatFromTemplate("MMMMd")
        return formatter.string(from: resetDate(now: now))
    }

    enum Action { case record, upload }

    /// The toast that answers a refused record or upload press (#341).
    static func toastMessage(_ action: Action, now: Date = Date()) -> String {
        let what = action == .upload ? "upload audio" : "start a new recording"
        return "You've reached your monthly usage limit, so you can't \(what) until it resets on \(resetDateText(now: now))."
    }

    /// True only for the 402 every capped endpoint returns.
    static func isSpendCapError(_ error: Error) -> Bool {
        if case .spendCap = error as? APIError { return true }
        return false
    }
}
