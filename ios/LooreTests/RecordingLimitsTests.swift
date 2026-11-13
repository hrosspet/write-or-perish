import XCTest
@testable import Loore

/// The server's limits on one recording (`backend/utils/recording_limits.py`):
/// at the monthly usage limit, or past the length one recording may have
/// transcribed, the rest of a dictation is kept as audio without its text.
/// The transcription stream says so with a `chunk_error` carrying a code; the
/// phone says it once, with no error sound, and keeps recording.
final class RecordingLimitsTests: StubbedAppTestCase {
    func testChunkErrorCarriesTheCode() {
        let kept = TranscriptionStreamEvent(SSEEvent(event: "chunk_error",
            data: #"{"chunk_index":20,"error":"Saved as audio","status":"failed","code":"monthly_spend_limit_reached"}"#))
        XCTAssertEqual(kept, .chunkError(chunkIndex: 20, error: "Saved as audio", code: "monthly_spend_limit_reached"))

        // A failure carries no code (and an older server sends none).
        let failed = TranscriptionStreamEvent(SSEEvent(event: "chunk_error",
            data: #"{"chunk_index":3,"error":"Transcription failed","status":"failed"}"#))
        XCTAssertEqual(failed, .chunkError(chunkIndex: 3, error: "Transcription failed", code: nil))
    }

    func testMessages() {
        XCTAssertNotNil(RecordingLimits.untranscribedMessage("monthly_spend_limit_reached"))
        XCTAssertNotNil(RecordingLimits.untranscribedMessage("recording_too_long"))
        XCTAssertNil(RecordingLimits.untranscribedMessage(nil))
        XCTAssertNil(RecordingLimits.untranscribedMessage("init_parse_failed"))
    }

    func testAtTheUsageLimitTheDictationSaysItOnceAndMarksTheAccount() {
        let dictation = DictationController(app: app)
        XCTAssertFalse(app.spendCapped)

        for _ in 0..<20 { dictation.noticeUntranscribed("monthly_spend_limit_reached") }

        XCTAssertEqual(app.toasts.toasts.map(\.message),
                       [RecordingLimits.untranscribedMessage("monthly_spend_limit_reached")!])
        XCTAssertTrue(app.spendCapped, "the banner shows")
    }

    func testPastTheLengthLimitItIsSaidOnceWithoutTheBanner() {
        let dictation = DictationController(app: app)

        dictation.noticeUntranscribed("recording_too_long")
        dictation.noticeUntranscribed("recording_too_long")

        XCTAssertEqual(app.toasts.toasts.count, 1)
        XCTAssertFalse(app.spendCapped)
    }

    func testAFailedChunkSaysNothingHere() {
        let dictation = DictationController(app: app)

        dictation.noticeUntranscribed(nil)

        XCTAssertTrue(app.toasts.toasts.isEmpty)
        XCTAssertFalse(app.spendCapped)
    }
}
