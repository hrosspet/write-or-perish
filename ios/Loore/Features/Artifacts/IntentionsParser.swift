import Foundation

/// Parser for the intentions artifact: a port of `utils/intentions.js`
/// (`parseIntentions`, `statusState`).
enum IntentionsParser {
    struct Entry: Equatable, Sendable {
        var name: String
        var status: String
        var body: [String]
        var notes: [String]
    }

    struct Section: Equatable, Sendable {
        var title: String
        var entries: [Entry]
    }

    /// Coarse state of an entry's status line, for the status dot.
    enum Status: String, Equatable, Sendable {
        case fulfilled, released, inferred, active
    }

    /// `parseIntentions(md)`: "# " lines open sections, "## " lines open
    /// entries; an entry's first italic line (before any body) is its status,
    /// "- "/"* " lines are dated notes, other non-blank lines are body.
    /// Entries before any "# " heading go into a section titled "".
    static func parse(_ md: String?) -> [Section] {
        var sections: [Section] = []
        var entry: Entry?

        func flushEntry() {
            if let e = entry, !sections.isEmpty { sections[sections.count - 1].entries.append(e) }
            entry = nil
        }

        for raw in (md ?? "").jsLines {
            let line = raw.jsTrimmed
            if let h2 = JSRegex.firstMatch(raw, Pattern.h2), let name = h2[1] {
                flushEntry()
                if sections.isEmpty { sections.append(Section(title: "", entries: [])) }
                entry = Entry(name: name.jsTrimmed, status: "", body: [], notes: [])
                continue
            }
            if let h1 = JSRegex.firstMatch(raw, Pattern.h1), let title = h1[1] {
                flushEntry()
                sections.append(Section(title: title.jsTrimmed, entries: []))
                continue
            }
            guard var current = entry else { continue }
            if let note = JSRegex.firstMatch(raw, Pattern.note), let text = note[1] {
                current.notes.append(text.jsTrimmed)
                entry = current
                continue
            }
            if line.isEmpty { continue }
            let italic = JSRegex.firstMatch(line, Pattern.starItalic) ?? JSRegex.firstMatch(line, Pattern.underscoreItalic)
            if let italic, let status = italic[1], current.status.isEmpty, current.body.isEmpty {
                current.status = status.jsTrimmed
            } else {
                current.body.append(line)
            }
            entry = current
        }
        flushEntry()
        return sections
    }

    /// The web's regexes with JS `\s`, `.` and `$` semantics.
    private enum Pattern {
        static let (s, dot) = (JSRegex.space, JSRegex.dot)
        static let h1 = "^#\(s)+(\(dot)+)"               // /^#\s+(.+)/
        static let h2 = "^##\(s)+(\(dot)+)"              // /^##\s+(.+)/
        static let note = "^\(s)*[-*]\(s)+(\(dot)+)"     // /^\s*[-*]\s+(.+)/
        static let starItalic = "^\\*(\(dot)+)\\*\\z"      // /^\*(.+)\*$/
        static let underscoreItalic = "^_(\(dot)+)_\\z"    // /^_(.+)_$/
    }

    /// `statusState(status)`: "fulfilled" / "released" / "inferred" or
    /// "unconfirmed" anywhere in the lowercased status, else active.
    static func statusState(_ status: String?) -> Status {
        let s = (status ?? "").lowercased(with: nil)
        func includes(_ word: String) -> Bool { s.range(of: word, options: .literal) != nil }
        if includes("fulfilled") { return .fulfilled }
        if includes("released") { return .released }
        if includes("inferred") || includes("unconfirmed") { return .inferred }
        return .active
    }
}
