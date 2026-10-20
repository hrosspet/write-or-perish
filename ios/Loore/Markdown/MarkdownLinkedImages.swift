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
/// "](href)". Fenced code blocks and inline code spans are left alone.
enum MarkdownLinkedImages {
    private static let broken = try! NSRegularExpression(
        pattern: #"\[\s*(!\[[^\]\n]*\]\([^()\s]+(?:[ \t]+"[^"\n]*")?\))\s*\]\(([^()\s]+)\)"#)

    private static let code = try! NSRegularExpression(
        pattern: #"^[ ]{0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ ]{0,3}\1[`~]*[ \t]*$|(?![\s\S]))|(`+)[^`][\s\S]*?\2(?!`)"#,
        options: [.anchorsMatchLines])

    static func normalize(_ text: String) -> String {
        guard text.contains("]("), text.range(of: #"\[\s*!\["#, options: .regularExpression) != nil else {
            return text
        }
        let ns = text as NSString
        var out = ""
        var last = 0
        for match in code.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            guard match.range.length > 0 else { continue }
            out += repair(ns.substring(with: NSRange(location: last, length: match.range.location - last)))
            out += ns.substring(with: match.range)
            last = match.range.location + match.range.length
        }
        out += repair(ns.substring(from: last))
        return out
    }

    private static func repair(_ segment: String) -> String {
        broken.stringByReplacingMatches(
            in: segment, range: NSRange(location: 0, length: (segment as NSString).length),
            withTemplate: "[$1]($2)")
    }
}
