import SwiftUI

/// Named, versioned documents the AI and the user keep together (web
/// `ArtifactsPage`, map E §1.4): built-in kinds, custom kinds, the create form,
/// the unsaved-changes guard, Intentions' structured view, version history.
struct ArtifactsPage: View {
    /// nil while creating.
    let kind: String?
    let creating: Bool
    let onSelect: (WorkspaceDocument) -> Void

    @Environment(AppState.self) private var app
    @State private var versionNumber: Int?
    @State private var editing = false
    @State private var editContent = ""
    @State private var editDescription = ""
    @State private var newKind = ""
    @State private var saving = false
    @State private var showHistory = false
    @State private var pendingNav: WorkspaceDocument?
    @State private var saveError: String?

    static let kindBlurbs = [
        "memory": "Durable facts the AI remembers about you across sessions. It updates this on its own as you talk.",
        "scratchpad": "The AI's working notes for ongoing threads — where it left off, open questions.",
        "intentions": "Your longer-running aspirations — noticed, clarified, and tracked together with the AI. Fulfilled or consciously released, both count.",
    ]

    static let kindEmptyIntros = [
        "intentions": "Intentions are your longer-running aspirations — the communication layer between your conscious goals and your subconscious orientation: directional rather than specific, quietly shaping which opportunities you notice and reach for.",
        "ai_preferences": "AI Interaction Preferences are your standing notes on how the AI should work with you — tone, style, boundaries, topics to leave alone — written by you or noted by the AI when you express one, and honored in every conversation.",
        "external_digest": "The Saved References Digest is a compact topic map of the tweets and bookmarks you saved elsewhere and imported into Loore — the AI reads it to judge whether your references hold something relevant before searching them, and it rebuilds itself after each import.",
    ]

    /// The active artifact: the listed one, or a synthetic empty one for a kind
    /// without a row (a deep link to an unknown custom kind).
    private var active: ArtifactDoc? {
        guard let kind, !creating else { return nil }
        return app.artifacts.artifact(kind)
            ?? ArtifactDoc(kind: kind, title: ArtifactKinds.titleFromKind(kind))
    }

    private var dirty: Bool {
        guard editing else { return false }
        if creating { return !newKind.isEmpty || !editContent.jsTrimmed.isEmpty || !editDescription.jsTrimmed.isEmpty }
        return editContent != (active?.content ?? "") || editDescription != (active?.description ?? "")
    }

    private var saveEnabled: Bool {
        editing && !saving && !(creating && newKind.isEmpty) && !editDescription.jsTrimmed.isEmpty
    }

    var body: some View {
        WorkspaceScroll(active: creating ? .create : .artifact(kind ?? "memory"), onSelect: guardedSelect) {
            if app.artifacts.loaded || creating {
                content
            }
        }
        .task {
            if creating {
                editing = true
            } else if let kind {
                app.api.fireAndForget(APIRequest(.post, APIPath.artifactViewed(kind)))
            }
            await app.artifacts.refresh(api: app.api)
            await fetchVersionCount()
        }
        .looreDialog(isPresented: Binding(get: { pendingNav != nil }, set: { if !$0 { pendingNav = nil } })) {
            LooreDialogCard(title: "Unsaved changes") {
                DialogBodyText(text: "You have unsaved edits to this artifact. Leave without saving?")
                HStack(spacing: 8) {
                    Spacer()
                    Button("Cancel") { pendingNav = nil }.buttonStyle(.looreOutline)
                    Button("Leave") {
                        let target = pendingNav
                        pendingNav = nil
                        editing = false
                        if let target { onSelect(target) }
                    }
                    .buttonStyle(.loorePrimary)
                    .accessibilityIdentifier("artifact.leave")
                }
            }
        }
        .sheet(isPresented: $showHistory) {
            if let active { VersionHistorySheet(source: historySource(active)) }
        }
    }

    private func guardedSelect(_ document: WorkspaceDocument) {
        if dirty { pendingNav = document } else { onSelect(document) }
    }

    @ViewBuilder private var content: some View {
        DocTitleRow(title: creating ? "New artifact" : (active?.title ?? "Artifacts")) {
            if let active, active.exists {
                HStack(spacing: 12) {
                    VersionChip(text: "v\(versionNumber ?? 1)",
                                textColor: editing ? LooreColor.accent : LooreColor.textMuted) {
                        if !editing { startEditing(active) }
                    }
                    Text(LooreDateFormat.date(active.createdAt))
                        .font(LooreFont.sans(12, .light))
                        .foregroundStyle(LooreColor.textMuted.opacity(0.7))
                    HistoryLink { showHistory = true }
                }
            }
        }
        if let active {
            DocMetaLine(text: Self.subtitle(active))
        }
        DocDivider()

        if editing {
            editForm
        } else if let active {
            if !active.content.isEmpty {
                Group {
                    if active.kind == "intentions" {
                        IntentionsView(content: active.content)
                    } else {
                        MarkdownView(markdown: active.content, style: .profile)
                    }
                }
                .accessibilityElement(children: .contain)
            .accessibilityIdentifier("artifact.content")
            } else {
                emptyState(active)
            }
        }
    }

    private var editForm: some View {
        VStack(alignment: .leading, spacing: 12) {
            if creating {
                field(text: $newKind, placeholder: "artifact-name (lowercase, dashes)", identifier: "artifact.kind")
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .onChange(of: newKind) { _, typed in
                        let clean = ArtifactKinds.sanitizeKindInput(typed)
                        if clean != typed { newKind = clean }
                    }
                if !newKind.isEmpty && !ArtifactKinds.isValidKind(newKind) {
                    Text("Invalid kind: use a short lowercase slug (letters, digits, dashes).")
                        .font(LooreFont.meta)
                        .foregroundStyle(LooreColor.accent)
                }
            }
            field(text: $editDescription, placeholder: "One-line description — what this artifact is for",
                  identifier: "artifact.description")
            DocEditor(text: $editContent, minHeight: 300,
                      placeholder: kind == "memory" && !creating ? "Facts the AI should remember about you..."
                          : "Artifact content (markdown)...",
                      identifier: "artifact.editor", label: "Artifact text")
            if let saveError {
                Text(saveError).font(LooreFont.meta).foregroundStyle(LooreColor.accent)
            }
            DocEditButtons(saving: saving,
                           saveDisabled: !saveEnabled || (creating && !ArtifactKinds.isValidKind(newKind)),
                           onSave: save,
                           onCancel: {
                               editing = false
                               if creating { onSelect(.artifact("memory")) }
                           })
        }
    }

    private func field(text: Binding<String>, placeholder: String, identifier: String) -> some View {
        TextField("", text: text, prompt: loorePrompt(placeholder))
            .font(LooreFont.sans(13.6, .light))
            .foregroundStyle(LooreColor.textPrimary)
            .padding(.vertical, 10)
            .padding(.horizontal, 12)
            .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
            .submitLabel(.next)
            .accessibilityIdentifier(identifier)
    }

    private func emptyState(_ active: ArtifactDoc) -> some View {
        VStack(spacing: 16) {
            if let intro = Self.kindEmptyIntros[active.kind] {
                Text(intro)
                    .font(LooreFont.sans(14.4, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                    .lineSpacing(6.3)
            }
            Text("Nothing here yet. The AI fills this in during Voice and Text sessions,\nor write your own.")
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textMuted)
            Button("Write \(active.title)") {
                editContent = ""
                editDescription = active.description ?? ""
                editing = true
            }
            .buttonStyle(.looreFilled)
            .accessibilityIdentifier("artifact.write")
        }
        .multilineTextAlignment(.center)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }

    /// `description` → the kind's blurb → the generic line, plus " · last updated by …".
    static func subtitle(_ artifact: ArtifactDoc) -> String {
        var text = (artifact.description?.isEmpty == false ? artifact.description : nil)
            ?? kindBlurbs[artifact.kind] ?? "A persistent document shared between you and the AI."
        if artifact.exists {
            text += " · last updated by \(updatedByLabel(artifact.generatedBy))"
        }
        return text
    }

    static func updatedByLabel(_ generatedBy: String?) -> String {
        switch generatedBy {
        case "user", "manual": return "edited manually"
        case "agentic_session": return "AI session"
        default: return generatedBy ?? ""
        }
    }

    private func startEditing(_ active: ArtifactDoc) {
        editContent = active.content
        editDescription = active.description ?? ""
        editing = true
    }

    private func fetchVersionCount() async {
        guard let kind, !creating, app.artifacts.artifact(kind)?.exists == true else {
            versionNumber = nil
            return
        }
        let list: VersionList? = try? await app.api.get(APIPath.artifactVersions(kind))
        versionNumber = list?.versions.count
    }

    /// `PUT /api/artifacts/<kind>`, refetch the list, signal, leave edit mode, show the kind.
    private func save() {
        let target = creating ? newKind : (kind ?? "")
        guard saveEnabled, !target.isEmpty else { return }
        saving = true
        saveError = nil
        Task {
            defer { saving = false }
            do {
                let body: [String: JSONValue] = [
                    "content": .string(editContent),
                    "description": .string(editDescription.jsTrimmed),
                    "generated_by": "user",
                ]
                let _: EmptyResponse = try await app.api.put(APIPath.artifact(target), json: .object(body))
                await app.artifacts.refresh(api: app.api)
                app.signals.post(.artifactsChanged)
                editing = false
                if creating || target != kind {
                    onSelect(.artifact(target))
                } else {
                    await fetchVersionCount()
                }
            } catch let error as APIError {
                // The web only logs; the app shows the server's reason (a bad kind slug).
                saveError = error.serverMessage
            } catch {}
        }
    }

    private func historySource(_ active: ArtifactDoc) -> VersionHistorySource {
        let api = app.api
        let kind = active.kind
        let store = app.artifacts
        let signals = app.signals
        return VersionHistorySource(
            title: "\(active.title) History",
            loadVersions: { try await (api.get(APIPath.artifactVersions(kind)) as VersionList).versions },
            loadContent: { version in
                guard let id = version.id.rowId else { return "" }
                return try await (api.get(APIPath.artifactVersion(id)) as VersionContent).content
            },
            revert: { version in
                guard let id = version.id.rowId else { return }
                let _: EmptyResponse = try await api.post(APIPath.artifactRevert(kind, id))
                await store.refresh(api: api)
                await MainActor.run { signals.post(.artifactsChanged) }
            })
    }
}

/// The intentions artifact as entries (web `IntentionsView`); falls back to
/// plain markdown when the content has no `## ` entries. Read-only.
struct IntentionsView: View {
    let content: String

    var body: some View {
        let sections = IntentionsParser.parse(content)
        if !sections.contains(where: { !$0.entries.isEmpty }) {
            MarkdownView(markdown: content, style: .profile)
        } else {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(sections.enumerated()), id: \.offset) { _, section in
                    VStack(alignment: .leading, spacing: 0) {
                        if !section.title.isEmpty {
                            HStack(spacing: 10) {
                                Text(section.title.uppercased())
                                    .font(LooreFont.sans(13.8, .semibold))
                                    .tracking(2.2)
                                    .foregroundStyle(LooreColor.accent.opacity(0.82))
                                Text("\(section.entries.count)")
                                    .font(LooreFont.sans(11.8, .light))
                                    .tracking(0.6)
                                    .foregroundStyle(LooreColor.textMuted)
                            }
                            .padding(.bottom, 17.6)
                        }
                        ForEach(Array(section.entries.enumerated()), id: \.offset) { _, entry in
                            IntentionEntryRow(entry: entry)
                        }
                    }
                    .padding(.bottom, 40)
                }
            }
        }
    }
}

private struct IntentionEntryRow: View {
    let entry: IntentionsParser.Entry

    var body: some View {
        let state = IntentionsParser.statusState(entry.status)
        HStack(alignment: .top, spacing: 12) {
            StatusDot(state: state).padding(.top, 4)
            VStack(alignment: .leading, spacing: 0) {
                Text(entry.name)
                    .font(LooreFont.serif(18.6, .semibold, relativeTo: .headline))
                    .foregroundStyle(LooreColor.textPrimary)
                    .opacity(state == .released ? 0.6 : 1)
                if !entry.status.isEmpty {
                    Text(entry.status)
                        .font(LooreFont.sans(11.5, .light))
                        .tracking(0.35)
                        .foregroundStyle(LooreColor.textMuted)
                        .padding(.top, 3)
                }
                if !entry.body.isEmpty {
                    Text(entry.body.joined(separator: " "))
                        .font(LooreFont.sans(14.4, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(5)
                        .padding(.top, 7)
                }
                if !entry.notes.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(entry.notes.enumerated()), id: \.offset) { _, note in
                            Text(note)
                                .font(LooreFont.sans(12.5, .light))
                                .foregroundStyle(LooreColor.textMuted)
                                .lineSpacing(3)
                        }
                    }
                    .padding(.leading, 12)
                    .overlay(alignment: .leading) { Rectangle().fill(LooreColor.border).frame(width: 1) }
                    .padding(.top, 8)
                }
            }
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 14)
        .overlay(alignment: .bottom) { Rectangle().fill(LooreColor.bgSurface).frame(height: 1) }
        .accessibilityElement(children: .combine)
    }
}

private struct StatusDot: View {
    let state: IntentionsParser.Status

    var body: some View {
        ZStack {
            switch state {
            case .fulfilled:
                Circle().fill(LooreColor.accent)
                Text("✓").font(.system(size: 8.8, weight: .bold)).foregroundStyle(LooreColor.bgDeep)
            case .released:
                Circle().strokeBorder(LooreColor.borderHover, lineWidth: 1.5).opacity(0.5)
            case .inferred:
                Circle().strokeBorder(LooreColor.borderHover, style: StrokeStyle(lineWidth: 1.5, dash: [3, 2]))
            case .active:
                Circle().fill(LooreColor.accentDim)
            }
        }
        .frame(width: 15, height: 15)
        .accessibilityLabel(Text(String(describing: state)))
    }
}
