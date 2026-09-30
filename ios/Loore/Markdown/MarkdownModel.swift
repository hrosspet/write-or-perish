import Foundation
import Markdown

// A small render tree built from swift-markdown's AST (design doc §8), so the
// SwiftUI renderer and the unit tests work on plain values.
//
// What it reproduces from the web's react-markdown + remark-gfm pipeline
// (map D §3.2):
// - GFM tables, strikethrough and task lists (cmark-gfm extensions).
// - GFM autolink literals (bare `https://…`, `www.…`, emails). swift-markdown
//   does not attach cmark's autolink extension, so they are detected here.
// - Raw HTML is never rendered: block HTML becomes a code block, inline HTML
//   inline code, pure `<!-- comments -->` are dropped (`remarkHtmlAsCode`).
// - No smart punctuation (remark keeps straight quotes and `--`).
// - Tight vs loose lists (loose items render as spaced paragraphs; in tight
//   items a soft line break is a space, as the web's `white-space: normal` li).

struct MarkdownDocument: Equatable, Sendable {
    var blocks: [MDBlock]

    static let empty = MarkdownDocument(blocks: [])
}

struct MDBlock: Identifiable, Equatable, Sendable {
    /// A structural path ("0", "2.i1.0"): unchanged when only text or a
    /// checkbox changes, so SwiftUI updates rows in place (#321).
    let id: String
    var kind: Kind

    indirect enum Kind: Equatable, Sendable {
        case paragraph([MDInline])
        case heading(level: Int, [MDInline])
        case blockquote([MDBlock])
        case list(MDList)
        case code(language: String?, text: String)
        case rule
        case table(MDTable)
    }
}

struct MDList: Equatable, Sendable {
    var ordered: Bool
    var start: Int
    var tight: Bool
    /// GFM `contains-task-list`: any item has a checkbox (no bullets are drawn).
    var isTaskList: Bool
    var items: [MDListItem]
}

struct MDListItem: Identifiable, Equatable, Sendable {
    let id: String
    /// nil = not a task item; true = `[x]`.
    var checked: Bool?
    var blocks: [MDBlock]
    /// The item's own plain text (nested lists excluded), trimmed: what the
    /// web's `extractText(liChildren).trim()` yields and what checklist
    /// toggles match against.
    var plainText: String
}

struct MDTable: Equatable, Sendable {
    enum Alignment: Equatable, Sendable { case left, center, right }
    var alignments: [Alignment?]
    var head: [[MDInline]]
    var rows: [[[MDInline]]]
}

indirect enum MDInline: Equatable, Sendable {
    case text(String)
    case softBreak
    case lineBreak
    case code(String)
    case emphasis([MDInline])
    case strong([MDInline])
    case strikethrough([MDInline])
    case link(destination: String, children: [MDInline])
    case image(source: String, alt: String)

    /// Plain text as the web's `extractText` sees it: link text (not the
    /// title a node link displays), code text, "\n" for a soft break,
    /// nothing for a hard break (`<br>`) or an image (`<img>` has no children).
    var plainText: String {
        switch self {
        case .text(let s), .code(let s): return s
        case .softBreak: return "\n"
        case .lineBreak, .image: return ""
        case .emphasis(let c), .strong(let c), .strikethrough(let c), .link(_, let c):
            return c.map(\.plainText).joined()
        }
    }
}

extension Array where Element == MDInline {
    var plainText: String { map(\.plainText).joined() }

    var containsImage: Bool {
        contains { inline in
            switch inline {
            case .image: return true
            case .emphasis(let c), .strong(let c), .strikethrough(let c), .link(_, let c): return c.containsImage
            default: return false
            }
        }
    }
}

// MARK: - Parsing

enum MarkdownParser {
    private static let cache: NSCache<NSString, CacheBox> = {
        let cache = NSCache<NSString, CacheBox>()
        cache.countLimit = 256
        return cache
    }()

    private final class CacheBox {
        let document: MarkdownDocument
        init(_ document: MarkdownDocument) { self.document = document }
    }

    /// Parses (and caches) `text`.
    static func document(for text: String) -> MarkdownDocument {
        let key = text as NSString
        if let hit = cache.object(forKey: key) { return hit.document }
        let doc = parse(text)
        cache.setObject(CacheBox(doc), forKey: key)
        return doc
    }

    static func parse(_ text: String) -> MarkdownDocument {
        guard !text.isEmpty else { return .empty }
        let document = Document(parsing: text, options: [.disableSmartOpts])
        return MarkdownDocument(blocks: blocks(Array(document.children), path: ""))
    }

    // MARK: Blocks

    private static func blocks(_ children: [Markup], path: String) -> [MDBlock] {
        var out: [MDBlock] = []
        for child in children {
            let id = path.isEmpty ? "\(out.count)" : "\(path).\(out.count)"
            if let block = block(child, id: id) { out.append(block) }
        }
        return out
    }

    private static func block(_ markup: Markup, id: String) -> MDBlock? {
        switch markup {
        case let p as Paragraph:
            return MDBlock(id: id, kind: .paragraph(inlines(p.children)))
        case let h as Heading:
            return MDBlock(id: id, kind: .heading(level: h.level, inlines(h.children)))
        case let q as BlockQuote:
            return MDBlock(id: id, kind: .blockquote(blocks(Array(q.children), path: id)))
        case let list as UnorderedList:
            return MDBlock(id: id, kind: .list(makeList(list, ordered: false, start: 1, id: id)))
        case let list as OrderedList:
            return MDBlock(id: id, kind: .list(makeList(list, ordered: true, start: Int(list.startIndex), id: id)))
        case let code as CodeBlock:
            return MDBlock(id: id, kind: .code(language: code.language, text: dropFinalNewline(code.code)))
        case let html as HTMLBlock:
            let raw = dropFinalNewline(html.rawHTML)
            if isPureComment(raw) { return nil }
            return MDBlock(id: id, kind: .code(language: nil, text: raw))
        case is ThematicBreak:
            return MDBlock(id: id, kind: .rule)
        case let table as Table:
            return MDBlock(id: id, kind: .table(makeTable(table)))
        default:
            // Directives, custom blocks: not produced with these parse options.
            let text = markup.format()
            return text.isEmpty ? nil : MDBlock(id: id, kind: .paragraph([.text(text)]))
        }
    }

    private static func makeList(_ list: ListItemContainer, ordered: Bool, start: Int, id: String) -> MDList {
        let items = Array(list.listItems)
        let tight = isTight(items)
        var mdItems: [MDListItem] = []
        for (i, item) in items.enumerated() {
            let itemId = "\(id).i\(i)"
            let children = blocks(Array(item.children), path: itemId)
            let own = children.filter { if case .list = $0.kind { return false } else { return true } }
            let text = own.map(blockPlainText).joined(separator: "\n").jsTrimmed
            let checked: Bool? = item.checkbox.map { $0 == .checked }
            mdItems.append(MDListItem(id: itemId, checked: checked, blocks: children, plainText: text))
        }
        return MDList(ordered: ordered, start: start, tight: tight,
                      isTaskList: mdItems.contains { $0.checked != nil }, items: mdItems)
    }

    /// CommonMark: a list is loose when items are separated by blank lines or an
    /// item holds two blocks with a blank line between them.
    private static func isTight(_ items: [ListItem]) -> Bool {
        for (i, item) in items.enumerated() {
            let children = Array(item.children)
            for pair in zip(children, children.dropFirst()) {
                if let end = contentEndLine(pair.0), let next = pair.1.range?.lowerBound.line, next - end > 1 {
                    return false
                }
            }
            if i + 1 < items.count, let end = contentEndLine(item),
               let next = items[i + 1].range?.lowerBound.line, next - end > 1 {
                return false
            }
        }
        return true
    }

    /// The last line holding content (a container's range may include
    /// trailing blank lines; a leaf block's does not).
    private static func contentEndLine(_ markup: Markup) -> Int? {
        if markup is ListItem || markup is BlockQuote || markup is UnorderedList || markup is OrderedList,
           let last = Array(markup.children).last {
            return contentEndLine(last) ?? markup.range?.upperBound.line
        }
        return markup.range?.upperBound.line
    }

    private static func makeTable(_ table: Table) -> MDTable {
        let alignments: [MDTable.Alignment?] = table.columnAlignments.map { alignment in
            switch alignment {
            case .left: return .left
            case .center: return .center
            case .right: return .right
            case .none: return nil
            }
        }
        let head: [[MDInline]] = table.head.cells.map { inlines($0.children) }
        let rows: [[[MDInline]]] = table.body.rows.map { row in row.cells.map { inlines($0.children) } }
        return MDTable(alignments: alignments, head: head, rows: rows)
    }

    private static func blockPlainText(_ block: MDBlock) -> String {
        switch block.kind {
        case .paragraph(let i), .heading(_, let i): return i.plainText
        case .code(_, let t): return t
        case .blockquote(let b): return b.map(blockPlainText).joined(separator: "\n")
        case .list, .rule, .table: return ""
        }
    }

    // MARK: Inlines

    private static func inlines(_ children: MarkupChildren) -> [MDInline] {
        autolink(merged(children.compactMap(inline)))
    }

    private static func inline(_ markup: Markup) -> MDInline? {
        switch markup {
        case let t as Markdown.Text: return .text(t.string)
        case is SoftBreak: return .softBreak
        case is LineBreak: return .lineBreak
        case let c as InlineCode: return .code(c.code)
        case let e as Emphasis: return .emphasis(inlines(e.children))
        case let s as Strong: return .strong(inlines(s.children))
        case let s as Strikethrough: return .strikethrough(inlines(s.children))
        case let l as Markdown.Link:
            // Link text is never autolinked again.
            return .link(destination: l.destination ?? "", children: merged(l.children.compactMap(inline)))
        case let img as Markdown.Image:
            return .image(source: img.source ?? "", alt: img.children.compactMap(inline).plainText)
        case let html as InlineHTML:
            return isPureComment(html.rawHTML) ? nil : .code(html.rawHTML)
        default:
            // Inline attributes and symbol links (Apple extensions) render as their text.
            let inner = markup.children.compactMap(inline)
            return inner.isEmpty ? nil : .emphasis(inner).flattenedEmphasis
        }
    }

    /// Joins adjacent text runs (cmark splits text at special characters).
    private static func merged(_ inlines: [MDInline]) -> [MDInline] {
        var out: [MDInline] = []
        for inline in inlines {
            if case .text(let s) = inline, case .text(let prev)? = out.last {
                out[out.count - 1] = .text(prev + s)
            } else {
                out.append(inline)
            }
        }
        return out
    }

    // MARK: Autolink literals (GFM)

    private static func autolink(_ inlines: [MDInline]) -> [MDInline] {
        inlines.flatMap { inline -> [MDInline] in
            if case .text(let s) = inline { return AutolinkScanner.split(s) }
            return [inline]
        }
    }

    private static func isPureComment(_ raw: String) -> Bool {
        JSRegex.test(raw.jsTrimmed, #"^<!--[\s\S]*-->$"#)
    }

    private static func dropFinalNewline(_ s: String) -> String {
        s.hasSuffix("\n") ? String(s.dropLast()) : s
    }
}

private extension MDInline {
    /// Unwraps the placeholder emphasis used for unknown containers.
    var flattenedEmphasis: MDInline {
        if case .emphasis(let children) = self, children.count == 1 { return children[0] }
        return self
    }
}

/// GFM autolink literals: `https://…`, `http://…`, `www.…` and bare emails,
/// with the spec's trailing-punctuation and parenthesis rules.
enum AutolinkScanner {
    private static let urlPattern =
        #"(?<![A-Za-z0-9])(?:https?://|(?<![^\s*_~(])www\.)[^\s<]+"#
    private static let emailPattern =
        #"(?<![A-Za-z0-9.+_-])[A-Za-z0-9.+_-]+@[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+"#

    static func split(_ text: String) -> [MDInline] {
        let ns = text as NSString
        var found: [(NSRange, String, String)] = [] // range, visible text, href
        let full = NSRange(location: 0, length: ns.length)
        for match in JSRegex.make(urlPattern).matches(in: text, range: full) {
            let raw = ns.substring(with: match.range)
            let trimmed = trimTrailing(raw)
            guard isValidDomain(afterScheme: trimmed) else { continue }
            let href = trimmed.lowercased().hasPrefix("www.") ? "http://" + trimmed : trimmed
            found.append((NSRange(location: match.range.location, length: (trimmed as NSString).length), trimmed, href))
        }
        for match in JSRegex.make(emailPattern).matches(in: text, range: full) {
            var raw = ns.substring(with: match.range)
            while let last = raw.last, ".".contains(last) { raw.removeLast() }
            guard let last = raw.last, last != "-", last != "_" else { continue }
            let range = NSRange(location: match.range.location, length: (raw as NSString).length)
            if found.contains(where: { NSIntersectionRange($0.0, range).length > 0 }) { continue }
            found.append((range, raw, "mailto:" + raw))
        }
        guard !found.isEmpty else { return [.text(text)] }
        found.sort { $0.0.location < $1.0.location }
        var out: [MDInline] = []
        var cursor = 0
        for (range, visible, href) in found where range.location >= cursor {
            if range.location > cursor {
                out.append(.text(ns.substring(with: NSRange(location: cursor, length: range.location - cursor))))
            }
            out.append(.link(destination: href, children: [.text(visible)]))
            cursor = range.location + range.length
        }
        if cursor < ns.length { out.append(.text(ns.substring(from: cursor))) }
        return out
    }

    /// Drops trailing punctuation, unbalanced closing parentheses/brackets and
    /// a trailing entity reference, repeatedly (GFM "extended autolink path validation").
    static func trimTrailing(_ link: String) -> String {
        var s = link
        while true {
            guard let last = s.last else { return s }
            if "?!.,:*_~\"'".contains(last) {
                s.removeLast()
                continue
            }
            if last == ")" || last == "]" {
                let open: Character = last == ")" ? "(" : "["
                let opens = s.filter { $0 == open }.count
                let closes = s.filter { $0 == last }.count
                if closes > opens {
                    s.removeLast()
                    continue
                }
            }
            if last == ";", let m = JSRegex.firstMatch(s, #"&[A-Za-z0-9]+;$"#), let entity = m[0] {
                s.removeLast(entity.count)
                continue
            }
            return s
        }
    }

    /// A domain after the scheme or `www.`: labels of letters, digits, `-`, `_`,
    /// no underscore in the last two labels; `www.` needs a further period.
    static func isValidDomain(afterScheme link: String) -> Bool {
        let lower = link.lowercased()
        var rest: Substring
        if lower.hasPrefix("https://") { rest = link.dropFirst(8) }
        else if lower.hasPrefix("http://") { rest = link.dropFirst(7) }
        else { rest = Substring(link) }
        let domain = rest.prefix { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" || $0 == "." }
        let labels = domain.split(separator: ".", omittingEmptySubsequences: false)
        guard let first = labels.first, !first.isEmpty else { return false }
        if lower.hasPrefix("www.") && labels.filter({ !$0.isEmpty }).count < 2 { return false }
        for label in labels.suffix(2) where label.contains("_") { return false }
        return true
    }
}
