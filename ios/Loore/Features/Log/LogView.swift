import SwiftUI

/// The Log (web `components/Log.js`, map E §2): the user's thread roots,
/// newest first, with cursor paging, rename and delete.
struct LogView: View {
    @Environment(AppState.self) private var app
    @State private var cards: [LogCard] = []
    @State private var loading = true
    @State private var loadingMore = false
    @State private var error: String?
    @State private var loadMoreError = false
    @State private var hasMore = false
    @State private var nextCursor: String?
    @State private var renameTarget: LogCard?
    @State private var renaming = false
    @State private var deleteTarget: LogCard?
    @State private var searching = false

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                header.padding(.bottom, 24)
                if loading {
                    LoadingLine(text: "Loading log...")
                } else if let error {
                    Text(error).font(LooreFont.body).foregroundStyle(LooreColor.accent)
                } else if cards.isEmpty {
                    Text("Your private entries will appear here as you share thoughts with Loore.")
                        .font(LooreFont.sans(15.2, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .lineSpacing(5)
                } else {
                    ForEach(cards) { card in
                        BubbleView(data: BubbleData(card), actions: actions(card)) {
                            // The thread opens once its node is in (NodePrefetch), not on a loading page.
                            NodePrefetch.shared.openThread(card.newestNodeId ?? card.id, app: app)
                        }
                        .onAppear {
                            if card.id == cards.last?.id { loadMoreIfNeeded() }
                        }
                    }
                    footer
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(720)
        }
        .loorePageBackground()
        .toolbar(.hidden, for: .navigationBar)
        .refreshable { await fetch(first: true) }
        .task { if loading { await fetch(first: true) } }
        .onChange(of: app.signals.nodeCreated) { _, _ in Task { await fetch(first: true) } }
        .onChange(of: app.signals.logChanged) { _, _ in Task { await fetch(first: true) } }
        .looreDialog(isPresented: Binding(get: { renameTarget != nil }, set: { if !$0 { renameTarget = nil } })) {
            if let target = renameTarget {
                RenameThreadDialog(currentName: target.threadName ?? "",
                                   fallbackTitle: BubblePreview.split(target.preview).title,
                                   saving: renaming, onSave: { rename(target, to: $0) },
                                   onCancel: { renameTarget = nil })
            }
        }
        .looreDialog(isPresented: Binding(get: { deleteTarget != nil }, set: { if !$0 { deleteTarget = nil } })) {
            DeleteConfirmDialog(mode: .thread, onConfirm: { _ in deleteThread() }, onCancel: { deleteTarget = nil })
        }
        .sheet(isPresented: $searching) {
            SearchView(scope: .archive)
        }
    }

    private var header: some View {
        PageHeader(title: "Log") {
            Button {
                searching = true
            } label: {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 18, weight: .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Search your entries")
            .keyboardShortcut("k", modifiers: .command)
            .padding(-8)
        }
    }

    @ViewBuilder private var footer: some View {
        if loadingMore {
            Text("Loading more...").frame(maxWidth: .infinity).padding(20)
                .font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
        } else if loadMoreError {
            HStack(spacing: 4) {
                Text("Couldn't load more entries.")
                Button("Retry") { Task { await fetch(first: false) } }
                    .buttonStyle(.plain).foregroundStyle(LooreColor.accent)
            }
            .font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
            .frame(maxWidth: .infinity).padding(20)
        } else if hasMore {
            Button("Load more...") { Task { await fetch(first: false) } }
                .buttonStyle(.plain)
                .font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
                .frame(maxWidth: .infinity).padding(20)
        }
    }

    private func actions(_ card: LogCard) -> [BubbleAction] {
        var list: [BubbleAction] = []
        if card.canRename {
            list.append(BubbleAction(label: "Rename thread") { renameTarget = card })
        }
        list.append(BubbleAction(label: "Delete thread", destructive: true) { deleteTarget = card })
        return list
    }

    private func loadMoreIfNeeded() {
        guard hasMore, !loading, !loadingMore, !loadMoreError else { return }
        Task { await fetch(first: false) }
    }

    /// `GET /api/log?per_page=20[&cursor=…]`; later pages dedupe ids already held.
    private func fetch(first: Bool) async {
        if first { if cards.isEmpty { loading = true } } else { loadingMore = true }
        loadMoreError = false
        var query = [URLQueryItem(name: "per_page", value: "20")]
        if !first, let nextCursor { query.append(URLQueryItem(name: "cursor", value: nextCursor)) }
        do {
            let page: LogPage = try await app.api.get(APIPath.log, query: query)
            var held = Set(first ? [] : cards.map(\.id))
            let fresh = page.nodes.filter { held.insert($0.id).inserted }
            cards = first ? fresh : cards + fresh
            hasMore = page.hasMore
            nextCursor = page.nextCursor
            error = nil
        } catch {
            if first && cards.isEmpty { self.error = "Error loading log." } else if !first { loadMoreError = true }
        }
        loading = false
        loadingMore = false
    }

    private func threadOf(_ card: LogCard) -> Int { card.threadRootId }

    private func rename(_ target: LogCard, to name: String) {
        renaming = true
        Task {
            defer { renaming = false }
            do {
                let answer: ThreadNameResponse = try await app.api.put(APIPath.threadName(threadOf(target)),
                                                                      json: .object(["thread_name": .string(name)]))
                let saved = answer.threadName.flatMap { $0.isEmpty ? nil : $0 }
                cards = cards.map { card in
                    guard threadOf(card) == threadOf(target) else { return card }
                    var copy = card
                    copy.threadName = saved
                    return copy
                }
                renameTarget = nil
            } catch {
                app.toasts.show((error as? APIError)?.userMessage(fallback: "Error renaming thread.") ?? "Error renaming thread.",
                                duration: 4)
            }
        }
    }

    private func deleteThread() {
        guard let target = deleteTarget else { return }
        let rootId = threadOf(target)
        Task {
            defer { deleteTarget = nil }
            do {
                let answer: NodeDeleteResponse = try await app.api.delete(APIPath.node(rootId), query: [
                    URLQueryItem(name: "delete_descendants", value: "true"),
                ])
                let n = answer.scheduled ?? 1
                app.toasts.show("Deleted \(n) node\(n == 1 ? "" : "s")")
                let gone = Set(answer.deletedPinnedIds)
                cards.removeAll { threadOf($0) == rootId || gone.contains($0.id) || gone.contains(threadOf($0)) }
            } catch {
                app.toasts.show((error as? APIError)?.userMessage(fallback: "Error deleting thread.") ?? "Error deleting thread.",
                                duration: 4)
            }
        }
    }
}
