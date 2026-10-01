import SwiftUI

/// The user's copies of the system prompts (web `PromptsPage`, map E §5; craft).
struct PromptsView: View {
    @Environment(AppState.self) private var app
    @State private var prompts: [PromptSummary] = []
    @State private var loading = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if loading {
                    Text("Loading...").font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
                } else {
                    Text("Prompts")
                        .font(LooreFont.pageTitle)
                        .foregroundStyle(LooreColor.textPrimary)
                        .accessibilityAddTraits(.isHeader)
                        .padding(.bottom, 8)
                    DocMetaLine(text: "System prompts that power Loore's AI features")
                    DocDivider()
                    ForEach(prompts) { prompt in
                        Button { app.open(.prompt(key: prompt.promptKey)) } label: { row(prompt) }
                            .buttonStyle(.plain)
                            .accessibilityIdentifier("prompt.row.\(prompt.promptKey)")
                    }
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(800)
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    private func row(_ prompt: PromptSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            FlowLayout(spacing: 12, lineSpacing: 4) {
                HStack(spacing: 6) {
                    Text(prompt.title)
                        .font(LooreFont.sans(15.2, .regular))
                        .foregroundStyle(LooreColor.textPrimary)
                    if prompt.defaultUpdated {
                        Circle().fill(LooreColor.accent).frame(width: 6, height: 6)
                            .accessibilityLabel("Default prompt has been updated")
                    }
                }
                Text("v\(prompt.versionNumber) · \(PromptDetailView.label(prompt.generatedBy)) · \(LooreDateFormat.date(prompt.createdAt, fallback: "default"))")
                    .font(LooreFont.sans(11.2, .light))
                    .foregroundStyle(LooreColor.textMuted.opacity(0.7))
            }
            Text(prompt.preview)
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textMuted)
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .padding(.vertical, 20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .bottom) { HairlineDivider() }
        .contentShape(Rectangle())
    }

    private func load() async {
        if let list: PromptList = try? await app.api.get(APIPath.prompts) { prompts = list.prompts }
        loading = false
    }
}

/// One prompt: view, edit, the "default updated" banner, version history with
/// the file default as v0 (web `PromptDetailPage`).
struct PromptDetailView: View {
    let promptKey: String

    @Environment(AppState.self) private var app
    @State private var prompt: PromptDoc?
    @State private var loading = true
    @State private var editing = false
    @State private var editContent = ""
    @State private var saving = false
    @State private var showHistory = false
    @State private var newDefaultContent: String?
    @State private var dismissing = false

    static func label(_ generatedBy: String?) -> String {
        switch generatedBy {
        case "default": return "default"
        case "user": return "edited manually"
        case "revert": return "reverted"
        default: return generatedBy ?? ""
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if loading {
                    Text("Loading...").font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
                } else if let prompt {
                    content(prompt)
                } else {
                    Text("Prompt not found.").font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(800)
        }
        .scrollDismissesKeyboard(.interactively)
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .sheet(isPresented: $showHistory) {
            if let prompt { VersionHistorySheet(source: historySource(prompt)) }
        }
    }

    @ViewBuilder private func content(_ prompt: PromptDoc) -> some View {
        Button("← All prompts") {
            if app.router.previousRoute == .prompts { app.router.pop() } else { app.open(.prompts) }
        }
        .buttonStyle(.plain)
        .font(LooreFont.sans(12, .light))
        .foregroundStyle(LooreColor.textMuted)
        .padding(.bottom, 16)

        DocTitleRow(title: prompt.title) {
            HStack(spacing: 12) {
                VersionChip(text: "v\(prompt.versionNumber) · \(LooreDateFormat.date(prompt.createdAt, fallback: "default"))") {
                    if editing { save() } else {
                        editContent = prompt.content
                        editing = true
                    }
                }
                HistoryLink { showHistory = true }
            }
        }
        DocMetaLine(text: Self.label(prompt.generatedBy)
                    + (prompt.createdAt != nil ? " · \(LooreDateFormat.date(prompt.createdAt, fallback: "default"))" : ""))
        DocDivider()

        if prompt.defaultUpdated {
            banner
        }
        if editing {
            DocEditor(text: $editContent, minHeight: 500, monospaced: true, identifier: "prompt.editor")
            DocEditButtons(saving: saving, onSave: save, onCancel: {
                editing = false
                editContent = prompt.content
            })
        } else {
            MarkdownView(markdown: prompt.content, style: .prompt)
                .accessibilityElement(children: .contain)
            .accessibilityIdentifier("prompt.content")
        }
    }

    private var banner: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("The default prompt has been updated.")
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textSecondary)
            HStack(spacing: 10) {
                bannerButton("View new default", color: LooreColor.accent) { viewNewDefault() }
                bannerButton("Accept new default", color: LooreColor.accent) { acceptNewDefault() }
                bannerButton("Dismiss", color: LooreColor.textMuted) { dismissDefault() }
                    .disabled(dismissing)
            }
            if let newDefaultContent {
                ScrollView {
                    Text(newDefaultContent)
                        .font(.system(size: 12.8, design: .monospaced))
                        .foregroundStyle(LooreColor.textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 300)
            }
        }
        .padding(.vertical, 14)
        .padding(.horizontal, 18)
        .background(LooreColor.accentSubtle, in: RoundedRectangle(cornerRadius: LooreRadius.small))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.accentDim))
        .padding(.bottom, 20)
    }

    private func bannerButton(_ title: String, color: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(12, .regular))
                .underline()
                .foregroundStyle(color)
                .frame(minHeight: 32)
        }
        .buttonStyle(.plain)
    }

    // MARK: Calls

    private func load() async {
        do {
            let envelope: PromptEnvelope = try await app.api.get(APIPath.prompt(promptKey))
            prompt = envelope.prompt
            editContent = envelope.prompt.content
        } catch {
            // 404 "Unknown prompt key" → "Prompt not found."
        }
        loading = false
    }

    private func adopt(_ doc: PromptDoc) {
        prompt = doc
        editContent = doc.content
    }

    private func save() {
        guard !saving, !editContent.jsTrimmed.isEmpty else { return }
        saving = true
        Task {
            defer { saving = false }
            do {
                let envelope: PromptEnvelope = try await app.api.put(APIPath.prompt(promptKey),
                                                                    json: ["content": .string(editContent)])
                adopt(envelope.prompt)
                editing = false
            } catch {
                app.toasts.show((error as? APIError)?.serverMessage ?? "Failed to save prompt.", duration: 10)
            }
        }
    }

    private func viewNewDefault() {
        Task {
            if let answer: VersionContent = try? await app.api.get(APIPath.promptDefault(promptKey)) {
                newDefaultContent = answer.content
            }
        }
    }

    private func acceptNewDefault() {
        Task {
            if let envelope: PromptEnvelope = try? await app.api.post(APIPath.promptRevertToDefault(promptKey)) {
                adopt(envelope.prompt)
                newDefaultContent = nil
            }
        }
    }

    private func dismissDefault() {
        dismissing = true
        Task {
            defer { dismissing = false }
            if let envelope: PromptEnvelope = try? await app.api.post(APIPath.promptAcknowledgeDefault(promptKey)) {
                prompt = envelope.prompt
                newDefaultContent = nil
            }
        }
    }

    private func historySource(_ prompt: PromptDoc) -> VersionHistorySource {
        let api = app.api
        let key = promptKey
        return VersionHistorySource(
            title: "\(prompt.title) History",
            loadVersions: { try await (api.get(APIPath.promptVersions(key)) as VersionList).versions },
            loadContent: { version in
                switch version.id {
                case .fileDefault: return try await (api.get(APIPath.promptDefault(key)) as VersionContent).content
                case .row(let id): return try await (api.get(APIPath.promptVersion(key, id)) as VersionContent).content
                }
            },
            revert: { version in
                let path: String
                switch version.id {
                case .fileDefault: path = APIPath.promptRevertToDefault(key)
                case .row(let id): path = APIPath.promptRevert(key, id)
                }
                let envelope: PromptEnvelope = try await api.post(path)
                await MainActor.run { adopt(envelope.prompt) }
            })
    }
}

extension MarkdownStyle {
    /// Prompt text (sans .85rem, secondary).
    static let prompt = MarkdownStyle(fontSize: 13.6, weight: .light, color: LooreColor.textSecondary, lineHeight: 1.6)
}
