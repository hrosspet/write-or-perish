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

    func testTheLockedAutoGenerateWordsFitPublicAndSharedThreads() {
        XCTAssertEqual(SharedThread.autoGenerateOffHint, "Auto-generate is off in public and shared threads.")
        XCTAssertEqual(SharedThread.autoGenerateLockedFormText,
                       "Auto-generate is off in public and shared threads. You can tap LLM Response on the created node; the reply stays private until you change it.")
        XCTAssertEqual(SharedThread.privateReplyNote, "Only you see this reply")
    }

    func testThePrivateReplyNoteRule() {
        func shows(ai: Bool = true, owned: Bool = true, deleted: Bool = false,
                   _ privacy: PrivacyLevel?, under parent: PrivacyLevel?) -> Bool {
            SharedThread.showsPrivateReplyNote(isAIReply: ai, owned: owned, deleted: deleted,
                                               privacy: privacy, parentPrivacy: parent)
        }
        XCTAssertTrue(shows(.private, under: .public))
        XCTAssertTrue(shows(.private, under: .circles))
        XCTAssertFalse(shows(.private, under: .private), "a private thread: nothing to point out")
        XCTAssertFalse(shows(.private, under: nil))
        XCTAssertFalse(shows(.public, under: .public), "shared by its owner")
        XCTAssertFalse(shows(.circles, under: .public))
        XCTAssertFalse(shows(owned: false, .private, under: .public), "only its owner sees it")
        XCTAssertFalse(shows(ai: false, .private, under: .public), "a user's own entry")
        XCTAssertFalse(shows(deleted: true, .private, under: .public))
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

    /// The parent was edited while the reply form under it was open.
    func testAReplyFormFollowsItsParentsPrivacyAndKeepsItsText() async {
        StubURLProtocol.install { request in
            switch (request.httpMethod ?? "", request.url?.path(percentEncoded: true) ?? "") {
            case ("GET", "/api/nodes/7"):
                return .json(200, #"{"id":7,"content":"p","privacy_level":"private","ai_usage":"chat"}"#)
            case ("GET", "/api/drafts/"): return .json(404, #"{"error":"No draft"}"#)
            default: return .json(200, "{}")
            }
        }
        defaults.removeObject(forKey: DefaultsKey.publicReplyAck)
        var config = NodeFormConfig(parentId: 7)
        config.parentPrivacy = .private
        let (model, _) = form(config)
        await model.start()
        XCTAssertEqual(model.privacy, .private)
        model.content = "half a thought"

        // Made public: the reply is pinned to public and the public-reply question comes back.
        model.parentPrivacyChanged(.public)
        XCTAssertEqual(model.privacy, .public)
        XCTAssertTrue(model.lockPrivacy)
        XCTAssertTrue(model.autoGenerateLocked)
        XCTAssertEqual(model.content, "half a thought")
        await model.submit()
        XCTAssertTrue(model.showPublicReplyDialog)
        XCTAssertFalse(calls.contains("POST /api/nodes/"))
        model.cancelDialog()

        // Made private again: the form returns to private.
        model.parentPrivacyChanged(.private)
        XCTAssertEqual(model.privacy, .private)
        XCTAssertFalse(model.lockPrivacy)
        XCTAssertFalse(model.autoGenerateLocked)
        XCTAssertEqual(model.content, "half a thought")

        // An edit form never follows a parent.
        let (edit, _) = form(NodeFormConfig(edit: .init(nodeId: 9, initialContent: "x", initialPrivacy: .private)))
        edit.parentPrivacyChanged(.public)
        XCTAssertEqual(edit.privacy, .private)
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

    /// A recorded entry is saved with the settings the form shows when it is sent.
    func testARecordedEntryIsSavedWithTheSettingsTheFormShows() async {
        StubURLProtocol.install { _ in .json(201, #"{"id":30,"user_node_id":30,"privacy_level":"private"}"#) }
        let model = writeNewEntry(agentic: true)
        model.privacy = .private
        model.aiUsage = .off
        model.streamingSessionId = "s1"
        await model.submit()
        let sent = body(of: "POST /api/drafts/streaming/s1/save-as-node")
        XCTAssertEqual(sent?["privacy_level"] as? String, "private")
        XCTAssertEqual(sent?["ai_usage"] as? String, "none")
        XCTAssertNil(sent?["agentic"])
    }

    func testTextModeSavesARecordedEntryWithTheFormsSettings() async throws {
        StubURLProtocol.install { _ in .json(201, #"{"id":31,"user_node_id":31}"#) }
        _ = try await TextModeView.submit(NodeFormSubmission(content: "hi", parentId: nil, privacy: .private, aiUsage: .off,
                                                             streamingSessionId: "s2"), app: app, defaults: defaults)
        let plain = body(of: "POST /api/drafts/streaming/s2/save-as-node")
        XCTAssertEqual(plain?["privacy_level"] as? String, "private")
        XCTAssertEqual(plain?["ai_usage"] as? String, "none")
        XCTAssertNil(plain?["agentic"])
        _ = try await TextModeView.submit(NodeFormSubmission(content: "hi", parentId: nil, privacy: .public, aiUsage: .train,
                                                             streamingSessionId: "s3"), app: app, defaults: defaults)
        let agentic = body(of: "POST /api/drafts/streaming/s3/save-as-node")
        XCTAssertEqual(agentic?["privacy_level"] as? String, "public")
        XCTAssertEqual(agentic?["ai_usage"] as? String, "train")
        XCTAssertEqual(agentic?["agentic"] as? Bool, true)
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

    func testARecordedReadReplyIsSavedWithTheFormsSettings() async throws {
        let model = await loadedModel("private")
        StubURLProtocol.install { _ in .json(201, #"{"id":12,"user_node_id":12}"#) }
        _ = try await model.submitReadReply(NodeFormSubmission(content: "why?", parentId: 10, privacy: .private, aiUsage: .off,
                                                               streamingSessionId: "s4"), autoGenerate: true)
        let plain = body(of: "POST /api/drafts/streaming/s4/save-as-node")
        XCTAssertEqual(plain?["privacy_level"] as? String, "private")
        XCTAssertEqual(plain?["ai_usage"] as? String, "none")
        XCTAssertNil(plain?["agentic"])
        _ = try await model.submitReadReply(NodeFormSubmission(content: "why?", parentId: 10, privacy: .private, aiUsage: .chat,
                                                               streamingSessionId: "s5"), autoGenerate: true)
        let agentic = body(of: "POST /api/drafts/streaming/s5/save-as-node")
        XCTAssertEqual(agentic?["ai_usage"] as? String, "chat")
        XCTAssertEqual(agentic?["agentic"] as? Bool, true)
    }

    // MARK: "Only you see this reply"

    private func loadedThread(_ json: String, id: Int) async -> ThreadModel {
        StubURLProtocol.install { _ in .json(200, json) }
        let model = ThreadModel(nodeId: id, awaitLLM: nil, app: app)
        await model.load()
        return model
    }

    /// The viewer is user 5. Under the public entry 10: their private AI reply
    /// (11, with their private AI reply 15 under it), their public AI reply (12),
    /// their own private entry (13), and someone else's entry (16) with that
    /// person's private AI reply (14).
    func testTheNoteShowsOnTheOwnersPrivateAIRepliesUnderASharedNode() async throws {
        let model = await loadedThread("""
        {"id":10,"content":"entry","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"public",
         "ai_usage":"chat","children":[
          {"id":11,"node_type":"llm","llm_model":"gpt-6-luna","user_id":99,"parent_user_id":5,"privacy_level":"private",
           "children":[{"id":15,"node_type":"llm","llm_model":"gpt-6-luna","user_id":99,"parent_user_id":5,
                        "privacy_level":"private","children":[]}]},
          {"id":12,"node_type":"llm","llm_model":"gpt-6-luna","user_id":99,"parent_user_id":5,"privacy_level":"public","children":[]},
          {"id":13,"node_type":"user","user_id":5,"privacy_level":"private","children":[]},
          {"id":16,"node_type":"user","user_id":7,"privacy_level":"public",
           "children":[{"id":14,"node_type":"llm","llm_model":"gpt-6-luna","user_id":99,"parent_user_id":7,
                        "privacy_level":"private","children":[]}]}]}
        """, id: 10)
        let rows = ChildRow.flatten(try XCTUnwrap(model.node).children, parentPrivacy: model.node?.privacyLevel)
        var shown: [Int: Bool] = [:]
        for row in rows {
            if case .bubble(let child) = row.kind {
                shown[child.id] = model.showsPrivateReplyNote(child, parentPrivacy: row.parentPrivacy)
            }
        }
        XCTAssertEqual(shown, [11: true, 15: false, 12: false, 13: false, 16: false, 14: false])
        XCTAssertFalse(model.focalShowsPrivateReplyNote, "a user entry")
    }

    func testTheNoteOnTheFocalReplyAndOnAncestors() async {
        let focal = """
        {"id":77,"content":"Answer","node_type":"llm","llm_model":"gpt-6-luna","user":{"id":99,"username":"gpt-6-luna"},
         "parent_user_id":5,"privacy_level":"private","ai_usage":"chat","children":[],
         "ancestors":[{"id":9,"node_type":"user","user_id":5,"privacy_level":"circles"},
                      {"id":20,"node_type":"llm","llm_model":"gpt-6-luna","user_id":99,"parent_user_id":5,"privacy_level":"private"},
                      {"id":21,"node_type":"user","user_id":5,"privacy_level":"PARENT"}]}
        """
        let underCircles = await loadedThread(focal.replacingOccurrences(of: "PARENT", with: "circles"), id: 77)
        XCTAssertTrue(underCircles.focalShowsPrivateReplyNote)
        XCTAssertFalse(underCircles.showsPrivateReplyNote(ancestorAt: 0), "the root answers nothing shown")
        XCTAssertTrue(underCircles.showsPrivateReplyNote(ancestorAt: 1), "an AI reply under the circles root")
        XCTAssertFalse(underCircles.showsPrivateReplyNote(ancestorAt: 2), "a user entry")

        let underPrivate = await loadedThread(focal.replacingOccurrences(of: "PARENT", with: "private"), id: 77)
        XCTAssertFalse(underPrivate.focalShowsPrivateReplyNote)
    }

    /// The note in a bubble, dark and light. With `TEST_RUNNER_LOORE_SNAPSHOT_DIR`
    /// set, the images are written there.
    func testThePrivateReplyNoteRenders() throws {
        for scheme in [ColorScheme.dark, .light] {
            let bubble = BubbleData(id: 11, content: "A reply only its owner reads.", username: "gpt-6-luna",
                                    llmModel: "gpt-6-luna", createdAt: Date(timeIntervalSince1970: 1_794_000_000),
                                    privateReplyNote: true)
            let view = VStack(spacing: 16) {
                BubbleView(data: bubble)
                BubbleView(data: BubbleData(id: 12, content: "Without the note.", username: "gpt-6-luna",
                                            llmModel: "gpt-6-luna"))
            }
            .padding(24)
            .frame(width: 402)
            .background(LooreColor.bgDeep)
            .environment(app)
            .environment(\.colorScheme, scheme)
            let renderer = ImageRenderer(content: view)
            renderer.scale = 3
            let image = try XCTUnwrap(renderer.uiImage)
            XCTAssertGreaterThan(image.size.height, 200)
            if let dir = ProcessInfo.processInfo.environment["LOORE_SNAPSHOT_DIR"], !dir.isEmpty {
                try image.pngData()?.write(to: URL(fileURLWithPath: dir)
                    .appendingPathComponent("private-reply-note-\(scheme == .dark ? "dark" : "light").png"))
            }
        }
    }
}
