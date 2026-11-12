import XCTest
import SwiftUI
@testable import Loore

// Long threads (#648; Peter, 2026-11-12): a notice from 90 % of the limit on the
// next reply's prompt, and at the limit no AI reply in the thread (writing still
// works) with a way to a new thread. The server's answers, the reply form's state
// and the Voice screen's block. The voice turn's spoken refusal is in VoiceTurnTests.

private let limitText = "This thread is too long for another AI reply. Each reply reads the whole thread, "
    + "including what it quotes, and this one has grown too large. You can still write here. "
    + "To keep talking with AI, start a new thread."
private let tooLongJSON = #"{"error":"\#(limitText)","code":"thread_too_long"}"#

final class ThreadLengthDecodingTests: XCTestCase {
    private func apiError(_ status: Int, _ body: String) -> APIError {
        APIError.from(status: status, contentType: "application/json", data: Data(body.utf8))
    }

    func testTheCopyMatchesTheWeb() {
        XCTAssertEqual(ThreadLength.noticeText,
                       "This thread is getting long. Each AI reply reads all of it, so AI replies here will stop soon. "
                       + "You can start a new thread to keep going.")
        XCTAssertEqual(ThreadLength.limitText, limitText)
        XCTAssertEqual(ThreadLength.spokenText, "This thread is too long for another reply. Start a new thread to keep talking.")
        XCTAssertEqual(ThreadLength.blockTitle, "This thread is too long")
        XCTAssertEqual(ThreadLength.newThreadLabel, "Start a new thread")
        // No price, numbers or percentages anywhere.
        for text in [ThreadLength.noticeText, ThreadLength.limitText, ThreadLength.spokenText] {
            XCTAssertNil(text.rangeOfCharacter(from: .decimalDigits), text)
            XCTAssertFalse(text.contains("$") || text.contains("%") || text.lowercased().contains("token"), text)
        }
    }

    func testTheNodesThreadLength() throws {
        let base = #""id":10,"content":"entry","node_type":"user","privacy_level":"private","ai_usage":"chat""#
        XCTAssertNil(try decode(NodeDetail.self, "{\(base)}").threadLength)
        XCTAssertNil(try decode(NodeDetail.self, #"{\#(base),"thread_length":null}"#).threadLength)
        XCTAssertEqual(try decode(NodeDetail.self, #"{\#(base),"thread_length":"notice"}"#).threadLength, .notice)
        XCTAssertEqual(try decode(NodeDetail.self, #"{\#(base),"thread_length":"limit"}"#).threadLength, .limit)
        // A value a later server may add is no state, and never breaks the page.
        XCTAssertNil(try decode(NodeDetail.self, #"{\#(base),"thread_length":"soon"}"#).threadLength)
        XCTAssertNil(try decode(NodeDetail.self, #"{\#(base),"thread_length":3}"#).threadLength)
    }

    func testTheRefusalCodes() throws {
        let error = apiError(409, tooLongJSON)
        XCTAssertTrue(error.isThreadTooLong)
        XCTAssertFalse(error.isAutoGenerateOff)
        XCTAssertFalse(apiError(409, #"{"error":"x","code":"auto_generate_off"}"#).isThreadTooLong)
        XCTAssertFalse(apiError(400, tooLongJSON).isThreadTooLong, "only the 409")
        XCTAssertTrue(ThreadLength.isRefusal("thread_too_long"))
        XCTAssertFalse(ThreadLength.isRefusal(nil))
        XCTAssertFalse(ThreadLength.isRefusal("auto_generate_off"))

        let status = try decode(LLMStatus.self,
                                #"{"node_id":7,"status":"failed","error":"\#(limitText)","error_code":"thread_too_long"}"#)
        XCTAssertEqual(status.errorCode, "thread_too_long")
        XCTAssertNil(try decode(LLMStatus.self, #"{"node_id":7,"status":"failed","error":"x"}"#).errorCode)

        let session = try decode(StreamingSessionStatus.self,
                                 #"{"streaming_status":"completed","content":"hi","warning":"saved","warning_code":"thread_too_long"}"#)
        XCTAssertEqual(session.warningCode, "thread_too_long")
        XCTAssertNil(try decode(StreamingSessionStatus.self, #"{"content":"hi","warning":"saved"}"#).warningCode)

        let start = try decode(TextmodeStartResponse.self,
                               #"{"user_node_id":4,"llm_error":"\#(limitText)","llm_error_code":"thread_too_long"}"#)
        XCTAssertEqual(start.llmErrorCode, "thread_too_long")
        XCTAssertNil(start.llmNodeId)
        let saved = try decode(SaveAsNodeResponse.self, #"{"id":4,"llm_error":"x","llm_error_code":"thread_too_long"}"#)
        XCTAssertEqual(saved.llmErrorCode, "thread_too_long")
    }

    // MARK: The Voice screen's block

    func testARefusedStartBlocksWithTheServersText() {
        let block = VoiceAIBlock.from(apiError(409, tooLongJSON), fallback: .account)
        XCTAssertEqual(block, VoiceAIBlock(scope: .thread, reason: .threadTooLong, message: limitText))
        XCTAssertEqual(block?.isThreadTooLong, true)
        XCTAssertEqual(block?.heading, "This thread is too long")
        XCTAssertEqual(block?.body, limitText)
        // Without a text: the app's.
        let bare = VoiceAIBlock.from(apiError(409, #"{"code":"thread_too_long"}"#), fallback: .account)
        XCTAssertEqual(bare?.body, ThreadLength.limitText)
        // The AI-usage block is unchanged.
        let none = VoiceAIBlock.from(apiError(403, #"{"error":"x","code":"ai_usage_none","scope":"account"}"#), fallback: .thread)
        XCTAssertEqual(none, VoiceAIBlock(scope: .account))
        XCTAssertEqual(none?.heading, VoiceAIBlock.title)
        XCTAssertEqual(none?.isThreadTooLong, false)
    }

    func testAvailabilityAtAndNearTheLimit() throws {
        let closed = try decode(VoiceAvailability.self,
                                #"{"allowed":false,"code":"thread_too_long","error":"\#(limitText)"}"#)
        XCTAssertEqual(closed.block, VoiceAIBlock.threadTooLong(message: limitText))
        XCTAssertFalse(closed.showsNotice)
        XCTAssertEqual(VoiceAIBlock.decide(threadId: 12, accountAIUsage: .chat, availability: closed)?.isThreadTooLong, true)

        let near = try decode(VoiceAvailability.self, #"{"allowed":true,"thread_length":"notice"}"#)
        XCTAssertNil(near.block)
        XCTAssertTrue(near.showsNotice)
        XCTAssertEqual(near.threadLength, .notice)

        let open = try decode(VoiceAvailability.self, #"{"allowed":true}"#)
        XCTAssertFalse(open.showsNotice)
        XCTAssertNil(open.block)
    }

    func testTheSpeakersVoiceIsEnglish() {
        XCTAssertEqual(SentenceSpeaker.voiceLanguage(preferred: ["en-GB", "cs-CZ"]), "en-GB")
        XCTAssertEqual(SentenceSpeaker.voiceLanguage(preferred: ["cs-CZ", "en-AU"]), "en-AU")
        XCTAssertEqual(SentenceSpeaker.voiceLanguage(preferred: ["cs-CZ"]), "en-US")
        XCTAssertEqual(SentenceSpeaker.voiceLanguage(preferred: []), "en-US")
    }
}

/// The reply form against a stubbed server.
@MainActor
final class ThreadLengthModelTests: StubbedAppTestCase {
    private func focalJSON(_ length: String?) -> String {
        let field = length.map { #","thread_length":"\#($0)""# } ?? ""
        return """
        {"id":10,"content":"entry","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"private",
         "ai_usage":"chat","child_count":0,"children":[]\(field),
         "ancestors":[{"id":9,"content":"root","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private"}]}
        """
    }

    private func loadedModel(_ length: String?, llm: StubResponse = .json(202, #"{"node_id":77,"status":"pending"}"#)) async -> ThreadModel {
        let focal = focalJSON(length)
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

    func testUnderTheNoticeNothingChanges() async {
        let model = await loadedModel(nil)
        XCTAssertNil(model.threadLength)
        XCTAssertFalse(model.atThreadLimit)
        XCTAssertTrue(model.showCraftBar(craftMode: true, autoGenerate: false))
    }

    func testTheNoticeKeepsEveryAction() async {
        let model = await loadedModel("notice")
        XCTAssertEqual(model.threadLength, .notice)
        XCTAssertFalse(model.atThreadLimit)
        XCTAssertTrue(model.showCraftBar(craftMode: true, autoGenerate: false), "LLM Response stays")
    }

    func testAtTheLimitNoReplyIsOfferedAndWritingStays() async {
        let model = await loadedModel("limit")
        XCTAssertEqual(model.threadLength, .limit)
        XCTAssertTrue(model.atThreadLimit)
        XCTAssertFalse(model.showCraftBar(craftMode: true, autoGenerate: false), "no LLM Response")
        XCTAssertFalse(model.showCraftBar(craftMode: false, autoGenerate: false))
        XCTAssertEqual(model.threadLimitText, ThreadLength.limitText)

        // The entry written under it is saved; no automatic reply is asked for.
        var auto = true
        await model.inlineReplySent(NodeFormResult(id: 11, privacy: .private),
                                    autoGenerate: Binding(get: { auto }, set: { auto = $0 }))
        XCTAssertFalse(calls.contains { $0.hasSuffix("/llm") })
        XCTAssertTrue(auto, "the preference is kept")
        let opened = await eventually { self.lastRoute == .thread(id: 11, awaitLLM: nil) }
        XCTAssertTrue(opened)

        // "Start a new thread": the Write screen.
        NodePrefetch.shared.resetForTesting()
        model.startNewThread()
        XCTAssertEqual(lastRoute, .textMode())
    }

    func testARefusedLLMResponseSwitchesTheFormToTheLimit() async {
        let model = await loadedModel(nil, llm: .json(409, tooLongJSON))
        XCTAssertNil(model.threadLength)
        model.llmResponsePressed()
        let refused = await eventually { model.threadLength == .limit }
        XCTAssertTrue(refused)
        XCTAssertTrue(model.atThreadLimit)
        XCTAssertEqual(model.threadLimitText, limitText)
        XCTAssertTrue(app.toasts.toasts.isEmpty, "the form says it, not a toast")
        XCTAssertFalse(model.showCraftBar(craftMode: true, autoGenerate: false))
    }

    func testAnEntryThatReachedTheLimitOpensQuietly() async {
        let model = await loadedModel("notice", llm: .json(409, tooLongJSON))
        var auto = true
        await model.inlineReplySent(NodeFormResult(id: 11, privacy: .private),
                                    autoGenerate: Binding(get: { auto }, set: { auto = $0 }))
        XCTAssertTrue(calls.contains("POST /api/nodes/11/llm"))
        XCTAssertTrue(app.toasts.toasts.isEmpty, "no error toast")
        let opened = await eventually { self.lastRoute == .thread(id: 11, awaitLLM: nil) }
        XCTAssertTrue(opened, "the entry's page shows the limit")
    }

    func testOnlyTheOwnerSeesIt() async {
        let focal = focalJSON("limit").replacingOccurrences(of: #""id":5,"username":"seowriter""#,
                                                            with: #""id":6,"username":"someone""#)
        StubURLProtocol.install { request in
            request.url?.path(percentEncoded: true) == "/api/nodes/10" ? .json(200, focal) : .json(200, "{}")
        }
        let model = ThreadModel(nodeId: 10, awaitLLM: nil, app: app)
        await model.load()
        XCTAssertFalse(model.isOwner)
        XCTAssertNil(model.threadLength)
        XCTAssertFalse(model.atThreadLimit)
    }
}

/// The writing form: a reply skipped for the thread's length is not toasted.
@MainActor
final class ThreadLengthFormTests: StubbedAppTestCase {
    func testASkippedReplyForTheLimitIsNotToasted() async {
        var config = NodeFormConfig(parentId: nil)
        config.submitOverride = { _ in
            NodeFormResult(id: 5, userNodeId: 5, llmError: limitText, llmErrorCode: ThreadLength.code)
        }
        StubURLProtocol.install { _ in .json(200, "{}") }
        let model = NodeFormModel(config: config, app: app, defaults: defaults)
        var result: NodeFormResult?
        model.onSuccess = { result = $0 }
        model.content = "a long document"
        await model.submit()
        XCTAssertEqual(result?.llmErrorCode, "thread_too_long")
        XCTAssertTrue(app.toasts.toasts.isEmpty)
    }

    func testOtherSkippedRepliesStillToast() async {
        var config = NodeFormConfig(parentId: nil)
        config.submitOverride = { _ in NodeFormResult(id: 5, userNodeId: 5, llmError: "refused", llmErrorCode: "other") }
        StubURLProtocol.install { _ in .json(200, "{}") }
        let model = NodeFormModel(config: config, app: app, defaults: defaults)
        model.content = "typed"
        await model.submit()
        XCTAssertEqual(app.toasts.toasts.last?.message, "refused")
    }
}
