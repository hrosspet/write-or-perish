import XCTest
@testable import Loore

/// Moving to another node from a thread page: the page stays (with a spinner)
/// until the node is fetched, then the node's page opens with the data in hand.
@MainActor
final class NodePrefetchTests: StubbedAppTestCase {
    private func nodeJSON(_ id: Int) -> String {
        """
        {"id":\(id),"content":"node \(id)","node_type":"user","user":{"id":5,"username":"seowriter"},
         "privacy_level":"private","ai_usage":"chat","child_count":0,"children":[],"ancestors":[]}
        """
    }

    /// Answers node GETs after `delay` seconds.
    private func stubNodes(delay: TimeInterval = 0) {
        StubURLProtocol.install { request in
            let path = request.url?.path(percentEncoded: true) ?? ""
            guard path.hasPrefix("/api/nodes/"), let id = Int(path.dropFirst("/api/nodes/".count)) else {
                return .json(200, "{}")
            }
            var stub = StubResponse.json(200, self.nodeJSON(id))
            stub.chunkDelay = delay
            return stub
        }
    }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    private func nodeGets(_ id: Int) -> Int { calls.filter { $0 == "GET /api/nodes/\(id)" }.count }

    func testOpensOnceTheNodeIsInAndHandsItOverOnce() async {
        stubNodes()
        let prefetch = NodePrefetch()
        var opened = 0
        prefetch.open(7, app: app) { opened += 1 }
        XCTAssertTrue(prefetch.isPending)
        XCTAssertEqual(opened, 0)
        let done = await eventually { opened == 1 }
        XCTAssertTrue(done)
        XCTAssertFalse(prefetch.isPending)
        guard case .arrived(let detail)? = prefetch.take(7) else { return XCTFail("expected the node") }
        XCTAssertEqual(detail.id, 7)
        XCTAssertNil(prefetch.take(7))
    }

    func testASlowNodeOpensAfterTheWaitAndThePageGetsTheSameRequest() async throws {
        stubNodes(delay: 0.4)
        let prefetch = NodePrefetch(maxWait: 0.1)
        var opened = 0
        prefetch.open(8, app: app) { opened += 1 }
        let done = await eventually { opened == 1 }
        XCTAssertTrue(done)
        guard case .inFlight(let request)? = prefetch.take(8) else { return XCTFail("expected the request") }
        let detail = try await request.value
        XCTAssertEqual(detail.id, 8)
        XCTAssertEqual(nodeGets(8), 1)
    }

    func testAnotherNodeReplacesTheFirst() async {
        stubNodes(delay: 0.2)
        let prefetch = NodePrefetch()
        var openedFirst = 0
        var openedSecond = 0
        prefetch.open(1, app: app) { openedFirst += 1 }
        prefetch.open(2, app: app) { openedSecond += 1 }
        let done = await eventually { openedSecond == 1 }
        XCTAssertTrue(done)
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(openedFirst, 0)
        XCTAssertNil(prefetch.take(1))
    }

    func testTheSameNodeAgainKeepsTheFirstRequest() async {
        stubNodes(delay: 0.2)
        let prefetch = NodePrefetch()
        var opened = 0
        prefetch.open(3, app: app) { opened += 1 }
        prefetch.open(3, app: app) { opened += 1 }
        let done = await eventually { opened == 1 }
        XCTAssertTrue(done)
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(opened, 1)
        XCTAssertEqual(nodeGets(3), 1)
    }

    func testATextModeSendOpensTheEntryOnceItsNodeIsIn() async {
        stubNodes(delay: 0.2)
        let tab = app.router.selectedTab
        TextModeView.open(NodeFormResult(id: 21, userNodeId: 21, llmNodeId: 22), app: app)
        XCTAssertTrue(NodePrefetch.shared.isPending)
        XCTAssertEqual(app.router.path(for: tab), [])
        let opened = await eventually { self.app.router.path(for: tab).last == .thread(id: 21, awaitLLM: 22) }
        XCTAssertTrue(opened)
        XCTAssertFalse(NodePrefetch.shared.isPending)
        XCTAssertEqual(nodeGets(21), 1)
    }

    func testNothingOpensWhenTheScreenThatAskedIsGone() async {
        stubNodes(delay: 0.2)
        let tab = app.router.selectedTab
        app.router.setPath([.thread(id: 10, awaitLLM: nil)], for: tab)
        let prefetch = NodePrefetch()
        var opened = 0
        prefetch.open(4, app: app) { opened += 1 }
        app.router.pop()
        let settled = await eventually { !prefetch.isPending }
        XCTAssertTrue(settled)
        XCTAssertEqual(opened, 0)
        XCTAssertNil(prefetch.take(4))
    }
}
