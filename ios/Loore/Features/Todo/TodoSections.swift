import Foundation

/// Todo checklist parsing: a port of `parseTodoSections`,
/// `findParentAtDepth`, `countAllItems` and `generatedByLabel` in
/// `pages/TodoPage.js`.
enum TodoSections {
    struct Item: Equatable, Sendable {
        /// nil for a plain "- text" line (a category without a checkbox).
        var checked: Bool?
        var text: String
        /// The source line, unmodified.
        var raw: String
        /// floor(indent / 2), indent counted in whitespace characters.
        var depth: Int
        var children: [Item]
    }

    struct Section: Equatable, Sendable {
        /// "" for items that appear before any "## " heading.
        var title: String
        var items: [Item]
    }

    /// `parseTodoSections(content)`: "## " headings open sections; "- [ ]" /
    /// "- [x]" lines are checkbox items, other "- " lines plain items. An
    /// item at depth d > 0 becomes a child of the last item at depth d − 1 on
    /// the last-child chain, else a top-level item.
    static func parse(_ content: String?) -> [Section] {
        guard let content, !content.isEmpty else { return [] }
        var sections: [Section] = []

        for line in content.jsLines {
            if let heading = JSRegex.firstMatch(line, Pattern.heading), let title = heading[1] {
                sections.append(Section(title: title.jsTrimmed, items: []))
                continue
            }
            let checkbox = JSRegex.firstMatch(line, Pattern.checkbox)
            let plain = checkbox == nil ? JSRegex.firstMatch(line, Pattern.plain) : nil
            guard let match = checkbox ?? plain else { continue }
            if sections.isEmpty { sections.append(Section(title: "", items: [])) }

            let depth = (match[1] ?? "").jsLength / 2
            let item: Item
            if let checkbox {
                item = Item(checked: checkbox[2] != " ", text: (checkbox[3] ?? "").jsTrimmed, raw: line,
                            depth: depth, children: [])
            } else {
                item = Item(checked: nil, text: (match[2] ?? "").jsTrimmed, raw: line, depth: depth, children: [])
            }

            let last = sections.count - 1
            if depth == 0 || !attach(item, toLastAt: depth - 1, in: &sections[last].items) {
                sections[last].items.append(item)
            }
        }
        return sections
    }

    /// The web's regexes with JS `\s` and `.` semantics.
    private enum Pattern {
        static let (s, dot) = (JSRegex.space, JSRegex.dot)
        static let heading = "^##\(s)+(\(dot)+)"                       // /^##\s+(.+)/
        static let checkbox = "^(\(s)*)- \\[([ xX])\\]\(s)+(\(dot)+)"  // /^(\s*)- \[([ xX])\]\s+(.+)/
        static let plain = "^(\(s)*)- (\(dot)+)"                       // /^(\s*)- (.+)/
    }

    /// `findParentAtDepth` + push: follow the last-item chain to the first
    /// item at `targetDepth` and append `item` to its children.
    private static func attach(_ item: Item, toLastAt targetDepth: Int, in items: inout [Item]) -> Bool {
        guard !items.isEmpty else { return false }
        let last = items.count - 1
        if items[last].depth == targetDepth {
            items[last].children.append(item)
            return true
        }
        if !items[last].children.isEmpty {
            return attach(item, toLastAt: targetDepth, in: &items[last].children)
        }
        return false
    }

    /// `countAllItems(items)`: items plus all nested children.
    static func countAll(_ items: [Item]) -> Int {
        items.reduce(0) { $0 + 1 + countAll($1.children) }
    }

    /// `generatedByLabel(g)` in TodoPage: who last updated the list.
    /// Unknown values are returned as-is; nil is "" (JSX renders null as nothing).
    static func updatedByLabel(_ generatedBy: String?) -> String {
        switch generatedBy {
        case "user", "manual": return "edited manually"
        case "orient_session": return "Orient session"
        case "voice_session": return "Voice"
        case "revert": return "reverted"
        case "import": return "imported"
        default: return generatedBy ?? ""
        }
    }
}
