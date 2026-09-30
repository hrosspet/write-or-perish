import SwiftUI

/// Interim Markdown for short texts (changelog bodies in the Updates sheet):
/// paragraphs, `#` headings, `-`/`*`/`1.` list items, and inline bold, italics,
/// code and links via `AttributedString`. Single newlines are line breaks, as
/// on the web (remark-breaks).
///
/// M2 replaces this with the full swift-markdown renderer (design doc §8);
/// swap call sites over and delete this file then.
struct SimpleMarkdownText: View {
    let markdown: String
    var font: Font = LooreFont.body
    var color: Color = LooreColor.textSecondary

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(Self.blocks(markdown).enumerated()), id: \.offset) { _, block in
                switch block {
                case .heading(let level, let text):
                    Text(Self.inline(text))
                        .font(LooreFont.serif(level <= 2 ? 22 : 19, .semibold))
                        .foregroundStyle(LooreColor.textPrimary)
                case .bullet(let marker, let text):
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(marker).font(font).foregroundStyle(color)
                        Text(Self.inline(text)).font(font).foregroundStyle(color)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                case .paragraph(let text):
                    Text(Self.inline(text))
                        .font(font)
                        .foregroundStyle(color)
                        .lineSpacing(4)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .tint(LooreColor.accent)
    }

    enum Block: Equatable {
        case heading(Int, String)
        case bullet(String, String)
        case paragraph(String)
    }

    static func blocks(_ text: String) -> [Block] {
        var blocks: [Block] = []
        var paragraph: [String] = []
        func flush() {
            if !paragraph.isEmpty {
                blocks.append(.paragraph(paragraph.joined(separator: "\n")))
                paragraph = []
            }
        }
        for rawLine in text.components(separatedBy: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty {
                flush()
                continue
            }
            if let hashes = line.firstIndex(where: { $0 != "#" }), line.hasPrefix("#"),
               line[hashes] == " " {
                flush()
                let level = line.distance(from: line.startIndex, to: hashes)
                blocks.append(.heading(level, String(line[hashes...]).trimmingCharacters(in: .whitespaces)))
                continue
            }
            if line.hasPrefix("- ") || line.hasPrefix("* ") {
                flush()
                blocks.append(.bullet("•", String(line.dropFirst(2))))
                continue
            }
            if let dot = line.firstIndex(of: "."), line[..<dot].allSatisfy(\.isNumber), !line[..<dot].isEmpty,
               line[line.index(after: dot)...].hasPrefix(" ") {
                flush()
                blocks.append(.bullet(String(line[...dot]), String(line[line.index(dot, offsetBy: 2)...])))
                continue
            }
            paragraph.append(rawLine)
        }
        flush()
        return blocks
    }

    static func inline(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}
