import XCTest
@testable import Loore

/// #689: an entry Loore fetched through the X API is never at Train. The node
/// JSON carries `train_refused` (focal node, ancestors, children); the editor's
/// AI usage select then offers None and Chat only, with the server's words
/// under it; an import that kept X posts at Chat says so.
final class XApiNoTrainTests: XCTestCase {
    private let message = "Posts fetched from X can't be used for training, under X's rules for developers."

    func testTheNodeJSONCarriesTheFlagOnEveryShape() throws {
        let node = try decode(NodeDetail.self, #"""
        {"id": 1, "content": "a tweet", "ai_usage": "chat", "train_refused": true,
         "ancestors": [{"id": 0, "content": "root", "ai_usage": "chat", "train_refused": true}],
         "children": [{"id": 2, "content": "my reply", "ai_usage": "train"}]}
        """#)
        XCTAssertTrue(node.trainRefused)
        XCTAssertTrue(node.ancestors[0].trainRefused)
        XCTAssertFalse(node.children[0].trainRefused)
    }

    func testAnOlderServerWithoutTheFlagOffersTrain() throws {
        // Control: the key is absent everywhere before #689.
        let node = try decode(NodeDetail.self, #"{"id": 1, "content": "x", "ai_usage": "train"}"#)
        XCTAssertFalse(node.trainRefused)
    }

    func testTheSelectLeavesOutTrainForAnXAPIEntry() {
        XCTAssertEqual(PrivacySelectorView.offeredAIOptions(trainRefused: true).map(\.0), [.off, .chat])
        XCTAssertEqual(PrivacySelectorView.offeredAIOptions(trainRefused: false).map(\.0), [.off, .chat, .train])
        XCTAssertEqual(PrivacySelectorView.trainRefusedMessage, message)
    }

    func testTheImportResultNamesThePostsKeptAtChat() {
        XCTAssertNil(ImportResult.keptFromTrainNote(0))
        XCTAssertEqual(ImportResult.keptFromTrainNote(1), "1 post that Loore fetched from X stays at Chat. \(message)")
        XCTAssertEqual(ImportResult.keptFromTrainNote(3), "3 posts that Loore fetched from X stay at Chat. \(message)")

        let kept = ImportResult(["created": 1, "skipped": 2, "kept_from_train": 2])
        XCTAssertEqual(kept.notes.first, ImportResult.keptFromTrainNote(2))
        // Also when nothing new was added.
        let nothingNew = ImportResult(["created": 0, "skipped": 2, "kept_from_train": 2])
        XCTAssertEqual(nothingNew.notes, [ImportResult.keptFromTrainNote(2)!,
                                          "Everything in this archive was already imported — nothing new was added."])
        XCTAssertFalse(ImportResult(["created": 1]).notes.contains { $0.contains("fetched from X") })
    }
}
