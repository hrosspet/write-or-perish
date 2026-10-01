import SwiftUI
import UniformTypeIdentifiers

/// Import (web `ImportPage` = `ImportData inline` + `ExternalImport`, map E §7).
/// `anchor` `x-bookmarks` scrolls to the X card.
struct ImportView: View {
    var anchor: String?

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Import Data")
                        .font(LooreFont.serif(22.4, .light, relativeTo: .title))
                        .foregroundStyle(LooreColor.textPrimary)
                        .accessibilityAddTraits(.isHeader)
                        .padding(.bottom, 24)
                    ImportDataSection()
                    ExternalImportSection()
                        .padding(.top, 40)
                }
                .padding(.horizontal, LooreSpacing.gutter)
                .padding(.top, 24)
                .padding(.bottom, 48)
                .looreReadableWidth(600)
            }
            .scrollDismissesKeyboard(.interactively)
            .onAppear {
                guard let anchor else { return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    withAnimation { proxy.scrollTo(anchor, anchor: .top) }
                }
            }
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// "Import References" (web `ExternalImport`, map E §7.2): Community Archive,
/// X bookmarks (sync, connect in a signed-in web view, JSON import) and the
/// Chrome clipper token card.
struct ExternalImportSection: View {
    @Environment(AppState.self) private var app
    @State private var counts: [String: Int] = [:]
    @State private var xStatus: TwitterSyncStatus?
    @State private var caUsername = ""
    @State private var caStatus: String?
    @State private var xSyncMessage: String?
    @State private var syncing = false
    @State private var importingJSON = false
    @State private var importMessage: String?
    @State private var busy = false
    @State private var tokens: [APITokenRow] = []
    @State private var newToken: String?
    @State private var tokenMessage: String?
    @State private var addressCopied = false
    @State private var pickingJSON = false
    @State private var connectingX = false
    @State private var pollTask: Task<Void, Never>?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Import References")
                .font(LooreFont.serif(22.4, .light, relativeTo: .title))
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.bottom, 8)
            help("Content you've saved elsewhere, made searchable next to your own writing (Cmd+K → Semantic)."
                 + (countsLine.map { " Imported so far: \($0)." } ?? ""))
            if countsLine != nil {
                Button("View references") { app.open(.references) }
                    .buttonStyle(.looreOutline)
                    .padding(.bottom, 16)
            }
            communityArchiveCard
            xBookmarksCard.id("x-bookmarks")
            if app.user?.externalContentEnabled == true {
                clipperCard
            }
        }
        .task {
            await refresh()
            await refreshTokens()
        }
        .onDisappear { pollTask?.cancel() }
        .fileImporter(isPresented: $pickingJSON, allowedContentTypes: [.json]) { outcome in
            if case .success(let url) = outcome { importBookmarks(url) }
        }
        .sheet(isPresented: $connectingX) {
            CookieWebFlowSheet(title: "Connect X", startURL: app.environment.url(path: APIPath.twitterConnect)) { _ in
                // The web shows nothing for `?x_connect=ok|failed`; the card refreshes.
                Task { await refresh() }
            }
        }
        .looreDialog(isPresented: Binding(get: { newToken != nil }, set: { _ in }), dismissOnBackdropTap: false) {
            if let newToken {
                NewTokenDialog(token: newToken) { self.newToken = nil }
            }
        }
    }

    private var countsLine: String? {
        let parts = [
            counts["community_archive"].flatMap { $0 > 0 ? "\($0) archive tweets" : nil },
            counts["twitter_bookmark"].flatMap { $0 > 0 ? "\($0) bookmarks" : nil },
            counts["web_clip"].flatMap { $0 > 0 ? "\($0) clipped pages" : nil },
        ].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    // MARK: Cards

    private var communityArchiveCard: some View {
        card("Community Archive") {
            help("Fetch tweets from the open Community Archive — any account that donated its archive. Try your own handle or someone you follow.")
            HStack(spacing: 8) {
                TextField("", text: $caUsername, prompt: loorePrompt("@username"))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .modifier(SmallFieldStyle())
                    .accessibilityIdentifier("import.caUsername")
                Button("Fetch tweets") { fetchCommunityArchive() }
                    .buttonStyle(FilledSmallButtonStyle())
                    .disabled(busy)
                    .accessibilityIdentifier("import.caFetch")
            }
            if let caStatus { status(caStatus) }
        }
    }

    @ViewBuilder private var xBookmarksCard: some View {
        card("X Bookmarks") {
            if let x = xStatus, x.revoked {
                help("X disconnected \(x.handle.map { "(@\($0)) " } ?? "")— access was revoked or expired. Reconnect to resume nightly bookmark sync.")
                Button("Reconnect X") { connectingX = true }.buttonStyle(FilledSmallButtonStyle())
            } else if let x = xStatus, x.connected {
                help("Connected as @\(x.handle ?? "")" + (x.lastSyncedAt.map { " · last synced \(LooreDateFormat.ymd($0, timeZone: TimeZone(identifier: "UTC")!).replacingOccurrences(of: "/", with: "-"))" } ?? ""))
                Button(syncing ? "Syncing…" : "Sync bookmarks") { syncX() }
                    .buttonStyle(FilledSmallButtonStyle())
                    .disabled(busy)
                if let xSyncMessage { status(xSyncMessage) }
                help("New bookmarks sync automatically once a night — the button is just for syncing right now.")
                    .padding(.top, 10)
            } else if let x = xStatus, x.configured {
                help("Connect your X account to pull in your bookmarks. X's API serves roughly the 100 most recent; after that, new bookmarks sync in nightly.")
                Button("Connect X") { connectingX = true }
                    .buttonStyle(FilledSmallButtonStyle())
                    .accessibilityIdentifier("import.connectX")
            } else {
                help("Direct sync isn't configured yet. You can still import a bookmarks JSON export:")
            }
            if !(xStatus?.configured ?? false) || app.capabilities.craftMode {
                VStack(alignment: .leading, spacing: 8) {
                    if xStatus?.configured == true {
                        help("Craft: import a bookmarks JSON export (browser-exporter format) — covers bookmarks beyond the API's recent window.")
                    }
                    Button(importingJSON ? "Importing…" : "Import bookmarks JSON") { pickingJSON = true }
                        .buttonStyle(GhostSmallButtonStyle())
                        .disabled(busy)
                        .accessibilityIdentifier("import.bookmarksJSON")
                    if let importMessage { status(importMessage) }
                }
                .padding(.top, 14)
            }
        }
    }

    private var clipperCard: some View {
        card("Chrome clipper") {
            help("Save any open tab into your references with one key press. The extension reads the page in your browser, sends the text to Loore, and closes the tab. Clips are references, not your writing: they are searchable and quotable, and never enter your profile.")
            (Text("Install: open ")
             + Text("[chrome://extensions](loore-app://copy)").font(.system(size: 12.8, design: .monospaced))
                .foregroundColor(LooreColor.textSecondary)
             + Text(addressCopied ? " (Copied)" : "")
             + Text(", turn on Developer mode, choose “Load unpacked” and pick the ")
             + Text("extension/").font(.system(size: 12.8, design: .monospaced)).foregroundColor(LooreColor.textSecondary)
             + Text(" folder of the Loore repository. Then paste a token below into the extension’s options. A token can only add references; it cannot read anything."))
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textMuted)
                .lineSpacing(5)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 14)
                .environment(\.openURL, OpenURLAction { _ in
                    UIPasteboard.general.string = "chrome://extensions"
                    addressCopied = true
                    Task {
                        try? await Task.sleep(nanoseconds: 1_500_000_000)
                        addressCopied = false
                    }
                    return .handled
                })
            if !tokens.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(tokens) { token in
                        FlowLayout(spacing: 10, lineSpacing: 4) {
                            Text("loore_\(token.prefix)…")
                                .font(.system(size: 12.8, design: .monospaced))
                                .foregroundStyle(LooreColor.textSecondary)
                            Text("created \(day(token.createdAt) ?? "?")")
                            Text(day(token.lastUsedAt).map { "last used \($0)" } ?? "never used")
                            Button("Revoke") { revoke(token.id) }
                                .buttonStyle(.plain)
                                .foregroundStyle(LooreColor.accent)
                                .disabled(busy)
                        }
                        .font(LooreFont.sans(12.5, .light))
                        .foregroundStyle(LooreColor.textMuted)
                    }
                }
                .padding(.bottom, 14)
            }
            Button("Create token") { createToken() }
                .buttonStyle(FilledSmallButtonStyle())
                .disabled(busy)
                .accessibilityIdentifier("import.createToken")
            if let tokenMessage { status(tokenMessage) }
        }
    }

    // MARK: Pieces

    private func card<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(LooreFont.serif(19.2, .light, relativeTo: .title3))
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.bottom, 6)
            content()
        }
        .padding(.vertical, 20)
        .padding(.horizontal, 24)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.card))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.card).strokeBorder(LooreColor.border))
        .padding(.bottom, 16)
    }

    private func help(_ text: String) -> some View {
        Text(text)
            .font(LooreFont.sans(12.8, .light))
            .foregroundStyle(LooreColor.textMuted)
            .lineSpacing(5)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.bottom, 14)
    }

    private func status(_ text: String) -> some View {
        Text(text)
            .font(LooreFont.sans(12.8, .light))
            .foregroundStyle(LooreColor.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.top, 10)
            .accessibilityIdentifier("import.status")
    }

    private func day(_ date: Date?) -> String? {
        date.map { LooreDateFormat.ymd($0, timeZone: TimeZone(identifier: "UTC")!).replacingOccurrences(of: "/", with: "-") }
    }

    // MARK: Calls

    private func refresh() async {
        async let items: ExternalItemsPage? = try? app.api.get(APIPath.externalItems,
                                                               query: [URLQueryItem(name: "per_page", value: "1")])
        async let status: TwitterSyncStatus? = try? app.api.get(APIPath.twitterStatus)
        let (page, x) = await (items, status)
        if let page { counts = page.counts }
        if let x { xStatus = x }
    }

    private func refreshTokens() async {
        if let list: APITokenList = try? await app.api.get(APIPath.apiTokens) { tokens = list.tokens }
    }

    /// `POST /api/external/community-archive/fetch`, then counts every 5 s, 12 times.
    private func fetchCommunityArchive() {
        let username = caUsername.jsTrimmed
        guard !username.isEmpty else { return }
        busy = true
        caStatus = nil
        Task {
            defer { busy = false }
            do {
                let _: EmptyResponse = try await app.api.post(APIPath.communityArchiveFetch,
                                                              json: ["username": .string(username)])
                caStatus = "Fetching in the background — counts update below."
                pollTask?.cancel()
                pollTask = Task {
                    for _ in 0..<12 {
                        try? await Task.sleep(nanoseconds: 5_000_000_000)
                        if Task.isCancelled { return }
                        await refresh()
                    }
                    app.signals.post(.referencesChanged)
                }
            } catch {
                caStatus = (error as? APIError)?.serverMessage ?? "Fetch failed."
            }
        }
    }

    /// `POST /api/external/twitter/sync`, then status + counts every 3 s, up to 20 times.
    private func syncX() {
        busy = true
        syncing = true
        xSyncMessage = nil
        let baseline = xStatus?.lastSyncedAt
        Task {
            do {
                let _: EmptyResponse = try await app.api.post(APIPath.twitterSync)
            } catch {
                xSyncMessage = (error as? APIError)?.serverMessage ?? "Sync failed."
                syncing = false
                busy = false
                return
            }
            for _ in 0..<20 {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                await refresh()
                if let synced = xStatus?.lastSyncedAt, synced != baseline {
                    let created = xStatus?.lastSyncCreated ?? 0
                    xSyncMessage = created > 0 ? "Synced — \(created) new bookmark\(created == 1 ? "" : "s")."
                        : "Synced — no new bookmarks."
                    app.signals.post(.referencesChanged)
                    break
                } else if xStatus?.revoked == true {
                    xSyncMessage = "X access was revoked — reconnect below."
                    break
                }
            }
            // Timeout: the web shows nothing more.
            syncing = false
            busy = false
        }
    }

    /// The file is read and posted as the JSON body (`{bookmarks: […]}` for a bare array).
    private func importBookmarks(_ url: URL) {
        busy = true
        importingJSON = true
        importMessage = nil
        Task {
            defer {
                importingJSON = false
                busy = false
            }
            do {
                let scoped = url.startAccessingSecurityScopedResource()
                defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                let data = try Data(contentsOf: url)
                let parsed = try JSONDecoder().decode(JSONValue.self, from: data)
                let body: JSONValue = parsed.arrayValue != nil ? .object(["bookmarks": parsed]) : parsed
                struct Answer: Decodable { var created: Int?; var skipped: Int? }
                let answer: Answer = try await app.api.post(APIPath.bookmarksImport, json: body)
                let created = answer.created ?? 0
                importMessage = "Imported \(created) bookmark\(created == 1 ? "" : "s") (\(answer.skipped ?? 0) already known)."
                await refresh()
                app.signals.post(.referencesChanged)
            } catch {
                importMessage = (error as? APIError)?.serverMessage ?? "Import failed — is it valid JSON?"
            }
        }
    }

    private func createToken() {
        busy = true
        tokenMessage = nil
        Task {
            defer { busy = false }
            do {
                let created: APITokenRow = try await app.api.post(APIPath.apiTokens, json: ["name": "Chrome clipper"])
                newToken = created.token
                await refreshTokens()
            } catch {
                tokenMessage = (error as? APIError)?.serverMessage ?? "Could not create a token."
            }
        }
    }

    private func revoke(_ id: Int) {
        busy = true
        tokenMessage = nil
        Task {
            defer { busy = false }
            do {
                let _: EmptyResponse = try await app.api.delete(APIPath.apiToken(id))
                tokens.removeAll { $0.id == id }
            } catch {
                tokenMessage = (error as? APIError)?.serverMessage ?? "Could not revoke the token."
            }
        }
    }
}

/// The one moment a clipper token's plaintext exists on screen (web
/// `NewTokenDialog`): no backdrop dismiss, tap the token to copy.
struct NewTokenDialog: View {
    let token: String
    let onClose: () -> Void
    @State private var copied = false

    var body: some View {
        LooreDialogCard(title: "Your clipper token") {
            DialogBodyText(text: "Paste it into the extension's options now. It is shown only this once: Loore keeps just a fingerprint of it, so after you close this there is no way to see it again. If it gets lost, revoke it and create another.")
            Button {
                UIPasteboard.general.string = token
                copied = true
                Task {
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    copied = false
                }
            } label: {
                HStack(alignment: .top) {
                    Text(token)
                        .font(.system(size: 13, design: .monospaced))
                        .foregroundStyle(LooreColor.textPrimary)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: copied ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 13))
                        .foregroundStyle(copied ? LooreColor.success : LooreColor.textMuted)
                }
                .padding(12)
                .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.control))
                .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Copy token to clipboard")
            Text("Tap the token to copy it.")
                .font(LooreFont.sans(12.5, .light))
                .foregroundStyle(LooreColor.textMuted)
            ChoiceButton(title: "Done, I've saved it", subtitle: "Closes this for good.", tone: .accent, action: onClose)
                .accessibilityIdentifier("token.done")
        }
    }
}

private struct SmallFieldStyle: ViewModifier {
    func body(content: Content) -> some View {
        content
            .font(LooreFont.sans(13.6, .light))
            .foregroundStyle(LooreColor.textPrimary)
            .padding(.vertical, 8)
            .padding(.horizontal, 12)
            .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
    }
}

/// The external cards' accent-filled button (`buttonStyle`).
private struct FilledSmallButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(LooreFont.sans(13.6, .regular))
            .foregroundStyle(LooreColor.bgDeep)
            .padding(.vertical, 8)
            .padding(.horizontal, 18)
            .background(configuration.isPressed ? LooreColor.accentHover : LooreColor.accent,
                        in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .opacity(isEnabled ? 1 : 0.5)
    }
}

/// `ghostButtonStyle`: outline, muted.
private struct GhostSmallButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(LooreFont.sans(13.6, .regular))
            .foregroundStyle(LooreColor.textMuted)
            .padding(.vertical, 8)
            .padding(.horizontal, 18)
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
            .opacity(isEnabled ? 1 : 0.5)
    }
}
