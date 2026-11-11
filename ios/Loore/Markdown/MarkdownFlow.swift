import SwiftUI
import UIKit

/// Which markdown blocks share one selectable text, so a selection can run
/// from one paragraph into the next, as in Safari. Paragraphs, headings and
/// lists of those join; code blocks, tables, block quotes, rules, task lists
/// and paragraphs with images keep their own views (and their inner text is
/// selectable on its own).
enum MarkdownFlow {
    struct Segment: Identifiable, Equatable {
        /// The first block's id: a run that grows while a reply streams keeps its view.
        let id: String
        let range: Range<Int>
        let isFlow: Bool
    }

    static func joins(_ block: MDBlock) -> Bool {
        switch block.kind {
        case .paragraph(let inlines): return !inlines.containsImage
        case .heading: return true
        case .list(let list): return !list.isTaskList && list.items.allSatisfy { $0.blocks.allSatisfy(joins) }
        case .blockquote, .code, .rule, .table: return false
        }
    }

    static func segments(_ blocks: [MDBlock]) -> [Segment] {
        var out: [Segment] = []
        var start: Int?
        for (index, block) in blocks.enumerated() {
            if joins(block) {
                if start == nil { start = index }
                continue
            }
            if let first = start {
                out.append(Segment(id: blocks[first].id, range: first..<index, isFlow: true))
                start = nil
            }
            out.append(Segment(id: block.id, range: index..<index + 1, isFlow: false))
        }
        if let first = start {
            out.append(Segment(id: blocks[first].id, range: first..<blocks.count, isFlow: true))
        }
        return out
    }
}

/// A run of joining blocks as one `SelectableText`.
struct FlowTextView: View {
    let blocks: [MDBlock]
    let context: MarkdownRenderContext
    var tightListItem: Bool
    var listDepth: Int
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        let traits = UITraitCollection(preferredContentSizeCategory: UIContentSizeCategory(typeSize))
        SelectableText(text: MarkdownFlowBuilder.build(blocks, context: context, traits: traits,
                                                       tightListItem: tightListItem, listDepth: listDepth))
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The attributed text of a run: each block's paragraphs with the spacing,
/// indents and list markers the SwiftUI blocks have (`MarkdownBlocks`,
/// `ListBlockView`), so the page looks the same.
@MainActor
final class MarkdownFlowBuilder {
    private let context: MarkdownRenderContext
    private let traits: UITraitCollection
    private let out = NSMutableAttributedString()
    /// Space owed above the next paragraph (margins and list gaps, added up as the nested SwiftUI paddings add up).
    private var pendingGap: CGFloat = 0
    /// A list item's marker, waiting for the item's first paragraph.
    private var pendingMarker: (text: String, indent: CGFloat)?
    private var previousLineSpacing: CGFloat = 0

    private init(context: MarkdownRenderContext, traits: UITraitCollection) {
        self.context = context
        self.traits = traits
    }

    static func build(_ blocks: [MDBlock], context: MarkdownRenderContext, traits: UITraitCollection,
                      tightListItem: Bool = false, listDepth: Int = 0) -> NSAttributedString {
        let builder = MarkdownFlowBuilder(context: context, traits: traits)
        builder.blocks(blocks, trimOuter: true, tight: tightListItem, depth: listDepth, indent: 0)
        return builder.out
    }

    private var style: MarkdownStyle { context.style }

    private func blocks(_ blocks: [MDBlock], trimOuter: Bool, tight: Bool, depth: Int, indent: CGFloat) {
        for (index, block) in blocks.enumerated() {
            let own = MarkdownSpacing.margins(block, style: style, tightListItem: tight)
            if index == 0 {
                if !trimOuter { pendingGap += own.top }
            } else {
                pendingGap += max(MarkdownSpacing.margins(blocks[index - 1], style: style, tightListItem: tight).bottom, own.top)
            }
            emit(block, tight: tight, depth: depth, indent: indent)
        }
        if !trimOuter, let last = blocks.last {
            pendingGap += MarkdownSpacing.margins(last, style: style, tightListItem: tight).bottom
        }
    }

    private func emit(_ block: MDBlock, tight: Bool, depth: Int, indent: CGFloat) {
        switch block.kind {
        case .paragraph(let inlines):
            let builder = InlineAttributedBuilder(context: context, softBreakAsSpace: tight || style.flowText,
                                                  baseSize: style.fontSize, isHeading: false,
                                                  ui: (traits, UIColor(style.color)))
            append(builder.build(inlines), lineSpacing: style.lineSpacing, indent: indent)
        case .heading(let level, let inlines):
            let size = HeadingMetrics.size(level) * style.fontSize
            let builder = InlineAttributedBuilder(context: context, softBreakAsSpace: true, baseSize: size, isHeading: true,
                                                  ui: (traits, LooreColor.textPrimaryUI))
            append(builder.build(inlines), lineSpacing: 0, indent: indent, headingLevel: level)
        case .list(let list):
            for (index, item) in list.items.enumerated() {
                if index > 0 { pendingGap += MarkdownSpacing.itemSpacing(list, style: style) }
                pendingMarker = (MarkdownSpacing.marker(list, index: index, depth: depth), indent)
                let itemIndent = indent + MarkdownSpacing.markerColumn + MarkdownSpacing.markerGap
                if item.blocks.isEmpty {
                    append(AttributedString(), lineSpacing: style.lineSpacing, indent: itemIndent)
                } else {
                    blocks(item.blocks, trimOuter: true, tight: list.tight, depth: depth + 1, indent: itemIndent)
                }
            }
        case .blockquote, .code, .rule, .table:
            assertionFailure("\(block.kind) does not join a text run")
        }
    }

    private func append(_ content: AttributedString, lineSpacing: CGFloat, indent: CGFloat, headingLevel: Int? = nil) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = lineSpacing
        // TextKit also puts the previous paragraph's line spacing below its last
        // line; a SwiftUI `Text` does not, so it comes off the gap. (A gap smaller
        // than the line spacing, as between tight list items, stays at the line spacing.)
        paragraph.paragraphSpacingBefore = out.length == 0 ? 0 : max(0, pendingGap - previousLineSpacing)
        paragraph.firstLineHeadIndent = indent
        paragraph.headIndent = indent
        // Like `Text`: no single word left alone on a paragraph's last line.
        paragraph.lineBreakStrategy = .standard
        pendingGap = 0
        previousLineSpacing = lineSpacing

        let line = NSMutableAttributedString()
        if let marker = pendingMarker {
            // Right-aligned in the marker column, then a tab to the item's text (`ListBlockView`).
            let font = style.uiFont(traits: traits)
            let width = (marker.text as NSString).size(withAttributes: [.font: font]).width
            paragraph.firstLineHeadIndent = max(0, marker.indent + MarkdownSpacing.markerColumn - width)
            paragraph.tabStops = [NSTextTab(textAlignment: .left, location: indent)]
            paragraph.defaultTabInterval = 0
            line.append(NSAttributedString(string: marker.text + "\t",
                                           attributes: [.font: font, .foregroundColor: UIColor(style.color)]))
            pendingMarker = nil
        }
        if let converted = try? NSAttributedString(content, including: \.uiKit) {
            line.append(converted)
        }
        if line.length == 0 {
            line.append(NSAttributedString(string: "", attributes: [.font: style.uiFont(traits: traits)]))
        }
        let whole = NSRange(location: 0, length: line.length)
        line.addAttribute(.paragraphStyle, value: paragraph, range: whole)
        if let headingLevel {
            line.addAttribute(.accessibilityTextHeadingLevel, value: headingLevel, range: whole)
        }

        if out.length > 0 {
            // The newline ends the previous paragraph and takes its attributes.
            let previous = out.attributes(at: out.length - 1, effectiveRange: nil)
            out.append(NSAttributedString(string: "\n", attributes: previous))
        }
        out.append(line)
    }
}
