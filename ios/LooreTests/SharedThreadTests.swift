import XCTest
import SwiftUI
@testable import Loore

// AI replies in shared threads and quotes in public nodes (Peter, 2026-11-12):
// every AI reply is created private; auto-generate is off in public and circles
// threads and can't be turned on; a public node never quotes one that isn't.

private let quotesRefusalJSON = """
{"error":"This can't be made public yet: it quotes an entry that isn't public. Make the quoted entry public first, then try again.",
 "code":"quotes_not_public",
 "quoted":[{"id":12,"can_open":true,"preview":"The river at dawn…","permalink":"/node/12","privacy_level":"private"},
           {"id":99,"can_open":false}]}
"""
private let autoGenerateOffJSON = """
{"error":"Auto-generate is off in public threads. Ask for a reply with LLM Response: it stays private until you change it.",
 "code":"auto_generate_off"}
"""
private let replyInProgressJSON = """
{"error":"This reply is still being written. You can change who sees it once it's done.","code":"reply_in_progress"}
"""

final class SharedPrivacyTests: XCTestCase {
    private func apiError(_ status: Int, _ body: String) -> APIError {
        APIError.from(status: status, contentType: "application/json", data: Data(body.utf8))
    }

    func testOnlyPrivateIsNotSharedWithOthers() {
        XCTAssertFalse(PrivacyLevel.private.isSharedWithOthers)
        XCTAssertTrue(PrivacyLevel.public.isSharedWithOthers)
        XCTAssertTrue(PrivacyLevel.circles.isSharedWithOthers)
        // A sharing the app does not know yet is shared; no value is private (the server's rule).
        XCTAssertTrue(PrivacyLevel(rawString: "friends").isSharedWithOthers)
        XCTAssertFalse(PrivacyLevel(rawString: "").isSharedWithOthers)
        XCTAssertFalse(PrivacyLevel?.none.isSharedWithOthers)
        XCTAssertTrue(PrivacyLevel?.some(.circles).isSharedWithOthers)
    }

    func testTheQuotesRefusalDecodesWithEachQuotedEntry() throws {
        let error = apiError(409, quotesRefusalJSON)
        let refusal = try XCTUnwrap(QuotesNotPublic(error))
        XCTAssertEqual(refusal.message, "This can't be made public yet: it quotes an entry that isn't public. Make the quoted entry public first, then try again.")
        XCTAssertEqual(refusal.quoted, [
            .init(id: 12, canOpen: true, preview: "The river at dawn…", permalink: "/node/12", privacyLevel: .private),
            .init(id: 99, canOpen: false),
        ])
        XCTAssertEqual(refusal.quoted.map(\.rowText),
                       ["The river at dawn…", "An entry you can't open (#99). Remove its quote to publish."])
        XCTAssertFalse(error.isAutoGenerateOff)
        XCTAssertFalse(error.isReplyInProgress)
    }

    func testTheQuotesRefusalNeedsItsCodeOnA409() {
        XCTAssertNil(QuotesNotPublic(apiError(409, replyInProgressJSON)))
        XCTAssertNil(QuotesNotPublic(apiError(400, #"{"error":"x","code":"quotes_not_public"}"#)))
        XCTAssertNil(QuotesNotPublic(URLError(.timedOut)))
        // Without words or a list: the fallback words and no rows.
        let bare = QuotesNotPublic(apiError(409, #"{"code":"quotes_not_public"}"#))
        XCTAssertEqual(bare?.message, QuotesNotPublic.fallbackMessage)
        XCTAssertEqual(bare?.quoted, [])
        // An entry the user can open but without a preview is named by its id.
        XCTAssertEqual(QuotesNotPublic.Quoted(id: 3, canOpen: true).rowText, "Entry #3")
    }

    func testAutoGenerateOffAndReplyInProgressCodes() {
        let off = apiError(409, autoGenerateOffJSON)
        XCTAssertTrue(off.isAutoGenerateOff)
        XCTAssertFalse(off.isReplyInProgress)
        let busy = apiError(409, replyInProgressJSON)
        XCTAssertTrue(busy.isReplyInProgress)
        XCTAssertFalse(busy.isAutoGenerateOff)
        XCTAssertEqual(busy.userMessage(fallback: "Error submitting form."),
                       "This reply is still being written. You can change who sees it once it's done.")
        XCTAssertFalse(apiError(400, #"{"code":"auto_generate_off"}"#).isAutoGenerateOff)
    }
}

/// The writing form: Auto-generate locked for shared entries, the automatic
/// request's flag, the quotes dialog, and an AI reply's privacy edit.
@MainActor
final class SharedEntryFormTests: StubbedAppTestCase {
    private func form(_ config: NodeFormConfig) -> (NodeFormModel, () -> NodeFormResult?) {
        let model = NodeFormModel(config: config, app: app, defaults: defaults)
        var captured: NodeFormResult?
        model.onSuccess = { captured = $0 }
        return (model, { captured })
    }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    private func writeNewEntry(agentic: Bool) -> NodeFormModel {
        defaults.set(agentic, forKey: DefaultsKey.agenticReply)
        defaults.set(true, forKey: DefaultsKey.autoGenerate)
        let (model, _) = form(NodeFormConfig(parentId: nil, allowAgenticPrompt: true))
        model.aiUsage = .chat
        model.content = "hello"
        return model
    }

    func testAutoGenerateIsOffAndLockedForPublicAndCirclesEntriesOnlyForPrivate() {
        let model = writeNewEntry(agentic: true)
        model.privacy = .private
        XCTAssertFalse(model.autoGenerateLocked)
        XCTAssertTrue(model.autoGenerateOn)
        for shared in [PrivacyLevel.public, .circles] {
            model.privacy = shared
            XCTAssertTrue(model.autoGenerateLocked, "\(shared)")
            XCTAssertFalse(model.autoGenerateOn, "\(shared)")
        }
        // The stored choice is the user's, for private entries.
        XCTAssertTrue(model.useAutoGenerate)
        XCTAssertTrue(defaults.bool(forKey: DefaultsKey.autoGenerate))
        model.privacy = .private
        XCTAssertTrue(model.autoGenerateOn)
    }

    func testAReplyUnderAPublicNodeIsLockedToo() async {
        StubURLProtocol.install { request in
            switch (request.httpMethod ?? "", request.url?.path(percentEncoded: true) ?? "") {
            case ("GET", "/api/nodes/7"):
                return .json(200, #"{"id":7,"content":"p","privacy_level":"public","ai_usage":"chat"}"#)
            case ("GET", "/api/drafts/"): return .json(404, #"{"error":"No draft"}"#)
            default: return .json(200, "{}")
            }
        }
        let (model, _) = form(NodeFormConfig(parentId: 7))
        await model.start()
        XCTAssertTrue(model.autoGenerateLocked)
    }

    func testAPublicAgenticEntryAsksForNoReply() async {
        StubURLProtocol.install { _ in .json(202, #"{"conversation_id":1,"user_node_id":2}"#) }
        let model = writeNewEntry(agentic: true)
        model.privacy = .public
        await model.submit()
        XCTAssertEqual(body(of: "POST /api/textmode/start")?["auto_generate"] as? Bool, false)
        XCTAssertTrue(defaults.bool(forKey: DefaultsKey.autoGenerate))
    }

    func testACirclesEntryMakesNoReplyRequest() async {
        StubURLProtocol.install { request in
            if request.url?.path(percentEncoded: true) == "/api/nodes/" {
                return .json(201, #"{"id":20,"privacy_level":"circles"}"#)
            }
            return .json(202, #"{"node_id":21,"status":"pending"}"#)
        }
        let model = writeNewEntry(agentic: false)
        var captured: NodeFormResult?
        model.onSuccess = { captured = $0 }
        model.privacy = .circles
        await model.submit()
        XCTAssertEqual(captured?.id, 20)
        XCTAssertNil(captured?.awaitLLM)
        XCTAssertEqual(captured?.privacy, .circles)
        XCTAssertFalse(calls.contains { $0.hasSuffix("/llm") })
    }

    func testThePrivateEntrysAutomaticReplyRequestSaysSo() async {
        StubURLProtocol.install { request in
            if request.url?.path(percentEncoded: true) == "/api/nodes/" { return .json(201, #"{"id":20,"privacy_level":"private"}"#) }
            return .json(202, #"{"node_id":21,"status":"pending"}"#)
        }
        let model = writeNewEntry(agentic: false)
        model.privacy = .private
        await model.submit()
        XCTAssertEqual(body(of: "POST /api/nodes/20/llm")?["auto_generate"] as? Bool, true)
    }

    func testARecordedPublicEntryAsksForNoReply() async {
        StubURLProtocol.install { _ in .json(201, #"{"id":30,"user_node_id":30,"privacy_level":"public"}"#) }
        let model = writeNewEntry(agentic: true)
        model.privacy = .public
        model.streamingSessionId = "s1"
        await model.submit()
        let sent = body(of: "POST /api/drafts/streaming/s1/save-as-node")
        XCTAssertEqual(sent?["agentic"] as? Bool, true)
        XCTAssertNil(sent?["auto_generate"])
    }

    func testTextModeAsksForNoReplyForASharedEntry() async throws {
        StubURLProtocol.install { _ in .json(202, #"{"conversation_id":1,"user_node_id":2}"#) }
        defaults.set(true, forKey: DefaultsKey.autoGenerate)
        func send(_ privacy: PrivacyLevel) async throws -> Bool? {
            _ = try await TextModeView.submit(NodeFormSubmission(content: "hi", parentId: nil, privacy: privacy, aiUsage: .chat),
                                              app: app, defaults: defaults)
            return body(of: "POST /api/textmode/start")?["auto_generate"] as? Bool
        }
        let afterPublic = try await send(.public)
        XCTAssertEqual(afterPublic, false)
        let afterCircles = try await send(.circles)
        XCTAssertEqual(afterCircles, false)
        let afterPrivate = try await send(.private)
        XCTAssertEqual(afterPrivate, true)
        XCTAssertTrue(defaults.bool(forKey: DefaultsKey.autoGenerate))
    }

    // MARK: Quotes in a public node

    func testAQuotesRefusalShowsTheDialogAndKeepsTheForm() async {
        StubURLProtocol.install { _ in .json(409, quotesRefusalJSON) }
        let (model, result) = form(NodeFormConfig(edit: .init(nodeId: 9, initialContent: "See {quote:12}",
                                                               initialPrivacy: .private, initialAIUsage: .chat)))
        model.privacy = .public
        await model.submit()
        XCTAssertEqual(model.quotesRefusal?.quoted.map(\.id), [12, 99])
        XCTAssertNil(model.error)
        XCTAssertFalse(model.loading)
        XCTAssertNil(result())
        XCTAssertEqual(model.content, "See {quote:12}")
        XCTAssertEqual(model.privacy, .public)
        XCTAssertFalse(calls.contains("DELETE /api/drafts/"), "nothing was saved: the draft stays")
        model.cancelDialog()
        XCTAssertNil(model.dialog)
    }

    func testAPublicReplyRefusedForItsQuotesShowsTheDialog() async {
        StubURLProtocol.install { _ in .json(409, quotesRefusalJSON) }
        let (model, _) = form(NodeFormConfig(parentId: nil))
        model.privacy = .public
        model.content = "{quote:12}"
        await model.submit()
        XCTAssertNotNil(model.quotesRefusal)
    }

    func testOpeningAQuotedEntryClosesTheDialogAndOpensIt() async throws {
        StubURLProtocol.install { request in
            if request.url?.path(percentEncoded: true) == "/api/nodes/12" { return .json(200, #"{"id":12,"content":"q"}"#) }
            return .json(200, "{}")
        }
        let refusal = try XCTUnwrap(QuotesNotPublic(APIError.from(status: 409, contentType: "application/json",
                                                                   data: Data(quotesRefusalJSON.utf8))))
        // A form in a sheet: the sheet closes, then the entry opens.
        var left = false
        var config = NodeFormConfig(parentId: nil)
        config.leaveForNode = { left = true }
        let (model, _) = form(config)
        model.dialog = .quotesNotPublic(refusal)
        model.openQuoted(12)
        XCTAssertNil(model.dialog)
        let opened = await eventually {
            left && self.app.router.path(for: self.app.router.selectedTab).last == .thread(id: 12, awaitLLM: nil)
        }
        XCTAssertTrue(opened)
    }

    // MARK: An AI reply's privacy, changed by its owner

    func testAnAIReplysPrivacyEditSendsItsTextUnchanged() async {
        StubURLProtocol.install { _ in
            .json(200, #"{"node":{"id":77,"content":"Answer","node_type":"llm","privacy_level":"public"},"descendants_updated":0}"#)
        }
        let (model, result) = form(NodeFormConfig(edit: .init(nodeId: 77, initialContent: "Answer", initialPrivacy: .private,
                                                               initialAIUsage: .chat)))
        model.privacy = .public
        await model.submit()
        XCTAssertEqual(body(of: "PUT /api/nodes/77")?["content"] as? String, "Answer")
        XCTAssertEqual(body(of: "PUT /api/nodes/77")?["privacy_level"] as? String, "public")
        XCTAssertEqual(result()?.editedNode?.privacyLevel, .public)
        XCTAssertEqual(result()?.privacy, .public)
    }

    func testAReplyStillBeingWrittenSaysSo() async {
        StubURLProtocol.install { _ in .json(409, replyInProgressJSON) }
        let (model, result) = form(NodeFormConfig(edit: .init(nodeId: 77, initialContent: "[LLM response generation pending...]",
                                                               initialPrivacy: .private, initialAIUsage: .chat)))
        model.privacy = .public
        await model.submit()
        XCTAssertEqual(model.error, "This reply is still being written. You can change who sees it once it's done.")
        XCTAssertNil(model.dialog)
        XCTAssertNil(result())
    }
}

/// The thread: auto-generate in shared threads, and the automatic request.
@MainActor
final class SharedThreadModelTests: StubbedAppTestCase {
    private func focalJSON(_ privacy: String) -> String {
        """
        {"id":10,"content":"entry","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"\(privacy)",
         "ai_usage":"chat","child_count":0,"children":[],
         "ancestors":[{"id":9,"content":"root","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"\(privacy)"}]}
        """
    }

    private func loadedModel(_ privacy: String, llm: StubResponse = .json(202, #"{"node_id":77,"status":"pending"}"#)) async -> ThreadModel {
        let focal = focalJSON(privacy)
        StubURLProtocol.install { request in
            let path = request.url?.path(percentEncoded: true) ?? ""
            if path == "/api/nodes/10" { return .json(200, focal) }
            if path.hasSuffix("/llm") { return llm }
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

    private var lastRoute: AppRoute? { app.router.path(for: app.router.selectedTab).last }

    func testCirclesThreadsLockAutoGenerateOffAndOfferLLMResponse() async {
        let circles = await loadedModel("circles")
        XCTAssertTrue(circles.isSharedThread)
        XCTAssertFalse(circles.isPublicThread)
        XCTAssertFalse(circles.autoGenerateActive(true))
        XCTAssertTrue(circles.showCraftBar(craftMode: false, autoGenerate: true))

        let open = await loadedModel("private")
        XCTAssertFalse(open.isSharedThread)
        XCTAssertTrue(open.autoGenerateActive(true))
        XCTAssertFalse(open.showCraftBar(craftMode: false, autoGenerate: true))
    }

    func testTheAutomaticRequestSaysSoAndLLMResponseDoesNot() async {
        let model = await loadedModel("private")
        var auto = true
        await model.inlineReplySent(NodeFormResult(id: 11, privacy: .private),
                                    autoGenerate: Binding(get: { auto }, set: { auto = $0 }))
        XCTAssertEqual(body(of: "POST /api/nodes/11/llm")?["auto_generate"] as? Bool, true)

        // LLM Response waits while another node loads (the entry just opened).
        let settled = await eventually { !NodePrefetch.shared.isPending }
        XCTAssertTrue(settled)
        model.llmResponsePressed()
        let asked = await eventually { self.calls.contains("POST /api/nodes/10/llm") }
        XCTAssertTrue(asked)
        XCTAssertNotNil(body(of: "POST /api/nodes/10/llm"))
        XCTAssertNil(body(of: "POST /api/nodes/10/llm")?["auto_generate"])
    }

    func testInASharedThreadNoAutomaticRequestIsMade() async {
        for privacy in ["public", "circles"] {
            let model = await loadedModel(privacy)
            var auto = true
            await model.inlineReplySent(NodeFormResult(id: 11), autoGenerate: Binding(get: { auto }, set: { auto = $0 }))
            XCTAssertFalse(calls.contains { $0.hasSuffix("/llm") }, privacy)
            XCTAssertTrue(auto, "the stored preference is kept (\(privacy))")
            NodePrefetch.shared.resetForTesting()
        }
    }

    func testANewPublicEntryInAPrivateThreadGetsNoAutomaticReply() async {
        let model = await loadedModel("private")
        var auto = true
        await model.inlineReplySent(NodeFormResult(id: 11, privacy: .public),
                                    autoGenerate: Binding(get: { auto }, set: { auto = $0 }))
        XCTAssertFalse(calls.contains { $0.hasSuffix("/llm") })
        XCTAssertTrue(auto)
        let opened = await eventually { self.lastRoute == .thread(id: 11, awaitLLM: nil) }
        XCTAssertTrue(opened)
    }

    func testARefusedAutomaticReplyIsQuiet() async {
        let model = await loadedModel("private", llm: .json(409, autoGenerateOffJSON))
        var auto = true
        await model.inlineReplySent(NodeFormResult(id: 11, privacy: .private),
                                    autoGenerate: Binding(get: { auto }, set: { auto = $0 }))
        XCTAssertTrue(calls.contains("POST /api/nodes/11/llm"))
        XCTAssertTrue(app.toasts.toasts.isEmpty, "no error toast")
        XCTAssertTrue(auto)
        let opened = await eventually { self.lastRoute == .thread(id: 11, awaitLLM: nil) }
        XCTAssertTrue(opened)
    }

    func testAReadReplyInASharedThreadAsksForNoReply() async throws {
        let model = await loadedModel("circles")
        StubURLProtocol.install { _ in .json(202, #"{"user_node_id":12}"#) }
        _ = try await model.submitReadReply(NodeFormSubmission(content: "why?", parentId: 10, privacy: .circles, aiUsage: .chat),
                                            autoGenerate: true)
        XCTAssertEqual(body(of: "POST /api/textmode/from-node/10")?["auto_generate"] as? Bool, false)
    }
}
