import XCTest
@testable import Loore

/// A gleaning pick's two verdicts (#502, option B, Peter 2026-10-14): one pair
/// at rest, a pair per part ("The intro", "The tweet") once the pick has a
/// verdict; the same rules and request as the web's PickVerdict.
final class PickVerdictTests: XCTestCase {
    func testAPickCarriesItsKindAndIntroVerdict() throws {
        let resolved = try decode(ResolvedQuotes.self, #"""
            {"has_quotes": true, "quotes": {}, "external_quotes": {"42": {
              "id": 42, "content": "A tweet.", "source": "read_pick", "user_id": 5,
              "feedback": "good", "recommendation_id": 9, "recommendation_kind": "read",
              "intro_verdict": "bad"}}}
            """#)
        let quote = try XCTUnwrap(resolved.externalQuotes.values[42] ?? nil)
        XCTAssertEqual(quote.recommendationKind, "read")
        XCTAssertEqual(quote.introVerdict, "bad")
        XCTAssertEqual(quote.feedback, "good")
        let plain = try decode(ResolvedQuotes.QuotedExternal.self, #"{"id": 43, "content": ""}"#)
        XCTAssertNil(plain.recommendationKind)
        XCTAssertNil(plain.introVerdict)
    }

    func testTheLabels() {
        XCTAssertEqual(PickVerdict.label(.pick, "good"), "Good pick")
        XCTAssertEqual(PickVerdict.label(.pick, "bad"), "Bad pick")
        XCTAssertEqual(PickVerdict.label(.intro, "good"), "Good intro")
        XCTAssertEqual(PickVerdict.label(.intro, "bad"), "Bad intro")
        XCTAssertEqual(PickVerdict.label(.tweet, "good"), "Good tweet")
        XCTAssertEqual(PickVerdict.label(.tweet, "bad"), "Bad tweet")
        XCTAssertEqual(ReferenceFeedbackControl.tweetLabels.good, "Good tweet")
        XCTAssertEqual(ReferenceFeedbackControl.quoteLabels.bad, "Bad quote")
    }

    func testAPickWithAnyVerdictIsOpened() {
        XCTAssertFalse(PickVerdict.Marks().opened)
        XCTAssertTrue(PickVerdict.Marks(intro: "bad").opened)
        XCTAssertTrue(PickVerdict.Marks(tweet: "good").opened)
    }

    func testThePicksTapSetsBothAndAPartsTapOnlyItself() {
        XCTAssertEqual(PickVerdict.body(.pick, "good", marks: .init()),
                       ["intro": .string("good"), "tweet": .string("good")])
        let both = PickVerdict.Marks(intro: "good", tweet: "good")
        // A lit glyph clears its part; the other one changes it.
        XCTAssertEqual(PickVerdict.body(.intro, "good", marks: both), ["intro": .null])
        XCTAssertEqual(PickVerdict.body(.tweet, "bad", marks: both), ["tweet": .string("bad")])
    }

    func testFocusFollowsWhenThePairOpensOrCloses() {
        let none = PickVerdict.Marks()
        XCTAssertEqual(PickVerdict.focusAfter("bad", was: none, now: .init(intro: "bad", tweet: "bad")), "intro-bad")
        XCTAssertEqual(PickVerdict.focusAfter("good", was: .init(tweet: "good"), now: none), "pick-good")
        XCTAssertNil(PickVerdict.focusAfter("good", was: .init(intro: "good", tweet: "good"), now: .init(tweet: "good")))
    }

    func testATapIsOneRequestToThePicksVerdict() async throws {
        StubURLProtocol.install { _ in
            .json(200, #"{"node_id": 5, "item_id": 42, "intro_verdict": "good", "feedback": "good", "feedback_shared": false, "read_at": "2026-10-14T09:00:00Z"}"#)
        }
        defer { StubURLProtocol.reset() }
        let answer = try await PickVerdict.send(nodeId: 5, itemId: 42,
                                                body: PickVerdict.body(.pick, "good", marks: .init()),
                                                api: makeStubbedClient())
        XCTAssertEqual(answer.introVerdict, "good")
        XCTAssertEqual(answer.feedback, "good")
        XCTAssertFalse(answer.feedbackShared)
        XCTAssertNotNil(answer.readAt)
        XCTAssertEqual(StubURLProtocol.requests.count, 1)
        let request = try XCTUnwrap(StubURLProtocol.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path, "/api/nodes/5/feed-picks/42/verdict")
        let sent = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: Any])
        XCTAssertEqual(sent["intro"] as? String, "good")
        XCTAssertEqual(sent["tweet"] as? String, "good")
    }

    func testClearingAPartSendsNull() async throws {
        StubURLProtocol.install { _ in .json(200, #"{"intro_verdict": null, "feedback": "good"}"#) }
        defer { StubURLProtocol.reset() }
        let answer = try await PickVerdict.send(nodeId: 5, itemId: 42,
                                                body: PickVerdict.body(.intro, "bad", marks: .init(intro: "bad", tweet: "good")),
                                                api: makeStubbedClient())
        XCTAssertNil(answer.introVerdict)
        XCTAssertEqual(answer.feedback, "good")
        let request = try XCTUnwrap(StubURLProtocol.requests.first)
        let sent = try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: Any])
        XCTAssertEqual(Array(sent.keys), ["intro"])
        XCTAssertTrue(sent["intro"] is NSNull)
    }
}
