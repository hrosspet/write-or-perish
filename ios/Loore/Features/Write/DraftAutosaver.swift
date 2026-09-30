import Foundation
import Observation

/// Server drafts for the writing form (port of `hooks/useDraft.js`, map D §4.3):
/// one draft per `(user, node_id)` for edits or `(user, parent_id)` for new
/// entries. Typing saves after a 1 s pause; a 3 s interval also flushes;
/// one save at a time; a failed save is retried on the next tick.
@MainActor
@Observable
final class DraftAutosaver {
    private(set) var draft: Draft?
    private(set) var isLoaded = false
    private(set) var lastSaved: Date?
    private(set) var isSaving = false

    @ObservationIgnored private let nodeId: Int?
    @ObservationIgnored private let parentId: Int?
    @ObservationIgnored private let api: APIClient
    @ObservationIgnored private var pending: String?
    @ObservationIgnored private var debounceTask: Task<Void, Never>?
    @ObservationIgnored private var intervalTask: Task<Void, Never>?
    @ObservationIgnored let debounceDelay: TimeInterval
    @ObservationIgnored let autoSaveInterval: TimeInterval

    init(api: APIClient, nodeId: Int?, parentId: Int?, debounceDelay: TimeInterval = 1, autoSaveInterval: TimeInterval = 3) {
        self.api = api
        self.nodeId = nodeId
        self.parentId = parentId
        self.debounceDelay = debounceDelay
        self.autoSaveInterval = autoSaveInterval
    }

    private var query: [URLQueryItem] {
        var items: [URLQueryItem] = []
        if let nodeId { items.append(URLQueryItem(name: "node_id", value: String(nodeId))) }
        if let parentId { items.append(URLQueryItem(name: "parent_id", value: String(parentId))) }
        return items
    }

    /// `GET /api/drafts/?…` (404 = no draft). Starts the flush interval.
    func load() async {
        defer { isLoaded = true }
        startInterval()
        do {
            let loaded: Draft = try await api.get(APIPath.drafts, query: query)
            draft = loaded
            lastSaved = loaded.updatedAt
        } catch {
            draft = nil
        }
    }

    /// Queues `content` (the form only calls this with non-blank text).
    func save(_ content: String) {
        pending = content
        debounceTask?.cancel()
        debounceTask = Task { [weak self, debounceDelay] in
            try? await Task.sleep(nanoseconds: UInt64(debounceDelay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.flush()
        }
    }

    /// Saves the pending content now (also used before the form goes away).
    func flush() async {
        guard let content = pending, !isSaving else { return }
        isSaving = true
        defer { isSaving = false }
        var body: [String: JSONValue] = ["content": .string(content)]
        body["node_id"] = .optional(nodeId)
        body["parent_id"] = .optional(parentId)
        do {
            let saved: Draft = try await api.post(APIPath.drafts, json: .object(body))
            lastSaved = saved.updatedAt ?? Date()
            if pending == content { pending = nil }
        } catch {
            // Kept pending: the next interval tick retries.
        }
    }

    /// `DELETE /api/drafts/?…` after a send, a transcription or "Discard draft".
    func delete() async {
        pending = nil
        debounceTask?.cancel()
        _ = try? await api.delete(APIPath.drafts, query: query, as: EmptyResponse.self)
        draft = nil
        lastSaved = nil
    }

    func stop() {
        debounceTask?.cancel()
        intervalTask?.cancel()
    }

    private func startInterval() {
        intervalTask?.cancel()
        intervalTask = Task { [weak self, autoSaveInterval] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(autoSaveInterval * 1_000_000_000))
                guard !Task.isCancelled, let self else { return }
                if self.pending != nil && !self.isSaving { await self.flush() }
            }
        }
    }

    /// "just now" / "Nm ago" / "Nh ago" (computed when shown, not a live clock).
    static func timeAgo(_ date: Date, now: Date = Date()) -> String {
        let seconds = Int(now.timeIntervalSince(date))
        if seconds < 60 { return "just now" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)m ago" }
        return "\(minutes / 60)h ago"
    }
}
