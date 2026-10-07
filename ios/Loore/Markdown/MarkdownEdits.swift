import Foundation

/// Ports of `frontend/src/utils/markdown.js`: the plain-text line matching
/// behind interactive checklists, the per-row "+" and section quick-adds.
///
/// Matching is by the item's plain text, so the renderer's text extraction
/// (`MarkdownListItem.plainText`) must equal `stripInlineMarkdown(label).trim()`
/// for the same source line, or a toggle silently does nothing (map D §3.5).
enum MarkdownEdits {
    /// Strips common inline markdown so a raw source line can be compared with
    /// the rendered plain text of a list item.
    static func stripInlineMarkdown(_ text: String) -> String {
        var s = text
        s = JSRegex.replaceAll(s, #"!\[([^\]]*)\]\([^)]*\)"#, "$1")     // ![alt](url) → alt
        s = JSRegex.replaceAll(s, #"\[([^\]]+)\]\([^)]*\)"#, "$1")      // [text](url) → text
        s = JSRegex.replaceAll(s, #"\*\*([^*]+?)\*\*"#, "$1")           // **bold**
        s = JSRegex.replaceAll(s, #"__([^_]+?)__"#, "$1")               // __bold__
        s = JSRegex.replaceAll(s, #"~~([^~]+?)~~"#, "$1")               // ~~strike~~
        s = JSRegex.replaceAll(s, #"`([^`]+?)`"#, "$1")                 // `code`
        s = JSRegex.replaceAll(s, #"(?<![*A-Za-z0-9_])\*([^*\n]+?)\*(?![A-Za-z0-9_])"#, "$1") // *italic*
        s = JSRegex.replaceAll(s, #"(?<![_A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])"#, "$1")   // _italic_
        return s
    }

    /// Flips `- [ ]` ↔ `- [x]` on every line whose stripped label equals
    /// `itemText` (only `-` bullets; the web's `toggleCheckbox`).
    static func toggleCheckbox(_ content: String, itemText: String, currentChecked: Bool) -> String {
        let lines = content.jsLines.map { line -> String in
            guard let m = JSRegex.firstMatch(line, #"^(\s*)- \[([ xX])\]\s+(.+)"#),
                  let label = m[3], stripInlineMarkdown(label).jsTrimmed == itemText else {
                return line
            }
            let indent = m[1] ?? ""
            return currentChecked ? "\(indent)- [ ] \(label)" : "\(indent)- [x] \(label)"
        }
        return lines.joined(separator: "\n")
    }

    /// Inserts a sibling item right after the item whose stripped label equals
    /// `afterItemText` (and after its deeper-indented subtree), copying its
    /// indent, bullet and checkbox style. Unchanged when nothing matches.
    static func insertItemAfter(_ content: String, afterItemText: String, newText: String) -> String {
        let text = newText.jsTrimmed
        guard !text.isEmpty else { return content }
        var lines = content.jsLines
        let lineRe = #"^(\s*)([-*])\s+(\[[ xX]\]\s+)?(.*)$"#

        var idx = -1
        var indent = ""
        var bullet = "-"
        var checkbox = ""
        for (i, line) in lines.enumerated() {
            if let m = JSRegex.firstMatch(line, lineRe),
               stripInlineMarkdown(m[4] ?? "").jsTrimmed == afterItemText {
                idx = i
                indent = m[1] ?? ""
                bullet = m[2] ?? "-"
                checkbox = m[3] != nil ? "[ ] " : ""
                break
            }
        }
        guard idx >= 0 else { return content }

        var insertAt = idx + 1
        var i = idx + 1
        while i < lines.count {
            let lead = JSRegex.firstMatch(lines[i], #"^(\s*)"#)?[1] ?? ""
            if !lines[i].jsTrimmed.isEmpty && lead.jsLength > indent.jsLength {
                insertAt = i + 1
                i += 1
            } else {
                break
            }
        }
        lines.insert("\(indent)\(bullet) \(checkbox)\(text)", at: insertAt)
        return lines.joined(separator: "\n")
    }

    enum SectionMatch { case exact, includes }

    /// Appends an item to the end of a named section, creating the section when
    /// it is missing (web `appendItemToSection`; the Todo quick-add, M4).
    static func appendItemToSection(_ content: String?, sectionTitle: String, task: String,
                                    headingLevel: Int = 2, match: SectionMatch = .exact,
                                    itemPrefix: String = "- [ ] ", createTitle: String? = nil,
                                    createAtStart: Bool = false) -> String {
        let cleanTask = task.jsTrimmed
        let base = content ?? ""
        guard !cleanTask.isEmpty else { return base }
        let newItem = "\(itemPrefix)\(cleanTask)"
        var lines = base.jsLines
        let hashes = String(repeating: "#", count: headingLevel)
        let headingRe = "^#{\(headingLevel)}\\s+(.+)"
        let boundaryRe = "^#{1,\(headingLevel)}\\s+"
        let wanted = sectionTitle.jsTrimmed.lowercased()
        func headingMatches(_ h: String) -> Bool { match == .includes ? h.contains(wanted) : h == wanted }

        var headingIdx = -1
        for (i, line) in lines.enumerated() {
            if let m = JSRegex.firstMatch(line, headingRe), let h = m[1], headingMatches(h.jsTrimmed.lowercased()) {
                headingIdx = i
                break
            }
        }
        if headingIdx == -1 {
            let newSection = "\(hashes) \(createTitle ?? sectionTitle)\n\n\(newItem)\n"
            if createAtStart {
                let body = base.jsTrimmed
                return body.isEmpty ? newSection : "\(newSection)\n\(body)\n"
            }
            let sep = (!base.isEmpty && !base.hasSuffix("\n")) ? "\n\n" : (base.hasSuffix("\n\n") ? "" : "\n")
            return "\(base)\(sep)\(newSection)"
        }
        var sectionEnd = lines.count
        for i in (headingIdx + 1)..<lines.count where JSRegex.test(lines[i], boundaryRe) {
            sectionEnd = i
            break
        }
        var insertAt = headingIdx + 1
        for i in (headingIdx + 1)..<max(headingIdx + 1, sectionEnd) where !lines[i].jsTrimmed.isEmpty {
            insertAt = i + 1
        }
        lines.insert(newItem, at: insertAt)
        return lines.joined(separator: "\n")
    }
}
