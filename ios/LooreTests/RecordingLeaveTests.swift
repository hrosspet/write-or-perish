import AVFoundation
import XCTest
@testable import Loore

/// A 440 Hz tone, 48 kHz mono, played by `AudioFileSource` in real time.
private func toneFile(in root: URL, seconds: Double) throws -> URL {
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

/// The chunk index in an upload's multipart body.
private func chunkIndex(_ body: Data) -> Int {
    let text = String(decoding: body, as: UTF8.self)
    guard let r = text.range(of: "name=\"chunk_index\"\r\n\r\n") else { return -1 }
    return Int(text[r.upperBound...].prefix { $0.isNumber }) ?? -1
}

private struct StoredChunk {
    var session: String
    var index: Int
    var body: Data
}

/// Holds the fake server's answers while closed (a slow network).
private final class Gate: @unchecked Sendable {
    private let lock = NSLock()
    private var isOpen = true
    var open: Bool {
        get { lock.lock(); defer { lock.unlock() }; return isOpen }
        set { lock.lock(); isOpen = newValue; lock.unlock() }
    }
}

/// A test upload queue whose fake server stores every chunk.
@MainActor
private func makeUploader(root: URL, gate: Gate, stored: @escaping @MainActor (StoredChunk) -> Void) -> ChunkUploader {
    let uploader = ChunkUploader(root: root.appendingPathComponent("uploads"), useBackgroundSession: false)
    uploader.delay = { _ in 0.001 }
    uploader.cookieHeader = { _ in [:] }
    uploader.transport = { request, file in
        while !gate.open { try await Task.sleep(nanoseconds: 20_000_000) }
        let body = try Data(contentsOf: file)
        stored(StoredChunk(session: request.url!.pathComponents[1], index: chunkIndex(body), body: body))
        return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil,
                                                 headerFields: nil)!)
    }
    return uploader
}

/// #753: leaving mid-recording (the Voice screen or the writing form went away)
/// keeps the piece recorded since the last 15-second cut. The real recorder,
/// encoder and upload queue, with a tone file as the microphone and a fake
/// server that stores every chunk.
@MainActor
final class RecordingLeaveTests: XCTestCase {
    private var root: URL!
    private var uploader: ChunkUploader!
    private var log: RecordingLog!
    private let gate = Gate()
    private var stored: [StoredChunk] = []
    private var released: [String] = []
    private var discarded: [String] = []

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("leave-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        uploader = makeUploader(root: root, gate: gate) { [unowned self] in self.stored.append($0) }
        log = RecordingLog(directory: root.appendingPathComponent("logs"))
    }

    override func tearDown() async throws {
        gate.open = true
        try? FileManager.default.removeItem(at: root)
    }

    private func url(_ session: String) -> URL {
        URL(string: "http://test/\(session)/audio-chunk")!
    }

    private func recorder(seconds: Double = 6) throws -> VoiceRecorder {
        VoiceRecorder(uploader: uploader, debugFile: try toneFile(in: root, seconds: seconds), recordingLog: log)
    }

    /// `leave()` as Voice and the dictation call it, noting releases and discards.
    private func leave(_ recorder: VoiceRecorder) -> Task<RecordingLeft, Never> {
        recorder.leave(release: { [unowned self] in self.released.append($0) },
                       discard: { [unowned self] in self.discarded.append($0) })
    }

    private func wait(_ what: String, timeout: Double = 10, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertTrue(condition(), "timed out waiting: \(what)")
    }

    private func hasFtyp(_ data: Data) -> Bool { data.range(of: Data("ftyp".utf8)) != nil }

    private func queued(_ session: String) -> Bool {
        FileManager.default.fileExists(atPath: root.appendingPathComponent("uploads/\(session)").path)
    }

    func testLeavingMidRecordingQueuesTheUnfinishedPiece() async throws {
        let recorder = try recorder()
        try recorder.start(sessionId: "s1", uploadURL: url("s1"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("2.5 s recorded") { recorder.elapsed >= 2.5 }
        XCTAssertTrue(stored.isEmpty, "nothing is cut before 15 s")

        let left = leave(recorder)
        XCTAssertFalse(recorder.isRecording, "capture stops at once")
        let result = await left.value

        XCTAssertEqual(result, .stored(sessionId: "s1"))
        XCTAssertEqual(stored.map(\.session), ["s1"])
        XCTAssertEqual(stored.map(\.index), [0])
        XCTAssertTrue(hasFtyp(stored[0].body), "chunk 0 carries the init segment")
        // 2.5 s of AAC at 64 kbps is about 20 KB.
        XCTAssertGreaterThan(stored[0].body.count, 12_000)
        XCTAssertEqual(released, ["s1"], "offered for recovery at once")
        XCTAssertEqual(discarded, [])
        XCTAssertFalse(queued("s1"), "nothing of it left in the queue")
    }

    // #753 review: a Record tapped by accident and Back. Under
    // `shortestKeptRecording`, with no earlier piece, the session is discarded
    // as before #753 (not offered, not transcribed by Text mode).
    func testALeftRecordingUnderTheMinimumWithNoEarlierPieceIsDiscarded() async throws {
        XCTAssertEqual(VoiceRecorder.shortestKeptRecording, 2)
        let recorder = try recorder()
        try recorder.start(sessionId: "s1", uploadURL: url("s1"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("1 s recorded") { recorder.elapsed >= 1 }

        let result = await leave(recorder).value

        XCTAssertEqual(result, .empty(sessionId: "s1"))
        XCTAssertEqual(discarded, ["s1"])
        XCTAssertEqual(released, [])
        XCTAssertTrue(stored.isEmpty, "nothing uploaded")
        XCTAssertFalse(queued("s1"))
    }

    // Continuing an existing recording is kept however short the new part is.
    func testAContinuedRecordingIsKeptHoweverShort() async throws {
        let recorder = try recorder()
        try recorder.startHoldingChunks(sessionId: "s5", uploadURL: url("s5"), firstChunkIndex: 4, elapsedOffset: 60)
        recorder.releaseHeldChunks(firstChunkIndex: 4)
        await wait("0.5 s recorded") { recorder.elapsed >= 60.5 }

        let result = await leave(recorder).value

        XCTAssertEqual(result, .stored(sessionId: "s5"))
        XCTAssertEqual(stored.map(\.index), [4])
        XCTAssertEqual(released, ["s5"])
        XCTAssertEqual(discarded, [])
    }

    // What `cancel()` (and leaving before #753) did: the piece since the last
    // cut never reaches the queue. Sign-out still cancels.
    func testCancelDropsTheUnfinishedPiece() async throws {
        let recorder = try recorder()
        try recorder.start(sessionId: "s1", uploadURL: url("s1"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("2.5 s recorded") { recorder.elapsed >= 2.5 }
        recorder.cancel()
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertTrue(stored.isEmpty)
    }

    // #388: a continued recording sends nothing before its claim answered.
    func testLeavingAContinueHeldForItsClaimSendsNothing() async throws {
        let recorder = try recorder()
        try recorder.startHoldingChunks(sessionId: "s2", uploadURL: url("s2"), firstChunkIndex: 4, elapsedOffset: 60)
        await wait("1 s recorded") { recorder.elapsed >= 61 }

        let result = await leave(recorder).value
        try await Task.sleep(nanoseconds: 200_000_000)

        XCTAssertEqual(result, .nothing)
        XCTAssertFalse(recorder.isRecording)
        XCTAssertTrue(stored.isEmpty)
        XCTAssertEqual(released + discarded, [])
    }

    func testLeavingWithoutAnyAudioDiscardsTheSession() async throws {
        let recorder = try recorder(seconds: 0)
        try recorder.start(sessionId: "s3", uploadURL: url("s3"), firstChunkIndex: 0, elapsedOffset: 0)
        try await Task.sleep(nanoseconds: 300_000_000)

        let result = await leave(recorder).value

        XCTAssertEqual(result, .empty(sessionId: "s3"))
        XCTAssertEqual(discarded, ["s3"])
        XCTAssertTrue(stored.isEmpty)
    }

    func testNothingRecordingIsNothingToLeave() async {
        let recorder = VoiceRecorder(uploader: uploader, recordingLog: log)
        let result = await leave(recorder).value
        XCTAssertEqual(result, .nothing)
        XCTAssertEqual(released + discarded, [])
    }

    // The recorder is free at once: the next recording starts while the left
    // one still finishes, and each keeps its own chunks.
    func testTheNextRecordingStartsAtOnceAndKeepsItsOwnChunks() async throws {
        let recorder = try recorder()
        try recorder.start(sessionId: "a", uploadURL: url("a"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("2.5 s recorded") { recorder.elapsed >= 2.5 }

        let left = leave(recorder)
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

    // #753 review, must-fix: the left recording's uploads are slow, and the
    // person continues the same session on this phone before they land (as
    // Voice's Continue recording does: held, then the claim answers). The late
    // leave does nothing to the session: no forget, no release, no discard.
    // The continuation's pieces are queued, and nothing is lost.
    func testALateLeaveLeavesAContinuedRecordingAlone() async throws {
        gate.open = false
        let recorder = try recorder(seconds: 10)
        try recorder.start(sessionId: "s1", uploadURL: url("s1"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("2.5 s recorded") { recorder.elapsed >= 2.5 }
        let left = leave(recorder)

        try recorder.startHoldingChunks(sessionId: "s1", uploadURL: url("s1"), firstChunkIndex: 1, elapsedOffset: 15)
        recorder.releaseHeldChunks(firstChunkIndex: 1)
        await wait("1 s more recorded") { recorder.elapsed >= 16 }
        gate.open = true
        let result = await left.value

        XCTAssertEqual(result, .reopened(sessionId: "s1"))
        XCTAssertEqual(released, [])
        XCTAssertEqual(discarded, [])
        XCTAssertTrue(recorder.isRecording)

        let outcome = await recorder.stop()
        XCTAssertEqual(outcome.failed, [])
        XCTAssertEqual(outcome.totalForFinalize, 2)
        XCTAssertEqual(stored.filter { $0.session == "s1" }.map(\.index).sorted(), [0, 1])
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
        let recorder = try recorder()
        try recorder.start(sessionId: "s4", uploadURL: url("s4"), firstChunkIndex: 0, elapsedOffset: 0)
        await wait("2.5 s recorded") { recorder.elapsed >= 2.5 }

        let left = leave(recorder)
        uploader.reset(account: nil)
        let result = await left.value

        XCTAssertEqual(result, .nothing)
        XCTAssertTrue(stored.isEmpty)
        XCTAssertEqual(released + discarded, [])
        let leftovers = (try? FileManager.default.contentsOfDirectory(
            atPath: root.appendingPathComponent("uploads").path)) ?? []
        XCTAssertEqual(leftovers, [])
    }
}

/// `DictationController.leave()`: the writing form went away for good. The
/// real controller and recorder, a tone file as the microphone, a stubbed
/// server and a test upload queue.
@MainActor
final class DictationLeaveTests: XCTestCase {
    private var root: URL!
    private var app: AppState!
    private var uploader: ChunkUploader!
    private let gate = Gate()
    private var stored: [StoredChunk] = []
    /// Seconds the session's init takes.
    private var initDelay: TimeInterval = 0

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("dictation-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        var launch = LaunchOptions()
        launch.debugAudioFile = try toneFile(in: root, seconds: 8).path
        app = AppState(launch: launch, secureStore: InMemorySecureStore(),
                       defaults: UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!)
        app.useForTesting(api: makeStubbedClient(environment: .local),
                          user: try decode(CurrentUser.self, StubbedAppTestCase.userJSON))
        uploader = makeUploader(root: root, gate: gate) { [unowned self] in self.stored.append($0) }
        StubURLProtocol.install { [unowned self] request in
            let path = request.url?.path(percentEncoded: true) ?? ""
            switch (request.httpMethod ?? "", path) {
            case ("POST", APIPath.streamingInit):
                return StubResponse(status: 200, chunks: [Data(#"{"session_id":"d1","draft_id":5}"#.utf8)],
                                    chunkDelay: self.initDelay)
            case ("GET", APIPath.streamingStatus("d1")):
                return .json(200, #"{"session_id":"d1","streaming_status":"completed","content":"hello","chunks":[]}"#)
            case (_, _) where path.contains("/sse/"):
                return .eventStream(": waiting\n\n", delay: 30)
            default:
                return .json(200, "{}")
            }
        }
    }

    override func tearDown() async throws {
        StubURLProtocol.reset()
        try? FileManager.default.removeItem(at: root)
    }

    private var calls: [String] {
        StubURLProtocol.requests.map { "\($0.httpMethod ?? "") \($0.url?.path(percentEncoded: true) ?? "")" }
    }

    private func wait(_ what: String, timeout: Double = 10, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertTrue(condition(), "timed out waiting: \(what)")
    }

    private func start() -> (DictationController, Counter) {
        let dictation = DictationController(app: app, uploader: uploader)
        let ended = Counter()
        dictation.callbacks.failed = { message, _ in if message == nil { ended.count += 1 } }
        dictation.start(parentId: nil, privacy: "private", aiUsage: "chat")
        return (dictation, ended)
    }

    func testLeavingWhileRecordingKeepsTheRecordingAndReleasesIt() async throws {
        let (dictation, ended) = start()
        await wait("recording") { dictation.state == .recording }
        await wait("2.5 s recorded") { dictation.elapsed >= 2.5 }

        let left = dictation.leave()
        XCTAssertEqual(dictation.state, .idle, "capture stops at once")
        XCTAssertEqual(ended.count, 1, "the form hears it ended")
        XCTAssertNil(app.audio.activeDictation)
        let result = await left?.value

        XCTAssertEqual(result, .stored(sessionId: "d1"))
        XCTAssertEqual(stored.map(\.index), [0])
        await wait("released") { calls.contains("POST /api/drafts/streaming/d1/release") }
        let release = StubURLProtocol.requests.last { $0.url?.path(percentEncoded: true) == "/api/drafts/streaming/d1/release" }
        let body = (try? JSONSerialization.jsonObject(with: release?.httpBody ?? Data())) as? [String: Any]
        XCTAssertEqual(body?["device_key"] as? String, RecordingDevice.key)
        XCTAssertFalse(calls.contains { $0.hasSuffix("/finalize") || $0.hasSuffix("/discard") })
    }

    func testLeavingAShortAccidentalRecordingDiscardsIt() async throws {
        let (dictation, _) = start()
        await wait("recording") { dictation.state == .recording }
        await wait("0.5 s recorded") { dictation.elapsed >= 0.5 }

        let result = await dictation.leave()?.value

        XCTAssertEqual(result, .empty(sessionId: "d1"))
        await wait("discarded") { calls.contains("DELETE /api/drafts/streaming/d1/discard") }
        XCTAssertTrue(stored.isEmpty)
        XCTAssertFalse(calls.contains { $0.hasSuffix("/release") })
    }

    // The form closed while the session was being made: no recording starts,
    // and the session made for it is discarded.
    func testLeavingWhileStartingDiscardsTheNewSession() async throws {
        initDelay = 0.5
        let (dictation, ended) = start()
        await wait("init sent") { calls.contains("POST /api/drafts/streaming/init") }
        XCTAssertEqual(dictation.state, .initializing)

        XCTAssertNil(dictation.leave())
        XCTAssertEqual(dictation.state, .idle)
        XCTAssertEqual(ended.count, 1)

        await wait("discarded") { calls.contains("DELETE /api/drafts/streaming/d1/discard") }
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(dictation.state, .idle, "no recording started")
        XCTAssertTrue(stored.isEmpty)
        XCTAssertFalse(calls.contains { $0.contains("/sse/") })
    }

    // A Stop already on its way finishes: finalize, then the transcript.
    func testLeavingWhileFinalizingLetsTheStopFinish() async throws {
        let (dictation, _) = start()
        var transcript: String?
        dictation.callbacks.finished = { _, content in transcript = content }
        await wait("recording") { dictation.state == .recording }
        await wait("0.5 s recorded") { dictation.elapsed >= 0.5 }
        dictation.stop()
        XCTAssertEqual(dictation.state, .finalizing)

        XCTAssertNil(dictation.leave())
        XCTAssertEqual(dictation.state, .finalizing)

        await wait("transcript") { transcript == "hello" }
        XCTAssertTrue(calls.contains("POST /api/drafts/streaming/d1/finalize"))
        XCTAssertFalse(calls.contains { $0.hasSuffix("/release") || $0.hasSuffix("/discard") })
    }

    func testLeavingAnIdleFormDoesNothing() {
        let dictation = DictationController(app: app, uploader: uploader)
        XCTAssertNil(dictation.leave())
        XCTAssertEqual(dictation.state, .idle)
        XCTAssertTrue(calls.isEmpty)
    }
}

@MainActor
private final class Counter {
    var count = 0
}
