import Foundation

/// The profile's imported-source share, e.g. " (96% public tweets)": a port
/// of `formatSourceMix` and `ORIGIN_LABELS` in `pages/ProfilePage.js`.
enum SourceMix {
    /// `ORIGIN_LABELS`; other origins print as-is.
    static let originLabels: [String: String] = [
        "twitter": "public tweets",
        "chatgpt": "ChatGPT imports",
        "claude": "Claude imports",
        "markdown": "markdown imports",
    ]

    /// `formatSourceMix(stats)` over (origin, tokens) pairs in the order the
    /// web iterates them. Shares are of all tokens (Loore included), rounded
    /// like JS `Math.round`; "loore" and 0% origins are left out; highest
    /// share first, ties keep input order. "" when nothing remains.
    static func format(_ stats: [(String, Int)]) -> String {
        let total = stats.reduce(0) { $0 + $1.1 }
        if total == 0 { return "" }
        let parts = stats
            .filter { $0.0 != "loore" }
            .map { (origin: $0.0, pct: jsRound(Double($0.1) / Double(total) * 100)) }
            .filter { $0.pct > 0 }
            .enumerated()
            .sorted { $0.element.pct != $1.element.pct ? $0.element.pct > $1.element.pct : $0.offset < $1.offset }
            .map { "\($0.element.pct)% \(originLabels[$0.element.origin] ?? $0.element.origin)" }
        return parts.isEmpty ? "" : " (\(parts.joined(separator: ", ")))"
    }

    /// `format` for a decoded `source_origin_stats` object. Flask's `jsonify`
    /// sorts keys, so the web iterates origins in code-point order; this does
    /// the same.
    static func format(_ stats: [String: Int]?) -> String {
        guard let stats else { return "" }
        let ordered = stats.sorted { $0.key.unicodeScalars.lexicographicallyPrecedes($1.key.unicodeScalars) { $0.value < $1.value } }
        return format(ordered.map { ($0.key, $0.value) })
    }

    /// JS `Math.round`: nearest integer, halves toward +∞.
    static func jsRound(_ x: Double) -> Int {
        let floor = x.rounded(.down)
        return Int(x - floor >= 0.5 ? floor + 1 : floor)
    }
}
