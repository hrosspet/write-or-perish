import Foundation

/// Artifact kind ordering and naming: a port of `utils/artifactKinds.js`
/// plus `titleFromKind` and the create-form kind input of
/// `pages/ArtifactsPage.js`, and the backend's `_KIND_RE`.
enum ArtifactKinds {
    /// `BUILTIN_KIND_ORDER`: built-in kinds, listed first in this order.
    static let builtinKindOrder = ["intentions", "predictions", "memory", "scratchpad", "ai_preferences"]

    /// `isBuiltinKind(kind)`.
    static func isBuiltinKind(_ kind: String) -> Bool {
        builtinKindOrder.contains(kind)
    }

    /// `compareArtifacts(a, b)`: built-in kinds first in `builtinKindOrder`,
    /// then the rest by title (the kind when the title is nil or empty).
    ///
    /// The web's `localeCompare` uses the browser's default-locale ICU
    /// collation; `localizedCompare` uses the device locale's ICU collation.
    /// They agree except that the web never returns a tie for strings that
    /// differ only by compatibility forms (e.g. "ﬁ" vs "fi").
    static func compare(kind kindA: String, title titleA: String?,
                        kind kindB: String, title titleB: String?) -> ComparisonResult {
        let ra = builtinKindOrder.firstIndex(of: kindA) ?? Int.max
        let rb = builtinKindOrder.firstIndex(of: kindB) ?? Int.max
        if ra != rb { return ra < rb ? .orderedAscending : .orderedDescending }
        let a = (titleA?.isEmpty == false ? titleA! : kindA)
        let b = (titleB?.isEmpty == false ? titleB! : kindB)
        return a.localizedCompare(b)
    }

    /// The custom-kind part of `compare`: `a.localeCompare(b) < 0` on titles.
    static func titleSortsBefore(_ a: String, _ b: String) -> Bool {
        a.localizedCompare(b) == .orderedAscending
    }

    /// `compare` as a `sort(by:)` predicate over (kind, title) pairs.
    static func areInIncreasingOrder(_ a: (kind: String, title: String?), _ b: (kind: String, title: String?)) -> Bool {
        compare(kind: a.kind, title: a.title, kind: b.kind, title: b.title) == .orderedAscending
    }

    /// `titleFromKind(k)` in ArtifactsPage: "-" and "_" become spaces and the
    /// first ASCII word character after a non-word one is uppercased
    /// ("ai_preferences" → "Ai Preferences").
    static func titleFromKind(_ kind: String) -> String {
        var out = String.UnicodeScalarView()
        var previousIsWord = false
        for scalar in kind.unicodeScalars {
            let c: Unicode.Scalar = (scalar == "-" || scalar == "_") ? " " : scalar
            let isWord = isASCIIWordChar(c)
            if isWord && !previousIsWord, ("a"..."z").contains(c) {
                out.append(Unicode.Scalar(c.value - 0x20)!)
            } else {
                out.append(c)
            }
            previousIsWord = isWord
        }
        return String(out)
    }

    /// The backend's `_KIND_RE`: `^[a-z0-9][a-z0-9_-]{0,47}$` (a trailing
    /// newline, which Python's `$` would allow, is rejected).
    static func isValidKind(_ kind: String) -> Bool {
        let units = Array(kind.utf16)
        guard let first = units.first, units.count <= 48, isSlugAlnum(first) else { return false }
        return units.dropFirst().allSatisfy { isSlugAlnum($0) || $0 == 0x5F || $0 == 0x2D }
    }

    /// The create form's kind input filter: lowercase, then every UTF-16 unit
    /// outside `[a-z0-9_-]` becomes "-" (so an emoji becomes "--", as on the web).
    static func sanitizeKindInput(_ input: String) -> String {
        let mapped = input.lowercased(with: nil).utf16.map { u -> UInt16 in
            isSlugAlnum(u) || u == 0x5F || u == 0x2D ? u : 0x2D
        }
        return String(decoding: mapped, as: UTF16.self)
    }

    private static func isSlugAlnum(_ u: UInt16) -> Bool {
        (u >= 0x61 && u <= 0x7A) || (u >= 0x30 && u <= 0x39)
    }

    /// JS `\w` (ASCII only).
    private static func isASCIIWordChar(_ c: Unicode.Scalar) -> Bool {
        ("a"..."z").contains(c) || ("A"..."Z").contains(c) || ("0"..."9").contains(c) || c == "_"
    }
}
