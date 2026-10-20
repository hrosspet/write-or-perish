import XCTest
@testable import Loore

// Voice mode where AI usage is `none` (server code `ai_usage_none`): the error
// and availability answers, the Voice screen's decision and the copy shared
// with the web.

final class VoiceAIBlockTests: XCTestCase {
    private func refusal(_ body: String, status: Int = 403) -> APIError {
        APIError.from(status: status, contentType: "application/json", data: Data(body.utf8))
    }

    // MARK: Server answers

    func testInitRefusalCarriesCodeAndScope() {
        let error = refusal(#"{"error":"Voice mode needs AI to listen and reply.","code":"ai_usage_none","scope":"account"}"#)
        XCTAssertEqual(error.status, 403)
        XCTAssertEqual(error.code, "ai_usage_none")
        XCTAssertEqual(VoiceAIBlock.from(error, fallback: .thread), VoiceAIBlock(scope: .account))
        let thread = refusal(#"{"error":"x","code":"ai_usage_none","scope":"thread"}"#)
        XCTAssertEqual(VoiceAIBlock.from(thread, fallback: .account), VoiceAIBlock(scope: .thread))
    }

    func testRefusalWithoutScopeUsesTheFallback() {
        let error = refusal(#"{"error":"x","code":"ai_usage_none"}"#)
        XCTAssertEqual(VoiceAIBlock.from(error, fallback: .thread)?.scope, .thread)
        XCTAssertEqual(VoiceAIBlock.from(error, fallback: .account)?.scope, .account)
        let odd = refusal(#"{"error":"x","code":"ai_usage_none","scope":"galaxy"}"#)
        XCTAssertEqual(VoiceAIBlock.from(odd, fallback: .account)?.scope, .account)
    }

    func testOtherErrorsAreNoBlock() {
        XCTAssertNil(VoiceAIBlock.from(refusal(#"{"error":"Unauthorized"}"#), fallback: .account))
        XCTAssertNil(VoiceAIBlock.from(refusal(#"{"error":"monthly_spend_limit_reached"}"#, status: 402), fallback: .account))
        XCTAssertNil(VoiceAIBlock.from(refusal(#"{"error":"x","code":"init_parse_failed"}"#, status: 400), fallback: .account))
        XCTAssertNil(VoiceAIBlock.from(APIError.transport(code: -1009, description: "offline"), fallback: .account))
        XCTAssertNil(VoiceAIBlock.from(NSError(domain: "x", code: 1), fallback: .account))
    }

    func testAvailabilityDecoding() throws {
        let open = try decode(VoiceAvailability.self, #"{"allowed":true}"#)
        XCTAssertEqual(open, VoiceAvailability(allowed: true))
        XCTAssertNil(open.block)

        let closed = try decode(VoiceAvailability.self,
                                #"{"allowed":false,"code":"ai_usage_none","scope":"thread","error":"AI usage in this thread is set to None."}"#)
        XCTAssertEqual(closed.block, VoiceAIBlock(scope: .thread))
        XCTAssertEqual(closed.error, "AI usage in this thread is set to None.")

        let account = try decode(VoiceAvailability.self, #"{"allowed":false,"code":"ai_usage_none","scope":"account"}"#)
        XCTAssertEqual(account.block, VoiceAIBlock(scope: .account))

        let noScope = try decode(VoiceAvailability.self, #"{"allowed":false,"code":"ai_usage_none"}"#)
        XCTAssertEqual(noScope.block, VoiceAIBlock(scope: .thread))

        // Only ai_usage_none closes Voice mode; an unknown reason does not.
        let other = try decode(VoiceAvailability.self, #"{"allowed":false,"code":"something_else"}"#)
        XCTAssertNil(other.block)
        // A body without "allowed" is read as open.
        XCTAssertNil(try decode(VoiceAvailability.self, "{}").block)
    }

    func testAvailabilityPath() {
        XCTAssertEqual(APIPath.voiceAvailability, "/api/voice/availability")
        XCTAssertEqual(APIPath.canonical(APIPath.voiceAvailability), APIPath.voiceAvailability)
    }

    // MARK: The Voice screen's decision

    func testFreshConversationFollowsTheAccountDefault() {
        XCTAssertEqual(VoiceAIBlock.decide(threadId: nil, accountAIUsage: .off, availability: nil),
                       VoiceAIBlock(scope: .account))
        XCTAssertEqual(VoiceAIBlock.decide(threadId: nil, accountAIUsage: .unknown("odd"), availability: nil),
                       VoiceAIBlock(scope: .account))
        XCTAssertNil(VoiceAIBlock.decide(threadId: nil, accountAIUsage: .chat, availability: nil))
        XCTAssertNil(VoiceAIBlock.decide(threadId: nil, accountAIUsage: .train, availability: nil))
        // No user loaded yet: the server's init answer is the backstop.
        XCTAssertNil(VoiceAIBlock.decide(threadId: nil, accountAIUsage: nil, availability: nil))
    }

    func testAThreadFollowsTheServer() {
        let closed = VoiceAvailability(allowed: false, code: "ai_usage_none", scope: "thread")
        XCTAssertEqual(VoiceAIBlock.decide(threadId: 12, accountAIUsage: .chat, availability: closed),
                       VoiceAIBlock(scope: .thread))
        // A chat thread continues by voice even when the account default is None.
        XCTAssertNil(VoiceAIBlock.decide(threadId: 12, accountAIUsage: .off, availability: VoiceAvailability(allowed: true)))
        // No answer (older server, offline): not blocked; init refuses if it must.
        XCTAssertNil(VoiceAIBlock.decide(threadId: 12, accountAIUsage: .off, availability: nil))
    }

    func testBackToTheThreadPopsOnlyOverAThread() {
        XCTAssertTrue(VoiceAIBlock.backPops(previous: .thread(id: 4, awaitLLM: nil)))
        XCTAssertFalse(VoiceAIBlock.backPops(previous: .home))
        XCTAssertFalse(VoiceAIBlock.backPops(previous: nil))
    }

    func testCopyMatchesTheWeb() {
        XCTAssertEqual(VoiceAIBlock.title, "Voice mode needs AI")
        XCTAssertEqual(VoiceAIBlock(scope: .account).body,
                       "Voice mode needs AI to listen and reply. Your Default AI usage is set to None, so Loore keeps "
                       + "your entries away from AI. You can change it in Account settings.")
        XCTAssertEqual(VoiceAIBlock(scope: .thread).body,
                       "Voice mode needs AI to listen and reply. AI usage in this thread is set to None, so Loore keeps "
                       + "it away from AI. You can change it when you edit the thread's entries, and the default for new "
                       + "entries in Account settings.")
        XCTAssertEqual(VoiceAIBlock.accountAnchor, "ai-usage")
        XCTAssertEqual(AppRoute.parse("/account#ai-usage", environment: .production),
                       .account(anchor: "ai-usage"))
    }
}
