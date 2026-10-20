import SwiftUI

/// Search results (map E §3): nodes, and for semantic search also saved references.
struct SearchResult: Decodable, Identifiable, Sendable {
    var kind: String?
    var rawId: Int
    var preview: String?
    var snippet: String?
    var nodeType: String?
    var createdAt: Date?
    var username: String?
    var childCount: Int
    var score: Double?
    var source: String?
    var authorHandle: String?
    var title: String?
    var externalURL: String?

    var id: String { "\(kind ?? "node")-\(rawId)" }
    var isReference: Bool { kind == "external" }

    enum CodingKeys: String, CodingKey {
        case kind, preview, snippet, username, score, source, title
        case rawId = "id"
        case nodeType = "node_type"
        case createdAt = "created_at"
        case childCount = "child_count"
        case authorHandle = "author_handle"
        case externalURL = "external_url"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rawId = try c.decode(Int.self, forKey: .rawId)
        kind = c.tolerant(.kind)
        preview = c.tolerant(.preview)
        snippet = c.tolerant(.snippet)
        nodeType = c.tolerant(.nodeType)
        createdAt = c.tolerant(.createdAt)
        username = c.tolerant(.username)
        childCount = c.tolerant(.childCount, default: 0)
        score = c.tolerant(.score)
        source = c.tolerant(.source)
        authorHandle = c.tolerant(.authorHandle)
        title = c.tolerant(.title)
        externalURL = c.tolerant(.externalURL)
    }

    /// `utils/references.js` labels (the web has no URL here, so never "Video").
    var sourceLabel: String {
        let labels = ["web_clip": "Page", "twitter_bookmark": "Tweet", "community_archive": "Archive tweet",
                      "read_pick": "Archive tweet"]
        return labels[source ?? ""] ?? source ?? ""
    }

    var authorLabel: String? {
        guard let handle = authorHandle, !handle.isEmpty else { return nil }
        return source == "web_clip" ? handle : "@\(handle)"
    }
}

struct SearchResponse: Decodable, Sendable {
    var results: [SearchResult]
    var total: Int
    var hasMore: Bool

    enum CodingKeys: String, CodingKey {
        case results, total
        case hasMore = "has_more"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        results = c.tolerant(.results, default: [])
        total = c.tolerant(.total, default: 0)
        hasMore = c.tolerant(.hasMore, default: false)
    }
}

/// Search snippets arrive as HTML with `<mark>` around matches (not escaped by
/// the server): keep the text, highlight the marked runs, drop other tags.
enum SearchSnippet {
    struct Run: Equatable {
        var text: String
        var marked: Bool
    }

    static func runs(_ html: String) -> [Run] {
        var runs: [Run] = []
        var marked = false
        for part in JSRegex.splitKeepingCaptures(html, #"(<\s*/?\s*[A-Za-z][^>]*>)"#) {
            if part.hasPrefix("<"), JSRegex.test(part, #"^<\s*/?\s*[A-Za-z][^>]*>$"#) {
                let name = JSRegex.firstMatch(part.lowercased(), #"^<\s*(/?)\s*([a-z0-9]+)"#)
                if name?[2] == "mark" { marked = name?[1] != "/" }
                if name?[2] == "br" { runs.append(Run(text: "\n", marked: marked)) }
                continue
            }
            guard !part.isEmpty else { continue }
            let text = decodeEntities(part)
            if let last = runs.last, last.marked == marked {
                runs[runs.count - 1].text += text
            } else {
                runs.append(Run(text: text, marked: marked))
            }
        }
        return runs
    }

    static func decodeEntities(_ s: String) -> String {
        s.replacingOccurrences(of: "&lt;", with: "<").replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&quot;", with: "\"").replacingOccurrences(of: "&#39;", with: "'")
            .replacingOccurrences(of: "&#x27;", with: "'").replacingOccurrences(of: "&nbsp;", with: " ")
            .replacingOccurrences(of: "&amp;", with: "&")
    }

    static func attributed(_ html: String) -> AttributedString {
        var out = AttributedString()
        for run in runs(html) {
            var piece = AttributedString(run.text)
            if run.marked {
                piece.backgroundColor = LooreColor.accentGlow
                piece.foregroundColor = LooreColor.accent
            }
            out += piece
        }
        return out
    }
}

/// The search sheet (web `SearchModal`): a 300 ms debounce (every semantic
/// search is billed), an admin-only Semantic/Keyword switch, a date range.
struct SearchView: View {
    enum Scope { case archive, external }
    let scope: Scope

    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var semantic = true
    @State private var showDates = false
    @State private var from: Date?
    @State private var to: Date?
    @State private var results: [SearchResult] = []
    @State private var total = 0
    @State private var loading = false
    @State private var loadingMore = false
    @State private var hasSearched = false
    @State private var page = 1
    @FocusState private var focused: Bool

    private var external: Bool { scope == .external }

    var body: some View {
        VStack(spacing: 0) {
            inputRow
            HairlineDivider()
            if showDates {
                dateRow
                HairlineDivider()
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if loading {
                        message("Searching...")
                    } else if hasSearched && results.isEmpty {
                        message("No results found")
                    } else if !hasSearched {
                        message(external ? "Type to search your references" : "Type to search your entries")
                    } else {
                        ForEach(results) { row($0) }
                        if total > results.count && !semantic {
                            Button(loadingMore ? "Loading..." : "Show more (\(results.count) of \(total))") {
                                Task { await loadMore() }
                            }
                            .buttonStyle(LooreButtonStyle(kind: .outline, font: LooreFont.sans(13, .regular)))
                            .disabled(loadingMore)
                            .frame(maxWidth: .infinity)
                            .padding(12)
                            .onAppear { Task { await loadMore() } }
                        }
                    }
                }
                .padding(.vertical, 8)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .background(LooreColor.bgCard.ignoresSafeArea())
        .presentationDragIndicator(.visible)
        .onAppear { focused = true }
        .task(id: searchKey) {
            try? await Task.sleep(nanoseconds: 300_000_000)
            guard !Task.isCancelled else { return }
            await search()
        }
    }

    private var searchKey: String {
        "\(query)|\(from.map(Self.day) ?? "")|\(to.map(Self.day) ?? "")|\(semantic)"
    }

    private var inputRow: some View {
        HStack(spacing: 12) {
            Text("\u{2315}").font(.system(size: 18)).foregroundStyle(LooreColor.textMuted)
                .accessibilityHidden(true)
            TextField("", text: $query, prompt: loorePrompt(external ? "Search your references..." : "Search your entries..."))
                .font(LooreFont.sans(16, .regular))
                .foregroundStyle(LooreColor.textPrimary)
                .focused($focused)
                .submitLabel(.search)
                .autocorrectionDisabled()
                .accessibilityIdentifier("search.field")
            if app.capabilities.isAdmin {
                chip(semantic ? "Semantic" : "Keyword", on: semantic) { semantic.toggle() }
            }
            chip("Dates", on: showDates) { showDates.toggle() }
        }
        .padding(.vertical, 16)
        .padding(.horizontal, 20)
    }

    private var dateRow: some View {
        HStack(spacing: 10) {
            Text("From").foregroundStyle(LooreColor.textMuted)
            DateField(date: $from, label: "From date")
            Text("to").foregroundStyle(LooreColor.textMuted)
            DateField(date: $to, label: "To date")
            if from != nil || to != nil {
                Button("Clear") { from = nil; to = nil }
                    .buttonStyle(.plain).foregroundStyle(LooreColor.textMuted)
            }
            Spacer(minLength: 0)
        }
        .font(LooreFont.sans(13, .regular))
        .padding(.vertical, 10)
        .padding(.horizontal, 20)
    }

    private func chip(_ title: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(12, .regular))
                .foregroundStyle(on ? LooreColor.accent : LooreColor.textMuted)
                .padding(.vertical, 4)
                .padding(.horizontal, 10)
                .background(on ? LooreColor.accentSubtle : .clear, in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(on ? LooreColor.accentDim : LooreColor.border))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private func message(_ text: String) -> some View {
        Text(text).font(LooreFont.sans(14, .regular)).foregroundStyle(LooreColor.textMuted)
            .frame(maxWidth: .infinity).padding(24)
    }

    private func row(_ r: SearchResult) -> some View {
        Button {
            dismiss()
            // A thread opens once its node is in, with the spinner over the screen under
            // the sheet meanwhile (NodePrefetch), not on a loading page.
            if r.isReference {
                app.open(.reference(id: r.rawId))
            } else {
                NodePrefetch.shared.openThread(r.rawId, app: app)
            }
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text((r.isReference ? r.sourceLabel : (r.nodeType ?? "")).uppercased())
                        .font(LooreFont.sans(11, .regular)).tracking(0.5)
                        .foregroundStyle(LooreColor.accentDim)
                        .padding(.horizontal, 6).padding(.vertical, 1)
                        .background(LooreColor.accentSubtle, in: RoundedRectangle(cornerRadius: 3))
                    if r.isReference, let author = r.authorLabel { Text(author) }
                    Text(LooreDateFormat.date(r.createdAt))
                    if r.childCount > 0 { Text("\(r.childCount) \(r.childCount == 1 ? "reply" : "replies")") }
                    Spacer(minLength: 0)
                    if semantic, let score = r.score {
                        Text("\(Int((score * 100).rounded()))%")
                            .font(LooreFont.sans(11, .regular)).foregroundStyle(LooreColor.accent)
                            .padding(.horizontal, 6).padding(.vertical, 1)
                            .background(LooreColor.accentSubtle, in: RoundedRectangle(cornerRadius: 3))
                    }
                }
                .font(LooreFont.sans(12, .regular))
                .foregroundStyle(LooreColor.textMuted)
                if r.isReference, let title = r.title, !title.isEmpty {
                    Text(title).font(LooreFont.sans(14, .regular)).foregroundStyle(LooreColor.textPrimary).lineLimit(1)
                }
                Text(SearchSnippet.attributed(r.snippet ?? r.preview ?? ""))
                    .font(LooreFont.sans(14, .regular))
                    .foregroundStyle(LooreColor.textSecondary)
                    .lineSpacing(4)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 12)
            .padding(.horizontal, 20)
            .overlay(alignment: .bottom) { HairlineDivider() }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    static func day(_ date: Date) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    private func params(page: Int) -> [URLQueryItem] {
        var items: [URLQueryItem] = []
        let trimmed = query.jsTrimmed
        if !trimmed.isEmpty { items.append(URLQueryItem(name: "q", value: trimmed)) }
        if let from { items.append(URLQueryItem(name: "from", value: Self.day(from))) }
        if let to { items.append(URLQueryItem(name: "to", value: Self.day(to))) }
        items.append(URLQueryItem(name: "per_page", value: "20"))
        items.append(URLQueryItem(name: "page", value: String(page)))
        if external { items.append(URLQueryItem(name: "scope", value: "external")) }
        return items
    }

    private func search() async {
        if query.jsTrimmed.isEmpty && from == nil && to == nil {
            results = []
            total = 0
            hasSearched = false
            page = 1
            return
        }
        loading = true
        hasSearched = true
        page = 1
        defer { loading = false }
        do {
            let answer: SearchResponse = try await app.api.get(semantic ? APIPath.semanticSearch : APIPath.search,
                                                               query: params(page: 1))
            results = answer.results
            total = answer.total
        } catch {
            if (error as? APIError)?.isCancelled == true { return }
            results = []
            total = 0
        }
    }

    private func loadMore() async {
        guard !semantic, !loadingMore else { return }
        loadingMore = true
        defer { loadingMore = false }
        if let answer: SearchResponse = try? await app.api.get(APIPath.search, query: params(page: page + 1)) {
            results += answer.results
            total = answer.total
            page += 1
        }
    }
}

/// A compact optional date field (the web's `<input type="date">`).
private struct DateField: View {
    @Binding var date: Date?
    let label: String

    var body: some View {
        if let value = date {
            DatePicker(label, selection: Binding(get: { value }, set: { date = $0 }), displayedComponents: .date)
                .labelsHidden()
                .tint(LooreColor.accent)
        } else {
            Button("yyyy-mm-dd") { date = Date() }
                .accessibilityLabel(label)
                .accessibilityHint("Not set. Double-tap to pick a date.")
                .buttonStyle(.plain)
                .foregroundStyle(LooreColor.textMuted)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: 4))
                .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(LooreColor.border))
        }
    }
}
