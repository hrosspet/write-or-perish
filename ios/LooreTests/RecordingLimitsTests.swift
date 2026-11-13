import XCTest
@testable import Loore

/// The server's limits on one recording (`backend/utils/recording_limits.py`):
/// past the length one recording may have transcribed, the rest of a dictation
/// is kept as audio without its text. The transcription stream says so with a
/// `chunk_error` carrying a code; the phone says it once, with no error sound,
/// and keeps recording. A start refused while other recordings are in progress
/// is shown in the server's words (`APIError.userMessage`, no change here).
final class RecordingLimitsTests: StubbedAppTestCase {
    func testChunkErrorCarriesTheCode() {
        let kept = TranscriptionStreamEvent(SSEEvent(event: "chunk_error",
            data: #"{"chunk_index":1440,"error":"Saved as audio","status":"failed","code":"recording_too_long"}"#))
        XCTAssertEqual(kept, .chunkError(chunkIndex: 1440, error: "Saved as audio", code: "recording_too_long"))

        // A failure carries no code (and an older server sends none).
        let failed = TranscriptionStreamEvent(SSEEvent(event: "chunk_error",
            data: #"{"chunk_index":3,"error":"Transcription failed","status":"failed"}"#))
        XCTAssertEqual(failed, .chunkError(chunkIndex: 3, error: "Transcription failed", code: nil))
    }

    func testMessages() {
        XCTAssertNotNil(RecordingLimits.untranscribedMessage("recording_too_long"))
        XCTAssertNil(RecordingLimits.untranscribedMessage(nil))
        XCTAssertNil(RecordingLimits.untranscribedMessage("init_parse_failed"))
        // The usage limit does not stop a recording's transcription (#341).
        XCTAssertNil(RecordingLimits.untranscribedMessage("monthly_spend_limit_reached"))
    }

    func testPastTheLengthLimitTheDictationSaysItOnce() {
        let dictation = DictationController(app: app)

        for _ in 0..<20 { dictation.noticeUntranscribed("recording_too_long") }

        XCTAssertEqual(app.toasts.toasts.map(\.message),
                       [RecordingLimits.untranscribedMessage("recording_too_long")!])
        XCTAssertFalse(app.spendCapped)
    }

    func testAFailedChunkSaysNothingHere() {
        let dictation = DictationController(app: app)

        dictation.noticeUntranscribed(nil)

        XCTAssertTrue(app.toasts.toasts.isEmpty)
    }

    func testARefusedContinueIsNotRetried() async throws {
        // A recording that had stopped and may not be continued (the usage
        // limit 402, the account's bounds 429): one attempt, then given up.
        for status in [402, 429] {
            let root = FileManager.default.temporaryDirectory.appendingPathComponent("uploads-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: root) }
            let uploader = ChunkUploader(root: root, useBackgroundSession: false)
            uploader.delay = { _ in 0.001 }
            uploader.cookieHeader = { _ in [:] }
            var attempts = 0
            uploader.transport = { request, _ in
                attempts += 1
                return (Data(#"{"error":"refused","code":"too_many_unfinished"}"#.utf8),
                        HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
            }
            uploader.open(sessionId: "s", uploadURL: URL(string: "http://x/c")!)
            uploader.enqueue(sessionId: "s", chunk: RecordedChunk(index: 3, data: Data(repeating: 3, count: 32), duration: 15))
            let outcome = await uploader.settle(sessionId: "s")
            XCTAssertEqual(outcome.failed, [3], "status \(status)")
            XCTAssertLessThanOrEqual(attempts, 2, "status \(status): one attempt, one more at Stop")
        }
    }

    func testARefusedStartShowsTheServersWords() {
        let text = "Other recordings of yours are still in progress. Finish one of them, then start this one."
        let error = APIError.from(status: 429, contentType: "application/json",
                                  data: Data(#"{"error":"\#(text)","code":"too_many_recordings"}"#.utf8))
        XCTAssertEqual(error.userMessage(fallback: "Could not start recording."), text)
        XCTAssertEqual(error.code, "too_many_recordings")
    }
}
