import Foundation

/// Repairs the shape the Chrome clipper used to save for a linked image
/// (Substack wraps every image in a link around block content):
///
///     [
///
///     ![alt](src)
///
///     ](href)
///
/// A blank line inside link text breaks the link in CommonMark, so it showed
/// as a stray "[", the image and a raw "](href)". Rewritten to
/// `[![alt](src)](href)`. Saved text is encrypted at rest, so the repair runs
/// when the text is rendered. Port of `utils/markdownLinkedImages.js`; keep
/// the two in step.
///
/// Narrow on purpose: only "[", whitespace, ONE image, whitespace,
/// "](href)". Fenced code blocks are left alone.
/// The repair needs a blank line inside the brackets: with single newlines
/// CommonMark already parses the link, and a code span cannot hold a blank line.
enum MarkdownLinkedImages {
    // The alt excludes "[" so a run of "[![" cannot make the scan quadratic.
    private static let imageLink = try! NSRegularExpression(
        pattern: #"\[(\s*)(!\[[^\[\]\n]*\]\([^()\s]+(?:[ \t]+"[^"\n]*")?\))(\s*)\]\(([^()\s]+)\)"#)

    private static let blankLine = try! NSRegularExpression(pattern: #"\n[^\S\n]*\n"#)

    static func normalize(_ text: String) -> String {
        guard text.contains("]("), text.range(of: #"\[\s*!\["#, options: .regularExpression) != nil else {
            return text
        }
        // One pass over the lines; a fenced block (closed by a fence of the
        // same character and at least the same length, or the end of the
        // text) is copied as it is.
        let ns = text as NSString
        var out = ""
        var segStart = 0
        var fence: (char: Character, length: Int)?
        var pos = 0
        while pos < ns.length {
            let range = ns.lineRange(for: NSRange(location: pos, length: 0))
            let line = ns.substring(with: range)
            if let open = fence {
                if let f = fenceRun(line, closing: true), f.char == open.char, f.length >= open.length {
                    out += ns.substring(with: NSRange(location: segStart, length: range.upperBound - segStart))
                    segStart = range.upperBound
                    fence = nil
                }
            } else if let f = fenceRun(line, closing: false) {
                out += repair(ns.substring(with: NSRange(location: segStart, length: pos - segStart)))
                segStart = pos
                fence = f
            }
            pos = range.upperBound
        }
        let rest = ns.substring(from: segStart)
        out += fence == nil ? repair(rest) : rest
        return out
    }

    /// A code fence on `line`: up to 3 spaces, then 3+ backticks or tildes
    /// (a closing fence may then have only spaces and tabs).
    private static func fenceRun(_ line: String, closing: Bool) -> (char: Character, length: Int)? {
        var chars = Substring(line)
        var indent = 0
        while indent < 4, chars.first == " " { chars = chars.dropFirst(); indent += 1 }
        guard indent <= 3, let first = chars.first, first == "`" || first == "~" else { return nil }
        let run = chars.prefix { $0 == first }
        guard run.count >= 3 else { return nil }
        if closing {
            let tail = chars.dropFirst(run.count)
            guard tail.allSatisfy({ $0 == " " || $0 == "\t" || $0 == "\n" || $0 == "\r" || $0 == "\r\n" }) else { return nil }
        }
        return (first, run.count)
    }

    private static func repair(_ segment: String) -> String {
        let ns = segment as NSString
        var out = ""
        var last = 0
        for m in imageLink.matches(in: segment, range: NSRange(location: 0, length: ns.length)) {
            let before = ns.substring(with: m.range(at: 1))
            let after = ns.substring(with: m.range(at: 3))
            guard hasBlankLine(before) || hasBlankLine(after) else { continue }
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            out += "[" + ns.substring(with: m.range(at: 2)) + "](" + ns.substring(with: m.range(at: 4)) + ")"
            last = m.range.upperBound
        }
        return out + ns.substring(from: last)
    }

    private static func hasBlankLine(_ s: String) -> Bool {
        blankLine.firstMatch(in: s, range: NSRange(location: 0, length: (s as NSString).length)) != nil
    }
}
