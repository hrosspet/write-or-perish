import AVFoundation
import XCTest
@testable import Loore

/// #753: leaving mid-recording (the Voice screen or the writing form went away)
/// keeps the piece recorded since the last 15-second cut. The real recorder,
/// encoder and upload queue, with a tone file as the microphone and a fake
/// server that stores every chunk.
@MainActor
final class RecordingLeaveTests: XCTestCase {
    private struct StoredChunk {
        var session: String
        var index: Int
        var body: Data
    }

    private var root: URL!
    private var uploader: ChunkUploader!
    private var log: RecordingLog!
    private var stored: [StoredChunk] = []

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("leave-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        uploader = ChunkUploader(root: root.appendingPathComponent("uploads"), useBackgroundSession: false)
        uploader.delay = { _ in 0.001 }
        uploader.cookieHeader = { _ in [:] }
        uploader.transport = { [unowned self] request, file in
            let body = try Data(contentsOf: file)
            self.stored.append(StoredChunk(session: request.url!.pathComponents[1],
                                           index: Self.chunkIndex(body), body: body))
            return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil,
                                                     headerFields: nil)!)
        }
        log = RecordingLog(directory: root.appendingPathComponent("logs"))
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: root)
    }

    private static func chunkIndex(_ body: Data) -> Int {
        let text = String(decoding: body, as: UTF8.self)
        guard let r = text.range(of: "name=\"chunk_index\"\r\n\r\n") else { return -1 }
        return Int(text[r.upperBound...].prefix { $0.isNumber }) ?? -1
    }

    private func url(_ session: String) -> URL {
        URL(string: "http://test/\(session)/audio-chunk")!
    }

    /// A 440 Hz tone, 48 kHz mono, played by `AudioFileSource` in real time.
    private func toneFile(seconds: Double) throws -> URL {
        let url = root.appendingPathComponent("tone-\(UUID().uuidString).caf")
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1)!
        let file = try AVAudioFile(forWriting: url, settings: format.settings)
        let frames = AVAudioFrameCount(seconds * 48_000)
        if frames > 0 {
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
            buffer.frameLength = frames
            let samples = buffer.floatChannelData![0]
            for i in 0..<Int(frames) {
                samples[i] = Float(sin(2 * Double.pi * 440 * Double(i) / 48_000) * 0.3)
            }
            try file.write(from: buffer)
        }
        return url
    }

    private func wait(_ what: String, timeout: Double = 10, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertTrue(condition(), "timed out waiting: \(what)")
    }

    private func hasFtyp(_ data: Data) -> Bool { data.range(of: Data("ftyp".utf8)) != nil }

    func testLeavingMidRecordingQueuesTheUnfinishedPiece() async throws {
        let recorder = VoiceRecorder(uploader: uploader, debugFile: try toneFile(seconds: 6), recordingLog: log)
        try recorder.start(sessionId: "s1", uploadURL: url("s1"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("1.5 s recorded") { recorder.elapsed >= 1.5 }
        XCTAssertTrue(stored.isEmpty, "nothing is cut before 15 s")

        let left = recorder.leave()
        XCTAssertFalse(recorder.isRecording, "capture stops at once")
        let result = await left.value

        XCTAssertEqual(result, .stored(sessionId: "s1"))
        XCTAssertEqual(stored.map(\.session), ["s1"])
        XCTAssertEqual(stored.map(\.index), [0])
        XCTAssertTrue(hasFtyp(stored[0].body), "chunk 0 carries the init segment")
        // 1.5 s of AAC at 64 kbps is about 12 KB.
        XCTAssertGreaterThan(stored[0].body.count, 8_000)
    }

    // What `cancel()` (and leaving before #753) did: the piece since the last
    // cut never reaches the queue. Sign-out still cancels.
    func testCancelDropsTheUnfinishedPiece() async throws {
        let recorder = VoiceRecorder(uploader: uploader, debugFile: try toneFile(seconds: 6), recordingLog: log)
        try recorder.start(sessionId: "s1", uploadURL: url("s1"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("1.5 s recorded") { recorder.elapsed >= 1.5 }
        recorder.cancel()
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertTrue(stored.isEmpty)
    }

    // #388: a continued recording sends nothing before its claim answered.
    func testLeavingAContinueHeldForItsClaimSendsNothing() async throws {
        let recorder = VoiceRecorder(uploader: uploader, debugFile: try toneFile(seconds: 6), recordingLog: log)
        try recorder.startHoldingChunks(sessionId: "s2", uploadURL: url("s2"), firstChunkIndex: 4, elapsedOffset: 60)
        await wait("1 s recorded") { recorder.elapsed >= 61 }

        let result = await recorder.leave().value
        try await Task.sleep(nanoseconds: 200_000_000)

        XCTAssertEqual(result, .nothing)
        XCTAssertFalse(recorder.isRecording)
        XCTAssertTrue(stored.isEmpty)
    }

    func testLeavingWithoutAnyAudioReportsAnEmptySession() async throws {
        let recorder = VoiceRecorder(uploader: uploader, debugFile: try toneFile(seconds: 0), recordingLog: log)
        try recorder.start(sessionId: "s3", uploadURL: url("s3"), firstChunkIndex: 0, elapsedOffset: 0)
        try await Task.sleep(nanoseconds: 300_000_000)

        let result = await recorder.leave().value

        XCTAssertEqual(result, .empty(sessionId: "s3"))
        XCTAssertTrue(stored.isEmpty)
    }

    func testNothingRecordingIsNothingToLeave() async {
        let recorder = VoiceRecorder(uploader: uploader, recordingLog: log)
        let result = await recorder.leave().value
        XCTAssertEqual(result, .nothing)
    }

    // The recorder is free at once: the next recording starts while the left
    // one still finishes, and each keeps its own chunks.
    func testTheNextRecordingStartsAtOnceAndKeepsItsOwnChunks() async throws {
        let recorder = VoiceRecorder(uploader: uploader, debugFile: try toneFile(seconds: 6), recordingLog: log)
        try recorder.start(sessionId: "a", uploadURL: url("a"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("1 s recorded") { recorder.elapsed >= 1 }

        let left = recorder.leave()
        try recorder.start(sessionId: "b", uploadURL: url("b"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("b records") { recorder.elapsed >= 1 }
        let leftResult = await left.value
        let outcome = await recorder.stop()

        XCTAssertEqual(leftResult, .stored(sessionId: "a"))
        XCTAssertEqual(outcome.produced, 1)
        XCTAssertEqual(outcome.failed, [])
        XCTAssertEqual(stored.filter { $0.session == "a" }.map(\.index), [0])
        XCTAssertEqual(stored.filter { $0.session == "b" }.map(\.index), [0])
        XCTAssertTrue(stored.allSatisfy { hasFtyp($0.body) })
    }

    // The writing form holds this next to its dictation: SwiftUI releases it
    // when the form goes away for good, and the dictation leaves then.
    func testTheFormsDictationLeavesWhenTheFormsStateIsReleased() async {
        let ended = Counter()
        var lifetime: DictationLifetime? = DictationLifetime { ended.count += 1 }
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertNotNil(lifetime)
        XCTAssertEqual(ended.count, 0, "not while the form is there")

        lifetime = nil
        await wait("left") { ended.count == 1 }
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(ended.count, 1, "once")
    }

    // Sign-out deletes the queue before the left recording's last piece is
    // handed over: nothing of it is written or sent, and nothing is released.
    func testALeaveFinishingAfterTheQueueWasDeletedKeepsNothing() async throws {
        let recorder = VoiceRecorder(uploader: uploader, debugFile: try toneFile(seconds: 6), recordingLog: log)
        try recorder.start(sessionId: "s4", uploadURL: url("s4"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("1 s recorded") { recorder.elapsed >= 1 }

        let left = recorder.leave()
        uploader.reset()
        let result = await left.value

        XCTAssertEqual(result, .nothing)
        XCTAssertTrue(stored.isEmpty)
        let leftovers = (try? FileManager.default.contentsOfDirectory(
            atPath: root.appendingPathComponent("uploads").path)) ?? []
        XCTAssertEqual(leftovers, [])
    }
}

@MainActor
private final class Counter {
    var count = 0
}
