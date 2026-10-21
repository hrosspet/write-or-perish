import XCTest
@testable import Loore

// A failed read-aloud is announced like the web's `SpeakerIcon` (#519, #527).

@MainActor
final class ListenFailureTests: XCTestCase {
    func testMessagesMatchTheWeb() {
        XCTAssertEqual(ListenFailure.nothingArrivedMessage,
                       "Loore couldn't read this aloud. Tap the speaker to try again.")
        XCTAssertEqual(ListenFailure.partialMessage,
                       "Loore couldn't read the rest aloud. Tap the speaker to try again from the start.")
    }

    func testNothingIsAnnouncedWithoutAPendingFailure() {
        let center = AudioCenter()
        XCTAssertFalse(center.finishedQueue())
    }

    func testAPendingFailureForOtherAudioIsDroppedSilently() {
        let center = AudioCenter()
        center.pendingListenFailure = .node(7)
        // The player holds nothing (the user moved on): say nothing, forget it.
        XCTAssertFalse(center.finishedQueue())
        XCTAssertNil(center.pendingListenFailure)
    }
}
