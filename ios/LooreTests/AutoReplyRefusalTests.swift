import XCTest
import SwiftUI
@testable import Loore

// #747: when an entry is saved but the server refuses or fails its automatic AI
// reply, the person is told why, the way the neighbouring paths already do: a
// toast with the server's sentence, or, for a thread too long for a reply (#648),
// the reply form's limit state. Three paths used to drop it: Write New Entry typed
// with Agentic Reply off, Write New Entry recorded, and an edit on the thread page.

private let exportRefusal = "{user_export} without a token limit loads your whole archive into every reply "
    + "and is available on the Pro plan."
private let exportRefusalJSON = #"{"error":"\#(exportRefusal)"}"#
private let limitText = "This thread is too long for another AI reply. Each reply reads the whole thread, "
    + "including what it quotes, and this one has grown too large. You can still write here. "
    + "To keep talking with AI, start a new thread."
private let tooLongJSON = #"{"error":"\#(limitText)","code":"thread_too_long"}"#
private let focalJSON = """
{"id":10,"content":"entry","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"private",
 "ai_usage":"chat","child_count":0,"children":[],
 "ancestors":[{"id":9,"content":"root","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private"}]}
"""
private let spendCapJSON = #"{"error":"monthly_spend_limit_reached","message":"You've reached your monthly usage limit."}"#

final class LLMRequestErrorTests: XCTestCase {
    private func apiError(_ status: Int, _ body: String) -> APIError {
        APIError.from(status: status, contentType: "application/json", data: Data(body.utf8))
    }

    func testTheToastIsTheServersSentence() {
        XCTAssertEqual(LLMRequestError.toastMessage(apiError(400, exportRefusalJSON)), exportRefusal)
        let readOnly = #"{"error":"GPT-5.6 Luna is only for Read. Choose another model for replies.","code":"model_read_only"}"#
        XCTAssertEqual(LLMRequestError.toastMessage(apiError(400, readOnly)),
                       "GPT-5.6 Luna is only for Read. Choose another model for replies.")
    }

    func testWithoutASentenceTheThreadPagesText() {
        let html = APIError.from(status: 500, contentType: "text/html", data: Data("<html></html>".utf8))
        XCTAssertEqual(LLMRequestError.toastMessage(html), "Error requesting LLM response.")
        let offline = APIError.transport(code: NSURLErrorTimedOut, description: "The request timed out.")
        XCTAssertEqual(LLMRequestError.toastMessage(offline), "Error requesting LLM response.")
    }

    func testWhatShowsElsewhereIsNotToasted() {
        XCTAssertNil(LLMRequestError.toastMessage(apiError(402, spendCapJSON)), "the banner says it")
        XCTAssertNil(LLMRequestError.toastMessage(apiError(409, #"{"error":"x","code":"auto_generate_off"}"#)),
                     "not an error: LLM Response stays")
        XCTAssertNil(LLMRequestError.toastMessage(apiError(409, tooLongJSON)), "the reply form shows the limit")
    }
}

/// Write New Entry with Auto-generate on, against a stubbed server.
@MainActor
final class WriteNewEntryReplyRefusalTests: StubbedAppTestCase {
    private func writeNewEntry() -> (NodeFormModel, () -> NodeFormResult?) {
        defaults.set(false, forKey: DefaultsKey.agenticReply)
        defaults.set(true, forKey: DefaultsKey.autoGenerate)
        let model = NodeFormModel(config: NodeFormConfig(parentId: nil, allowAgenticPrompt: true), app: app,
                                  defaults: defaults)
        var captured: NodeFormResult?
        model.onSuccess = { captured = $0 }
        model.aiUsage = .chat
        model.privacy = .private
        model.content = "Use {user_export}"
        return (model, { captured })
    }

    private func sendTyped(llm: StubResponse) async -> NodeFormResult? {
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/nodes/": return .json(201, #"{"id":20,"privacy_level":"private"}"#)
            case "/api/nodes/20/llm": return llm
            default: return .json(200, "{}")
            }
        }
        let (model, result) = writeNewEntry()
        await model.submit()
        XCTAssertEqual(body(of: "POST /api/nodes/20/llm")?["auto_generate"] as? Bool, true)
        return result()
    }

    private func sendRecorded(saved: String) async -> NodeFormResult? {
        StubURLProtocol.install { request in
            request.url?.path(percentEncoded: true) == "/api/drafts/streaming/s1/save-as-node"
                ? .json(201, saved) : .json(200, "{}")
        }
        let (model, result) = writeNewEntry()
        model.streamingSessionId = "s1"
        await model.submit()
        XCTAssertEqual(body(of: "POST /api/drafts/streaming/s1/save-as-node")?["auto_generate"] as? Bool, true)
        return result()
    }

    private var toastMessages: [String] { app.toasts.toasts.map(\.message) }

    // MARK: Typed, Agentic Reply off

    func testATypedEntrysRefusedReplySaysWhyAndTheEntryOpens() async {
        let result = await sendTyped(llm: .json(400, exportRefusalJSON))
        XCTAssertEqual(toastMessages, [exportRefusal])
        XCTAssertEqual(result?.id, 20)
        XCTAssertNil(result?.awaitLLM)
    }

    func testATypedEntrysFailedReplySaysWhatTheThreadPageSays() async {
        let result = await sendTyped(llm: .html(500))
        XCTAssertEqual(toastMessages, ["Error requesting LLM response."])
        XCTAssertEqual(result?.id, 20)
    }

    func testATypedEntryTooLongForAReplyIsNotToasted() async {
        let result = await sendTyped(llm: .json(409, tooLongJSON))
        XCTAssertTrue(toastMessages.isEmpty, "the entry's page shows the limit")
        XCTAssertEqual(result?.id, 20)
    }

    func testATypedEntryOverTheUsageLimitLeavesItToTheBanner() async {
        let result = await sendTyped(llm: .json(402, spendCapJSON))
        XCTAssertTrue(toastMessages.isEmpty)
        XCTAssertEqual(result?.id, 20)
    }

    func testATypedEntrysReplyThatStartsOpensIt() async {
        let result = await sendTyped(llm: .json(202, #"{"node_id":21,"status":"pending"}"#))
        XCTAssertTrue(toastMessages.isEmpty)
        XCTAssertEqual(result?.id, 21)
        XCTAssertEqual(result?.awaitLLM, 21)
    }

    // MARK: Recorded

    func testARecordedEntrysRefusedReplySaysWhy() async {
        let result = await sendRecorded(saved: #"{"id":30,"user_node_id":30,"llm_error":"\#(exportRefusal)"}"#)
        XCTAssertEqual(toastMessages, [exportRefusal])
        XCTAssertEqual(result?.id, 30)
        XCTAssertNil(result?.awaitLLM)
    }

    func testARecordedEntryTooLongForAReplyIsNotToasted() async {
        let result = await sendRecorded(
            saved: #"{"id":30,"user_node_id":30,"llm_error":"\#(limitText)","llm_error_code":"thread_too_long"}"#)
        XCTAssertTrue(toastMessages.isEmpty, "the entry's page shows the limit")
        XCTAssertEqual(result?.llmErrorCode, "thread_too_long")
    }

    func testARecordedEntrysReplyThatStartsIsNotToasted() async {
        let result = await sendRecorded(saved: #"{"id":30,"user_node_id":30,"llm_node_id":31}"#)
        XCTAssertTrue(toastMessages.isEmpty)
        XCTAssertEqual(result?.awaitLLM, 31)
    }
}

/// The thread page's automatic reply after an edit or an inline reply.
@MainActor
final class ThreadAutoReplyRefusalTests: StubbedAppTestCase {
    private func loadedModel(llm: StubResponse) async -> ThreadModel {
        StubURLProtocol.install { request in
            let path = request.url?.path(percentEncoded: true) ?? ""
            if path == "/api/nodes/10" { return .json(200, focalJSON) }
            if path.hasSuffix("/llm") { return llm }
            return .json(200, "{}")
        }
        let model = ThreadModel(nodeId: 10, awaitLLM: nil, app: app)
        await model.load()
        return model
    }

    private func saveEdit(_ model: ThreadModel) async {
        guard let node = model.node else { return XCTFail("not loaded") }
        model.beginEdit(model.target(focal: node))
        var auto = true
        let edited = try? decode(NodeDetail.self, """
            {"id":10,"content":"entry, now quoting a large entry","node_type":"user","privacy_level":"private","ai_usage":"chat"}
            """)
        await model.editSaved(NodeFormResult(id: 10, editedNode: edited, privacy: .private),
                              autoGenerate: Binding(get: { auto }, set: { auto = $0 }))
    }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    private var lastRoute: AppRoute? { app.router.path(for: app.router.selectedTab).last }

    func testAnEditThatMadeTheThreadTooLongSwitchesTheFormToTheLimit() async {
        let model = await loadedModel(llm: .json(409, tooLongJSON))
        XCTAssertNil(model.threadLength)
        await saveEdit(model)
        XCTAssertTrue(calls.contains("POST /api/nodes/10/llm"))
        XCTAssertEqual(body(of: "POST /api/nodes/10/llm")?["auto_generate"] as? Bool, true)
        XCTAssertEqual(model.node?.content, "entry, now quoting a large entry", "the edit is saved")
        XCTAssertEqual(model.threadLength, .limit, "now, without opening the page again")
        XCTAssertTrue(model.atThreadLimit)
        XCTAssertEqual(model.threadLimitText, limitText)
        XCTAssertFalse(model.showCraftBar(craftMode: true, autoGenerate: false), "no LLM Response")
        XCTAssertTrue(app.toasts.toasts.isEmpty, "the form says it, not a toast")
    }

    func testAnEditsOtherRefusalIsToasted() async {
        let model = await loadedModel(llm: .json(400, exportRefusalJSON))
        await saveEdit(model)
        XCTAssertEqual(app.toasts.toasts.map(\.message), [exportRefusal])
        XCTAssertNil(model.threadLength)
    }

    /// An inline reply that took the thread past the limit opens its own page, which
    /// shows the limit; this page's next reply would not include that entry.
    func testAnInlineReplyThatReachedTheLimitLeavesThisPageAsItWas() async {
        let model = await loadedModel(llm: .json(409, tooLongJSON))
        var auto = true
        await model.inlineReplySent(NodeFormResult(id: 11, privacy: .private),
                                    autoGenerate: Binding(get: { auto }, set: { auto = $0 }))
        XCTAssertTrue(calls.contains("POST /api/nodes/11/llm"))
        XCTAssertTrue(app.toasts.toasts.isEmpty)
        XCTAssertNil(model.threadLength)
        let opened = await eventually { self.lastRoute == .thread(id: 11, awaitLLM: nil) }
        XCTAssertTrue(opened)
    }
}
