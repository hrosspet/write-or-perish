import XCTest
@testable import Loore

/// #795: a tick (or the row "+") is shown at once and saved only over the text
/// the screen shows; refused because the text changed elsewhere, it is undone
/// and a toast says there is a newer version.
@MainActor
final class ContentEditQueueTests: XCTestCase {
    private let status = "- [ ] deploy\n- [ ] write the note"

    /// A screen holding one text, and the saves it was asked for.
    private final class Screen {
        var text: String
        var saves: [(content: String, base: String)] = []
        var toasts: [String] = []
        /// Answers the n-th save (0-based); nil saves.
        var failure: (Int) -> Error? = { _ in nil }
        /// Holds every save until released.
        var gate: CheckedContinuation<Void, Never>?
        var holdSaves = false

        init(_ text: String) { self.text = text }

        func target() -> ContentEditQueue.Target {
            ContentEditQueue.Target(
                onScreen: { [unowned self] in self.text },
                show: { [unowned self] in self.text = $0 },
                save: { [unowned self] content, base in
                    let index = self.saves.count
                    self.saves.append((content, base))
                    if self.holdSaves { await withCheckedContinuation { self.gate = $0 } }
                    if let error = self.failure(index) { throw error }
                },
                notify: { [unowned self] in self.toasts.append($0) },
                newerMessage: "newer")
        }
    }

    private let changed = APIError.server(status: 409, body: ServerErrorBody(error: "x", code: "content_changed"))

    private func tick(_ label: String) -> (String) -> String {
        { MarkdownEdits.toggleCheckbox($0, itemText: label, currentChecked: false) }
    }

    func testATickIsShownAtOnceAndSentWithTheShownTextAsItsBase() async {
        let screen = Screen(status)
        let queue = ContentEditQueue()
        queue.apply(tick("deploy"), to: screen.target()) { "failed \($0)" }
        XCTAssertEqual(screen.text, "- [x] deploy\n- [ ] write the note")
        await queue.settled()
        XCTAssertEqual(screen.saves.map(\.base), [status])
        XCTAssertEqual(screen.saves.map(\.content), ["- [x] deploy\n- [ ] write the note"])
        XCTAssertTrue(screen.toasts.isEmpty)
    }

    func testQuickTicksAreSentInOrderEachOnTheTextTheLastOneSaved() async {
        let screen = Screen(status)
        let queue = ContentEditQueue()
        queue.apply(tick("deploy"), to: screen.target()) { "failed \($0)" }
        queue.apply(tick("write the note"), to: screen.target()) { "failed \($0)" }
        XCTAssertEqual(screen.text, "- [x] deploy\n- [x] write the note")
        await queue.settled()
        XCTAssertEqual(screen.saves.map(\.base), [status, "- [x] deploy\n- [ ] write the note"])
        XCTAssertEqual(screen.saves.last?.content, "- [x] deploy\n- [x] write the note")
    }

    func testARefusedTickIsUndoneWithTheTicksWaitingBehindIt() async {
        let screen = Screen(status)
        screen.failure = { [changed] _ in changed }
        let queue = ContentEditQueue()
        queue.apply(tick("deploy"), to: screen.target()) { "failed \($0)" }
        queue.apply(tick("write the note"), to: screen.target()) { "failed \($0)" }
        await queue.settled()
        XCTAssertEqual(screen.text, status, "both undone")
        XCTAssertEqual(screen.saves.count, 1, "the second is not sent")
        XCTAssertEqual(screen.toasts, ["newer"])
    }

    func testAnotherFailureUndoesOnlyThatTick() async {
        let screen = Screen(status)
        screen.failure = { $0 == 0 ? APIError.server(status: 500, body: ServerErrorBody(error: "DB error")) : nil }
        let queue = ContentEditQueue()
        queue.apply(tick("deploy"), to: screen.target()) { "Couldn't save change — reverted (\($0))" }
        queue.apply(tick("write the note"), to: screen.target()) { "failed \($0)" }
        await queue.settled()
        XCTAssertEqual(screen.text, "- [ ] deploy\n- [x] write the note")
        XCTAssertEqual(screen.saves.last?.base, status, "the second is made on the text the server still has")
        XCTAssertEqual(screen.toasts, ["Couldn't save change — reverted (DB error)"])
    }

    func testAnUndoLeavesATextThatReplacedTheTickMeanwhile() async {
        let screen = Screen(status)
        screen.failure = { [changed] _ in changed }
        screen.holdSaves = true
        let queue = ContentEditQueue()
        queue.apply(tick("deploy"), to: screen.target()) { "failed \($0)" }
        while screen.gate == nil { await Task.yield() }
        // The screen loaded the entry again while the save was out.
        screen.text = "- [ ] deploy on Monday"
        screen.gate?.resume()
        await queue.settled()
        XCTAssertEqual(screen.text, "- [ ] deploy on Monday")
        XCTAssertEqual(screen.toasts, ["newer"])
    }

    func testNoChangeSendsNothing() async {
        let screen = Screen("- [x] deploy")
        let queue = ContentEditQueue()
        queue.apply(tick("deploy"), to: screen.target()) { "failed \($0)" }
        await queue.settled()
        XCTAssertTrue(screen.saves.isEmpty)
    }
}

/// The thread page's checklist sends the text it shows as `base_content`, and
/// a refusal undoes the tick (#795).
@MainActor
final class ThreadChecklistBaseTests: StubbedAppTestCase {
    private let focal = """
    {"id":10,"content":"- [ ] deploy\\n- [ ] write the note","node_type":"user","user":{"id":5,"username":"seowriter"},
     "privacy_level":"private","ai_usage":"chat","child_count":0,"children":[],"ancestors":[]}
    """

    private func loadedModel(put: @escaping (URLRequest) -> StubResponse) async -> ThreadModel {
        StubURLProtocol.install { [focal] request in
            let path = request.url?.path(percentEncoded: true) ?? ""
            if request.httpMethod == "PUT" { return put(request) }
            if path == "/api/nodes/10" { return .json(200, focal) }
            return .json(200, "{}")
        }
        let model = ThreadModel(nodeId: 10, awaitLLM: nil, app: app)
        await model.load()
        return model
    }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    func testATickSendsTheShownTextAsItsBase() async {
        let model = await loadedModel { [focal] _ in .json(200, #"{"node":\#(focal)}"#) }
        model.toggleCheckbox("deploy", checked: false)
        XCTAssertEqual(model.node?.content, "- [x] deploy\n- [ ] write the note")
        let sent = await eventually { self.calls.contains("PUT /api/nodes/10") }
        XCTAssertTrue(sent)
        XCTAssertEqual(body(of: "PUT /api/nodes/10")?["base_content"] as? String, "- [ ] deploy\n- [ ] write the note")
        XCTAssertEqual(body(of: "PUT /api/nodes/10")?["content"] as? String, "- [x] deploy\n- [ ] write the note")
    }

    func testATickOnAnEntryChangedElsewhereIsUndoneWithAToast() async {
        let model = await loadedModel { _ in
            .json(409, #"{"error":"There's a newer version of this text","code":"content_changed"}"#)
        }
        model.toggleCheckbox("deploy", checked: false)
        let toasted = await eventually { !self.app.toasts.toasts.isEmpty }
        XCTAssertTrue(toasted)
        XCTAssertEqual(app.toasts.toasts.last?.message, ThreadModel.newerEntryMessage)
        XCTAssertEqual(model.node?.content, "- [ ] deploy\n- [ ] write the note")
    }
}
