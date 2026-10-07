import Foundation
import Observation

/// In-text links to other Loore nodes (port of `utils/nodeLinks.js`): which
/// hrefs are node links, and the node titles they render as.
enum NodeLinks {
    static let hosts: Set<String> = ["loore.org", "www.loore.org", "staging.loore.org"]

    /// The node id an href points at, or nil. Accepts app-relative
    /// `/node/<digits>` (optional trailing slash) and absolute http(s) URLs on
    /// a Loore host or on `currentOrigin` (the environment's frontend origin,
    /// the web's `window.location.origin`).
    static func nodeId(_ href: String?, currentOrigin: URL? = nil) -> Int? {
        guard let href, !href.isEmpty else { return nil }
        let path: String
        if href.hasPrefix("/") {
            path = href
        } else {
            guard let url = URL(string: href), let scheme = url.scheme?.lowercased(),
                  scheme == "http" || scheme == "https", let host = url.host?.lowercased() else { return nil }
            let sameOrigin = currentOrigin.map { origin in
                origin.scheme?.lowercased() == scheme && origin.host?.lowercased() == host && origin.port == url.port
            } ?? false
            guard hosts.contains(host) || sameOrigin else { return nil }
            path = url.path(percentEncoded: true).isEmpty ? "/" : url.path(percentEncoded: true)
        }
        guard let m = JSRegex.firstMatch(path, #"^/node/(\d+)/?$"#), let digits = m[1] else { return nil }
        return Int(digits)
    }
}

/// What `GET /api/nodes/titles` said about a node.
enum NodeTitleRecord: Equatable, Sendable {
    case title(String)
    /// Soft-deleted: rendered "[Node deleted]".
    case deleted
    /// Missing or not visible to the viewer: "[Node inaccessible]".
    case inaccessible
    /// Visible but without a title: the raw link text stays.
    case untitled
}

/// Node-title lookups shared by every markdown body (the web's module-level
/// cache): lookups asked for in the same run-loop turn share one request,
/// answers are cached for the session, failures are not cached.
@MainActor
@Observable
final class NodeTitleStore {
    typealias Fetch = @Sendable ([Int]) async throws -> [Int: NodeTitlesResponse.Title?]

    private(set) var records: [Int: NodeTitleRecord] = [:]
    @ObservationIgnored private var inFlight: Set<Int> = []
    @ObservationIgnored private var queued: [Int] = []
    @ObservationIgnored private var flushScheduled = false
    @ObservationIgnored private var waiters: [Int: [CheckedContinuation<NodeTitleRecord?, Never>]] = [:]
    @ObservationIgnored var fetch: Fetch?
    /// The server answers at most 50 ids per request.
    static let batchSize = 50

    init(fetch: Fetch? = nil) {
        self.fetch = fetch
    }

    /// The cached record, or nil while unknown (after requesting it).
    func record(for id: Int) -> NodeTitleRecord? {
        if let known = records[id] { return known }
        request(id)
        return nil
    }

    /// Queues a lookup (no-op when cached or already in flight).
    func request(_ id: Int) {
        guard records[id] == nil, !inFlight.contains(id), !queued.contains(id) else { return }
        queued.append(id)
        scheduleFlush()
    }

    /// Resolves the record for `id`; nil when the lookup failed.
    func lookup(_ id: Int) async -> NodeTitleRecord? {
        if let known = records[id] { return known }
        return await withCheckedContinuation { continuation in
            waiters[id, default: []].append(continuation)
            request(id)
        }
    }

    func reset() {
        records = [:]
        inFlight = []
        queued = []
    }

    private func scheduleFlush() {
        guard !flushScheduled else { return }
        flushScheduled = true
        // One request per run-loop turn, like the web's microtask flush.
        DispatchQueue.main.async { [weak self] in
            Task { @MainActor in await self?.flush() }
        }
    }

    private func flush() async {
        flushScheduled = false
        let ids = queued
        queued = []
        guard !ids.isEmpty else { return }
        inFlight.formUnion(ids)
        for start in stride(from: 0, to: ids.count, by: Self.batchSize) {
            let batch = Array(ids[start..<min(start + Self.batchSize, ids.count)])
            do {
                guard let fetch else { throw CancellationError() }
                let answer = try await fetch(batch)
                for id in batch {
                    let record: NodeTitleRecord
                    if let entry = answer[id], let title = entry {
                        if title.deleted { record = .deleted }
                        else if let text = title.title, !text.isEmpty { record = .title(text) }
                        else { record = .untitled }
                    } else {
                        record = .inaccessible
                    }
                    records[id] = record
                    inFlight.remove(id)
                    resume(id, with: record)
                }
            } catch {
                for id in batch {
                    inFlight.remove(id)
                    resume(id, with: nil)
                }
            }
        }
    }

    private func resume(_ id: Int, with record: NodeTitleRecord?) {
        for continuation in waiters.removeValue(forKey: id) ?? [] {
            continuation.resume(returning: record)
        }
    }
}
