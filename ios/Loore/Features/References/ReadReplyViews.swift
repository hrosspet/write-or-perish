import SwiftUI

/// What a Community Archive read covered (web `ReadWindowLine`).
struct ReadWindowLine: View {
    let window: ReadWindow

    static func text(_ w: ReadWindow) -> String? {
        guard w.windowEnd != nil else { return nil }
        let from = LooreDateFormat.dateTime(w.windowStart)
        let to = LooreDateFormat.dateTime(w.windowEnd)
        var text = "Tweets from \(from) to \(to) (your time): \(enUS(w.tweets)) by \(enUS(w.accounts)) accounts."
        if let excluded = w.excluded, excluded > 0 {
            text += " \(enUS(excluded)) you had already read were left out."
        }
        return text
    }

    /// `Number(n || 0).toLocaleString('en-US')`.
    static func enUS(_ n: Int?) -> String { jsLocaleNumber(n ?? 0) }

    var body: some View {
        if let text = Self.text(window) {
            Text(text)
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textMuted)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 10)
                .accessibilityIdentifier("thread.readWindow")
        }
    }
}

/// The read state of a read reply's picks under the card (web `ReadReplyTail`).
struct ReadReplyTail: View {
    let nodeId: Int
    let unread: Int
    let total: Int
    var loaded = true
    let onMarkedAll: ([Int: Date]) -> Void

    @Environment(AppState.self) private var app
    @State private var marking = false

    /// "n unread" / "u of n unread", or "All read."; nil while loading or empty.
    static func stateText(unread: Int, total: Int, loaded: Bool) -> String? {
        guard loaded, total > 0 else { return nil }
        if unread == 0 { return "All read." }
        return unread == total ? "\(total) unread" : "\(unread) of \(total) unread"
    }

    var body: some View {
        if let text = Self.stateText(unread: unread, total: total, loaded: loaded) {
            HStack(spacing: 10) {
                Text(text)
                if unread > 0 {
                    MarkAllReadButton(nodeId: nodeId, onMarkedAll: onMarkedAll)
                }
            }
            .font(LooreFont.sans(12.8, .light))
            .foregroundStyle(LooreColor.textMuted)
            .padding(.top, 12)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("thread.readTail")
        }
    }
}

/// `POST /api/nodes/<id>/feed-picks/read` → `{read_at: {item_id: iso}}`.
private struct MarkAllReadButton: View {
    let nodeId: Int
    let onMarkedAll: ([Int: Date]) -> Void
    @Environment(AppState.self) private var app
    @State private var marking = false

    var body: some View {
        Button("Mark all as read") {
            marking = true
            Task {
                defer { marking = false }
                do {
                    let answer: FeedPicksReadAnswer = try await app.api.post(APIPath.feedPicksRead(nodeId))
                    onMarkedAll(answer.readAt)
                } catch {
                    app.toasts.show("Could not mark the list as read.", duration: 4)
                }
            }
        }
        .buttonStyle(.plain)
        .font(LooreFont.sans(12.8, .regular))
        .foregroundStyle(LooreColor.accentDim)
        .disabled(marking)
    }
}

/// The tweets a legacy Community Archive feed reply named (web `FeedPicks`,
/// map E §4.5): relevance margin, handle, tweet text, the model's reason,
/// date, "Open on X" (marks it read), verdict and read toggle. Owner only.
struct FeedPicksView: View {
    let nodeId: Int

    @Environment(AppState.self) private var app
    @State private var picks: [FeedPicksAnswer.Pick]?
    @State private var error: String?

    var body: some View {
        Group {
            if let error {
                Text(error).font(LooreFont.sans(12.8, .light)).foregroundStyle(LooreColor.accent)
            } else if let picks, !picks.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(picks.enumerated()), id: \.element.id) { index, pick in
                        row(pick, index: index)
                        HairlineDivider()
                    }
                    Group {
                        if picks.contains(where: { $0.item.readAt == nil }) {
                            MarkAllReadButton(nodeId: nodeId) { readAt in markedAll(readAt) }
                        } else {
                            Text("All read.")
                        }
                    }
                    .font(LooreFont.sans(12.8, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.top, 12)
                }
                .padding(.top, 12)
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Tweets picked from the Community Archive")
            }
        }
        .task { await load() }
    }

    private func row(_ pick: FeedPicksAnswer.Pick, index: Int) -> some View {
        let item = pick.item
        let read = item.readAt != nil
        return HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 4) {
                if let relevance = pick.relevance {
                    Text("\(relevance)%").font(LooreFont.sans(11.2, .regular)).foregroundStyle(LooreColor.textMuted)
                }
                if pick.recommended {
                    Circle().fill(LooreColor.accent).frame(width: 7, height: 7)
                }
            }
            .frame(width: 38)
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Button("@\(item.authorHandle ?? "")") {
                        if let url = URL(string: "https://x.com/\(item.authorHandle ?? "")") { app.open(.external(url)) }
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(LooreColor.textSecondary)
                    if pick.recommended { Text("recommended").foregroundStyle(LooreColor.accent) }
                    if let by = pick.pickedBy { Text(by == "random" ? "random sample" : by) }
                }
                .font(LooreFont.sans(12.8, .regular))
                .foregroundStyle(LooreColor.textMuted)
                .accessibilityValue(pick.relevance.map { "relevance \($0) percent" } ?? "")
                MarkdownView(markdown: item.content ?? "", style: .quote)
                if let why = pick.why, !why.isEmpty {
                    Text(why)
                        .font(LooreFont.sansOblique(13.6, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 10) {
                    if let posted = item.postedAt { Text(LooreDateFormat.date(posted, relative: false)) }
                    if let link = item.url, let url = URL(string: link) {
                        Button("Open on X") {
                            app.open(.external(url))
                            markReadOnOpen(index: index, item: item)
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(LooreColor.accentDim)
                    }
                    Spacer(minLength: 0)
                    ReferenceFeedbackControl(itemId: item.id, feedback: item.feedback, nodeId: nodeId,
                                             shared: item.feedbackShared) { verdict, readAt in
                        patch(index) {
                            $0.feedback = verdict
                            $0.feedbackShared = false
                            if let readAt { $0.readAt = readAt }
                        }
                    }
                    ReadToggleLink(itemId: item.id, nodeId: nodeId, readAt: item.readAt) { readAt in
                        patch(index) { $0.readAt = readAt }
                    }
                }
                .font(LooreFont.sans(12.5, .light))
                .foregroundStyle(LooreColor.textMuted)
            }
        }
        .padding(.vertical, 12)
        .opacity(read ? 0.55 : 1)
    }

    private func patch(_ index: Int, _ change: (inout ExternalItem) -> Void) {
        guard var list = picks, list.indices.contains(index) else { return }
        change(&list[index].item)
        picks = list
    }

    private func markedAll(_ readAt: [Int: Date]) {
        guard var list = picks else { return }
        for i in list.indices { if let date = readAt[list[i].item.id] { list[i].item.readAt = date } }
        picks = list
    }

    private func load() async {
        guard picks == nil else { return }
        do {
            let answer: FeedPicksAnswer = try await app.api.get(APIPath.feedPicks(nodeId))
            picks = answer.picks
        } catch {
            self.error = "Could not load the picks."
        }
    }

    /// "Open on X" logs the open and marks the pick read (`{node_id, via:'open'}`).
    private func markReadOnOpen(index: Int, item: ExternalItem) {
        Task {
            do {
                let answer: ReadMarkAnswer = try await app.api.post(
                    APIPath.externalItemRead(item.id), json: ["node_id": .int(nodeId), "via": "open"])
                patch(index) { $0.readAt = answer.readAt }
            } catch {
                app.toasts.show("Could not update the read mark.", duration: 4)
            }
        }
    }
}

/// "Mark as read" / "Mark as unread" for one reference (web `ReferenceReadToggle`).
struct ReadToggleLink: View {
    let itemId: Int
    var nodeId: Int?
    let readAt: Date?
    let onChange: (Date?) -> Void

    @Environment(AppState.self) private var app
    @State private var marking = false

    var body: some View {
        Button(readAt != nil ? "Mark as unread" : "Mark as read") {
            marking = true
            var body: [String: JSONValue] = [:]
            if let nodeId { body["node_id"] = .int(nodeId) }
            Task {
                defer { marking = false }
                do {
                    let path = APIPath.externalItemRead(itemId)
                    let answer: ReadMarkAnswer = readAt != nil
                        ? try await app.api.send(.json(.delete, path, .object(body)))
                        : try await app.api.post(path, json: .object(body))
                    onChange(answer.readAt)
                } catch {
                    app.toasts.show("Could not update the read mark.", duration: 4)
                }
            }
        }
        .buttonStyle(.plain)
        .font(LooreFont.sans(12.8, .regular))
        .foregroundStyle(readAt != nil ? LooreColor.textMuted : LooreColor.accentDim)
        .disabled(marking)
    }
}
