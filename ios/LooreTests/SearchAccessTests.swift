import XCTest
@testable import Loore

// Search for an account whose Default AI usage is None (#684): search by meaning
// sends the search words to an embedding model, so such an account has no search;
// an admin keeps keyword search. The copy is the web's (SearchModal).

final class SearchAccessTests: XCTestCase {
    private func refusal(_ body: String, status: Int = 403) -> APIError {
        APIError.from(status: status, contentType: "application/json", data: Data(body.utf8))
    }

    func testAnAccountSetToNoneHasNoSearch() {
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .off, isAdmin: false), .needsAI)
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .unknown("odd"), isAdmin: false), .needsAI)
    }

    func testAnAdminSetToNoneKeepsKeywordSearch() {
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .off, isAdmin: true), .keywordOnly)
    }

    func testChatAndTrainAccountsSearchByMeaning() {
        // Control: today's search.
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .chat, isAdmin: false), .semantic)
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .train, isAdmin: false), .semantic)
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .chat, isAdmin: true), .semantic)
        // No user loaded yet: the server refuses if it must.
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: nil, isAdmin: false), .semantic)
    }

    func testTheServersRefusalClosesSearch() {
        XCTAssertTrue(SearchAccess.isRefusal(refusal(#"{"error":"x","code":"ai_usage_none","scope":"account"}"#)))
        XCTAssertFalse(SearchAccess.isRefusal(refusal(#"{"error":"Provide a query (q)."}"#, status: 400)))
        XCTAssertFalse(SearchAccess.isRefusal(refusal(#"{"error":"Embedding the query failed."}"#, status: 502)))
        XCTAssertFalse(SearchAccess.isRefusal(APIError.transport(code: -1009, description: "offline")))
        XCTAssertFalse(SearchAccess.isRefusal(NSError(domain: "x", code: 1)))
    }

    func testCopyMatchesTheWeb() {
        XCTAssertEqual(SearchAccess.title, "Search needs AI")
        XCTAssertEqual(SearchAccess.body,
                       "Search needs AI to find things by meaning. Your Default AI usage is set to None, so Loore "
                       + "keeps your entries away from AI. You can change it in Account settings.")
    }

    // An account that keeps its writing with Anthropic: the search model is
    // OpenAI's, so there is no search by meaning; an admin keeps keyword search.

    func testAnAccountThatKeepsToAnthropicHasNoSearch() {
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .chat, isAdmin: false, keptToProvider: "anthropic"),
                       .oneProvider("anthropic"))
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .chat, isAdmin: true, keptToProvider: "anthropic"),
                       .keywordOnly)
        // Set to None as well: the None notice comes first.
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .off, isAdmin: false, keptToProvider: "anthropic"),
                       .needsAI)
        // Control: no provider named, today's search.
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .chat, isAdmin: false, keptToProvider: nil), .semantic)
        XCTAssertEqual(SearchAccess.decide(accountAIUsage: .chat, isAdmin: false, keptToProvider: ""), .semantic)
    }

    func testTheServersOneProviderRefusalNamesTheProvider() {
        XCTAssertEqual(SearchAccess.oneProviderRefusal(
            refusal(#"{"error":"x","code":"search_one_provider","provider":"anthropic"}"#)), "anthropic")
        XCTAssertEqual(SearchAccess.oneProviderRefusal(refusal(#"{"error":"x","code":"search_one_provider"}"#)),
                       "unknown")
        XCTAssertNil(SearchAccess.oneProviderRefusal(
            refusal(#"{"error":"x","code":"ai_usage_none","scope":"account"}"#)))
        XCTAssertFalse(SearchAccess.isRefusal(
            refusal(#"{"error":"x","code":"search_one_provider","provider":"anthropic"}"#)))
    }

    func testTheUserFieldIsRead() throws {
        let kept = try JSONDecoder().decode(
            CurrentUser.self, from: Data(#"{"id":1,"search_kept_to_provider":"anthropic"}"#.utf8))
        XCTAssertEqual(kept.searchKeptToProvider, "anthropic")
        let other = try JSONDecoder().decode(CurrentUser.self, from: Data(#"{"id":1,"search_kept_to_provider":null}"#.utf8))
        XCTAssertNil(other.searchKeptToProvider)
        XCTAssertNil(try JSONDecoder().decode(CurrentUser.self, from: Data(#"{"id":1}"#.utf8)).searchKeptToProvider)
    }

    func testOneProviderCopy() {
        XCTAssertEqual(SearchAccess.oneProviderTitle, "Search is off")
        XCTAssertEqual(SearchAccess.oneProviderBody("anthropic"),
                       "Search finds things by meaning with a model from OpenAI. You chose to keep your writing "
                       + "with Anthropic only, so Loore sends neither your entries nor your search words to OpenAI, "
                       + "and there is no search. You can change this under Background tasks in Account settings "
                       + "on loore.org.")
    }
}
