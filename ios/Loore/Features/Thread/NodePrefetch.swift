import Foundation

/// Opening a node from a thread (an ancestor or a reply): the node is fetched
/// while the current page stays, and its page opens with the data in hand, so
/// there is no "Loading node..." page in between. A slow or failed answer opens
/// the page anyway, which then loads as before.
@MainActor
final class NodePrefetch {
    static let shared = NodePrefetch()

    /// Longest a tap waits for the node before its page opens (heuristic).
    static let maxWait: Double = 2
    /// How long a fetched node may be handed to its page (heuristic).
    static let freshFor: Double = 10

    private var fetched: [Int: (detail: NodeDetail, at: Date)] = [:]
    private var pending = false

    /// Fetches `id` (waiting at most `maxWait`), then calls `open`. Taps while
    /// one is pending are ignored.
    func open(_ id: Int, api: APIClient, then open: @escaping () -> Void) {
        guard !pending else { return }
        pending = true
        Task {
            let detail: NodeDetail? = await withTaskGroup(of: NodeDetail?.self) { group in
                group.addTask { try? await api.nodeDetail(id) }
                group.addTask {
                    try? await Task.sleep(nanoseconds: UInt64(Self.maxWait * 1_000_000_000))
                    return nil
                }
                let first = await group.next() ?? nil
                group.cancelAll()
                return first
            }
            if let detail { fetched[id] = (detail, Date()) }
            pending = false
            open()
        }
    }

    /// The fetched node for a page that is starting, if fresh; handed out once.
    func take(_ id: Int) -> NodeDetail? {
        guard let entry = fetched.removeValue(forKey: id),
              Date().timeIntervalSince(entry.at) < Self.freshFor else { return nil }
        return entry.detail
    }
}
