import SwiftUI
import Observation

/// How a markdown body looks where it is used (the web's inherited font,
/// colour and line height plus `MarkdownBody`'s `paragraphMargin`/`flowText`).
struct MarkdownStyle: Equatable {
    /// Base font size in points (1em).
    var fontSize: CGFloat = 16
    var weight: LooreFontFace.SansWeight = .regular
    var color: Color = LooreColor.textPrimary
    /// CSS line-height multiple (Outfit's own is about 1.26).
    var lineHeight: CGFloat = 1.3
    /// Paragraph top/bottom margin in em (web default `0.5em 0`).
    var paragraphMargin: CGFloat = 0.5
    /// Soft line breaks flow (authored docs such as the changelog) instead of
    /// breaking the line (node content).
    var flowText = false
    var textStyle: Font.TextStyle = .body

    /// The focal node card (inherits the page body: Outfit 400, 1rem).
    static let focal = MarkdownStyle()
    /// Expanded non-focal bubbles: .95rem, 300, secondary, line-height 1.7.
    static let bubble = MarkdownStyle(fontSize: 15.2, weight: .light, color: LooreColor.textSecondary, lineHeight: 1.7)
    /// Quote cards (`InlineQuoteBubble`): .95em of the card, line-height 1.4, no paragraph margin.
    static let quote = MarkdownStyle(fontSize: 15.2, weight: .regular, color: LooreColor.textPrimary,
                                     lineHeight: 1.4, paragraphMargin: 0)
    /// Proposal card bodies (issue description, feedback, shares).
    static let proposal = MarkdownStyle(fontSize: 13.1, weight: .light, color: LooreColor.textSecondary,
                                        lineHeight: 1.7, paragraphMargin: 0.4)
    /// Changelog bodies in the Updates sheet (`flowText`).
    static let changelog = MarkdownStyle(fontSize: 14.7, weight: .light, color: LooreColor.textSecondary,
                                         lineHeight: 1.6, flowText: true)

    func with(_ change: (inout MarkdownStyle) -> Void) -> MarkdownStyle {
        var copy = self
        change(&copy)
        return copy
    }

    var baseFont: Font { LooreFont.sans(fontSize, weight, relativeTo: textStyle) }
    var lineSpacing: CGFloat { max(0, (lineHeight - 1.26) * fontSize) }
    var paragraphSpacing: CGFloat { paragraphMargin * fontSize }
}

/// Owner-only checklist actions (web `onCheckboxToggle` / `onAddTask`).
struct ChecklistActions {
    /// (item plain text, currently checked)
    var toggle: (String, Bool) -> Void
    /// (after item plain text, new item text); nil hides the "+".
    var add: ((String, String) -> Void)?
}

/// The one markdown renderer (design doc §8), replacing `SimpleMarkdownText`.
/// Parse → `MarkdownDocument` (cached) → SwiftUI views.
///
/// Links: `/node/<id>` links (relative or on a Loore host) open the thread in
/// the app; a bare node URL shows the node's title; other Loore paths open
/// their native screen; other web links open in an in-app Safari view and
/// `mailto:` in Mail. Every other scheme renders as plain text (#442).
/// Images: only Loore's own media loads by itself; any other image waits for a
/// tap (#441).
struct MarkdownView: View {
    let markdown: String
    var style: MarkdownStyle = .focal
    var checklist: ChecklistActions?
    /// Sees every tapped link first (the Updates sheet closes itself before
    /// navigating). Return true when handled; false falls back to the default.
    var onLink: ((String) -> Bool)?

    @Environment(AppState.self) private var app
    @State private var edit = ChecklistEditState()

    var body: some View {
        let document = MarkdownParser.document(for: markdown)
        MarkdownBlocks(blocks: document.blocks, context: context, trimOuter: true)
            .environment(\.openURL, OpenURLAction { url in handle(url) })
            .tint(LooreColor.accent)
    }

    private var context: MarkdownRenderContext {
        MarkdownRenderContext(style: style, checklist: checklist, edit: edit,
                              titles: app.nodeTitles, environment: app.environment)
    }

    /// Never `.systemAction`: the system opens any app's URL scheme, so every
    /// link goes through the router's allowlist instead (#442).
    private func handle(_ url: URL) -> OpenURLAction.Result {
        let link = url.absoluteString
        if let onLink, onLink(link) { return .handled }
        switch MarkdownLinkTarget.resolve(link, environment: app.environment) {
        case .thread(let id): app.open(.thread(id: id, awaitLLM: nil))
        case .route(let route): app.open(route)
        case .ignore: break
        }
        return .handled
    }
}

/// Per-body UI state for the checklist "+" (survives content re-renders, #321).
@MainActor
@Observable
final class ChecklistEditState {
    /// Plain text of the row whose "+" input is open.
    var addingAfter: String?
    var draft = ""
}

struct MarkdownRenderContext {
    var style: MarkdownStyle
    var checklist: ChecklistActions?
    var edit: ChecklistEditState
    var titles: NodeTitleStore
    var environment: AppEnvironment
    var origin: URL? { environment.frontendOrigin }
    /// Blockquotes render italic.
    var italic = false

    func with(_ change: (inout MarkdownRenderContext) -> Void) -> MarkdownRenderContext {
        var copy = self
        change(&copy)
        return copy
    }
}

// MARK: - Blocks

/// A column of blocks with the web's collapsing vertical margins.
private struct MarkdownBlocks: View {
    let blocks: [MDBlock]
    let context: MarkdownRenderContext
    /// Trim the first block's top and the last block's bottom margin (`.loore-md > :first-child`).
    var trimOuter: Bool
    var tightListItem = false
    var listDepth = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(blocks.enumerated()), id: \.element.id) { index, block in
                BlockView(block: block, context: context, tightListItem: tightListItem, listDepth: listDepth)
                    .padding(.top, topSpacing(index))
                    .padding(.bottom, index == blocks.count - 1 && !trimOuter ? margins(block).bottom : 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func topSpacing(_ index: Int) -> CGFloat {
        let own = margins(blocks[index]).top
        if index == 0 { return trimOuter ? 0 : own }
        return max(margins(blocks[index - 1]).bottom, own)
    }

    private func margins(_ block: MDBlock) -> (top: CGFloat, bottom: CGFloat) {
        let em = context.style.fontSize
        switch block.kind {
        case .paragraph:
            if tightListItem { return (0, 0) }
            let m = context.style.paragraphSpacing
            return (m, m)
        case .heading(let level, _):
            let size = HeadingMetrics.size(level) * em
            return (HeadingMetrics.top(level) * size, HeadingMetrics.bottom(level) * size)
        case .blockquote, .code: return (0.75 * em, 0.75 * em)
        case .list: return (4, 4)
        case .rule: return (20, 20)
        case .table: return (8, 8)
        }
    }
}

enum HeadingMetrics {
    static func size(_ level: Int) -> CGFloat { [2.2, 1.8, 1.5, 1.25, 1.1, 0.95][min(max(level, 1), 6) - 1] }
    static func top(_ level: Int) -> CGFloat { [1.2, 1.1, 1, 1, 0.9, 0.9][min(max(level, 1), 6) - 1] }
    static func bottom(_ level: Int) -> CGFloat { [0.4, 0.4, 0.35, 0.35, 0.3, 0.3][min(max(level, 1), 6) - 1] }
    static func lineHeight(_ level: Int) -> CGFloat { [1.2, 1.25, 1.3, 1.3, 1.35, 1.35][min(max(level, 1), 6) - 1] }
    static func weight(_ level: Int) -> LooreFontFace.SerifWeight { level <= 2 ? .bold : .semibold }
}

private struct BlockView: View {
    let block: MDBlock
    let context: MarkdownRenderContext
    var tightListItem: Bool
    var listDepth: Int

    var body: some View {
        let style = context.style
        switch block.kind {
        case .paragraph(let inlines):
            ParagraphView(inlines: inlines, context: context, softBreakAsSpace: tightListItem || style.flowText)
        case .heading(let level, let inlines):
            let size = HeadingMetrics.size(level) * style.fontSize
            let font = LooreFont.serif(size, HeadingMetrics.weight(level), relativeTo: level <= 2 ? .title : .title3)
            InlineText(inlines: inlines, context: context, font: font, fontSize: size, isHeading: true,
                       color: LooreColor.textPrimary, softBreakAsSpace: true)
                .lineSpacing(max(0, (HeadingMetrics.lineHeight(level) - 1.2) * size))
                .accessibilityAddTraits(.isHeader)
        case .blockquote(let children):
            MarkdownBlocks(blocks: children, context: context.with { ctx in
                ctx.style.color = LooreColor.textSecondary
                ctx.italic = true
            }, trimOuter: false)
            .padding(.vertical, 0.5 * style.fontSize - style.paragraphSpacing / 2)
            .padding(.horizontal, style.fontSize)
            .background(LooreColor.bgCard)
            .overlay(alignment: .leading) {
                Rectangle().fill(LooreColor.accentDim).frame(width: 3)
            }
        case .list(let list):
            ListBlockView(list: list, context: context, depth: listDepth)
        case .code(_, let text):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(text)
                    .font(MarkdownFonts.mono(0.9 * style.fontSize))
                    .foregroundStyle(style.color)
                    .fixedSize(horizontal: true, vertical: true)
                    .textSelection(.enabled)
                    .padding(.vertical, 0.75 * 0.9 * style.fontSize)
                    .padding(.horizontal, 0.9 * style.fontSize)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
        case .rule:
            Rectangle().fill(LooreColor.border).frame(height: 1).frame(maxWidth: .infinity)
        case .table(let table):
            TableBlockView(table: table, context: context)
        }
    }
}

enum MarkdownFonts {
    /// The web's `code` stack resolves to Menlo on iOS.
    static func mono(_ size: CGFloat) -> Font { .custom("Menlo", size: size, relativeTo: .body) }
}

// MARK: - Paragraphs and inline text

/// A paragraph; images are placed as their own rows between the text runs.
private struct ParagraphView: View {
    let inlines: [MDInline]
    let context: MarkdownRenderContext
    var softBreakAsSpace: Bool

    var body: some View {
        if inlines.containsImage {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(pieces.enumerated()), id: \.offset) { _, piece in
                    switch piece {
                    case .text(let run):
                        InlineText(inlines: run, context: context, softBreakAsSpace: softBreakAsSpace)
                    case .image(let source, let alt):
                        // Keyed on the source: a tap allows that image only, also
                        // when a streaming reply shifts the images' positions.
                        MarkdownImage(source: source, alt: alt, environment: context.environment)
                            .id(source)
                    }
                }
            }
        } else {
            InlineText(inlines: inlines, context: context, softBreakAsSpace: softBreakAsSpace)
        }
    }

    private enum Piece { case text([MDInline]), image(String, String) }

    private var pieces: [Piece] {
        var out: [Piece] = []
        var run: [MDInline] = []
        for inline in inlines {
            if case .image(let source, let alt) = inline {
                if !run.isEmpty { out.append(.text(run)); run = [] }
                out.append(.image(source, alt))
            } else {
                run.append(inline)
            }
        }
        if !run.isEmpty { out.append(.text(run)) }
        return out
    }
}

/// A markdown image (#441). Loore's own media loads as the view appears. Any
/// other image is a quiet placeholder with its host and alt text; a tap loads
/// that one image (the tap is the user's consent for it).
private struct MarkdownImage: View {
    let source: String
    let alt: String
    let environment: AppEnvironment
    @State private var allowed = false

    var body: some View {
        switch MarkdownImageSource.classify(source, environment: environment) {
        case .own(let url):
            loaded(url)
        case .remote(let url, let host):
            if allowed {
                loaded(url)
            } else {
                Button { allowed = true } label: { placeholder(host) }
                    .buttonStyle(.plain)
                    .accessibilityLabel(placeholderText(host))
                    .accessibilityHint("Loads the image")
            }
        case .none:
            altText
        }
    }

    private func loaded(_ url: URL) -> some View {
        AsyncImage(url: url) { phase in
            switch phase {
            case .success(let image):
                image.resizable().scaledToFit().frame(maxWidth: .infinity, alignment: .leading)
            default:
                altText
            }
        }
        .accessibilityLabel(alt)
    }

    private var altText: some View {
        Text(verbatim: alt).font(LooreFont.meta).foregroundStyle(LooreColor.textMuted)
    }

    private func placeholderText(_ host: String) -> String {
        alt.isEmpty ? "Image from \(host)" : "Image from \(host): \(alt)"
    }

    private func placeholder(_ host: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: "photo").imageScale(.small)
            Text(verbatim: placeholderText(host)).multilineTextAlignment(.leading)
        }
        .font(LooreFont.meta)
        .foregroundStyle(LooreColor.textMuted)
        .padding(.vertical, 5)
        .padding(.horizontal, 10)
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
        .contentShape(Rectangle())
    }
}

/// Inline runs as one `Text` with attributes (bold, italics, strike, code, links).
struct InlineText: View {
    let inlines: [MDInline]
    let context: MarkdownRenderContext
    var font: Font?
    var fontSize: CGFloat?
    var isHeading = false
    var color: Color?
    var softBreakAsSpace = false

    var body: some View {
        let style = context.style
        Text(InlineAttributedBuilder(context: context, softBreakAsSpace: softBreakAsSpace,
                                     baseSize: fontSize ?? style.fontSize, isHeading: isHeading)
                .build(inlines))
            .font(font ?? (context.italic ? LooreFont.sansOblique(style.fontSize, style.weight, relativeTo: style.textStyle)
                                          : style.baseFont))
            .foregroundStyle(color ?? style.color)
            .lineSpacing(isHeading ? 0 : style.lineSpacing)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Builds the `AttributedString` for a run of inlines.
@MainActor
struct InlineAttributedBuilder {
    let context: MarkdownRenderContext
    var softBreakAsSpace: Bool
    var baseSize: CGFloat
    var isHeading: Bool

    struct Traits {
        var bold = false
        var italic = false
        var strike = false
    }

    func build(_ inlines: [MDInline]) -> AttributedString {
        var out = AttributedString()
        for inline in inlines { out += piece(inline, traits: Traits(), link: nil) }
        return out
    }

    private func font(_ traits: Traits) -> Font {
        if isHeading {
            let base = LooreFont.serif(baseSize, traits.bold ? .bold : .semibold, relativeTo: .title3)
            return traits.italic ? base.italic() : base
        }
        let weight: LooreFontFace.SansWeight = traits.bold ? .bold : context.style.weight
        if traits.italic || context.italic {
            return LooreFont.sansOblique(baseSize, weight, relativeTo: context.style.textStyle)
        }
        return LooreFont.sans(baseSize, weight, relativeTo: context.style.textStyle)
    }

    private func styled(_ text: String, _ traits: Traits, link: URL?) -> AttributedString {
        var s = AttributedString(text)
        s.font = font(traits)
        if traits.strike {
            s.strikethroughStyle = .single
            s.foregroundColor = LooreColor.textMuted
        }
        if let link {
            s.link = link
            s.underlineStyle = .single
        }
        return s
    }

    private func piece(_ inline: MDInline, traits: Traits, link: URL?) -> AttributedString {
        switch inline {
        case .text(let text):
            return styled(text, traits, link: link)
        case .softBreak:
            return styled(softBreakAsSpace ? " " : "\n", traits, link: link)
        case .lineBreak:
            return styled("\n", traits, link: link)
        case .code(let code):
            var s = styled(code, traits, link: link)
            s.font = MarkdownFonts.mono(0.9 * baseSize)
            return s
        case .emphasis(let children):
            var t = traits; t.italic = true
            return children.reduce(into: AttributedString()) { $0 += piece($1, traits: t, link: link) }
        case .strong(let children):
            var t = traits; t.bold = true
            return children.reduce(into: AttributedString()) { $0 += piece($1, traits: t, link: link) }
        case .strikethrough(let children):
            var t = traits; t.strike = true
            return children.reduce(into: AttributedString()) { $0 += piece($1, traits: t, link: link) }
        case .image(_, let alt):
            return styled(alt, traits, link: link)
        case .link(let destination, let children):
            return linkPiece(destination: destination, children: children, traits: traits)
        }
    }

    private func linkPiece(destination: String, children: [MDInline], traits: Traits) -> AttributedString {
        var url = URL(string: destination) ?? URL(string: destination.addingPercentEncoding(withAllowedCharacters: .urlFragmentAllowed) ?? "")
        // A link the router would ignore (other apps' schemes, `tel:`, no
        // scheme) is plain text, not a link that does nothing (#442).
        if let target = url, MarkdownLinkTarget.resolve(target.absoluteString, environment: context.environment) == .ignore {
            url = nil
        }
        if let nodeId = NodeLinks.nodeId(destination, currentOrigin: context.origin), Self.isBare(children, href: destination) {
            switch context.titles.record(for: nodeId) {
            case .title(let title)?:
                return styled(title, traits, link: url)
            case .deleted?:
                return unavailable("[Node deleted]", traits)
            case .inaccessible?:
                return unavailable("[Node inaccessible]", traits)
            case .untitled?, nil:
                break
            }
        }
        return children.reduce(into: AttributedString()) { $0 += piece($1, traits: traits, link: url) }
    }

    /// Muted italic, not tappable (the node cannot be opened).
    private func unavailable(_ text: String, _ traits: Traits) -> AttributedString {
        var t = traits
        t.italic = true
        var s = styled(text, t, link: nil)
        s.foregroundColor = LooreColor.textMuted
        return s
    }

    /// The link's visible text is its own URL (a pasted bare URL or `[url](url)`).
    nonisolated static func isBare(_ children: [MDInline], href: String) -> Bool {
        func dropSlash(_ s: String) -> String { s.hasSuffix("/") ? String(s.dropLast()) : s }
        return dropSlash(children.plainText.jsTrimmed) == dropSlash(href)
    }
}

// MARK: - Lists

private struct ListBlockView: View {
    let list: MDList
    let context: MarkdownRenderContext
    let depth: Int

    var body: some View {
        VStack(alignment: .leading, spacing: list.tight ? 2 : max(2, context.style.paragraphSpacing)) {
            ForEach(Array(list.items.enumerated()), id: \.element.id) { index, item in
                if list.isTaskList {
                    TaskItemView(item: item, list: list, context: context, depth: depth)
                } else {
                    HStack(alignment: .firstTextBaseline, spacing: 0) {
                        Text(marker(index))
                            .font(context.style.baseFont)
                            .foregroundStyle(context.style.color)
                            .frame(width: 18, alignment: .trailing)
                            .padding(.trailing, 6)
                            .accessibilityHidden(true)
                        ItemContent(item: item, list: list, context: context, depth: depth)
                    }
                }
            }
        }
        .padding(.leading, list.isTaskList && depth > 0 && isNestedTask ? 1.5 * context.style.fontSize : 0)
    }

    /// Nested task lists indent 1.5em (`ul.contains-task-list ul.contains-task-list`).
    private var isNestedTask: Bool { depth > 0 }

    private func marker(_ index: Int) -> String {
        if list.ordered { return "\(list.start + index)." }
        switch depth % 3 {
        case 0: return "•"
        case 1: return "◦"
        default: return "▪"
        }
    }
}

private struct ItemContent: View {
    let item: MDListItem
    let list: MDList
    let context: MarkdownRenderContext
    let depth: Int

    var body: some View {
        MarkdownBlocks(blocks: item.blocks, context: context, trimOuter: true,
                       tightListItem: list.tight, listDepth: depth + 1)
    }
}

/// A GFM task item: the web's round checkbox, owner-only toggle and "+".
private struct TaskItemView: View {
    let item: MDListItem
    let list: MDList
    let context: MarkdownRenderContext
    let depth: Int
    @FocusState private var inputFocused: Bool

    var body: some View {
        let own = item.blocks.filter { if case .list = $0.kind { return false } else { return true } }
        let nested = item.blocks.filter { if case .list = $0.kind { return true } else { return false } }
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if let checked = item.checked {
                    checkbox(checked)
                }
                MarkdownBlocks(blocks: own, context: context, trimOuter: true,
                               tightListItem: list.tight, listDepth: depth + 1)
                    .layoutPriority(1)
                if let add = context.checklist?.add {
                    Button {
                        context.edit.draft = ""
                        context.edit.addingAfter = item.plainText
                        inputFocused = true
                    } label: {
                        Text("+")
                            .font(LooreFont.sans(1.1 * context.style.fontSize, .regular))
                            .foregroundStyle(LooreColor.accent)
                            .opacity(0.55)
                            .frame(minWidth: 28, minHeight: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Add an item below")
                }
            }
            if !nested.isEmpty {
                MarkdownBlocks(blocks: nested, context: context, trimOuter: true, listDepth: depth + 1)
            }
            if let add = context.checklist?.add, context.edit.addingAfter == item.plainText {
                addInput(add)
            }
        }
    }

    private func checkbox(_ checked: Bool) -> some View {
        let circle = ZStack {
            Circle()
                .strokeBorder(checked ? LooreColor.accentDim : LooreColor.borderHover, lineWidth: 1.5)
                .background(Circle().fill(checked ? LooreColor.accentDim : Color.clear))
            if checked {
                Text("✓")
                    .font(LooreFont.sans(9.6, .semibold))
                    .foregroundStyle(LooreColor.bgDeep)
            }
        }
        .frame(width: 18, height: 18)
        return Group {
            if let toggle = context.checklist?.toggle {
                Button { toggle(item.plainText, checked) } label: {
                    circle.padding(6).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .padding(-6)
                .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + context.style.fontSize * 0.34 }
                .accessibilityElement()
                .accessibilityLabel(item.plainText)
                .accessibilityAddTraits(.isButton)
                .accessibilityValue(checked ? "Checked" : "Not checked")
            } else {
                circle
                    .alignmentGuide(.firstTextBaseline) { d in d[VerticalAlignment.center] + context.style.fontSize * 0.34 }
                    .accessibilityHidden(true)
            }
        }
    }

    private func addInput(_ add: @escaping (String, String) -> Void) -> some View {
        @Bindable var edit = context.edit
        return HStack(spacing: 8) {
            Circle()
                .strokeBorder(LooreColor.borderHover, style: StrokeStyle(lineWidth: 1.5, dash: [3, 2]))
                .frame(width: 18, height: 18)
                .opacity(0.5)
            TextField("", text: $edit.draft, prompt: loorePrompt("New item…"))
                .font(LooreFont.sans(0.92 * context.style.fontSize, .light))
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.vertical, 4)
                .padding(.horizontal, 8)
                .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
                .focused($inputFocused)
                .submitLabel(.done)
                .onSubmit {
                    let text = edit.draft.jsTrimmed
                    if !text.isEmpty { add(item.plainText, text) }
                    edit.addingAfter = nil
                    edit.draft = ""
                }
                .onChange(of: inputFocused) { _, focused in
                    if !focused && edit.draft.jsTrimmed.isEmpty { edit.addingAfter = nil }
                }
                .onAppear { inputFocused = true }
        }
        .padding(.vertical, 4)
    }
}

// MARK: - Tables

private struct TableBlockView: View {
    let table: MDTable
    let context: MarkdownRenderContext

    var body: some View {
        let cellStyle = context.with { $0.style.fontSize *= 0.9 }
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(Array(table.head.enumerated()), id: \.offset) { column, cell in
                        InlineText(inlines: cell, context: cellStyle.with { $0.style.weight = .semibold },
                                   color: context.style.color, softBreakAsSpace: true)
                            .fixedSize()
                            .padding(.vertical, 6)
                            .padding(.horizontal, 10)
                            .gridColumnAlignment(horizontal(column))
                    }
                }
                Rectangle().fill(LooreColor.border).frame(height: 2).gridCellUnsizedAxes(.horizontal)
                ForEach(Array(table.rows.enumerated()), id: \.offset) { rowIndex, row in
                    if rowIndex > 0 {
                        Rectangle().fill(LooreColor.border).frame(height: 1).gridCellUnsizedAxes(.horizontal)
                    }
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { column, cell in
                            InlineText(inlines: cell, context: cellStyle, softBreakAsSpace: true)
                                .frame(minWidth: 40, maxWidth: 320, alignment: alignment(column))
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.vertical, 6)
                                .padding(.horizontal, 10)
                        }
                    }
                }
            }
        }
    }

    private func horizontal(_ column: Int) -> HorizontalAlignment {
        switch alignment(column) {
        case .trailing: return .trailing
        case .center: return .center
        default: return .leading
        }
    }

    private func alignment(_ column: Int) -> Alignment {
        guard column < table.alignments.count, let a = table.alignments[column] else { return .leading }
        switch a {
        case .left: return .leading
        case .center: return .center
        case .right: return .trailing
        }
    }
}
