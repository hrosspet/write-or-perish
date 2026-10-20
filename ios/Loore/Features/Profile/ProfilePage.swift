import SwiftUI

/// The AI-written living profile (web `ProfilePage`, map E §1.2): view, edit
/// (with the regenerate-audio question), version history, listen aloud, and
/// the background generation indicator.
struct ProfilePage: View {
    let onSelect: (WorkspaceDocument) -> Void

    @Environment(AppState.self) private var app
    @State private var profile: LatestProfile?
    @State private var versionNumber: Int?
    @State private var loading = true
    @State private var editing = false
    @State private var editContent = ""
    @State private var saving = false
    /// The version the editor's text started from (nil: "Write Profile" with no profile).
    @State private var editingBaseId: Int?
    @State private var dialog: SaveDialog?
    @State private var showHistory = false
    @State private var generating = false
    @State private var failMessage = ""

    var body: some View {
        WorkspaceScroll(active: .profile, onSelect: onSelect) {
            if !loading {
                content
            }
        }
        .task { await fetchProfile() }
        .onAppear { if hasRunningFlag { generating = true } }
        .onChange(of: hasRunningFlag) { _, running in if running { generating = true } }
        .onChange(of: app.profileWatcher.progress) { _, progress in handleProgress(progress) }
        .onChange(of: app.profileWatcher.doneCount) { _, _ in
            generating = false
            Task { await fetchProfile() }
        }
        .onChange(of: profile?.id) { _, _ in Task { await fetchVersionCount() } }
        // One presenter whose content switches (PROGRESS.md "Chained dialogs").
        .looreDialog(isPresented: Binding(get: { dialog != nil }, set: { if !$0 { dialog = nil } })) {
            switch dialog {
            case .regenerateTts:
                RegenerateTtsDialog(onChoice: { regenerate in
                    dialog = nil
                    save(regenerateTts: regenerate)
                }, onCancel: { dialog = nil })
            case .newerVersion:
                NewerProfileVersionDialog(onSaveAsNew: {
                    dialog = nil
                    send(.create)
                }, onDiscard: {
                    dialog = nil
                    editing = false
                    editContent = profile?.content ?? ""
                }, onKeepEditing: { dialog = nil })
            case nil:
                EmptyView()
            }
        }
        .sheet(isPresented: $showHistory) {
            VersionHistorySheet(source: historySource) {
                Task { await fetchProfile() }
            }
        }
    }

    private var hasRunningFlag: Bool {
        app.user?.profileGenerationTaskId != nil || app.user?.profileBatchPending == true
    }

    @ViewBuilder private var content: some View {
        DocTitleRow(title: "Profile") {
            if let profile {
                SpeakerButton(target: .profile(profile.id), content: profile.content, aiUsage: profile.aiUsage,
                              onTtsGenerated: { self.profile?.hasTTS = true })
                HStack(spacing: 12) {
                    VersionChip(text: "v\(versionNumber.map(String.init) ?? "") · \(LooreDateFormat.date(profile.createdAt))") {
                        if editing { save() } else { startEditing() }
                    }
                    HistoryLink { showHistory = true }
                }
            }
            if generating || !failMessage.isEmpty {
                PulsingText(text: indicatorText,
                            color: failMessage.isEmpty ? LooreColor.accent : LooreColor.textMuted,
                            pulsing: failMessage.isEmpty)
                    .accessibilityIdentifier("profile.generating")
            }
        }
        if let profile {
            DocMetaLine(text: Self.metaLine(profile))
        }
        DocDivider()

        if profile == nil && !editing {
            emptyState
        }
        if editing {
            DocEditor(text: $editContent, identifier: "profile.editor", label: "Profile text")
            DocEditButtons(saving: saving, onSave: { save() }, onCancel: { editing = false })
        } else if let profile {
            MarkdownView(markdown: profile.content, style: .profile)
                .accessibilityElement(children: .contain)
            .accessibilityIdentifier("profile.content")
        }
    }

    private var emptyState: some View {
        VStack(spacing: 16) {
            Text("Your profile is a living document the AI writes about you as you use Loore — what you're working on, what you care about, how you've changed.")
                .foregroundStyle(LooreColor.textSecondary)
            Text("It's the first of your artifacts — the row above: documents you and the AI keep together. Todo and Intentions hold what you mean to do; Memory holds what the AI has learned. They start empty and fill in as you write and talk.")
                .foregroundStyle(LooreColor.textMuted)
            Text("There's nothing to set up. Start writing, and this page will follow — or write the first version yourself.")
                .foregroundStyle(LooreColor.textMuted)
                .padding(.bottom, 4)
            Button("Write Profile") { startEditing() }
            .buttonStyle(.looreFilled)
            .accessibilityIdentifier("profile.write")
        }
        .font(LooreFont.sans(14.4, .light))
        .lineSpacing(6.3)
        .multilineTextAlignment(.center)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }

    /// No message yet → "Starting generation..."; batch → the message; sync → "{message} · {n}%".
    private var indicatorText: String {
        if !failMessage.isEmpty { return failMessage }
        guard let progress = app.profileWatcher.progress, progress.running, !progress.message.isEmpty else {
            return "Starting generation..."
        }
        if progress.source == "batch" { return progress.message }
        return "\(progress.message) · \(Int(progress.progress.rounded()))%"
    }

    /// "Built from ~N tokens of writing (96% public tweets) · model · Data through …".
    static func metaLine(_ profile: LatestProfile) -> String {
        var text: String
        if let source = profile.sourceTokensUsed, source != 0 {
            text = "Built from ~\(jsLocaleNumber(source)) tokens of writing"
        } else {
            text = "Generated from \(profile.tokensUsed.map(jsLocaleNumber) ?? "0") tokens"
        }
        text += SourceMix.format(Self.originStats(profile.sourceOriginStats))
        text += " · \(profile.generatedBy ?? "")"
        if let cutoff = profile.sourceDataCutoff {
            text += " · Data through \(LooreDateFormat.date(cutoff, relative: false))"
        }
        return text
    }

    /// `{origin: {tokens}}` as ordered pairs (keys sorted: JSON key order is not kept by the decoder).
    static func originStats(_ value: JSONValue?) -> [(String, Int)] {
        guard let object = value?.objectValue else { return [] }
        return object.keys.sorted().map { key in
            (key, object[key]?["tokens"]?.intValue ?? Int(object[key]?["tokens"]?.doubleValue ?? 0))
        }
    }

    // MARK: Loading

    /// The dashboard is the web page's source of truth (`latest_profile` carries `has_tts`).
    private func fetchProfile() async {
        do {
            let dashboard: DashboardResponse = try await app.api.get(APIPath.dashboard)
            profile = dashboard.latestProfile
            if let latest = dashboard.latestProfile, !editing { editContent = latest.content }
        } catch {
            // Logged only on the web; the page then shows the empty state.
        }
        loading = false
    }

    private func fetchVersionCount() async {
        guard profile != nil, let list: VersionList = try? await app.api.get(APIPath.profileVersions) else { return }
        versionNumber = list.versions.count
    }

    private func handleProgress(_ progress: ProfileGenerationWatcher.Progress?) {
        guard let progress else { return }
        if progress.running {
            generating = true
            if let latest = progress.latestProfileId, latest != profile?.id {
                Task { await fetchProfile() }
            }
            return
        }
        if progress.status == "failed" || progress.status == "stalled" {
            failMessage = progress.status == "failed" ? "Generation failed" : "Generation stopped before finishing"
            Task {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                failMessage = ""
            }
        }
    }

    // MARK: Saving

    private enum SaveDialog { case regenerateTts, newerVersion }

    /// Where Save sends the editor's text.
    enum SaveTarget: Equatable {
        /// `PUT /profile/<id>`: the version the edit started from is still the latest.
        case update(Int)
        /// `POST /profile`: the first profile, or the user's text as a new version.
        case create
        /// A version was generated while the editor was open: saving over it would
        /// replace it in place (and keep it out of history), so ask first.
        case newerVersionArrived
    }

    static func saveTarget(editingBaseId: Int?, latestId: Int?) -> SaveTarget {
        guard latestId == editingBaseId else { return .newerVersionArrived }
        return latestId.map(SaveTarget.update) ?? .create
    }

    private func startEditing() {
        editContent = profile?.content ?? ""
        editingBaseId = profile?.id
        editing = true
    }

    private func save(regenerateTts: Bool? = nil) {
        guard !saving, !editContent.jsTrimmed.isEmpty else { return }
        let target = Self.saveTarget(editingBaseId: editingBaseId, latestId: profile?.id)
        if target == .newerVersionArrived {
            dialog = .newerVersion
            return
        }
        if case .update = target, let profile, profile.hasTTS, regenerateTts == nil, editContent != profile.content {
            dialog = .regenerateTts
            return
        }
        send(target, regenerateTts: regenerateTts)
    }

    private func send(_ target: SaveTarget, regenerateTts: Bool? = nil) {
        guard !saving, !editContent.jsTrimmed.isEmpty else { return }
        saving = true
        Task {
            defer { saving = false }
            do {
                switch target {
                case .update(let id):
                    var body: [String: JSONValue] = ["content": .string(editContent)]
                    if regenerateTts == true { body["regenerate_tts"] = .bool(true) }
                    let _: EmptyResponse = try await app.api.put(APIPath.profileItem(id), json: .object(body))
                case .create:
                    let _: EmptyResponse = try await app.api.post(APIPath.profile, json: ["content": .string(editContent)])
                case .newerVersionArrived:
                    return
                }
                editing = false
                await fetchProfile()
            } catch {
                // The web logs save errors only.
            }
        }
    }

    private var historySource: VersionHistorySource {
        let api = app.api
        return VersionHistorySource(
            title: "Profile History",
            loadVersions: { try await (api.get(APIPath.profileVersions) as VersionList).versions },
            loadContent: { version in
                guard let id = version.id.rowId else { return "" }
                return try await (api.get(APIPath.profileVersion(id)) as VersionContent).content
            },
            revert: { version in
                guard let id = version.id.rowId else { return }
                let _: EmptyResponse = try await api.post(APIPath.profileRevert(id))
            })
    }
}

/// Save found a version generated while the editor was open.
private struct NewerProfileVersionDialog: View {
    let onSaveAsNew: () -> Void
    let onDiscard: () -> Void
    let onKeepEditing: () -> Void

    var body: some View {
        LooreDialogCard(title: "A new version arrived") {
            DialogBodyText(text: "A new version of your profile was generated while you were editing. Saving now would replace it.")
            VStack(spacing: 8) {
                ChoiceButton(title: "Save mine as a new version",
                             subtitle: "The generated version stays in history; yours becomes the latest.",
                             tone: .accent, action: onSaveAsNew)
                ChoiceButton(title: "Discard my edit", subtitle: "Shows the new version.",
                             tone: .destructive, action: onDiscard)
                ChoiceButton(title: "Keep editing", action: onKeepEditing)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("profile.newerVersion")
    }
}

extension MarkdownStyle {
    /// `.loore-profile` (sans .9rem 300, secondary, line-height 1.7); also Todo-free documents.
    static let profile = MarkdownStyle(fontSize: 14.4, weight: .light, color: LooreColor.textSecondary, lineHeight: 1.7)
}
