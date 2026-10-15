import SwiftUI

/// One saved reference, read in full (web `ReferenceDetailPage`, map E §4.2).
/// Option A of the 2026-10-13 mockup (Peter): the gleaning's tweet card at
/// page size, for every kind of reference. A full hairline border and no
/// accent rail, so it never reads as one of the user's own entries. Top to
/// bottom: the source in a plain sentence (with the kebab: Open source / Edit
/// / Delete), the byline, a page's title, the stored text in the reading
/// serif (a video's player, its description behind a toggle), then Listen,
/// the link out, the verdict and the read toggle. No tweet embed: the stored
/// text is the tweet. Under the card, how often Loore showed it.
struct ReferenceDetailView: View {
    let itemId: Int
    /// The reply whose quote card opened the page: the open mark is logged against it.
    var fromNode: Int?

    @Environment(AppState.self) private var app
    @State private var item: ExternalItem?
    @State private var error: String?
    /// A video's description sits under the player behind this toggle.
    @State private var showStored = false
    @State private var marking = false
    @State private var editing = false
    @State private var saving = false
    @State private var pendingEdit: (title: String, content: String)?
    @State private var deleting = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                headingRow
                if let error {
                    Text(error).font(LooreFont.body).foregroundStyle(LooreColor.accent).padding(.top, 20)
                } else if let item {
                    card(item)
                    Text(Self.surfacingLine(item))
                        .font(LooreFont.sans(12.5, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 10)
                        .accessibilityIdentifier("reference.surfacing")
                } else {
                    LoadingLine()
                }
            }
            .padding(.horizontal, 12)
            .padding(.top, 8)
            .padding(.bottom, 48)
            .looreReadableWidth()
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .sheet(isPresented: $editing) {
            if let item {
                ReferenceEditSheet(item: item, saving: saving) { title, content in
                    saveEdit(title: title, content: content, regenerateTts: nil)
                }
                .looreDialog(isPresented: Binding(get: { pendingEdit != nil }, set: { if !$0 { pendingEdit = nil } })) {
                    RegenerateTtsDialog(onChoice: { regenerate in
                        let edit = pendingEdit
                        pendingEdit = nil
                        if let edit { saveEdit(title: edit.title, content: edit.content, regenerateTts: regenerate) }
                    }, onCancel: { pendingEdit = nil })
                }
            }
        }
        .looreDialog(isPresented: $deleting) {
            DeleteConfirmDialog(mode: .reference, onConfirm: { _ in confirmDelete() }, onCancel: { deleting = false })
        }
    }

    private var headingRow: some View {
        HStack(alignment: .center, spacing: 16) {
            Text("Reference")
                .font(LooreFont.serif(28.8, .light, relativeTo: .largeTitle))
                .foregroundStyle(LooreColor.textPrimary)
                .accessibilityAddTraits(.isHeader)
            Spacer()
            Button("All references") {
                if app.router.previousRoute == .references { app.router.pop() } else { app.open(.references) }
            }
            .buttonStyle(.plain)
            .font(LooreFont.sans(13.6, .light))
            .foregroundStyle(LooreColor.textMuted)
        }
        .padding(.bottom, 12)
    }

    /// The web's `max-width: 65ch` for the text at 1.12rem (641 px there).
    static let textMeasure: CGFloat = 640

    private func card(_ item: ExternalItem) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            sourceRow(item)
            byline(item)
            if let title = item.title, !title.isEmpty {
                Text(title)
                    .font(LooreFont.serif(24, .regular, relativeTo: .title2))
                    .foregroundStyle(LooreColor.textPrimary)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
            }
            if let video = item.youtubeVideo {
                // A video's stored text is its description (with the chapter
                // list): under the player, behind the toggle.
                YouTubeEmbedView(videoId: video.id, start: video.start)
                Button(showStored ? "Hide stored text" : "Show stored text") { showStored.toggle() }
                    .buttonStyle(.plain)
                    .font(LooreFont.sans(12.8, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .accessibilityIdentifier("reference.storedToggle")
                if showStored { text(item) }
            } else {
                text(item)
            }
            footer(item)
        }
        .padding(.top, 18)
        .padding(.horizontal, 20)
        .padding(.bottom, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.large))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.borderHover))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("reference.card")
        .padding(.top, 18)
    }

    /// "Tweet from the Community Archive" with a small glyph, and the kebab.
    private func sourceRow(_ item: ExternalItem) -> some View {
        HStack(alignment: .center, spacing: 6) {
            Image(systemName: Self.glyph(item.kind))
                .font(.system(size: 11, weight: .light))
                .accessibilityHidden(true)
            Text(item.sourceLine)
                .font(LooreFont.sans(12.5, .light))
                .accessibilityIdentifier("reference.source")
            Spacer(minLength: 8)
            // The 44 pt tap target overhangs the row instead of making it taller.
            KebabMenu(actions: actions(item))
                .padding(.vertical, -14)
                .padding(.trailing, -8)
        }
        .foregroundStyle(LooreColor.textMuted)
    }

    /// The SF Symbol before the source line (the web's Feather glyphs).
    static func glyph(_ kind: ReferenceUtils.Kind) -> String {
        switch kind {
        case .tweet: return "bubble.left"
        case .video: return "play.circle"
        case .page: return "doc.text"
        }
    }

    /// Who wrote it and when: a tweet's display name and @handle (opening the
    /// account), a page's or video's byline, then the date. The site is
    /// already named in the source line.
    struct Byline: Equatable {
        var name: String?
        var handle: String?
        var date: String?
    }

    static func byline(_ item: ExternalItem) -> Byline {
        func nonEmpty(_ s: String?) -> String? { s.flatMap { $0.isEmpty ? nil : $0 } }
        let date = nonEmpty(LooreDateFormat.dateTime(item.postedAt ?? item.fetchedAt))
        if item.kind == .tweet, let handle = nonEmpty(item.authorHandle) {
            return Byline(name: nonEmpty(item.authorName), handle: "@\(handle)", date: date)
        }
        return Byline(name: nonEmpty(item.authorName) ?? nonEmpty(item.authorHandle), handle: nil, date: date)
    }

    @ViewBuilder
    private func byline(_ item: ExternalItem) -> some View {
        let parts = Self.byline(item)
        let hasAuthor = parts.name != nil || parts.handle != nil
        if hasAuthor || parts.date != nil {
            FlowLayout(spacing: 6, lineSpacing: 2) {
                if hasAuthor {
                    let author = HStack(alignment: .firstTextBaseline, spacing: 6) {
                        if let name = parts.name {
                            Text(name).font(LooreFont.sans(13.6, .regular)).foregroundStyle(LooreColor.textPrimary)
                        }
                        if let handle = parts.handle {
                            Text(handle).font(LooreFont.sans(13.6, .light)).foregroundStyle(LooreColor.textMuted)
                        }
                    }
                    if parts.handle != nil, let handle = item.authorHandle,
                       let url = URL(string: "https://x.com/\(handle)") {
                        Button { app.open(.external(url)) } label: { author }
                            .buttonStyle(.plain)
                            .accessibilityHint("Opens the account on Twitter")
                    } else {
                        author
                    }
                }
                if let date = parts.date {
                    Text(hasAuthor ? "· \(date)" : date)
                        .font(LooreFont.sans(13.6, .light))
                        .foregroundStyle(LooreColor.textMuted)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("reference.byline")
        }
    }

    private func text(_ item: ExternalItem) -> some View {
        MarkdownView(markdown: item.bodyWithoutTitle, style: .referenceText)
            .frame(maxWidth: Self.textMeasure, alignment: .leading)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("reference.body")
    }

    /// Listen and the link out on the left; the verdict and the read toggle
    /// on the right, or on a line of their own when the row is too narrow.
    private func footer(_ item: ExternalItem) -> some View {
        let leading = HStack(spacing: 10) {
            // The speaker's 28 pt frame: its icon in line with the text edge.
            SpeakerButton(target: .item(item.id), content: speakerContent(item),
                          onTtsGenerated: { self.item?.hasTTS = true })
                .padding(.leading, -7.5)
            if let link = item.url, let url = URL(string: link) {
                Button { openOriginal(url) } label: {
                    Text(item.sourceLinkLabel)
                        .font(LooreFont.sans(12.5, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .padding(.vertical, 10)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("reference.openOriginal")
            }
        }
        let trailing = HStack(spacing: 0) {
            // A tweet's verdict is named as on its gleaning pick (#502).
            ReferenceFeedbackControl(itemId: item.id, feedback: item.feedback, size: 18,
                                     labels: item.kind == .tweet
                                         ? ReferenceFeedbackControl.tweetLabels
                                         : ReferenceFeedbackControl.quoteLabels) { verdict, readAt in
                self.item?.feedback = verdict
                if let readAt { self.item?.readAt = readAt }
            }
            Button(item.readAt != nil ? "Mark as unread" : "Mark as read") { setRead(item.readAt == nil) }
                .buttonStyle(.plain)
                .font(LooreFont.sans(12.8, .regular))
                .foregroundStyle(item.readAt != nil ? LooreColor.textMuted : LooreColor.accent)
                .frame(minHeight: 40)
                .contentShape(Rectangle())
                .disabled(marking)
                .accessibilityIdentifier("reference.readToggle")
        }
        return ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                leading
                Spacer(minLength: 0)
                trailing
            }
            VStack(alignment: .leading, spacing: 0) {
                leading
                // The ⊕ glyph, not its 40 pt target, in line with the text edge.
                trailing.padding(.leading, -11)
            }
        }
        .padding(.top, 4)
        .overlay(alignment: .top) { HairlineDivider() }
    }

    private func speakerContent(_ item: ExternalItem) -> String {
        if let title = item.title, !title.isEmpty { return "# \(title)\n\(item.content ?? "")" }
        return item.content ?? ""
    }

    /// "Shown by Loore N× · last {date}" or "Not yet shown…", then the read and edit dates.
    static func surfacingLine(_ item: ExternalItem) -> String {
        var text = item.surfacedCount > 0
            ? "Shown by Loore \(item.surfacedCount)× · last \(LooreDateFormat.date(item.lastSurfacedAt))"
            : "Not yet shown by Loore in a conversation"
        if let read = item.readAt { text += " · you read it \(LooreDateFormat.date(read))" }
        if let edited = item.editedAt { text += " · edited \(LooreDateFormat.date(edited))" }
        return text
    }

    private func actions(_ item: ExternalItem) -> [BubbleAction] {
        var list: [BubbleAction] = []
        if let link = item.url, let url = URL(string: link) {
            list.append(BubbleAction(label: "Open source") { app.open(.external(url)) })
        }
        list.append(BubbleAction(label: "Edit") { editing = true })
        list.append(BubbleAction(label: "Delete", destructive: true) { deleting = true })
        return list
    }

    // MARK: Actions

    private func load() async {
        guard item == nil else { return }
        do {
            var loaded: ExternalItem = try await app.api.get(APIPath.externalItem(itemId))
            // Opening the page is reading it (Peter, 2026-10-14), as on the web:
            // once per open (`.task` runs once per appearance), before the card
            // shows, so it never flashes "Mark as read". A failed mark leaves the
            // reference as loaded.
            if let marked = try? await Self.markOpened(itemId: loaded.id, fromNode: fromNode, api: app.api) {
                loaded.readAt = marked
                app.signals.post(.referencesChanged)
            }
            item = loaded
        } catch let failure as APIError {
            error = failure.isNotFound ? "This reference does not exist or was deleted." : "Error loading reference."
        } catch {
            self.error = "Error loading reference."
        }
    }

    /// `POST /external/items/<id>/read {via: 'open', node_id?}`: logs the open and
    /// marks an unread reference read (a read one keeps its date). Returns `read_at`.
    static func markOpened(itemId: Int, fromNode: Int?, api: APIClient) async throws -> Date? {
        var body: [String: JSONValue] = ["via": .string("open")]
        if let fromNode { body["node_id"] = .int(fromNode) }
        let answer: ReadMarkAnswer = try await api.post(APIPath.externalItemRead(itemId), json: .object(body))
        return answer.readAt
    }

    /// Opening the original is reading it, as on the gleaning card: the open
    /// is logged and an unread reference is marked read (a read one keeps its date).
    private func openOriginal(_ url: URL) {
        app.open(.external(url))
        if !marking { setRead(true, via: "open") }
    }

    /// Only the user marks a reference read (opening this page does, in `load`).
    private func setRead(_ want: Bool, via: String? = nil) {
        guard let current = item else { return }
        marking = true
        Task {
            defer { marking = false }
            do {
                let path = APIPath.externalItemRead(current.id)
                let answer: ReadMarkAnswer
                if !want {
                    answer = try await app.api.delete(path)
                } else if let via {
                    answer = try await app.api.post(path, json: .object(["via": .string(via)]))
                } else {
                    answer = try await app.api.post(path)
                }
                item?.readAt = answer.readAt
                app.signals.post(.referencesChanged)
            } catch {
                app.toasts.show("Could not update the read mark.", duration: 4)
            }
        }
    }

    private func saveEdit(title: String, content: String, regenerateTts: Bool?) {
        guard let current = item else { return }
        let textChanged = content.jsTrimmed != (current.content ?? "").jsTrimmed
        if current.hasTTS && textChanged && regenerateTts == nil {
            pendingEdit = (title, content)
            return
        }
        saving = true
        var body: [String: JSONValue] = ["title": .string(title), "content": .string(content)]
        if regenerateTts == true { body["regenerate_tts"] = .bool(true) }
        Task {
            defer { saving = false }
            do {
                let updated: ExternalItem = try await app.api.put(APIPath.externalItem(current.id), json: .object(body))
                item = updated
                editing = false
                app.toasts.show("Reference updated", duration: 3)
                app.signals.post(.referencesChanged)
            } catch {
                app.toasts.show((error as? APIError)?.userMessage(fallback: "Error saving reference.")
                                ?? "Error saving reference.", duration: 4)
            }
        }
    }

    private func confirmDelete() {
        guard let current = item else { return }
        Task {
            defer { deleting = false }
            do {
                let _: DeleteReferenceAnswer = try await app.api.delete(APIPath.externalItem(current.id))
                app.toasts.show("Reference deleted", duration: 3)
                app.signals.post(.referencesChanged)
                if app.router.previousRoute == .references { app.router.pop() } else { app.router.replaceTop(with: .references) }
            } catch {
                app.toasts.show((error as? APIError)?.userMessage(fallback: "Error deleting reference.")
                                ?? "Error deleting reference.", duration: 4)
            }
        }
    }
}

/// The edit sheet's Save (accent fill) and Cancel (outline).
private struct SheetButtonStyle: ButtonStyle {
    let filled: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(LooreFont.sans(13.1, .regular))
            .foregroundStyle(filled ? LooreColor.bgDeep : LooreColor.textMuted)
            .padding(.vertical, 6)
            .padding(.horizontal, 14)
            .background(filled ? LooreColor.accent : Color.clear, in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control)
                .strokeBorder(filled ? LooreColor.accent : LooreColor.border))
            .opacity(configuration.isPressed ? 0.8 : 1)
            .frame(minHeight: 40)
            .contentShape(Rectangle())
    }
}

/// "Edit Reference" (web `NodeFormModal` + `ReferenceEditForm`).
private struct ReferenceEditSheet: View {
    let item: ExternalItem
    let saving: Bool
    let onSave: (String, String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var content = ""
    @FocusState private var contentFocused: Bool

    private var overCap: Bool { content.jsLength > nodeCharCap }
    private var changed: Bool {
        title.jsTrimmed != (item.title ?? "") || content.jsTrimmed != (item.content ?? "").jsTrimmed
    }
    private var canSave: Bool { !saving && !overCap && !content.jsTrimmed.isEmpty && changed }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    TextField("", text: $title, prompt: loorePrompt("Title"))
                        .font(LooreFont.serif(20.8, .regular, relativeTo: .title3))
                        .foregroundStyle(LooreColor.textPrimary)
                        .padding(.vertical, 10)
                        .padding(.horizontal, 12)
                        .background(LooreColor.bgDeep, in: RoundedRectangle(cornerRadius: LooreRadius.control))
                        .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
                        .onChange(of: title) { _, new in if new.jsLength > 512 { title = new.jsPrefix(512) } }
                        .accessibilityIdentifier("referenceEdit.title")
                    TextEditor(text: $content)
                        .accessibilityLabel("Reference text")
                        .font(LooreFont.sans(15.2, .light))
                        .foregroundStyle(LooreColor.textPrimary)
                        .lineSpacing(5)
                        .scrollContentBackground(.hidden)
                        .padding(8)
                        .frame(minHeight: 260)
                        .background(LooreColor.bgDeep, in: RoundedRectangle(cornerRadius: LooreRadius.control))
                        .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
                        .focused($contentFocused)
                        .accessibilityIdentifier("referenceEdit.content")
                    ViewThatFits(in: .horizontal) {
                        HStack { hint; Spacer(); buttons }
                        VStack(alignment: .leading, spacing: 8) { hint; HStack { Spacer(); buttons } }
                    }
                }
                .padding(LooreSpacing.md)
            }
            .background(LooreColor.bgCard)
            .navigationTitle("Edit Reference")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationBackground(LooreColor.bgCard)
        .onAppear {
            title = item.title ?? ""
            content = item.content ?? ""
            contentFocused = true
        }
    }

    private var hint: some View {
        Text(overCap ? "\(jsLocaleNumber(content.jsLength)) characters — the limit is \(jsLocaleNumber(nodeCharCap))."
                     : "⌘↵ to save · Esc to cancel")
            .font(LooreFont.sans(12.5, .light))
            .foregroundStyle(overCap ? LooreColor.accent : LooreColor.textMuted)
    }

    private var buttons: some View {
        HStack(spacing: 8) {
            Button("Cancel") { dismiss() }
                .buttonStyle(SheetButtonStyle(filled: false))
                .disabled(saving)
                .keyboardShortcut(.cancelAction)
            Button(saving ? "Saving…" : "Save") { onSave(title.jsTrimmed, content) }
                .buttonStyle(SheetButtonStyle(filled: true))
                .opacity(canSave ? 1 : 0.5)
                .disabled(!canSave)
                .keyboardShortcut(.return, modifiers: .command)
                .accessibilityIdentifier("referenceEdit.save")
        }
    }
}

extension MarkdownStyle {
    /// Secondary sans body text (sans .95rem 300, secondary, line-height 1.7): Commons posts.
    static let referenceBody = MarkdownStyle(fontSize: 15.2, weight: .light, color: LooreColor.textSecondary, lineHeight: 1.7)
    /// A reference's stored text on its page: the reading serif (the web's
    /// Newsreader 1.12rem; New York's larger x-height reads the same at 17 pt),
    /// primary, line-height 1.62. The paragraph margin is 0.9em, not the web's
    /// 0.5em: CSS line-height also adds half-leading at a paragraph's edges,
    /// which SwiftUI's line spacing does not.
    static let referenceText = MarkdownStyle(fontSize: 17, weight: .regular, color: LooreColor.textPrimary,
                                             lineHeight: 1.62, paragraphMargin: 0.9, face: .readingSerif)
}
