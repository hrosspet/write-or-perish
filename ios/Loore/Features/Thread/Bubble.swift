import SwiftUI

/// The collapsed card text rules of the web's `Bubble` (`splitPreview` and
/// the thread-name rule), pinned by `Bubble.test.js`.
enum BubblePreview {
    struct Split: Equatable {
        var title: String
        var body: String
        var isHeading: Bool
    }

    /// First line (a leading `# ` stripped) and the rest, trimmed.
    static func split(_ text: String?) -> Split {
        let raw = text ?? ""
        let isHeading = JSRegex.test(raw, #"^#\s+"#)
        let display = JSRegex.replaceFirst(raw, #"^#\s+"#, "")
        let newline = display.jsIndexOf("\n")
        return Split(
            title: newline > 0 ? display.jsSubstring(0, newline) : display,
            body: newline > 0 ? display.jsSubstring(newline + 1).jsTrimmed : "",
            isHeading: isHeading)
    }

    /// Title and body a card shows. A thread name replaces the title; the
    /// entry's first line is dropped only when it was a heading.
    static func display(text: String?, threadName: String?) -> (heading: String, body: String) {
        let parts = split(text)
        let name = (threadName ?? "").jsTrimmed
        let heading = name.isEmpty ? parts.title : name
        let body = !name.isEmpty && !parts.isHeading ? (text ?? "").jsTrimmed : parts.body
        return (heading, body)
    }

    /// Whether the collapsed card hides something (title > 120, body > 250, > 2 lines).
    static func canExpand(content: String?, text: String?) -> Bool {
        guard content != nil else { return false }
        let parts = split(text)
        return parts.title.jsLength > 120 || parts.body.jsLength > 250 || parts.body.jsLines.count > 2
    }

    /// Prompt tags: both read prompts are "Read"; other keys capitalised, `_` → space.
    static func promptLabel(_ key: String) -> String {
        if key == "read" || key == "read_thread" { return "Read" }
        guard let first = key.first else { return key }
        return (String(first).uppercased() + key.dropFirst()).replacingOccurrences(of: "_", with: " ")
    }
}

/// What a card shows, from any of the node shapes (ancestor, child, Log card).
struct BubbleData: Identifiable, Equatable {
    var id: Int
    var content: String?
    var preview: String?
    var threadName: String?
    var deleted = false
    var inaccessible = false
    var username: String = ""
    var llmModel: String?
    var humanOwnerUsername: String?
    var origin: String?
    var createdAt: Date?
    var childCount = 0
    var pinned = false
    var promptKey: String?
    var hasOriginalAudio = false
    var isPublic = false

    var text: String? { content ?? preview }
    var isPlaceholder: Bool { deleted || inaccessible }
}

extension BubbleData {
    init(_ n: AncestorNode) {
        self.init(id: n.id, content: n.content, preview: n.preview, deleted: n.deleted,
                  username: n.username ?? "", llmModel: n.llmModel, createdAt: n.createdAt,
                  childCount: n.childCount, promptKey: n.systemPrompt.promptKey,
                  isPublic: n.privacyLevel == .public)
    }

    init(_ n: TreeNode) {
        self.init(id: n.id, content: n.content, deleted: n.deleted, inaccessible: n.inaccessible,
                  username: n.username ?? "", llmModel: n.llmModel, origin: n.origin, createdAt: n.createdAt,
                  childCount: n.childCount, promptKey: n.systemPrompt.promptKey,
                  isPublic: n.privacyLevel == .public)
    }

    init(_ card: LogCard) {
        self.init(id: card.id, preview: card.preview, threadName: card.threadName, username: card.username,
                  llmModel: card.llmModel, humanOwnerUsername: card.humanOwnerUsername, origin: card.origin,
                  createdAt: card.createdAt, childCount: card.childCount, pinned: card.pinnedAt != nil,
                  promptKey: card.promptKey, hasOriginalAudio: card.hasOriginalAudio)
    }
}

/// One kebab menu entry (web `buildActions`).
struct BubbleAction: Identifiable {
    enum Kind { case reply, other }
    var id: String { label }
    var label: String
    var kind: Kind = .other
    var destructive = false
    var action: () -> Void
}

/// Non-focal card (web `Bubble`): ancestors, children, Log cards.
struct BubbleView: View {
    let data: BubbleData
    var actions: [BubbleAction] = []
    /// A caller's tag shown before the built-in ones (web `tag` prop; References).
    var tag: String?
    /// Replaces `NodeFooter` (web `footer` prop; References pass `ReferenceFooter`).
    var footerOverride: AnyView?
    var onOpen: (() -> Void)?
    @State private var expanded = false

    var body: some View {
        HStack(alignment: .center, spacing: 4) {
            card
            if !data.isPlaceholder && !actions.isEmpty {
                KebabMenu(actions: actions)
            } else {
                Color.clear.frame(width: KebabMenu.width)
            }
        }
    }

    private var card: some View {
        let preview = BubblePreview.display(text: data.text, threadName: data.threadName)
        let heading = preview.heading.jsLength > 120 ? preview.heading.jsPrefix(120) + "..." : preview.heading
        let body = preview.body.jsLength > 250 ? preview.body.jsPrefix(250) + "..." : preview.body
        return VStack(alignment: .leading, spacing: 0) {
            if data.deleted {
                placeholderLine("[Node deleted]")
            } else if data.inaccessible {
                placeholderLine("[Node inaccessible]")
            } else if expanded, let content = data.content {
                MarkdownView(markdown: content, style: .bubble)
                    .padding(.bottom, 9.6)
            } else {
                Text(heading)
                    .font(LooreFont.sans(16, .regular))
                    .foregroundStyle(LooreColor.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.bottom, body.isEmpty ? 0 : 9.6)
                if !body.isEmpty {
                    Text(body)
                        .font(LooreFont.sans(14.7, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(6)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if !data.inaccessible {
                footerRow
            }
        }
        .padding(.vertical, LooreSpacing.cardVertical)
        .padding(.horizontal, LooreSpacing.cardHorizontal)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.card))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.card).strokeBorder(LooreColor.border))
        .contentShape(RoundedRectangle(cornerRadius: LooreRadius.card))
        .onTapGesture {
            guard !data.isPlaceholder else { return }
            onOpen?()
        }
        .accessibilityElement(children: .contain)
        .accessibilityAddTraits(data.isPlaceholder ? [] : .isButton)
    }

    private func placeholderLine(_ text: String) -> some View {
        Text(text)
            .font(LooreFont.sansOblique(15.2, .regular))
            .foregroundStyle(LooreColor.textMuted)
            .padding(.bottom, 9.6)
    }

    @ViewBuilder private var footer: some View {
        if let footerOverride {
            footerOverride
        } else {
            nodeFooter
        }
    }

    private var nodeFooter: some View {
        NodeFooterView(
            username: data.username, createdAt: data.createdAt, childCount: data.childCount,
            humanOwnerUsername: data.humanOwnerUsername, llmModel: data.llmModel, origin: data.origin,
            isPublic: data.isPublic,
            onReply: data.isPlaceholder ? nil : actions.first(where: { $0.kind == .reply })?.action)
    }

    /// Footer left, tags right; on a narrow card the tags drop below.
    private var footerRow: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 8) {
                footer.fixedSize()
                Spacer(minLength: 0)
                tags.fixedSize()
            }
            VStack(alignment: .leading, spacing: 0) {
                footer
                HStack {
                    Spacer(minLength: 0)
                    tags
                }
            }
        }
    }

    @ViewBuilder private var tags: some View {
        if !data.isPlaceholder {
            HStack(spacing: 6) {
                if let tag { LooreTag(text: tag) }
                if data.pinned { LooreTag(text: "Pinned") }
                if let key = data.promptKey {
                    LooreTag(text: BubblePreview.promptLabel(key))
                } else if data.hasOriginalAudio {
                    LooreTag(text: "Voice Note")
                }
                if BubblePreview.canExpand(content: data.content, text: data.text) {
                    Button {
                        withAnimation(LooreMotion.quick) { expanded.toggle() }
                    } label: {
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(LooreColor.textMuted)
                            .frame(width: 32, height: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(expanded ? "Collapse preview" : "Expand preview")
                }
            }
            .padding(.top, 12)
        }
    }
}

/// The ⋮ button beside a card (always visible on touch, as the web does on
/// `hover: none` devices).
struct KebabMenu: View {
    static let width: CGFloat = 26
    let actions: [BubbleAction]

    var body: some View {
        Menu {
            ForEach(actions) { item in
                Button(role: item.destructive ? .destructive : nil, action: item.action) {
                    Text(item.label)
                }
            }
        } label: {
            Image(systemName: "ellipsis")
                .rotationEffect(.degrees(90))
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(LooreColor.textMuted)
                .frame(width: Self.width, height: 44)
                .contentShape(Rectangle())
        }
        .accessibilityLabel("More actions")
    }
}

/// Author · via origin · date · replies (web `NodeFooter`), plus trailing extras.
struct NodeFooterView<Extras: View>: View {
    let username: String
    let createdAt: Date?
    let childCount: Int
    var humanOwnerUsername: String?
    var llmModel: String?
    var origin: String?
    var isPublic = false
    var onReply: (() -> Void)?
    @ViewBuilder var extras: () -> Extras

    @Environment(AppState.self) private var app

    var body: some View {
        HStack(spacing: 6) {
            Button(action: openAuthor) {
                Text(displayName).lineLimit(1)
            }
            .buttonStyle(.plain)
            if let origin, !origin.isEmpty {
                dot
                Text("via \(origin)").lineLimit(1)
                    .accessibilityLabel("Imported from \(origin)")
            }
            dot
            Text(LooreDateFormat.dateTime(createdAt)).lineLimit(1).fixedSize()
            dot
            if let onReply {
                Button(action: onReply) { replyIcon }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Reply")
            } else {
                replyIcon
            }
            if Extras.self != EmptyView.self {
                dot
                HStack(spacing: 10) { extras() }
            }
        }
        .font(LooreFont.meta)
        .foregroundStyle(LooreColor.textMuted)
        .padding(.top, 12)
    }

    private var dot: some View {
        Text("·").foregroundStyle(LooreColor.border).accessibilityHidden(true)
    }

    private var replyIcon: some View {
        HStack(spacing: 4) {
            Image(systemName: "ellipsis.bubble")
                .font(.system(size: 12, weight: .regular))
            if childCount > 0 { Text("\(childCount)") }
        }
        .frame(minHeight: 28)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel(childCount == 1 ? "1 reply" : "\(childCount) replies")
    }

    /// Model id for AI replies; "model · via human" on public nodes.
    private var displayName: String {
        if let llmModel, !llmModel.isEmpty {
            if isPublic, let human = humanOwnerUsername, !human.isEmpty { return "\(llmModel) · via \(human)" }
            return llmModel
        }
        return username
    }

    private func openAuthor() {
        let who = (humanOwnerUsername?.isEmpty == false ? humanOwnerUsername : nil) ?? username
        if isPublic || app.user?.username != who {
            app.open(.webPage(path: "/@\(who)"))
        } else {
            app.open(.profile)
        }
    }
}

extension NodeFooterView where Extras == EmptyView {
    init(username: String, createdAt: Date?, childCount: Int, humanOwnerUsername: String? = nil,
         llmModel: String? = nil, origin: String? = nil, isPublic: Bool = false, onReply: (() -> Void)? = nil) {
        self.init(username: username, createdAt: createdAt, childCount: childCount,
                  humanOwnerUsername: humanOwnerUsername, llmModel: llmModel, origin: origin,
                  isPublic: isPublic, onReply: onReply, extras: { EmptyView() })
    }
}
