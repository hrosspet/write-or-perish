import XCTest
@testable import Loore

private func json(_ body: [String: Any]) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: body), encoding: .utf8)!
}

/// #799: an edit form's Save is made only over the text the form was opened on.
/// It sends that text as `base_content`; refused because the entry or the
/// artifact changed since (409 `content_changed`), the form keeps the person's
/// text, stays open and says what to do. Edit autosaves send the same base.
@MainActor
final class EditFormBaseTests: StubbedAppTestCase {
    private let openedOn = "Call the bank on Monday.\r\n- [ ] deploy\n"

    private func nodeJSON(_ content: String) -> String {
        json(["id": 9, "content": content, "node_type": "user", "user": ["id": 5, "username": "seowriter"],
              "privacy_level": "private", "ai_usage": "chat", "child_count": 0, "children": [], "ancestors": []])
    }

    /// An edit form opened on `openedOn`; the server's entry is `current`
    /// (the same unless a test says otherwise) and its draft `draftJSON`.
    private func editForm(current: String? = nil, draftJSON: String? = nil,
                          put: @escaping (URLRequest) -> StubResponse) -> (NodeFormModel, () -> NodeFormResult?) {
        let node = nodeJSON(current ?? openedOn)
        StubURLProtocol.install { request in
            let path = request.url?.path(percentEncoded: true)
            if request.httpMethod == "PUT" { return put(request) }
            if request.httpMethod == "GET" && path == "/api/nodes/9" { return .json(200, node) }
            if request.httpMethod == "GET" && path == "/api/drafts/" {
                return draftJSON.map { .json(200, $0) } ?? .json(404, #"{"error":"No draft found"}"#)
            }
            return .json(200, #"{"id":5,"content":"typed","updated_at":"2026-11-24T10:00:00Z"}"#)
        }
        let model = NodeFormModel(config: NodeFormConfig(edit: .init(nodeId: 9, initialContent: openedOn,
                                                                     initialPrivacy: .private, initialAIUsage: .chat)),
                                  app: app, defaults: defaults)
        var captured: NodeFormResult?
        model.onSuccess = { captured = $0 }
        return (model, { captured })
    }

    func testTheSaveSendsTheTextTheFormWasOpenedOnAsItsBase() async {
        let (model, result) = editForm { _ in .json(200, #"{"node":{"id":9,"content":"edited"},"descendants_updated":0}"#) }
        await model.start()
        model.content = "Call the bank on Tuesday."
        await model.submit()
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["base_content"] as? String, openedOn,
                       "exactly the page's text, line endings and all")
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["content"] as? String, "Call the bank on Tuesday.")
        XCTAssertNotNil(result())
        XCTAssertFalse(model.newerVersion)
    }

    func testASaveRefusedForANewerVersionKeepsTheTextAndSaysWhatToDo() async {
        let (model, result) = editForm { _ in
            .json(409, #"{"error":"There's a newer version of this text","code":"content_changed"}"#)
        }
        await model.start()
        model.content = "my long edit"
        await model.submit()
        XCTAssertNil(result(), "the form stays open")
        XCTAssertTrue(model.newerVersion)
        XCTAssertEqual(model.error, NodeFormModel.newerEntryEditMessage)
        XCTAssertEqual(model.content, "my long edit")
        XCTAssertFalse(calls.contains("DELETE /api/drafts/"), "the draft keeps the text too")
    }

    func testAnotherFailureIsNotCalledANewerVersion() async {
        let (model, _) = editForm { _ in .json(500, #"{"error":"DB error updating node"}"#) }
        await model.start()
        model.content = "edit"
        await model.submit()
        XCTAssertFalse(model.newerVersion)
        XCTAssertEqual(model.error, "DB error updating node")
    }

    func testEditAutosavesSendTheSameBase() async {
        let (model, _) = editForm { _ in .json(200, "{}") }
        await model.start()
        model.content = "typed"
        model.contentChanged(from: openedOn, to: "typed")
        await model.drafts.flush()
        XCTAssertEqual(body(of: "POST /api/drafts/")?["base_content"] as? String, openedOn)
        XCTAssertEqual(body(of: "POST /api/drafts/")?["node_id"] as? Int, 9)
    }

    func testANewEntrysDraftSendsNoBase() async {
        StubURLProtocol.install { _ in .json(200, #"{"id":5,"content":"typed","updated_at":"2026-11-24T10:00:00Z"}"#) }
        let drafts = DraftAutosaver(api: app.api, nodeId: nil, parentId: 7, base: "ignored",
                                    debounceDelay: 5, autoSaveInterval: 60)
        drafts.save("typed")
        await drafts.flush()
        XCTAssertNil(body(of: "POST /api/drafts/")?["base_content"])
    }

    // #799 review 1: another device's Save deleted every draft of the entry,
    // this form's included. On the refusal the form saves its text again at
    // once, with its base, so going back does not lose it.
    func testARefusedSaveSavesTheTextAsADraftAtOnce() async {
        let (model, _) = editForm { _ in
            .json(409, #"{"error":"There's a newer version of this text","code":"content_changed"}"#)
        }
        await model.start()
        model.content = "my long edit"
        await model.submit()
        XCTAssertEqual(body(of: "POST /api/drafts/")?["content"] as? String, "my long edit")
        XCTAssertEqual(body(of: "POST /api/drafts/")?["base_content"] as? String, openedOn)
        XCTAssertTrue(model.hasDraft)
    }

    // #799 review 2: a Save deletes only the drafts the form showed, its own
    // row and the older edit on offer; another device's unsaved edit stays.
    func testASaveDeletesOnlyTheDraftsTheFormShowed() async {
        let older = #"{"id":3,"content":"older","node_id":9,"updated_at":"2026-11-20T14:03:00Z","older_than_entry":true}"#
        let (model, result) = editForm(draftJSON: older) { _ in
            .json(200, #"{"node":{"id":9,"content":"saved"},"descendants_updated":0}"#)
        }
        await model.start()
        XCTAssertNotNil(model.olderDraft)
        model.content = "typed on the current text"
        model.contentChanged(from: openedOn, to: model.content)
        await model.drafts.flush()  // the autosave lands: row 5
        await model.submit()
        XCTAssertNotNil(result())
        let deletes = StubURLProtocol.requests.filter { $0.httpMethod == "DELETE" }
        XCTAssertEqual(deletes.map { $0.url?.query ?? "" }, ["node_id=9&draft_id=3&draft_id=5"])
    }

    // #799 review 3: the page that opened the form still shows an older text
    // (after a refused Save). The form reads the entry and works on its
    // current text: editor, Save's base and draft's base.
    func testAFormOpenedFromAPageWithAnOlderTextWorksOnTheCurrentOne() async {
        let current = "Call the bank on Monday.\n- [x] deploy\n"
        let (model, _) = editForm(current: current) { _ in
            .json(200, #"{"node":{"id":9,"content":"saved"},"descendants_updated":0}"#)
        }
        XCTAssertFalse(model.ready, "the editor waits")
        await model.start()
        XCTAssertTrue(model.ready)
        XCTAssertEqual(model.content, current)
        XCTAssertEqual(model.base, current)
        model.content = current + "more"
        model.contentChanged(from: current, to: model.content)
        await model.drafts.flush()
        XCTAssertEqual(body(of: "POST /api/drafts/")?["base_content"] as? String, current)
        await model.submit()
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["base_content"] as? String, current)
    }

    func testTheArtifactSaveCarriesItsBase() {
        let editing = ArtifactsPage.saveBody(content: "- [ ] a\n- [ ] c", description: " Ideas ", base: "- [ ] a")
        XCTAssertEqual(editing["base_content"], .string("- [ ] a"))
        XCTAssertEqual(editing["description"], .string("Ideas"))
        let empty = ArtifactsPage.saveBody(content: "- x", description: "Facts", base: "")
        XCTAssertEqual(empty["base_content"], .string(""), "an artifact with no version yet")
        let creating = ArtifactsPage.saveBody(content: "- x", description: "Facts", base: nil)
        XCTAssertNil(creating["base_content"])
    }
}

/// #799 review: a tick tapped while a quick-add is still saving waits for it.
/// When the quick-add lands on a newer list than the page showed (its re-fetch
/// brought one), the tick, made on the older list, is undone with a toast
/// instead of being saved on a version the page never showed.
@MainActor
final class TodoQuickAddQueueTests: StubbedAppTestCase {
    private let shown = "## Today\n\n- [ ] call mom\n"

    private func eventually(_ condition: () -> Bool) async -> Bool {
        for _ in 0..<200 {
            if condition() { return true }
            await Task.yield()
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        return condition()
    }

    /// The page shows `shown` (r1); the quick-add's re-fetch then finds
    /// `serverAfter` at `revision`.
    private func loadedModel(serverAfter: String, revision: String) async -> (TodoModel, DispatchSemaphore) {
        let m = TodoModel(app: app)
        StubURLProtocol.install { [shown] _ in
            .json(200, json(["todo": ["id": 1, "content": shown, "version_number": 1, "revision": "r1"]]))
        }
        await m.fetch()
        // The quick-add's re-fetch waits until the tick has been tapped.
        let gate = DispatchSemaphore(value: 0)
        StubURLProtocol.install { request in
            if request.httpMethod == "GET" {
                _ = gate.wait(timeout: .now() + 5)
                return .json(200, json(["todo": ["id": 2, "content": serverAfter, "version_number": 2,
                                                 "revision": revision]]))
            }
            let sent = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any]
            return .json(200, json(["todo": ["id": 2, "content": sent?["content"] ?? "", "version_number": 2,
                                             "revision": "r3"]]))
        }
        return (m, gate)
    }

    private var patches: [[String: Any]] {
        StubURLProtocol.requests.filter { $0.httpMethod == "PATCH" }.compactMap { request in
            request.httpBody.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        }
    }

    func testATickWaitingBehindAQuickAddThatLandedOnANewerListIsUndone() async {
        let phone = "## Today\n\n- [ ] call mom\n- [ ] from the phone\n"
        let (m, gate) = await loadedModel(serverAfter: phone, revision: "r2")
        let item = TodoSections.parse(m.todo?.content).first!.items.first!

        let added = Task { await m.quickAdd("add me") }
        let queued = await eventually { m.todo?.content.contains("add me") == true }
        XCTAssertTrue(queued)
        let ticked = Task { await m.toggle(item) }
        let shownTicked = await eventually { m.todo?.content.contains("- [x] call mom") == true }
        XCTAssertTrue(shownTicked, "shown at once")
        gate.signal()
        _ = await added.value
        await ticked.value

        XCTAssertEqual(patches.count, 1, "the tick is not sent")
        XCTAssertEqual(patches.first?["content"] as? String,
                       "## Today\n\n- [ ] call mom\n- [ ] from the phone\n- [ ] add me\n")
        XCTAssertEqual(m.todo?.content, "## Today\n\n- [ ] call mom\n- [ ] from the phone\n- [ ] add me\n",
                       "the newer list, without the tick")
        XCTAssertEqual(app.toasts.toasts.map(\.message), [TodoModel.newerTodoShownMessage])
    }

    func testATickWaitingBehindAQuickAddOnTheShownListIsSavedAfterIt() async {
        let (m, gate) = await loadedModel(serverAfter: shown, revision: "r1")
        let item = TodoSections.parse(m.todo?.content).first!.items.first!

        let added = Task { await m.quickAdd("add me") }
        _ = await eventually { m.todo?.content.contains("add me") == true }
        let ticked = Task { await m.toggle(item) }
        _ = await eventually { m.todo?.content.contains("- [x] call mom") == true }
        gate.signal()
        _ = await added.value
        await ticked.value

        XCTAssertEqual(patches.count, 2)
        XCTAssertEqual(patches.last?["content"] as? String, "## Today\n\n- [x] call mom\n- [ ] add me\n")
        XCTAssertEqual(patches.last?["base_revision"] as? String, "r3")
        XCTAssertTrue(app.toasts.toasts.isEmpty)
    }
}
