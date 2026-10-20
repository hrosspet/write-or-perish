import Foundation

/// Small helpers that make `NSRegularExpression` behave like the web's
/// JavaScript regexes, so ported functions produce identical strings.
///
/// - Both work on UTF-16, so offsets and lengths agree with JS.
/// - JS `\w` is ASCII-only (`[A-Za-z0-9_]`); ICU's `\w` includes every Unicode
///   letter. Ported patterns spell out `[A-Za-z0-9_]` instead of `\w`.
/// - `$1` templates work the same way.
enum JSRegex {
    private static var cache: [String: NSRegularExpression] = [:]
    private static let lock = NSLock()

    static func make(_ pattern: String, _ options: NSRegularExpression.Options = []) -> NSRegularExpression {
        let key = "\(options.rawValue)|\(pattern)"
        lock.lock()
        defer { lock.unlock() }
        if let cached = cache[key] { return cached }
        // Patterns are compile-time constants; a failure is a programming error.
        let regex = try! NSRegularExpression(pattern: pattern, options: options)
        cache[key] = regex
        return regex
    }

    /// `s.replace(/pattern/g, template)`.
    static func replaceAll(_ s: String, _ pattern: String, _ template: String,
                           options: NSRegularExpression.Options = []) -> String {
        let regex = make(pattern, options)
        let range = NSRange(s.startIndex..., in: s)
        return regex.stringByReplacingMatches(in: s, options: [], range: range, withTemplate: template)
    }

    /// `s.replace(/pattern/, template)` (first match only).
    static func replaceFirst(_ s: String, _ pattern: String, _ template: String,
                             options: NSRegularExpression.Options = []) -> String {
        let regex = make(pattern, options)
        let ns = s as NSString
        guard let match = regex.firstMatch(in: s, options: [], range: NSRange(location: 0, length: ns.length)) else {
            return s
        }
        let replacement = regex.replacementString(for: match, in: s, offset: 0, template: template)
        return ns.replacingCharacters(in: match.range, with: replacement)
    }

    /// `s.match(/pattern/)`: the capture groups of the first match (index 0 =
    /// whole match); a group that did not participate is nil.
    static func firstMatch(_ s: String, _ pattern: String,
                           options: NSRegularExpression.Options = []) -> [String?]? {
        let regex = make(pattern, options)
        let ns = s as NSString
        guard let match = regex.firstMatch(in: s, options: [], range: NSRange(location: 0, length: ns.length)) else {
            return nil
        }
        return (0..<match.numberOfRanges).map { i in
            let r = match.range(at: i)
            return r.location == NSNotFound ? nil : ns.substring(with: r)
        }
    }

    /// `/pattern/.test(s)`.
    static func test(_ s: String, _ pattern: String, options: NSRegularExpression.Options = []) -> Bool {
        let regex = make(pattern, options)
        return regex.firstMatch(in: s, options: [], range: NSRange(location: 0, length: (s as NSString).length)) != nil
    }

    /// All whole-match strings (`s.match(/pattern/g)`), in order.
    static func allMatches(_ s: String, _ pattern: String, options: NSRegularExpression.Options = []) -> [String] {
        let regex = make(pattern, options)
        let ns = s as NSString
        return regex.matches(in: s, options: [], range: NSRange(location: 0, length: ns.length))
            .map { ns.substring(with: $0.range) }
    }

    /// `s.split(/pattern/)` for a pattern WITH one capture group: like JS, the
    /// captured separators are kept in the output between the pieces.
    static func splitKeepingCaptures(_ s: String, _ pattern: String,
                                     options: NSRegularExpression.Options = []) -> [String] {
        let regex = make(pattern, options)
        let ns = s as NSString
        var parts: [String] = []
        var cursor = 0
        for match in regex.matches(in: s, options: [], range: NSRange(location: 0, length: ns.length)) {
            parts.append(ns.substring(with: NSRange(location: cursor, length: match.range.location - cursor)))
            for group in 1..<match.numberOfRanges {
                let r = match.range(at: group)
                parts.append(r.location == NSNotFound ? "" : ns.substring(with: r))
            }
            cursor = match.range.location + match.range.length
        }
        parts.append(ns.substring(from: cursor))
        return parts
    }

    /// `s.split(/pattern/)` for a pattern without capture groups.
    static func split(_ s: String, _ pattern: String, options: NSRegularExpression.Options = []) -> [String] {
        let regex = make(pattern, options)
        let ns = s as NSString
        var parts: [String] = []
        var cursor = 0
        for match in regex.matches(in: s, options: [], range: NSRange(location: 0, length: ns.length)) {
            parts.append(ns.substring(with: NSRange(location: cursor, length: match.range.location - cursor)))
            cursor = match.range.location + match.range.length
        }
        parts.append(ns.substring(from: cursor))
        return parts
    }
}

extension String {
    /// JS `String.prototype.trim()`.
    var jsTrimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }

    /// JS `str.length` (UTF-16 code units). Use it wherever the web compares lengths.
    var jsLength: Int { utf16.count }

    /// JS `str.split('\n')` (keeps empty pieces, and a `\r` before `\n`).
    var jsLines: [String] { components(separatedBy: "\n") }

    /// JS `str.substring(0, n)` without leaving half a surrogate pair.
    func jsPrefix(_ n: Int) -> String {
        guard jsLength > n else { return self }
        var index = utf16.index(utf16.startIndex, offsetBy: n)
        while index > utf16.startIndex, String.Index(index, within: self) == nil {
            index = utf16.index(before: index)
        }
        return String(self[..<(String.Index(index, within: self) ?? endIndex)])
    }

    /// JS `str.indexOf(sub)` in UTF-16 units (-1 when absent).
    func jsIndexOf(_ sub: String) -> Int {
        let r = (self as NSString).range(of: sub)
        return r.location == NSNotFound ? -1 : r.location
    }

    /// JS `str.substring(from, to)` in UTF-16 units.
    func jsSubstring(_ from: Int, _ to: Int? = nil) -> String {
        let ns = self as NSString
        let start = max(0, min(from, ns.length))
        let end = max(start, min(to ?? ns.length, ns.length))
        return ns.substring(with: NSRange(location: start, length: end - start))
    }
}

/// "100,000" (JS `toLocaleString()` in en-US).
func jsLocaleNumber(_ n: Int) -> String {
    let formatter = NumberFormatter()
    formatter.locale = Locale(identifier: "en_US")
    formatter.numberStyle = .decimal
    return formatter.string(from: NSNumber(value: n)) ?? String(n)
}
