import XCTest
import SwiftUI
@testable import Loore

/// Shared setup: an `AppState` whose API goes through `StubURLProtocol`.
@MainActor
class StubbedAppTestCase: XCTestCase {
    var app: AppState!
    var defaults: UserDefaults!

    static let userJSON = """
    {"id":5,"username":"seowriter","approved":true,"terms_up_to_date":true,"plan":"alpha","craft_mode":true,
     "preferred_model":"gpt-6-luna","default_privacy_level":"private","default_ai_usage":"chat",
     "share_v1_enabled":true,"voice_mode_enabled":true,"timezone":"UTC"}
    """

    override func setUp() async throws {
        StubURLProtocol.reset()
        defaults = UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")
        app = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(), defaults: defaults)
        let user = try decode(CurrentUser.self, Self.userJSON)
        app.useForTesting(api: makeStubbedClient(environment: .local), user: user)
    }

    override func tearDown() async throws {
        StubURLProtocol.reset()
    }

    /// Requests seen so far as "METHOD /path".
    var calls: [String] {
        StubURLProtocol.requests.map { "\($0.httpMethod ?? "") \($0.url?.path(percentEncoded: true) ?? "")" }
    }

    func body(of call: String) -> [String: Any]? {
        guard let request = StubURLProtocol.requests.last(where: { "\($0.httpMethod ?? "") \($0.url?.path(percentEncoded: true) ?? "")" == call }),
              let data = request.httpBody else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }
}

/// `NodeForm.handleSubmit` (map D §4.5), against a stubbed backend.
@MainActor
final class NodeFormModelTests: StubbedAppTestCase {
    private func form(_ config: NodeFormConfig) -> (NodeFormModel, () -> NodeFormResult?) {
        let model = NodeFormModel(config: config, app: app, defaults: defaults)
        var captured: NodeFormResult?
        model.onSuccess = { captured = $0 }
        return (model, { captured })
    }

    func testEmptyContentIsRequired() async {
        let (model, _) = form(NodeFormConfig(parentId: 7))
        model.content = "   "
        await model.submit()
        XCTAssertEqual(model.error, "Content is required.")
        XCTAssertTrue(calls.isEmpty)
    }

    func testPlainReplyPostsTheNodeAndDeletesTheDraft() async {
        StubURLProtocol.install { request in
            if request.httpMethod == "POST" && request.url?.path(percentEncoded: true) == "/api/nodes/" {
                return .json(201, #"{"id":42,"content":"hi","node_type":"user","parent_id":7}"#)
            }
            return .json(200, "{}")
        }
        let (model, result) = form(NodeFormConfig(parentId: 7))
        model.content = "hi"
        await model.submit()
        XCTAssertEqual(result()?.id, 42)
        XCTAssertNil(result()?.awaitLLM)
        XCTAssertEqual(body(of: "POST /api/nodes/")?["parent_id"] as? Int, 7)
        XCTAssertEqual(body(of: "POST /api/nodes/")?["content"] as? String, "hi")
        XCTAssertTrue(calls.contains("DELETE /api/drafts/"))
    }

    func testEditWithGeneratedAudioAsksFirstThenSavesTheChoice() async {
        StubURLProtocol.install { _ in .json(200, #"{"node":{"id":9,"content":"new"},"descendants_updated":0}"#) }
        let (model, result) = form(NodeFormConfig(edit: .init(nodeId: 9, initialContent: "old", initialPrivacy: .private,
                                                               initialAIUsage: .chat, hasGeneratedTTS: true)))
        model.content = "new"
        await model.submit()
        XCTAssertTrue(model.showTtsDialog)
        XCTAssertTrue(calls.isEmpty)
        model.cancelDialog()
        await model.submit(regenerateTts: true)
        XCTAssertEqual(result()?.editedNode?.content, "new")
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["regenerate_tts"] as? Bool, true)
        XCTAssertNil(body(of: "PUT /api/nodes/9")?["apply_to_descendants"])
    }

    func testEditChangingAIUsageOnANodeWithRepliesAsksForScope() async {
        StubURLProtocol.install { _ in .json(200, #"{"node":{"id":9,"content":"same"},"descendants_updated":3}"#) }
        let (model, result) = form(NodeFormConfig(edit: .init(nodeId: 9, initialContent: "same", initialPrivacy: .private,
                                                               initialAIUsage: .chat, hasChildren: true)))
        model.aiUsage = .off
        await model.submit()
        XCTAssertTrue(model.showScopeDialog)
        model.answerScope(true)
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(result()?.descendantsUpdated, 3)
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["apply_to_descendants"] as? Bool, true)
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["ai_usage"] as? String, "none")
    }

    func testEditAboveTheCapIsAnErrorNotASplit() async {
        let (model, _) = form(NodeFormConfig(edit: .init(nodeId: 9, initialContent: "x")))
        model.content = String(repeating: "a", count: nodeCharCap + 1)
        await model.submit()
        XCTAssertEqual(model.error, "This entry is 100,001 characters — above the 100,000-character limit. Please move part of it into separate entries.")
        XCTAssertFalse(model.showSplitDialog)
    }

    func testANewEntryAboveTheCapAsksToSplitOnce() async {
        StubURLProtocol.install { _ in .json(201, #"{"id":1,"tip_id":3,"split_into":2}"#) }
        let (model, result) = form(NodeFormConfig(parentId: nil))
        model.content = String(repeating: "a", count: nodeCharCap + 5)
        await model.submit()
        XCTAssertTrue(model.showSplitDialog)
        model.confirmSplit()
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(result()?.id, 1)
        XCTAssertEqual(result()?.tipId, 3)
        XCTAssertEqual(SplitContentDialog.parts(250_001), 3)
        XCTAssertEqual(SplitContentDialog.parts(100_001), 2)
    }

    func testAPasteOverTheCapIsHeldForTheSplitDialog() {
        let (model, _) = form(NodeFormConfig(parentId: nil))
        let pasted = String(repeating: "b", count: nodeCharCap + 10)
        model.content = pasted
        model.contentChanged(from: "", to: pasted)
        XCTAssertEqual(model.content, "")
        XCTAssertTrue(model.showSplitDialog)
        model.confirmSplit()
        XCTAssertEqual(model.content, pasted)
    }

    func testReplyUnderAPublicNodeAsksOnceAndIsPublic() async {
        StubURLProtocol.install { request in
            switch (request.httpMethod ?? "", request.url?.path(percentEncoded: true) ?? "") {
            case ("GET", "/api/nodes/7"):
                return .json(200, #"{"id":7,"content":"p","privacy_level":"public","ai_usage":"chat","reply_ai_usage":"chat"}"#)
            case ("GET", "/api/drafts/"): return .json(404, #"{"error":"No draft"}"#)
            case ("POST", "/api/nodes/"): return .json(201, #"{"id":8}"#)
            default: return .json(200, "{}")
            }
        }
        let (model, result) = form(NodeFormConfig(parentId: 7))
        await model.start()
        XCTAssertTrue(model.lockPrivacy)
        model.content = "reply"
        await model.submit()
        XCTAssertTrue(model.showPublicReplyDialog)
        model.answerPublicReply()
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(result()?.id, 8)
        XCTAssertEqual(body(of: "POST /api/nodes/")?["privacy_level"] as? String, "public")
    }

    func testWriteNewEntryAgenticStartsATextmodeThread() async {
        StubURLProtocol.install { _ in .json(202, #"{"conversation_id":1,"user_node_id":2,"llm_node_id":3}"#) }
        defaults.set(true, forKey: DefaultsKey.agenticReply)
        defaults.set(false, forKey: DefaultsKey.autoGenerate)
        let (model, result) = form(NodeFormConfig(parentId: nil, allowAgenticPrompt: true))
        model.aiUsage = .chat
        model.content = "hello"
        await model.submit()
        XCTAssertEqual(result()?.id, 2)
        XCTAssertEqual(result()?.awaitLLM, 3)
        XCTAssertEqual(body(of: "POST /api/textmode/start")?["auto_generate"] as? Bool, false)
    }

    func testWriteNewEntryWithoutAgenticButAutoGenerateRequestsAReply() async {
        StubURLProtocol.install { request in
            if request.url?.path(percentEncoded: true) == "/api/nodes/" { return .json(201, #"{"id":20}"#) }
            if request.url?.path(percentEncoded: true) == "/api/nodes/20/llm" { return .json(202, #"{"node_id":21,"status":"pending"}"#) }
            return .json(200, "{}")
        }
        defaults.set(false, forKey: DefaultsKey.agenticReply)
        defaults.set(true, forKey: DefaultsKey.autoGenerate)
        let (model, result) = form(NodeFormConfig(parentId: nil, allowAgenticPrompt: true))
        model.aiUsage = .chat
        model.content = "hello"
        await model.submit()
        XCTAssertEqual(result()?.id, 21)
        XCTAssertEqual(result()?.awaitLLM, 21)
        XCTAssertEqual(body(of: "POST /api/nodes/20/llm")?["source_mode"] as? String, "textmode")
    }

    func testAPlainFormNeverAutoGenerates() async {
        StubURLProtocol.install { _ in .json(201, #"{"id":20}"#) }
        defaults.set(true, forKey: DefaultsKey.autoGenerate)
        let (model, result) = form(NodeFormConfig(parentId: nil))
        XCTAssertFalse(model.useAutoGenerate)
        model.content = "hello"
        await model.submit()
        XCTAssertEqual(result()?.id, 20)
        XCTAssertFalse(calls.contains { $0.hasSuffix("/llm") })
    }

    func testCustomSubmitTakesOverAndDeletesTheDraft() async {
        var got: NodeFormSubmission?
        var config = NodeFormConfig(parentId: nil)
        config.aiUsageFromGlobalDefault = true
        config.submitOverride = { submission in
            got = submission
            return NodeFormResult(id: 5, userNodeId: 5, llmNodeId: 6, llmError: "refused")
        }
        StubURLProtocol.install { _ in .json(200, "{}") }
        let (model, result) = form(config)
        XCTAssertEqual(model.aiUsage, .chat) // the account default
        model.content = "typed"
        await model.submit()
        XCTAssertEqual(got?.content, "typed")
        XCTAssertEqual(result()?.llmNodeId, 6)
        XCTAssertTrue(calls.contains("DELETE /api/drafts/"))
        XCTAssertEqual(app.toasts.toasts.last?.message, "refused")
    }

    func testFreshEntriesRememberPrivacyAndAIUsage() {
        let (model, _) = form(NodeFormConfig(parentId: nil))
        model.privacy = .public
        model.aiUsage = .train
        XCTAssertEqual(defaults.string(forKey: DefaultsKey.lastPrivacyLevel), "public")
        XCTAssertEqual(defaults.string(forKey: DefaultsKey.lastAIUsage), "train")
        let (again, _) = form(NodeFormConfig(parentId: nil))
        XCTAssertEqual(again.privacy, .public)
        XCTAssertEqual(again.aiUsage, .train)
        let (reply, _) = form(NodeFormConfig(parentId: 3))
        XCTAssertEqual(reply.privacy, .private) // replies take the parent's, never the remembered
    }

    func testDraftTimeAgo() {
        let now = Date()
        XCTAssertEqual(DraftAutosaver.timeAgo(now.addingTimeInterval(-10), now: now), "just now")
        XCTAssertEqual(DraftAutosaver.timeAgo(now.addingTimeInterval(-600), now: now), "10m ago")
        XCTAssertEqual(DraftAutosaver.timeAgo(now.addingTimeInterval(-7300), now: now), "2h ago")
    }
}

/// Reply rules of the thread screen (map D §4.5 "After success", §5.5–5.8).
@MainActor
final class ThreadModelTests: StubbedAppTestCase {
    private let focalJSON = """
    {"id":10,"content":"entry","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"private",
     "ai_usage":"chat","child_count":0,"children":[],
     "ancestors":[{"id":9,"content":"root","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private"}]}
    """

    private func loadedModel(_ json: String? = nil, awaitLLM: Int? = nil, id: Int = 10) async -> ThreadModel {
        let focal = json ?? focalJSON
        StubURLProtocol.install { request in
            if request.url?.path(percentEncoded: true) == "/api/nodes/\(id)" { return .json(200, focal) }
            if request.url?.path(percentEncoded: true).hasSuffix("/llm") == true { return .json(202, #"{"node_id":77,"status":"pending"}"#) }
            return .json(200, "{}")
        }
        let model = ThreadModel(nodeId: id, awaitLLM: awaitLLM, app: app)
        await model.load()
        return model
    }

    func testOwnershipAndCraftBarRules() async {
        let model = await loadedModel()
        XCTAssertTrue(model.isOwner)
        XCTAssertTrue(model.showCraftBar(craftMode: true, autoGenerate: false))
        XCTAssertFalse(model.showCraftBar(craftMode: true, autoGenerate: true))
        XCTAssertFalse(model.showCraftBar(craftMode: false, autoGenerate: false))
        XCTAssertEqual(model.tabTitle, "entry")
    }

    func testInlineReplyAutoGeneratesWhenTheChainAllowsAI() async {
        let model = await loadedModel()
        var auto = true
        let binding = Binding(get: { auto }, set: { auto = $0 })
        await model.inlineReplySent(NodeFormResult(id: 11), autoGenerate: binding)
        XCTAssertEqual(body(of: "POST /api/nodes/11/llm")?["model"] as? String, "gpt-6-luna")
        XCTAssertEqual(app.router.path(for: app.router.selectedTab).last, .thread(id: 11, awaitLLM: 77))
        XCTAssertTrue(auto)
    }

    func testAChainWithAIOffTurnsAutoGenerateOff() async {
        let json = focalJSON.replacingOccurrences(of: #""ai_usage":"chat","privacy_level":"private"}"#,
                                                  with: #""ai_usage":"none","privacy_level":"private"}"#)
        let model = await loadedModel(json)
        var auto = true
        let binding = Binding(get: { auto }, set: { auto = $0 })
        await model.inlineReplySent(NodeFormResult(id: 11), autoGenerate: binding)
        XCTAssertFalse(auto)
        XCTAssertFalse(calls.contains { $0.hasSuffix("/llm") })
        XCTAssertEqual(app.toasts.toasts.last?.message, "Turning off auto-generate. AI usage on some nodes is turned off.")
        XCTAssertEqual(app.router.path(for: app.router.selectedTab).last, .thread(id: 11, awaitLLM: nil))
    }

    func testPublicThreadsForceAutoGenerateOff() async {
        let json = focalJSON.replacingOccurrences(of: #""privacy_level":"private","#, with: #""privacy_level":"public","#)
        let model = await loadedModel(json)
        XCTAssertTrue(model.isPublicThread)
        XCTAssertFalse(model.autoGenerateActive(true))
        XCTAssertTrue(model.showCraftBar(craftMode: false, autoGenerate: true))
    }

    func testAnEntryAwaitingAnotherNodesReplyHandsOffToIt() async {
        app.router.setPath([.thread(id: 10, awaitLLM: 55)], for: app.router.selectedTab)
        let model = await loadedModel(awaitLLM: 55)
        await model.start()
        let path = app.router.path(for: app.router.selectedTab)
        XCTAssertEqual(path, [.thread(id: 10, awaitLLM: nil), .thread(id: 55, awaitLLM: 55)])
        model.stop()
    }

    func testPinRulesAndTitles() async {
        let model = await loadedModel()
        XCTAssertFalse(model.canPin)
        XCTAssertEqual(model.pinTitle, "Cannot pin a private node")
    }

    func testAudioGeneratedFromTheSpeakerMakesAnEditAskAboutRegenerating() async {
        let model = await loadedModel()
        let before = try? XCTUnwrap(model.node)
        XCTAssertEqual(before.map { model.target(focal: $0).hasTTS }, false)
        model.ttsGenerated()
        let after = try? XCTUnwrap(model.node)
        XCTAssertEqual(after.map { model.target(focal: $0).hasTTS }, true)
    }

    // MARK: Watching a pending reply (map D §5.5–5.6)

    private let pendingJSON = """
    {"id":77,"content":"[LLM response generation pending...]","node_type":"llm","llm_model":"gpt-6-luna",
     "llm_task_status":"pending","user":{"id":99,"username":"gpt-6-luna"},"parent_user_id":5,
     "privacy_level":"private","ai_usage":"chat","streaming_content":"So far",
     "ancestors":[{"id":10,"content":"entry","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private"}]}
    """

    private func watch(status: String) async -> ThreadModel {
        let pending = pendingJSON
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/nodes/77": return .json(200, pending)
            case "/api/nodes/77/llm-status": return .json(200, status)
            default: return .json(200, "{}")
            }
        }
        let tab = app.router.selectedTab
        app.router.setPath([.thread(id: 10, awaitLLM: nil), .thread(id: 77, awaitLLM: 77)], for: tab)
        let model = ThreadModel(nodeId: 77, awaitLLM: 77, app: app)
        await model.start()
        return model
    }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    func testAPendingReplyShowsItsTextSoFar() async {
        let model = await watch(status: #"{"node_id":77,"status":"processing"}"#)
        XCTAssertTrue(model.isLLMPending)
        XCTAssertEqual(model.streamText, "So far")
        XCTAssertEqual(model.tabTitle, "Thinking…")
        model.stop()
    }

    func testACompletedReplyIsPatchedInPlace() async {
        let model = await watch(status: #"{"node_id":77,"status":"completed","content":"Final text","tool_calls_meta":[]}"#)
        let done = await eventually { model.node?.llmTaskStatus == .completed }
        XCTAssertTrue(done)
        XCTAssertEqual(model.node?.content, "Final text")
        XCTAssertEqual(app.router.path(for: app.router.selectedTab).count, 2)
        model.stop()
    }

    func testAContinuationIsFollowed() async {
        let model = await watch(status: #"{"node_id":77,"status":"completed","continuation_node_id":78}"#)
        let followed = await eventually {
            self.app.router.path(for: self.app.router.selectedTab).last == .thread(id: 78, awaitLLM: 78)
        }
        XCTAssertTrue(followed)
        model.stop()
    }

    func testAFailedReplyToastsAndGoesBackToItsEntry() async {
        let model = await watch(status: #"{"node_id":77,"status":"failed","error":"Provider overloaded"}"#)
        let back = await eventually { self.app.router.path(for: self.app.router.selectedTab) == [.thread(id: 10, awaitLLM: nil)] }
        XCTAssertTrue(back)
        XCTAssertEqual(app.toasts.toasts.last?.message, "Provider overloaded")
        model.stop()
    }

    func testAFailedReplyWithoutTextSaysTaskFailed() async {
        let model = await watch(status: #"{"node_id":77,"status":"failed"}"#)
        let toasted = await eventually { self.app.toasts.toasts.last?.message == "Task failed" }
        XCTAssertTrue(toasted)
        model.stop()
    }

    func testChildRowsIndentOnlyWhenThereAreSiblings() throws {
        let children = try decode([TreeNode].self, """
        [{"id":1,"children":[{"id":2,"children":[]},{"id":3,"children":[{"id":4,"children":[]}]}]}]
        """)
        let rows = ChildRow.flatten(children)
        XCTAssertEqual(rows.map(\.id), ["n1", "n2", "s2", "n3", "n4"])
        XCTAssertEqual(rows.map(\.guides), [[false], [false, true], [false, true], [false, true], [false, true, false]])
    }
}

/// Port of `ModelSelector.test.js` `pickerOptions` plus the default rule.
final class ModelPickerTests: XCTestCase {
    private func models() throws -> [ModelInfo] {
        try decode([ModelInfo].self, """
        [{"id":"gpt-6-astra","name":"GPT-6 Astra","provider":"openai","featured":true,"read":false},
         {"id":"gpt-6-luna","name":"GPT-6 Luna","provider":"openai","featured":false,"read":true},
         {"id":"gpt-5.6-luna","name":"GPT-5.6 Luna","provider":"openai","featured":false,"read":true},
         {"id":"claude-opus-5.5","name":"Opus 5.5","provider":"anthropic","featured":true,"read":false},
         {"id":"claude-fable-5.1","name":"Fable 5.1","provider":"anthropic","featured":false,"read":false},
         {"id":"claude-opus-4.6","name":"Opus 4.6","provider":"anthropic","featured":true,"read":false}]
        """)
    }

    private func ids(_ options: ModelPickerOptions) -> [String] { options.allModels.map(\.id) }

    func testFeaturedModelsAnthropicFirstThenMore() throws {
        let o = ModelPickerOptions.make(models: try models(), selectedId: "claude-opus-4.6", purpose: .chat, expanded: false)
        XCTAssertEqual(ids(o), ["claude-opus-5.5", "claude-opus-4.6", "gpt-6-astra"])
        guard case .flat(_, let more) = o else { return XCTFail() }
        XCTAssertTrue(more)
    }

    func testANonFeaturedSelectionComesFirstOnce() throws {
        let o = ModelPickerOptions.make(models: try models(), selectedId: "claude-fable-5.1", purpose: .chat, expanded: false)
        XCTAssertEqual(ids(o), ["claude-fable-5.1", "claude-opus-5.5", "claude-opus-4.6", "gpt-6-astra"])
    }

    func testExpandedGroupsByProvider() throws {
        guard case .grouped(let groups) = ModelPickerOptions.make(models: try models(), selectedId: "claude-opus-4.6",
                                                                   purpose: .chat, expanded: true) else { return XCTFail() }
        XCTAssertEqual(groups.map(\.label), ["Anthropic", "OpenAI"])
        XCTAssertEqual(groups[1].models.map(\.id), ["gpt-6-astra", "gpt-6-luna", "gpt-5.6-luna"])
    }

    func testReadOffersOnlyReadModelsWithoutMore() throws {
        let o = ModelPickerOptions.make(models: try models(), selectedId: "gpt-6-luna", purpose: .read, expanded: false)
        XCTAssertEqual(ids(o), ["gpt-6-luna", "gpt-5.6-luna"])
        guard case .flat(_, let more) = o else { return XCTFail() }
        XCTAssertFalse(more)
    }

    func testSuggestionRules() throws {
        let all = try models()
        func suggestion(_ id: String, _ source: String) throws -> SuggestedModel {
            try decode(SuggestedModel.self, #"{"suggested_model":"\#(id)","source":"\#(source)"}"#)
        }
        // A read picker replaces a chat model with the read default.
        XCTAssertEqual(ModelPickerOptions.appliedSuggestion(models: all, suggestion: try suggestion("gpt-6-luna", "default"),
                                                            selected: "claude-opus-4.6", purpose: .read), "gpt-6-luna")
        // A deprecated preference is replaced.
        XCTAssertEqual(ModelPickerOptions.appliedSuggestion(models: all, suggestion: try suggestion("claude-opus-4.6", "default"),
                                                            selected: "claude-opus-5", purpose: .chat), "claude-opus-4.6")
        // An offered selection is kept over a non-thread default.
        XCTAssertNil(ModelPickerOptions.appliedSuggestion(models: all, suggestion: try suggestion("claude-opus-4.6", "user_preference"),
                                                          selected: "gpt-6-astra", purpose: .chat))
        // A thread predecessor always wins.
        XCTAssertEqual(ModelPickerOptions.appliedSuggestion(models: all, suggestion: try suggestion("claude-opus-4.6", "predecessor"),
                                                            selected: "gpt-6-astra", purpose: .chat), "claude-opus-4.6")
    }
}

final class SearchSnippetTests: XCTestCase {
    func testMarkRunsAreHighlightedAndOtherTagsDropped() {
        XCTAssertEqual(SearchSnippet.runs("the <mark>river</mark> bends <b>slowly</b> &amp; wide"), [
            .init(text: "the ", marked: false), .init(text: "river", marked: true),
            .init(text: " bends slowly & wide", marked: false),
        ])
        XCTAssertEqual(SearchSnippet.runs("a < b"), [.init(text: "a < b", marked: false)])
    }

    func testSearchResultDecodesNodesAndReferences() throws {
        let answer = try decode(SearchResponse.self, """
        {"results":[{"id":1,"preview":"p","node_type":"user","child_count":2,"score":0.83},
                    {"kind":"external","id":4,"source":"web_clip","author_handle":"Ann","title":"T"}],
         "total":2,"mode":"semantic"}
        """)
        XCTAssertEqual(answer.results.map(\.id), ["node-1", "external-4"])
        XCTAssertEqual(answer.results[1].sourceLabel, "Page")
        XCTAssertEqual(answer.results[1].authorLabel, "Ann")
    }
}
