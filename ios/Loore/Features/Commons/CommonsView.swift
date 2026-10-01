import SwiftUI

/// The public forum feed (web `CommonsPage`, map E §8.2): public roots by
/// everyone with sharing on, newest first, no vanity metrics. Behind
/// `share_v1_enabled`. A card opens the thread on the node id directly.
struct CommonsView: View {
    @Environment(AppState.self) private var app
    @State private var model = CommonsModel()

    var body: some View {
        Group {
            if !app.capabilities.shareEnabled {
                Text("Not available.")
                    .font(LooreFont.sans(14.4, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.loading {
                LoadingLine()
            } else if let error = model.error {
                Text(error).font(LooreFont.body).foregroundStyle(LooreColor.accent)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(20)
            } else {
                page
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .loorePageBackground()
        .toolbar(.hidden, for: .navigationBar)
        .task {
            guard app.capabilities.shareEnabled, model.items.isEmpty else { return }
            await model.fetch(page: 1, api: app.api)
        }
    }

    private var page: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                header.padding(.bottom, 24)
                if model.items.isEmpty {
                    Text("Nothing public yet.")
                        .font(LooreFont.sans(14.4, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .frame(maxWidth: .infinity)
                        .padding(.top, 48)
                } else {
                    ForEach(model.items) { item in
                        CommonsCard(item: item) { app.open(.thread(id: item.id, awaitLLM: nil)) }
                    }
                }
                if model.loadingMore {
                    Text("Loading more...").frame(maxWidth: .infinity).padding(20)
                        .font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
                } else if model.hasMore {
                    Button("Load more...") { Task { await model.fetch(page: model.page + 1, api: app.api) } }
                        .buttonStyle(.plain)
                        .font(LooreFont.body).foregroundStyle(LooreColor.textMuted)
                        .frame(maxWidth: .infinity).padding(20)
                        .accessibilityIdentifier("commons.loadMore")
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(720)
        }
        .refreshable { await model.fetch(page: 1, api: app.api) }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center) {
                Text("Commons")
                    .font(LooreFont.pageTitle)
                    .foregroundStyle(LooreColor.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Share →") { app.open(.share) }
                    .buttonStyle(.plain)
                    .font(LooreFont.sans(12.5, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .frame(minHeight: 44)
            }
            .padding(.bottom, 8)
            Text("What people here have chosen to make public.")
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textMuted)
                .padding(.bottom, 12.8)
            AccentRule()
        }
    }
}

/// One public root: username · date, the markdown content, "{n} responses".
struct CommonsCard: View {
    let item: CommonsItem
    let onOpen: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(item.username)
                Spacer()
                Text(LooreDateFormat.date(item.createdAt)).fixedSize()
            }
            .font(LooreFont.meta)
            .foregroundStyle(LooreColor.textMuted)
            .padding(.bottom, 10)
            MarkdownView(markdown: item.content, style: .referenceBody)
            if item.replyCount > 0 {
                Text("\(item.replyCount) \(item.replyCount == 1 ? "response" : "responses")")
                    .font(LooreFont.meta)
                    .foregroundStyle(LooreColor.textMuted.opacity(0.8))
                    .padding(.top, 12)
            }
        }
        .padding(.vertical, 24)
        .padding(.horizontal, 28)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.large))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.border))
        .contentShape(RoundedRectangle(cornerRadius: LooreRadius.large))
        .onTapGesture(perform: onOpen)
        .accessibilityAddTraits(.isButton)
    }
}

/// Paging state for the Commons (`GET /api/commons/feed?page=N`, click-to-load).
@MainActor
@Observable
final class CommonsModel {
    private(set) var items: [CommonsItem] = []
    private(set) var loading = true
    private(set) var loadingMore = false
    private(set) var error: String?
    private(set) var hasMore = false
    private(set) var page = 1

    func fetch(page pageNumber: Int, api: APIClient) async {
        let first = pageNumber == 1
        if first { if items.isEmpty { loading = true } } else { loadingMore = true }
        defer {
            loading = false
            loadingMore = false
        }
        do {
            let answer: CommonsPage = try await api.get(APIPath.commonsFeed, query: [
                URLQueryItem(name: "page", value: String(pageNumber)),
            ])
            items = first ? answer.items : items + answer.items
            hasMore = answer.hasMore
            page = pageNumber
            error = nil
        } catch {
            self.error = "Error loading the commons."
        }
    }
}
