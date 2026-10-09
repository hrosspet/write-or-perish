import SwiftUI

/// The item helpers of `utils/references.js`, over the app's model.
extension ExternalItem {
    var sourceLabel: String { ReferenceUtils.sourceLabel(source: source, url: url) }
    var authorLabel: String? { ReferenceUtils.authorLabel(source: source, handle: authorHandle, name: authorName) }
    var tweetId: String? { ReferenceUtils.tweetId(source: source, externalId: externalId) }
    var youtubeVideo: ReferenceUtils.YouTubeVideo? { ReferenceUtils.youtubeVideo(source: source, url: url) }
    var bodyWithoutTitle: String { ReferenceUtils.bodyWithoutTitle(title: title, content: content ?? "") }
    var isTweet: Bool { ["twitter_bookmark", "community_archive", "read_pick"].contains(source) }

    /// `asCardNode`: the Log card shape (title as thread name, posted or fetched date, no replies).
    var cardData: BubbleData {
        BubbleData(id: id, preview: preview, threadName: title, username: authorHandle ?? "",
                   createdAt: postedAt ?? fetchedAt, childCount: 0)
    }
}

/// The saved references, newest saved first (web `ReferencesPage`, map E §4.1).
struct ReferencesView: View {
    @Environment(AppState.self) private var app
    @State private var items: [ExternalItem] = []
    @State private var loading = true
    @State private var loadingMore = false
    @State private var error: String?
    @State private var hasMore = false
    @State private var page = 1
    @State private var deleteTarget: ExternalItem?
    @State private var searching = false

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                header.padding(.bottom, 24)
                if loading {
                    LoadingLine(text: "Loading references...")
                } else if let error {
                    Text(error).font(LooreFont.body).foregroundStyle(LooreColor.accent)
                } else if items.isEmpty {
                    Text("Pages and tweets you save from elsewhere will appear here.")
                        .font(LooreFont.sans(15.2, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .lineSpacing(5)
                } else {
                    ForEach(items) { item in
                        BubbleView(data: item.cardData, actions: actions(item),
                                   tag: item.readAt != nil ? "Read · \(item.sourceLabel)" : item.sourceLabel,
                                   footerOverride: AnyView(ReferenceFooter(item: item))) {
                            app.open(.reference(id: item.id))
                        }
                        .onAppear { if item.id == items.last?.id { loadMoreIfNeeded() } }
                        .accessibilityElement(children: .contain)
            .accessibilityIdentifier("reference.card.\(item.id)")
                    }
                    if loadingMore {
                        Text("Loading more...").frame(maxWidth: .infinity).padding(20)
                            .font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
                    } else if hasMore {
                        Button("Load more...") { Task { await fetch(page + 1) } }
                            .buttonStyle(.plain)
                            .font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
                            .frame(maxWidth: .infinity).padding(20)
                    }
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(720)
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await fetch(1) }
        .task { if loading { await fetch(1) } }
        .onChange(of: app.signals.referencesChanged) { _, _ in Task { await fetch(1) } }
        .looreDialog(isPresented: Binding(get: { deleteTarget != nil }, set: { if !$0 { deleteTarget = nil } })) {
            DeleteConfirmDialog(mode: .reference, onConfirm: { _ in confirmDelete() }, onCancel: { deleteTarget = nil })
        }
        .sheet(isPresented: $searching) {
            SearchView(scope: .external)
        }
    }

    /// H2 "References" with a rule the title's width, and the search button.
    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 12.8) {
                Text("References")
                    .font(LooreFont.pageTitle)
                    .foregroundStyle(LooreColor.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                Rectangle().fill(LooreColor.accent.opacity(0.5)).frame(height: 1)
            }
            .fixedSize()
            Spacer()
            Button { searching = true } label: {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 18, weight: .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Search your references")
            .keyboardShortcut("k", modifiers: .command)
            .padding(-8)
        }
    }

    private func actions(_ item: ExternalItem) -> [BubbleAction] {
        var list: [BubbleAction] = []
        if let link = item.url, let url = URL(string: link) {
            list.append(BubbleAction(label: "Open source") { app.open(.external(url)) })
        }
        list.append(BubbleAction(label: "Delete", destructive: true) { deleteTarget = item })
        return list
    }

    private func loadMoreIfNeeded() {
        guard hasMore, !loading, !loadingMore else { return }
        Task { await fetch(page + 1) }
    }

    /// `GET /api/external/items?sort=saved&page=N&per_page=20`.
    private func fetch(_ pageNumber: Int) async {
        let first = pageNumber == 1
        if first { if items.isEmpty { loading = true } } else { loadingMore = true }
        defer { loading = false; loadingMore = false }
        do {
            let answer: ExternalItemsPage = try await app.api.get(APIPath.externalItems, query: [
                URLQueryItem(name: "sort", value: "saved"),
                URLQueryItem(name: "page", value: String(pageNumber)),
                URLQueryItem(name: "per_page", value: "20"),
            ])
            items = first ? answer.items : items + answer.items
            hasMore = answer.hasMore
            page = pageNumber
            error = nil
        } catch {
            self.error = "Error loading references."
        }
    }

    private func confirmDelete() {
        guard let target = deleteTarget else { return }
        Task {
            defer { deleteTarget = nil }
            do {
                let _: DeleteReferenceAnswer = try await app.api.delete(APIPath.externalItem(target.id))
                app.toasts.show("Reference deleted", duration: 3)
                items.removeAll { $0.id == target.id }
            } catch {
                app.toasts.show((error as? APIError)?.userMessage(fallback: "Error deleting reference.")
                                ?? "Error deleting reference.", duration: 4)
            }
        }
    }
}

/// `{author} · {yyyy/mm/dd HH:MM} · {host}` (web `ReferenceFooter`); the author
/// opens the account on X for tweets, the host opens the original.
struct ReferenceFooter: View {
    let item: ExternalItem
    @Environment(AppState.self) private var app

    var body: some View {
        HStack(spacing: 6) {
            if let author = item.authorLabel, !author.isEmpty {
                if item.isTweet, let handle = item.authorHandle, let url = URL(string: "https://x.com/\(handle)") {
                    Button(author) { app.open(.external(url)) }
                        .buttonStyle(.plain)
                        .lineLimit(1)
                } else {
                    Text(author).lineLimit(1)
                }
                dot
            }
            Text(LooreDateFormat.dateTime(item.postedAt ?? item.fetchedAt)).lineLimit(1).fixedSize()
            if let link = item.url {
                dot
                Button(Self.host(link) ?? "source") {
                    if let url = URL(string: link) { app.open(.external(url)) }
                }
                .buttonStyle(.plain)
                .lineLimit(1)
            }
        }
        .font(LooreFont.meta)
        .foregroundStyle(LooreColor.textMuted)
        .padding(.top, 12)
    }

    private var dot: some View {
        Text("·").foregroundStyle(LooreColor.border).accessibilityHidden(true)
    }

    /// `new URL(url).hostname` without a leading `www.`.
    static func host(_ link: String) -> String? {
        guard let host = URL(string: link)?.host, !host.isEmpty else { return nil }
        return host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
    }
}
