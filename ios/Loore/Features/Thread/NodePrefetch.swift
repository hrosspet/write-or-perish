import Foundation
import Observation

/// Opening a node from a thread (an ancestor or a reply): the node is fetched
/// while the current page stays, and its page opens with the data in hand, so
/// there is no "Loading node..." page in between. A slow or failed answer opens
/// the page anyway, which then loads as before. While it waits, the thread page
/// shows a spinner (`isPending`); another tap meanwhile replaces the first (a
/// misclick can be corrected before its page opens).
@MainActor
@Observable
final class NodePrefetch {
    static let shared = NodePrefetch()

    /// Longest a tap waits for the node before its page opens (heuristic).
    static let maxWait: Double = 2
    /// How long a fetched node may be handed to its page (heuristic).
    static let freshFor: Double = 10

    @ObservationIgnored private var fetched: [Int: (detail: NodeDetail, at: Date)] = [:]
    @ObservationIgnored private var task: Task<Void, Never>?
    /// A tapped node is being fetched.
    private(set) var isPending = false

    /// Fetches `id` (waiting at most `maxWait`), then calls `open`. A newer tap
    /// cancels this one.
    func open(_ id: Int, api: APIClient, then open: @escaping () -> Void) {
        task?.cancel()
        isPending = true
        task = Task {
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
            guard !Task.isCancelled else { return }
            if let detail { fetched[id] = (detail, Date()) }
            open()
            isPending = false
        }
    }

    /// The fetched node for a page that is starting, if fresh; handed out once.
    func take(_ id: Int) -> NodeDetail? {
        guard let entry = fetched.removeValue(forKey: id),
              Date().timeIntervalSince(entry.at) < Self.freshFor else { return nil }
        return entry.detail
    }
}
