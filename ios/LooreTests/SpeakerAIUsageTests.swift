import XCTest
@testable import Loore

// The speaker icon follows the server's speech rule (`speech_allowed`): no new
// speech where AI usage does not let AI read, model replies included.

final class SpeakerAIUsageTests: XCTestCase {
    func testSpeechIsOffWhereAIMayNotRead() {
        // Any node, a model's reply included: the server no longer exempts replies.
        XCTAssertTrue(SpeakerButton.speechOff(target: .node(1), aiUsage: .off))
        XCTAssertTrue(SpeakerButton.speechOff(target: .node(1), aiUsage: .unknown("odd")))
        XCTAssertFalse(SpeakerButton.speechOff(target: .node(1), aiUsage: .chat))
        XCTAssertFalse(SpeakerButton.speechOff(target: .node(1), aiUsage: .train))
        // Profile versions follow their own AI usage when the server sends it.
        XCTAssertTrue(SpeakerButton.speechOff(target: .profile(2), aiUsage: .off))
        XCTAssertFalse(SpeakerButton.speechOff(target: .profile(2), aiUsage: .chat))
        XCTAssertFalse(SpeakerButton.speechOff(target: .profile(2), aiUsage: nil))
        // Saved references have no AI usage.
        XCTAssertFalse(SpeakerButton.speechOff(target: .item(3), aiUsage: .off))
    }

    func testLLMReplyNodeDecodesItsAIUsageForTheSpeaker() throws {
        let node = try decode(NodeDetail.self, #"""
        {"id":101,"content":"A reply","node_type":"llm","llm_model":"gpt-6-luna","ai_usage":"none",
         "privacy_level":"private","user_id":9,"username":"gpt-6-luna","ancestors":[],"children":[]}
        """#)
        XCTAssertEqual(node.aiUsage, .off)
        XCTAssertTrue(SpeakerButton.speechOff(target: .node(node.id), aiUsage: node.aiUsage))
    }

    func testLatestProfileReadsAnOptionalAIUsage() throws {
        let with = try decode(LatestProfile.self, #"{"id":5,"content":"p","has_tts":false,"ai_usage":"none"}"#)
        XCTAssertEqual(with.aiUsage, .off)
        let without = try decode(LatestProfile.self, #"{"id":5,"content":"p","has_tts":false}"#)
        XCTAssertNil(without.aiUsage)
    }
}
