import Foundation
import Observation

/// Moving from a thread page to another node's page (an ancestor or a reply
/// tapped, a reply that finished or continues on a new node, a sent entry, a
/// delete, …): the node is fetched while the current page stays, and its page
/// opens with the data in hand, so there is no "Loading node..." page in
/// between. A slow answer opens the page anyway, and the page waits for the
/// same request (it is not sent twice); a failed one opens the page, which
/// shows its error. While it waits, the thread page shows a spinner
/// (`isPending`). Another node meanwhile replaces the first, so a misclick can
/// be corrected before its page opens; the same node again (a double tap)
/// keeps the first request. Nothing opens when the screen that asked is no
/// longer on top (the back button, another tab). The web does the same
/// (useNodePrefetch.js, NodeDetailWrapper.js).
@MainActor
@Observable
final class NodePrefetch {
    static let shared = NodePrefetch()

    /// A node's fetch, handed to its page: the node, or the request still running
    /// (or failed, for the page to show its error).
    enum Handoff {
        case arrived(NodeDetail)
        case inFlight(Task<NodeDetail, Error>)
    }

    /// Longest a move waits for the node before its page opens (heuristic, 2 s).
    let maxWait: Double
    /// How long a fetched node may be handed to its page (heuristic, 10 s).
    let freshFor: Double

    private struct Move {
        let generation: Int
        let id: Int
        let request: Task<NodeDetail, Error>
        let router: Router
        let tab: AppTab
        let path: [AppRoute]
        let open: () -> Void
    }

    @ObservationIgnored private var fetched: [Int: (handoff: Handoff, at: Date)] = [:]
    @ObservationIgnored private var move: Move?
    @ObservationIgnored private var timer: Task<Void, Never>?
    @ObservationIgnored private var generation = 0
    /// A node is being fetched.
    private(set) var isPending = false

    init(maxWait: Double = 2, freshFor: Double = 10) {
        self.maxWait = maxWait
        self.freshFor = freshFor
    }

    /// Fetches `id` (waiting at most `maxWait`), then calls `open` if the screen
    /// that asked is still on top. A move to another node cancels this one.
    func open(_ id: Int, app: AppState, then open: @escaping () -> Void) {
        if let move, move.id == id { return }
        cancel()
        generation += 1
        let current = generation
        let api = app.api
        let request = Task { try await api.nodeDetail(id) }
        let tab = app.router.selectedTab
        move = Move(generation: current, id: id, request: request, router: app.router, tab: tab,
                    path: app.router.path(for: tab), open: open)
        isPending = true
        let wait = UInt64(maxWait * 1_000_000_000)
        timer = Task { [weak self] in
            try? await Task.sleep(nanoseconds: wait)
            guard !Task.isCancelled else { return }
            self?.finish(current, .inFlight(request))
        }
        Task { [weak self] in
            let handoff: Handoff
            do { handoff = .arrived(try await request.value) } catch { handoff = .inFlight(request) }
            self?.finish(current, handoff)
        }
    }

    private func finish(_ generation: Int, _ handoff: Handoff) {
        guard let move, move.generation == generation else { return }
        self.move = nil
        timer?.cancel()
        timer = nil
        isPending = false
        guard move.router.selectedTab == move.tab, move.router.path(for: move.tab) == move.path else {
            move.request.cancel()
            return
        }
        fetched[move.id] = (handoff, Date())
        move.open()
    }

    /// Drops the move in flight and cancels its request.
    func cancel() {
        move?.request.cancel()
        move = nil
        timer?.cancel()
        timer = nil
        isPending = false
    }

    /// The fetch for a page that is starting, if fresh; handed out once (a later
    /// visit fetches afresh).
    func take(_ id: Int) -> Handoff? {
        guard let entry = fetched.removeValue(forKey: id),
              Date().timeIntervalSince(entry.at) < freshFor else { return nil }
        return entry.handoff
    }

    /// Tests: forget every move and fetch.
    func resetForTesting() {
        cancel()
        fetched = [:]
    }
}
