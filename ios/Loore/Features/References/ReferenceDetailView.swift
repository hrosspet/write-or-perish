import SwiftUI

/// One saved reference, read in full (web `ReferenceDetailPage`, map E §4.2):
/// title with listen-aloud, tweet / YouTube embed or the stored markdown,
/// footer and tags, the surfacing line, feedback, the read toggle, and
/// Open source / Edit / Delete.
struct ReferenceDetailView: View {
    let itemId: Int

    @Environment(AppState.self) private var app
    @Environment(\.colorScheme) private var colorScheme
    @State private var item: ExternalItem?
    @State private var error: String?
    @State private var embedStatus = "loading"
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

    private func card(_ item: ExternalItem) -> some View {
        let video = item.youtubeVideo
        let embedShown = video != nil || (item.tweetId != nil && embedStatus == "shown")
        return HStack(alignment: .top, spacing: 4) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .center, spacing: 2) {
                    if let title = item.title, !title.isEmpty {
                        Text(title)
                            .font(LooreFont.serif(24, .regular, relativeTo: .title2))
                            .foregroundStyle(LooreColor.textPrimary)
                            .lineSpacing(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    SpeakerButton(target: .item(item.id), content: speakerContent(item),
                                  onTtsGenerated: { self.item?.hasTTS = true })
                }
                .padding(.bottom, 12.8)

                if let tweet = item.tweetId {
                    TweetEmbedView(tweetId: tweet, dark: colorScheme == .dark) { embedStatus = $0 }
                }
                if let video {
                    YouTubeEmbedView(videoId: video.id, start: video.start)
                }
                if embedShown {
                    VStack(alignment: .leading, spacing: 8) {
                        Button(showStored ? "Hide stored text" : "Show stored text") { showStored.toggle() }
                            .buttonStyle(.plain)
                            .font(LooreFont.sans(12.8, .light))
                            .foregroundStyle(LooreColor.textMuted)
                            .accessibilityIdentifier("reference.storedToggle")
                        if showStored {
                            MarkdownView(markdown: item.content ?? "", style: .profile)
                        }
                    }
                    .padding(.top, 10)
                } else {
                    MarkdownView(markdown: item.bodyWithoutTitle, style: .referenceBody)
                        .accessibilityElement(children: .contain)
            .accessibilityIdentifier("reference.body")
                }

                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .center, spacing: 8) {
                        ReferenceFooter(item: item).fixedSize()
                        Spacer(minLength: 0)
                        tags(item).fixedSize()
                    }
                    VStack(alignment: .leading, spacing: 0) {
                        ReferenceFooter(item: item)
                        tags(item)
                    }
                }

                VStack(alignment: .leading, spacing: 8) {
                    Text(Self.surfacingLine(item))
                        .font(LooreFont.sans(12.5, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 0) {
                        ReferenceFeedbackControl(itemId: item.id, feedback: item.feedback, size: 18) { verdict, readAt in
                            self.item?.feedback = verdict
                            if let readAt { self.item?.readAt = readAt }
                        }
                        Button(item.readAt != nil ? "Mark as unread" : "Mark as read") { toggleRead() }
                            .buttonStyle(ReadToggleStyle(read: item.readAt != nil))
                            .disabled(marking)
                            .accessibilityIdentifier("reference.readToggle")
                    }
                }
                .padding(.top, 12)
                .overlay(alignment: .top) { HairlineDivider() }
                .padding(.top, 14)
            }
            KebabMenu(actions: actions(item))
        }
        .padding(.vertical, 28.8)
        .padding(.leading, 32)
        .padding(.trailing, 12)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.card))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.card).strokeBorder(LooreColor.border))
        .overlay(alignment: .leading) {
            UnevenRoundedRectangle(topLeadingRadius: LooreRadius.card, bottomLeadingRadius: LooreRadius.card)
                .fill(LooreColor.accent).frame(width: 3)
        }
        .padding(.top, 18)
    }

    private func tags(_ item: ExternalItem) -> some View {
        HStack(spacing: 8) {
            if item.readAt != nil { LooreTag(text: "Read") }
            LooreTag(text: item.sourceLabel)
        }
        .padding(.top, 12)
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
            item = try await app.api.get(APIPath.externalItem(itemId))
        } catch let failure as APIError {
            error = failure.isNotFound ? "This reference does not exist or was deleted." : "Error loading reference."
        } catch {
            self.error = "Error loading reference."
        }
    }

    private func toggleRead() {
        guard let current = item else { return }
        marking = true
        Task {
            defer { marking = false }
            do {
                let path = APIPath.externalItemRead(current.id)
                let answer: ReadMarkAnswer = current.readAt != nil
                    ? try await app.api.delete(path)
                    : try await app.api.post(path)
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

/// "Mark as read" (accent fill) / "Mark as unread" (outline).
private struct ReadToggleStyle: ButtonStyle {
    let read: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(LooreFont.sans(13.1, .regular))
            .foregroundStyle(read ? LooreColor.textMuted : LooreColor.bgDeep)
            .padding(.vertical, 6)
            .padding(.horizontal, 14)
            .background(read ? Color.clear : LooreColor.accent, in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control)
                .strokeBorder(read ? LooreColor.border : LooreColor.accent))
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
                .buttonStyle(ReadToggleStyle(read: true))
                .disabled(saving)
                .keyboardShortcut(.cancelAction)
            Button(saving ? "Saving…" : "Save") { onSave(title.jsTrimmed, content) }
                .buttonStyle(ReadToggleStyle(read: false))
                .opacity(canSave ? 1 : 0.5)
                .disabled(!canSave)
                .keyboardShortcut(.return, modifiers: .command)
                .accessibilityIdentifier("referenceEdit.save")
        }
    }
}

extension MarkdownStyle {
    /// The reference body (sans .95rem 300, secondary, line-height 1.7).
    static let referenceBody = MarkdownStyle(fontSize: 15.2, weight: .light, color: LooreColor.textSecondary, lineHeight: 1.7)
}
