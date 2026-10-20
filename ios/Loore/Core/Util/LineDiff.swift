import Foundation

/// Line diff for artifact version history: a port of `utils/diff.js`
/// (`computeLineDiff`, `refineWordDiffs`, `collapseUnchanged`) plus the
/// counts `components/VersionHistoryDrawer.js` derives from it.
///
/// Lines and word tokens are compared by UTF-16 code units, like JS `===`
/// (Swift `==` treats canonically equivalent strings as equal).
enum LineDiff {
    enum Kind: String, Equatable, Sendable {
        case same, add, del
    }

    /// A run of a paired line: `changed` marks words that differ.
    struct Segment: Equatable, Sendable {
        var changed: Bool
        var text: String
    }

    /// One line of the diff. `segments` is set by `refineWordDiffs` on paired
    /// del/add lines that are similar enough.
    struct Op: Equatable, Sendable {
        var type: Kind
        var text: String
        var segments: [Segment]?

        init(type: Kind, text: String, segments: [Segment]? = nil) {
            self.type = type
            self.text = text
            self.segments = segments
        }
    }

    /// A display row from `collapseUnchanged`: a line, or a fold of `count`
    /// unchanged lines (`{ type: 'skip', count }` on the web).
    enum Row: Equatable, Sendable {
        case line(Op)
        case skip(count: Int)
    }

    /// One change object of jsdiff's `diffWordsWithSpace`.
    struct WordChange: Equatable, Sendable {
        var value: String
        var added: Bool
        var removed: Bool
        var count: Int
    }

    /// `LCS_LINE_CAP`: past this many differing lines the middle is rendered
    /// as one del block + one add block.
    static let lcsLineCap = 1500
    /// `WORD_REFINE_MIN_SIMILARITY`.
    static let wordRefineMinSimilarity = 0.4

    // MARK: - computeLineDiff

    /// `computeLineDiff(oldText, newText)`: trim the common prefix/suffix,
    /// then LCS on the middle (coarse del+add fallback past `lcsLineCap`).
    static func computeLineDiff(_ oldText: String?, _ newText: String?) -> [Op] {
        let a = (oldText ?? "").jsLines
        let b = (newText ?? "").jsLines
        var interner = Interner()
        let ia = a.map { interner.id($0) }
        let ib = b.map { interner.id($0) }

        var start = 0
        while start < a.count && start < b.count && ia[start] == ib[start] {
            start += 1
        }
        var endA = a.count
        var endB = b.count
        while endA > start && endB > start && ia[endA - 1] == ib[endB - 1] {
            endA -= 1
            endB -= 1
        }

        var ops: [Op] = []
        for i in 0..<start { ops.append(Op(type: .same, text: a[i])) }

        let n = endA - start
        let m = endB - start
        if n > lcsLineCap || m > lcsLineCap {
            for i in start..<endA { ops.append(Op(type: .del, text: a[i])) }
            for j in start..<endB { ops.append(Op(type: .add, text: b[j])) }
        } else if n > 0 || m > 0 {
            let midA = Array(ia[start..<endA])
            let midB = Array(ib[start..<endB])
            // (n+1) x (m+1) LCS length table, row-major (Uint16Array rows on the web).
            let w = m + 1
            var table = [UInt16](repeating: 0, count: (n + 1) * w)
            table.withUnsafeMutableBufferPointer { t in
                var i = n - 1
                while i >= 0 {
                    var j = m - 1
                    while j >= 0 {
                        t[i * w + j] = midA[i] == midB[j]
                            ? t[(i + 1) * w + j + 1] &+ 1
                            : max(t[(i + 1) * w + j], t[i * w + j + 1])
                        j -= 1
                    }
                    i -= 1
                }
            }
            var i = 0
            var j = 0
            while i < n && j < m {
                if midA[i] == midB[j] {
                    ops.append(Op(type: .same, text: a[start + i]))
                    i += 1
                    j += 1
                } else if table[(i + 1) * w + j] >= table[i * w + j + 1] {
                    ops.append(Op(type: .del, text: a[start + i]))
                    i += 1
                } else {
                    ops.append(Op(type: .add, text: b[start + j]))
                    j += 1
                }
            }
            while i < n { ops.append(Op(type: .del, text: a[start + i])); i += 1 }
            while j < m { ops.append(Op(type: .add, text: b[start + j])); j += 1 }
        }

        for i in endA..<a.count { ops.append(Op(type: .same, text: a[i])) }
        return ops
    }

    // MARK: - refineWordDiffs

    /// `refineWordDiffs(ops)`: within each del-run→add-run block, pair the
    /// i-th deleted line with the i-th added line and give both word
    /// `segments` when their similarity is at least `wordRefineMinSimilarity`.
    static func refineWordDiffs(_ input: [Op]) -> [Op] {
        var ops = input
        var i = 0
        while i < ops.count {
            if ops[i].type != .del { i += 1; continue }
            let delStart = i
            while i < ops.count && ops[i].type == .del { i += 1 }
            let addStart = i
            while i < ops.count && ops[i].type == .add { i += 1 }
            let pairs = min(addStart - delStart, i - addStart)
            for k in 0..<pairs {
                if let (del, add) = refinePair(ops[delStart + k].text, ops[addStart + k].text) {
                    ops[delStart + k].segments = del
                    ops[addStart + k].segments = add
                }
            }
        }
        return ops
    }

    /// `refinePair`: the del and add segments, or nil when the lines are too
    /// dissimilar. Similarity = unchanged UTF-16 units / longer line length.
    private static func refinePair(_ delText: String, _ addText: String) -> ([Segment], [Segment])? {
        let parts = diffWordsWithSpace(delText, addText)
        let commonChars = parts.filter { !$0.added && !$0.removed }.reduce(0) { $0 + $1.value.jsLength }
        let similarity = Double(commonChars) / Double(max(delText.jsLength, addText.jsLength, 1))
        if similarity < wordRefineMinSimilarity { return nil }
        let del = parts.filter { !$0.added }.map { Segment(changed: $0.removed, text: $0.value) }
        let add = parts.filter { !$0.removed }.map { Segment(changed: $0.added, text: $0.value) }
        return (del, add)
    }

    // MARK: - collapseUnchanged

    /// `collapseUnchanged(ops, context)`: fold long runs of unchanged lines,
    /// keeping `context` lines (>= 0) next to each change; no leading context
    /// at the start of the document, no trailing context at its end.
    static func collapseUnchanged(_ ops: [Op], context: Int = 2) -> [Row] {
        var rows: [Row] = []
        var sameRun: [Op] = []

        func flushRun(isLast: Bool) {
            if sameRun.isEmpty { return }
            let keepHead = rows.isEmpty ? 0 : context
            let keepTail = isLast ? 0 : context
            if sameRun.count > keepHead + keepTail + 1 {
                for op in sameRun.prefix(keepHead) { rows.append(.line(op)) }
                rows.append(.skip(count: sameRun.count - keepHead - keepTail))
                for op in sameRun.suffix(keepTail) { rows.append(.line(op)) }
            } else {
                for op in sameRun { rows.append(.line(op)) }
            }
            sameRun = []
        }

        for op in ops {
            if op.type == .same {
                sameRun.append(op)
            } else {
                flushRun(isLast: false)
                rows.append(.line(op))
            }
        }
        flushRun(isLast: true)
        return rows
    }

    // MARK: - VersionHistoryDrawer helpers

    /// `changedCount` in VersionHistoryDrawer: ops that are not `same`.
    static func changedCount(_ ops: [Op]) -> Int {
        ops.filter { $0.type != .same }.count
    }

    /// `isHeavyRewrite`: more than half the ops changed AND more than 40 of
    /// them. The drawer opens such versions in full-text mode.
    static func isHeavyRewrite(_ ops: [Op]) -> Bool {
        let changed = changedCount(ops)
        return Double(changed) > Double(ops.count) * 0.5 && changed > 40
    }

    /// The drawer's "No changes from the previous version." condition.
    static func hasNoChanges(_ rows: [Row]) -> Bool {
        rows.allSatisfy { row in
            if case .line(let op) = row { return op.type == .same }
            return true
        }
    }

    /// The fold separator text: "⋯ 13 unchanged lines ⋯".
    static func skipLabel(count: Int) -> String {
        "⋯ \(count) unchanged \(count == 1 ? "line" : "lines") ⋯"
    }

    // MARK: - diffWordsWithSpace (jsdiff 9)

    /// jsdiff `diffWordsWithSpace(oldStr, newStr)`: the `WordsWithSpaceDiff`
    /// tokenizer and the base Myers diff (`base.js`), without post-processing.
    static func diffWordsWithSpace(_ oldStr: String, _ newStr: String) -> [WordChange] {
        let oldTokens = wordsWithSpaceTokens(oldStr)
        let newTokens = wordsWithSpaceTokens(newStr)
        var interner = Interner()
        let oldIds = oldTokens.map { interner.id($0) }
        let newIds = newTokens.map { interner.id($0) }
        let oldLen = oldIds.count
        let newLen = newIds.count

        func extractCommon(_ path: inout DiffPath, _ diagonal: Int) -> Int {
            var oldPos = path.oldPos
            var newPos = oldPos - diagonal
            var common = 0
            while newPos + 1 < newLen && oldPos + 1 < oldLen && oldIds[oldPos + 1] == newIds[newPos + 1] {
                newPos += 1
                oldPos += 1
                common += 1
            }
            if common > 0 {
                path.last = DiffComponent(count: common, added: false, removed: false, previous: path.last)
            }
            path.oldPos = oldPos
            return newPos
        }

        func addToPath(_ path: DiffPath, added: Bool, removed: Bool, oldPosInc: Int) -> DiffPath {
            if let last = path.last, last.added == added, last.removed == removed {
                return DiffPath(oldPos: path.oldPos + oldPosInc,
                                last: DiffComponent(count: last.count + 1, added: added, removed: removed,
                                                    previous: last.previous))
            }
            return DiffPath(oldPos: path.oldPos + oldPosInc,
                            last: DiffComponent(count: 1, added: added, removed: removed, previous: path.last))
        }

        func buildValues(_ lastComponent: DiffComponent?) -> [WordChange] {
            var components: [DiffComponent] = []
            var next = lastComponent
            while let c = next {
                components.append(c)
                next = c.previous
            }
            components.reverse()
            var out: [WordChange] = []
            var newPos = 0
            var oldPos = 0
            for c in components {
                let value: String
                if !c.removed {
                    value = newTokens[newPos..<(newPos + c.count)].joined()
                    newPos += c.count
                    if !c.added { oldPos += c.count }
                } else {
                    value = oldTokens[oldPos..<(oldPos + c.count)].joined()
                    oldPos += c.count
                }
                out.append(WordChange(value: value, added: c.added, removed: c.removed, count: c.count))
            }
            return out
        }

        let maxEditLength = newLen + oldLen
        // bestPath is indexed by diagonal (negative on the web); shift by `offset`.
        let offset = maxEditLength + 1
        var bestPath = [DiffPath?](repeating: nil, count: 2 * maxEditLength + 3)
        var seed = DiffPath(oldPos: -1, last: nil)
        var newPos = extractCommon(&seed, 0)
        bestPath[offset] = seed
        if seed.oldPos + 1 >= oldLen && newPos + 1 >= newLen {
            return buildValues(seed.last)
        }

        var minDiagonal = Int.min
        var maxDiagonal = Int.max
        var editLength = 1
        while editLength <= maxEditLength {
            var diagonal = max(minDiagonal, -editLength)
            while diagonal <= min(maxDiagonal, editLength) {
                let removePath = bestPath[offset + diagonal - 1]
                let addPath = bestPath[offset + diagonal + 1]
                if removePath != nil { bestPath[offset + diagonal - 1] = nil }
                var canAdd = false
                if let addPath {
                    let addPathNewPos = addPath.oldPos - diagonal
                    canAdd = 0 <= addPathNewPos && addPathNewPos < newLen
                }
                let canRemove = removePath.map { $0.oldPos + 1 < oldLen } ?? false
                if !canAdd && !canRemove {
                    bestPath[offset + diagonal] = nil
                    diagonal += 2
                    continue
                }
                var basePath: DiffPath
                if !canRemove || (canAdd && removePath!.oldPos < addPath!.oldPos) {
                    basePath = addToPath(addPath!, added: true, removed: false, oldPosInc: 0)
                } else {
                    basePath = addToPath(removePath!, added: false, removed: true, oldPosInc: 1)
                }
                newPos = extractCommon(&basePath, diagonal)
                if basePath.oldPos + 1 >= oldLen && newPos + 1 >= newLen {
                    return buildValues(basePath.last)
                }
                bestPath[offset + diagonal] = basePath
                if basePath.oldPos + 1 >= oldLen { maxDiagonal = min(maxDiagonal, diagonal - 1) }
                if newPos + 1 >= newLen { minDiagonal = max(minDiagonal, diagonal + 1) }
                diagonal += 2
            }
            editLength += 1
        }
        return []  // Unreachable: the edit graph is always crossed within maxEditLength.
    }

    /// `WordsWithSpaceDiff.tokenize`: the matches of
    /// `/(\r?\n)|[W]+|[^\S\n\r]+|[^W]/gu`, W = jsdiff's `extendedWordChars`,
    /// `\s` = JS whitespace. Works on code points like the `u` flag.
    static func wordsWithSpaceTokens(_ value: String) -> [String] {
        let scalars = value.unicodeScalars
        var tokens: [String] = []
        var i = scalars.startIndex
        while i < scalars.endIndex {
            let c = scalars[i]
            var end = scalars.index(after: i)
            if c == "\r", end < scalars.endIndex, scalars[end] == "\n" {
                end = scalars.index(after: end)
            } else if c == "\n" {
                // single newline token
            } else if isExtendedWordChar(c) {
                while end < scalars.endIndex, isExtendedWordChar(scalars[end]) { end = scalars.index(after: end) }
            } else if isJSWhitespace(c), c != "\r" {
                while end < scalars.endIndex, isJSWhitespace(scalars[end]), scalars[end] != "\n", scalars[end] != "\r" {
                    end = scalars.index(after: end)
                }
            }
            tokens.append(String(scalars[i..<end]))
            i = end
        }
        return tokens
    }

    /// jsdiff `extendedWordChars`: ASCII word chars plus most Latin letters
    /// with diacritics (see `diff/lib/diff/word.js`).
    static func isExtendedWordChar(_ c: Unicode.Scalar) -> Bool {
        switch c.value {
        case 0x61...0x7A, 0x41...0x5A, 0x30...0x39, 0x5F: return true
        case 0xAD, 0xC0...0xD6, 0xD8...0xF6, 0xF8...0x2C6, 0x2C8...0x2D7, 0x2DE...0x2FF, 0x1E00...0x1EFF: return true
        default: return false
        }
    }

    /// JS regex `\s`.
    static func isJSWhitespace(_ c: Unicode.Scalar) -> Bool {
        switch c.value {
        case 0x09...0x0D, 0x20, 0xA0, 0x1680, 0x2000...0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF:
            return true
        default:
            return false
        }
    }
}

/// Maps strings to small ints by exact UTF-16 content (JS `===` equality).
private struct Interner {
    private var ids: [[UInt16]: Int] = [:]

    mutating func id(_ s: String) -> Int {
        let key = Array(s.utf16)
        if let id = ids[key] { return id }
        let id = ids.count
        ids[key] = id
        return id
    }
}

/// A jsdiff change component in the path's linked list (`lastComponent`).
private final class DiffComponent {
    let count: Int
    let added: Bool
    let removed: Bool
    let previous: DiffComponent?

    init(count: Int, added: Bool, removed: Bool, previous: DiffComponent?) {
        self.count = count
        self.added = added
        self.removed = removed
        self.previous = previous
    }
}

/// A jsdiff `bestPath` entry.
private struct DiffPath {
    var oldPos: Int
    var last: DiffComponent?
}

/// The diff VersionHistoryDrawer shows for one version against the next
/// older one: word-refined line ops plus the drawer's derived values.
struct VersionDiff: Equatable, Sendable {
    let ops: [LineDiff.Op]

    /// `refineWordDiffs(computeLineDiff(old, new))`.
    init(old: String?, new: String?) {
        ops = LineDiff.refineWordDiffs(LineDiff.computeLineDiff(old, new))
    }

    /// "Changes (n)".
    var changedCount: Int { LineDiff.changedCount(ops) }
    /// Opens in full-text mode instead of the diff.
    var isHeavyRewrite: Bool { LineDiff.isHeavyRewrite(ops) }
    /// Display rows with unchanged runs folded (context 2, as on the web).
    var rows: [LineDiff.Row] { LineDiff.collapseUnchanged(ops, context: 2) }
    /// "No changes from the previous version."
    var hasNoChanges: Bool { LineDiff.hasNoChanges(rows) }
}
