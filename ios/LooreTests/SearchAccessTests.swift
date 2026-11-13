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
}
