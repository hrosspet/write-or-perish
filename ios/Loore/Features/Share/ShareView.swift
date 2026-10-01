import SwiftUI

/// Drafts of writing to give outward (web `SharePage`, map E §8.1). Nothing is
/// public until "Publish to Loore" in the inline confirmation, and publishing
/// can be revoked. Behind `share_v1_enabled`.
struct ShareView: View {
    @Environment(AppState.self) private var app
    @State private var shares: [ShareItem] = []
    @State private var loading = true
    /// nil, a share id, or -1 for a new share.
    @State private var editingId: Int?
    @State private var editContent = ""
    @State private var editType = "other"
    @State private var saving = false
    @State private var confirmPublishId: Int?
    @State private var confirmDeleteId: Int?

    static let shareTypes = ["need", "offering", "insight", "exploration", "intention", "other"]
    private static let newId = -1

    var body: some View {
        Group {
            if !app.capabilities.shareEnabled {
                Text("Not available.")
                    .font(LooreFont.body)
                    .foregroundStyle(LooreColor.textMuted)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                page
            }
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
    }

    private var page: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                Text("Pieces of your writing worth giving outward — nothing is visible to anyone until you publish it, and you can take anything back.")
                    .font(LooreFont.sans(12, .light))
                    .foregroundStyle(LooreColor.textMuted.opacity(0.7))
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 8)
                Rectangle().fill(LooreColor.accentDim.opacity(0.3)).frame(height: 1).padding(.vertical, 24)
                if editingId == Self.newId { editForm.padding(.bottom, 32) }
                if !loading && shares.isEmpty && editingId == nil { emptyState }
                ForEach(groups, id: \.title) { group in
                    if !group.items.isEmpty {
                        VStack(alignment: .leading, spacing: 0) {
                            Text(group.title.uppercased())
                                .font(LooreFont.sans(10.9, .medium))
                                .tracking(1.96)
                                .foregroundStyle(LooreColor.accent.opacity(0.6))
                                .padding(.bottom, 19)
                            ForEach(group.items) { share in
                                if editingId == share.id {
                                    editForm.padding(.bottom, 32)
                                } else {
                                    card(share)
                                }
                            }
                        }
                        .padding(.bottom, 40)
                    }
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(800)
        }
        .scrollDismissesKeyboard(.interactively)
        .refreshable { await fetch() }
        .task { await fetch() }
    }

    private var groups: [(title: String, items: [ShareItem])] {
        [("Published", shares.filter { $0.status == "published" }),
         ("Drafts", shares.filter { $0.status == "draft" }),
         ("Revoked", shares.filter { $0.status == "revoked" })]
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 16) {
            Text("Share")
                .font(LooreFont.pageTitle)
                .foregroundStyle(LooreColor.textPrimary)
                .accessibilityAddTraits(.isHeader)
            if editingId == nil {
                pillButton("+") { openNew() }
                    .accessibilityLabel("Write a new share")
                    .accessibilityIdentifier("share.new")
            }
            Spacer()
            Button("Commons →") { app.open(.commons) }
                .buttonStyle(.plain)
                .font(LooreFont.sans(13.6, .light))
                .foregroundStyle(LooreColor.textMuted)
        }
        .padding(.bottom, 8)
    }

    private var emptyState: some View {
        VStack(spacing: 16) {
            Text("Shares the AI proposes in conversation land here as private drafts — or start one yourself.")
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textMuted)
                .multilineTextAlignment(.center)
            pillButton("+ new share") { openNew() }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }

    private func pillButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(13.6, .light))
                .foregroundStyle(LooreColor.textMuted)
                .padding(.vertical, 6)
                .padding(.horizontal, 14)
                .overlay(Capsule().strokeBorder(LooreColor.border))
                .frame(minHeight: 36)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var editForm: some View {
        VStack(alignment: .leading, spacing: 12) {
            DocEditor(text: $editContent, minHeight: 160, placeholder: "What would you like to give outward? (markdown)",
                      identifier: "share.editor")
            HStack(spacing: 8) {
                Menu {
                    ForEach(Self.shareTypes, id: \.self) { type in
                        Button { editType = type } label: {
                            if type == editType { Label(type, systemImage: "checkmark") } else { Text(type) }
                        }
                    }
                } label: {
                    HStack(spacing: 6) {
                        Text(editType)
                        Image(systemName: "chevron.up.chevron.down").font(.system(size: 10))
                    }
                    .font(LooreFont.sans(13.6, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                    .padding(.vertical, 8)
                    .padding(.horizontal, 12)
                    .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.control))
                    .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
                }
                .accessibilityLabel("Share type")
                DocEditButtons(saving: saving, saveDisabled: editContent.jsTrimmed.isEmpty, onSave: save, onCancel: closeEdit)
                    .padding(.top, -12)
            }
        }
    }

    private func card(_ share: ShareItem) -> some View {
        let linksToThread = share.status == "published" && share.publicNodeId != nil
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Text(share.shareType.uppercased())
                    .font(LooreFont.sans(10.4, .light))
                    .tracking(0.8)
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.vertical, 2)
                    .padding(.horizontal, 8)
                    .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(LooreColor.border))
                Text(dateLabel(share))
                    .font(LooreFont.sans(11.2, .light))
                    .foregroundStyle(LooreColor.textMuted.opacity(0.7))
            }
            .padding(.bottom, 8)
            MarkdownView(markdown: share.content, style: .share)
            actions(share)
                .padding(.top, 14)
        }
        .padding(.vertical, 24)
        .padding(.horizontal, 28)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.large))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.border))
        .opacity(share.status == "revoked" ? 0.7 : 1)
        .contentShape(Rectangle())
        .onTapGesture {
            guard linksToThread, let nodeId = share.publicNodeId else { return }
            app.open(.thread(id: nodeId, awaitLLM: nil))
        }
        .padding(.bottom, 16)
        .accessibilityElement(children: .contain)
            .accessibilityIdentifier("share.card.\(share.id)")
    }

    @ViewBuilder private func actions(_ share: ShareItem) -> some View {
        if confirmPublishId == share.id {
            VStack(alignment: .leading, spacing: 10) {
                publishQuestion
                FlowLayout(spacing: 12, lineSpacing: 8) {
                    Button("Publish to Loore") { publish(share.id) }
                        .font(LooreFont.sans(13.1, .regular))
                        .foregroundStyle(LooreColor.bgDeep)
                        .padding(.vertical, 7)
                        .padding(.horizontal, 16)
                        .background(LooreColor.accent, in: RoundedRectangle(cornerRadius: LooreRadius.control))
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("share.publishConfirm")
                    ForEach(["Twitter / X", "Substack"], id: \.self) { channel in
                        Text("\(channel) · coming soon")
                            .font(LooreFont.sans(13.1, .light))
                            .foregroundStyle(LooreColor.textMuted)
                            .padding(.vertical, 7)
                            .padding(.horizontal, 16)
                            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control)
                                .strokeBorder(LooreColor.border, style: StrokeStyle(lineWidth: 1, dash: [3, 2])))
                            .accessibilityAddTraits(.isStaticText)
                    }
                    quietAction("Cancel") { confirmPublishId = nil }
                }
            }
        } else if confirmDeleteId == share.id {
            HStack(spacing: 14) {
                Text("Delete this share?")
                    .font(LooreFont.sans(12.8, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                quietAction("Delete", color: LooreColor.error) { delete(share.id) }
                    .accessibilityIdentifier("share.deleteConfirm")
                quietAction("Cancel") { confirmDeleteId = nil }
            }
        } else {
            HStack(spacing: 16) {
                if share.status != "published" {
                    quietAction("Edit") { openEdit(share) }
                    quietAction("Publish") {
                        confirmDeleteId = nil
                        confirmPublishId = share.id
                    }
                    .accessibilityIdentifier("share.publish.\(share.id)")
                }
                if share.status == "published" {
                    quietAction("Revoke") { revoke(share.id) }
                }
                quietAction("Delete") {
                    confirmPublishId = nil
                    confirmDeleteId = share.id
                }
                .accessibilityIdentifier("share.delete.\(share.id)")
            }
        }
    }

    private var publishQuestion: some View {
        let username = app.user?.username ?? ""
        return (Text("Where should this go? Publishing to Loore puts it in the Commons and on ")
                + Text("[your public page](loore-app://public)").foregroundColor(LooreColor.accent).underline()
                + Text("."))
            .font(LooreFont.sans(12.8, .light))
            .foregroundStyle(LooreColor.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
            .environment(\.openURL, OpenURLAction { _ in
                app.open(.webPage(path: "/@\(username)"))
                return .handled
            })
    }

    private func quietAction(_ title: String, color: Color = LooreColor.textMuted, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(12, .light))
                .underline()
                .foregroundStyle(color)
                .frame(minHeight: 32)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func dateLabel(_ share: ShareItem) -> String {
        switch share.status {
        case "published": return "published \(LooreDateFormat.date(share.publishedAt))"
        case "revoked": return "revoked \(LooreDateFormat.date(share.revokedAt))"
        default: return LooreDateFormat.date(share.createdAt)
        }
    }

    // MARK: Calls

    private func fetch() async {
        if let list: ShareList = try? await app.api.get(APIPath.share) { shares = list.shares }
        loading = false
    }

    private func openNew() {
        editingId = Self.newId
        editContent = ""
        editType = "other"
        confirmPublishId = nil
        confirmDeleteId = nil
    }

    private func openEdit(_ share: ShareItem) {
        editingId = share.id
        editContent = share.content
        editType = share.shareType
        confirmPublishId = nil
        confirmDeleteId = nil
    }

    private func closeEdit() {
        editingId = nil
        editContent = ""
    }

    private func save() {
        guard !editContent.jsTrimmed.isEmpty, !saving, let id = editingId else { return }
        saving = true
        let body: JSONValue = ["content": .string(editContent), "share_type": .string(editType)]
        Task {
            defer { saving = false }
            do {
                if id == Self.newId {
                    let _: EmptyResponse = try await app.api.post(APIPath.share, json: body)
                } else {
                    let _: EmptyResponse = try await app.api.patch(APIPath.shareItem(id), json: body)
                }
                await fetch()
                closeEdit()
            } catch {
                // Logged only on the web.
            }
        }
    }

    private func publish(_ id: Int) {
        confirmPublishId = nil
        Task {
            _ = try? await app.api.post(APIPath.sharePublish(id), as: EmptyResponse.self)
            await fetch()
        }
    }

    private func revoke(_ id: Int) {
        Task {
            _ = try? await app.api.post(APIPath.shareRevoke(id), as: EmptyResponse.self)
            await fetch()
        }
    }

    private func delete(_ id: Int) {
        confirmDeleteId = nil
        Task {
            _ = try? await app.api.delete(APIPath.shareItem(id), as: EmptyResponse.self)
            await fetch()
        }
    }
}

extension MarkdownStyle {
    /// Share cards (sans .92rem 300, secondary, line-height 1.7).
    static let share = MarkdownStyle(fontSize: 14.7, weight: .light, color: LooreColor.textSecondary, lineHeight: 1.7)
}
