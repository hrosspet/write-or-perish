import XCTest
import SwiftUI
@testable import Loore

// #791: the thread page named the account's model in its reply requests unless the
// LLM Response picker was on screen (it fills in the thread's model), which on the
// iPhone is only with auto-generate off. So the automatic reply to a typed message, in
// a thread that ran on another model, moved the thread to the account's model, and
// that turn read the whole thread without the prompt cache. The page now names a
// model only when the person picked one in the picker on this page; otherwise the
// server continues on the thread's model.

/// The account's model (StubbedAppTestCase) is GPT-6 Luna; the thread runs on GPT-6 Astra.
@MainActor
final class ThreadReplyModelTests: StubbedAppTestCase {
    /// The thread's newest AI reply, open on the page.
    private let astraReplyJSON = """
    {"id":10,"content":"An answer from Astra.","node_type":"llm","llm_model":"gpt-6-astra",
     "user":{"id":3,"username":"gpt-6-astra"},"parent_user_id":5,"llm_task_status":"completed",
     "privacy_level":"private","ai_usage":"chat","child_count":0,"children":[],
     "ancestors":[{"id":9,"content":"root","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private"}]}
    """

    /// An entry of the user's under the Astra reply.
    private let entryJSON = """
    {"id":10,"content":"entry","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"private",
     "ai_usage":"chat","child_count":0,"children":[],
     "ancestors":[{"id":8,"content":"root","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private"},
                  {"id":9,"content":"An answer from Astra.","node_type":"llm","llm_model":"gpt-6-astra","user_id":3,
                   "parent_user_id":5,"ai_usage":"chat","privacy_level":"private"}]}
    """

    private func loadedModel(_ json: String) async -> ThreadModel {
        StubURLProtocol.install { request in
            let path = request.url?.path(percentEncoded: true) ?? ""
            if path == "/api/nodes/10" { return .json(200, json) }
            if path.hasSuffix("/llm") { return .json(202, #"{"node_id":77,"status":"pending"}"#) }
            if path == "/api/voice/from-node/10" { return .json(200, #"{"mode":"ready","parent_id":10}"#) }
            if path == "/api/textmode/from-node/10" { return .json(201, #"{"user_node_id":12}"#) }
            if path.hasSuffix("/save-as-node") { return .json(201, #"{"id":12,"user_node_id":12}"#) }
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

    private func autoGenerateOn() -> Binding<Bool> {
        var auto = true
        return Binding(get: { auto }, set: { auto = $0 })
    }

    private func pressLLMResponse(_ model: ThreadModel) async -> [String: Any]? {
        let settled = await eventually { !NodePrefetch.shared.isPending }
        XCTAssertTrue(settled)
        model.aiReplyPressed()   // LLM Response (the Circles side names it so)
        let asked = await eventually { self.calls.contains("POST /api/nodes/10/llm") }
        XCTAssertTrue(asked)
        return body(of: "POST /api/nodes/10/llm")
    }

    // MARK: Nothing picked

    func testTheAutomaticReplyToATypedReplyNamesNoModel() async {
        let model = await loadedModel(astraReplyJSON)
        XCTAssertEqual(model.selectedModel, "gpt-6-luna", "the account's, until a picker fills in the thread's")
        await model.inlineReplySent(NodeFormResult(id: 11, privacy: .private), autoGenerate: autoGenerateOn())
        let sent = body(of: "POST /api/nodes/11/llm")
        XCTAssertEqual(sent?["auto_generate"] as? Bool, true)
        XCTAssertNil(sent?["model"])
    }

    func testAnEditsAutomaticReplyNamesNoModel() async throws {
        let model = await loadedModel(entryJSON)
        let node = try XCTUnwrap(model.node)
        model.beginEdit(model.target(focal: node))
        let edited = try decode(NodeDetail.self, """
            {"id":10,"content":"entry, rewritten","node_type":"user","privacy_level":"private","ai_usage":"chat"}
            """)
        await model.editSaved(NodeFormResult(id: 10, editedNode: edited, privacy: .private), autoGenerate: autoGenerateOn())
        let sent = body(of: "POST /api/nodes/10/llm")
        XCTAssertEqual(sent?["auto_generate"] as? Bool, true)
        XCTAssertNil(sent?["model"])
    }

    func testARepliesTypedOrRecordedUnderAGleanReplyNameNoModel() async throws {
        let model = await loadedModel(astraReplyJSON)
        _ = try await model.submitReadReply(NodeFormSubmission(content: "why?", parentId: 10, privacy: .private,
                                                               aiUsage: .chat), autoGenerate: true)
        let typed = body(of: "POST /api/textmode/from-node/10")
        XCTAssertEqual(typed?["auto_generate"] as? Bool, true)
        XCTAssertNil(typed?["model"])
        // The form's level still goes along (#794).
        XCTAssertEqual(typed?["privacy_level"] as? String, "private")

        _ = try await model.submitReadReply(NodeFormSubmission(content: "why?", parentId: 10, privacy: .private,
                                                               aiUsage: .chat, streamingSessionId: "s1"),
                                            autoGenerate: true)
        let recorded = body(of: "POST /api/drafts/streaming/s1/save-as-node")
        XCTAssertEqual(recorded?["agentic"] as? Bool, true)
        XCTAssertNil(recorded?["model"])
    }

    func testVoiceModeNamesNoModel() async {
        let model = await loadedModel(astraReplyJSON)
        model.startVoice()
        let asked = await eventually { self.calls.contains("POST /api/voice/from-node/10") }
        XCTAssertTrue(asked)
        XCTAssertNotNil(body(of: "POST /api/voice/from-node/10"))
        XCTAssertNil(body(of: "POST /api/voice/from-node/10")?["model"])
    }

    func testLLMResponseNamesNoModelBeforeThePickerHasTheThreadsModel() async {
        let model = await loadedModel(astraReplyJSON)
        let sent = await pressLLMResponse(model)
        XCTAssertNotNil(sent)
        XCTAssertNil(sent?["model"])
    }

    func testTheModelThePickerFillsInByItselfIsNotSent() async {
        let model = await loadedModel(astraReplyJSON)
        model.selectedModel = "gpt-6-astra"  // the picker's default, through its binding
        XCTAssertNil(model.requestModel)
        let sent = await pressLLMResponse(model)
        XCTAssertNil(sent?["model"])
    }

    // MARK: A pick on this page

    func testAModelThePersonPicksIsSentWithLLMResponse() async {
        let model = await loadedModel(astraReplyJSON)
        model.selectedModel = "gpt-6-astra"
        model.modelPicked("claude-opus-5.5")
        XCTAssertEqual(model.selectedModel, "claude-opus-5.5")
        let sent = await pressLLMResponse(model)
        XCTAssertEqual(sent?["model"] as? String, "claude-opus-5.5")
    }

    /// Picked with auto-generate off (where the picker shows), then auto-generate on.
    func testAModelThePersonPicksIsSentWithTheAutomaticReplyToWhatTheyTypeNext() async {
        let model = await loadedModel(astraReplyJSON)
        model.modelPicked("gpt-6-luna")
        await model.inlineReplySent(NodeFormResult(id: 11, privacy: .private), autoGenerate: autoGenerateOn())
        XCTAssertEqual(body(of: "POST /api/nodes/11/llm")?["model"] as? String, "gpt-6-luna")
    }

    func testAPickThePickerHasSinceReplacedIsNotSent() async {
        let model = await loadedModel(astraReplyJSON)
        model.modelPicked("claude-opus-5.5")
        model.selectedModel = "gpt-6-astra"  // the picker opened again and filled in the thread's
        XCTAssertNil(model.requestModel)
        let sent = await pressLLMResponse(model)
        XCTAssertNil(sent?["model"])
    }
}
