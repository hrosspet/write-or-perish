import AVFoundation
import XCTest
@testable import Loore

final class ChunkQueueMathTests: XCTestCase {
    private let math = ChunkQueueMath(durations: [10, 5, 20])

    func testCumulativeStartsAndTotal() {
        XCTAssertEqual(math.total, 35)
        XCTAssertEqual(math.start(of: 0), 0)
        XCTAssertEqual(math.start(of: 1), 10)
        XCTAssertEqual(math.start(of: 2), 15)
        XCTAssertEqual(math.start(of: 3), 35)
        XCTAssertEqual(math.cumulative(index: 2, offset: 4.5), 19.5)
    }

    func testLocateAcrossChunks() throws {
        var spot = try XCTUnwrap(math.locate(0))
        XCTAssertEqual(spot.index, 0); XCTAssertEqual(spot.offset, 0); XCTAssertFalse(spot.atEnd)
        spot = try XCTUnwrap(math.locate(12))
        XCTAssertEqual(spot.index, 1); XCTAssertEqual(spot.offset, 2, accuracy: 1e-9)
        spot = try XCTUnwrap(math.locate(15))
        XCTAssertEqual(spot.index, 2); XCTAssertEqual(spot.offset, 0, accuracy: 1e-9)
        spot = try XCTUnwrap(math.locate(-3))
        XCTAssertEqual(spot.index, 0); XCTAssertEqual(spot.offset, 0)
        // Within 0.1 s of the end, or past it: the end (web seekToCumulativeTime).
        spot = try XCTUnwrap(math.locate(34.95))
        XCTAssertTrue(spot.atEnd); XCTAssertEqual(spot.index, 2)
        spot = try XCTUnwrap(math.locate(99))
        XCTAssertTrue(spot.atEnd)
        XCTAssertNil(ChunkQueueMath(durations: []).locate(1))
    }

    func testChapterStartsFollowLiveDurations() {
        let chapters = [QueueChapter(title: "I", chunkIndex: 0, startTime: 0),
                        QueueChapter(title: "II", chunkIndex: 2, startTime: 99),
                        QueueChapter(title: "file", chunkIndex: nil, startTime: 7)]
        XCTAssertEqual(math.chapterStart(chapters[1]), 15, "chunk-anchored: the live cumulative start")
        XCTAssertEqual(math.chapterStart(chapters[2]), 7, "single-file chapter: its stored start")
        XCTAssertEqual(math.chapterIndex(at: 3, chapters: Array(chapters.prefix(2))), 0)
        XCTAssertEqual(math.chapterIndex(at: 15, chapters: Array(chapters.prefix(2))), 1)
        XCTAssertEqual(math.chapterIndex(at: 30, chapters: Array(chapters.prefix(2))), 1)
        XCTAssertNil(math.chapterIndex(at: 3, chapters: []))
    }

    func testTimeFormats() {
        XCTAssertEqual(AudioTimeFormat.clock(0), "0:00")
        XCTAssertEqual(AudioTimeFormat.clock(65.9), "1:05")
        XCTAssertEqual(AudioTimeFormat.clock(.nan), "0:00")
        XCTAssertEqual(AudioTimeFormat.recording(3599), "59:59")
        XCTAssertEqual(AudioTimeFormat.recording(7), "00:07")
        XCTAssertEqual(NowPlayingController.recordingTitle(elapsed: 83, paused: false), "Recording 1:23")
        XCTAssertEqual(NowPlayingController.recordingTitle(elapsed: 83, paused: true), "Paused 1:23")
    }
}

final class SoundTests: XCTestCase {
    func testThinkingCueIsSoftAndLoopsCleanly() {
        let data = ToneSynth.thinkingCue()
        XCTAssertEqual(String(data: data.prefix(4), encoding: .ascii), "RIFF")
        XCTAssertEqual(ToneSynth.peakDBFS(data), -24, accuracy: 0.5)
        // Soft ≈ −30 dBFS, Very soft ≈ −38 dBFS after the player volume.
        XCTAssertEqual(ToneSynth.peakDBFS(data) + 20 * log10(Double(ThinkingCueLevel.soft.volume)), -30, accuracy: 0.6)
        XCTAssertEqual(ToneSynth.peakDBFS(data) + 20 * log10(Double(ThinkingCueLevel.verySoft.volume)), -38, accuracy: 0.6)
        // Starts and ends in silence so the loop has no click.
        let pcm = [UInt8](data.dropFirst(44))
        XCTAssertEqual(pcm.prefix(4), [0, 0, 0, 0])
        XCTAssertEqual(Array(pcm.suffix(4)), [0, 0, 0, 0])
    }

    func testChimesAreAudible() {
        XCTAssertGreaterThan(ToneSynth.peakDBFS(ToneSynth.interruptionAlert()), ToneSynth.peakDBFS(ToneSynth.errorSound()))
        XCTAssertGreaterThan(ToneSynth.peakDBFS(ToneSynth.longRecordingWarning()), -20)
    }

    func testCueLevelDefaultsToSoft() {
        let key = ThinkingCueLevel.defaultsKey
        let saved = UserDefaults.standard.string(forKey: key)
        defer { UserDefaults.standard.set(saved, forKey: key) }
        UserDefaults.standard.removeObject(forKey: key)
        XCTAssertEqual(ThinkingCueLevel.current, .soft)
        ThinkingCueLevel.current = .off
        XCTAssertEqual(ThinkingCueLevel.current, .off)
        XCTAssertEqual(ThinkingCueLevel.off.volume, 0)
    }
}

@MainActor
final class ChunkUploaderTests: XCTestCase {
    private var root: URL!
    private var uploader: ChunkUploader!
    private var sent: [(Int, Int)] = [] // (chunk index, attempt)

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("uploads-\(UUID().uuidString)")
        uploader = ChunkUploader(root: root, useBackgroundSession: false)
        uploader.delay = { _ in 0.001 }
        uploader.cookieHeader = { _ in ["Cookie": "session=test"] }
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: root)
    }

    private func index(of request: URLRequest, file: URL) -> Int {
        let body = (try? String(contentsOf: file, encoding: .isoLatin1)) ?? ""
        let marker = "name=\"chunk_index\"\r\n\r\n"
        guard let r = body.range(of: marker) else { return -1 }
        return Int(body[r.upperBound...].prefix { $0.isNumber }) ?? -1
    }

    private func chunk(_ i: Int) -> RecordedChunk {
        RecordedChunk(index: i, data: Data(repeating: UInt8(i), count: 32), duration: 15)
    }

    func testUploadsInOrderWithCookiesAndMultipartFields() async throws {
        var requests: [URLRequest] = []
        uploader.transport = { [unowned self] request, file in
            requests.append(request)
            self.sent.append((self.index(of: request, file: file), 0))
            return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        let url = URL(string: "http://localhost:5010/api/drafts/streaming/s1/audio-chunk")!
        uploader.open(sessionId: "s1", uploadURL: url)
        for i in 0..<3 { uploader.enqueue(sessionId: "s1", chunk: chunk(i)) }
        let outcome = await uploader.settle(sessionId: "s1")
        XCTAssertEqual(sent.map(\.0), [0, 1, 2])
        XCTAssertEqual(outcome, .init(produced: 3, stored: 3, failed: [], fatalMessage: nil))
        XCTAssertEqual(outcome.totalForFinalize, 3)
        XCTAssertEqual(requests.first?.value(forHTTPHeaderField: "Cookie"), "session=test")
        XCTAssertTrue(requests.first?.value(forHTTPHeaderField: "Content-Type")?.hasPrefix("multipart/form-data; boundary=") == true)
        XCTAssertEqual(requests.first?.url, url)
        // Stored chunks leave no files behind.
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent("s1").path)
        XCTAssertEqual(leftovers, ["manifest.json"])
        uploader.forget(sessionId: "s1")
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("s1").path))
    }

    // The 4-hour bound (backend/utils/recording_limits, 960 chunks): the chunk
    // the server refuses as past what one recording may hold, and every later
    // one, are never sent again, not at Stop and not after a relaunch. The
    // recording is finalized with the chunks the server holds.
    func testAChunkPastTheRecordingsLimitEndsItsUploadsForGood() async throws {
        let words = "This recording has reached the longest Loore takes, about four hours, and has stopped. Everything before this is saved."
        var sentIndexes: [Int] = []
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            sentIndexes.append(i)
            if i >= 3 {
                let body = #"{"error":"\#(words)","code":"chunk_index_out_of_range"}"#
                return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: 400, httpVersion: nil, headerFields: nil)!)
            }
            return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        var said: [String] = []
        uploader.onLimit = { sid, message in said.append("\(sid): \(message)") }
        uploader.onRefused = { _, _ in XCTFail("not a refusal to retry later") }
        uploader.open(sessionId: "s20", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<5 { uploader.enqueue(sessionId: "s20", chunk: chunk(i)) }
        let deadline = Date().addingTimeInterval(3)
        while said.isEmpty && Date() < deadline { try await Task.sleep(nanoseconds: 5_000_000) }
        // A chunk the recorder hands over after the refusal is not sent either.
        uploader.enqueue(sessionId: "s20", chunk: chunk(5))
        let outcome = await uploader.settle(sessionId: "s20")

        XCTAssertEqual(said, ["s20: \(words)"], "said once, in the server's words")
        XCTAssertEqual(sentIndexes, [0, 1, 2, 3], "nothing after the refused chunk, and no retry at Stop")
        XCTAssertEqual(outcome.stored, 3)
        XCTAssertEqual(outcome.failed, [], "no missing chunks: the recording finalizes")
        XCTAssertEqual(outcome.beyond, 3)
        XCTAssertEqual(outcome.produced, 6)
        XCTAssertEqual(outcome.totalForFinalize, 3, "the chunks the server holds, not the ones recorded")
        let files = try FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent("s20").path)
        XCTAssertEqual(files, ["manifest.json"], "the refused chunks are not kept for another try")

        // A relaunch sends nothing of it and drops what is left.
        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.delay = { _ in 0.001 }
        var resent: [Int] = []
        relaunched.transport = { [unowned self] request, file in
            resent.append(self.index(of: request, file: file))
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        relaunched.resumePending()
        _ = await relaunched.settle(sessionId: "s20")
        XCTAssertEqual(resent, [])
    }

    func testTheRecordingsByteLimitEndsItsUploadsTooAndOtherRefusalsDoNot() async throws {
        // 413 recording_too_large without words: the web's default words.
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            let body = i == 1 ? #"{"code":"recording_too_large"}"# : "{}"
            return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: i == 1 ? 413 : 202,
                                                     httpVersion: nil, headerFields: nil)!)
        }
        var said: [String] = []
        uploader.onLimit = { _, message in said.append(message) }
        uploader.open(sessionId: "s21", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<3 { uploader.enqueue(sessionId: "s21", chunk: chunk(i)) }
        let outcome = await uploader.settle(sessionId: "s21")
        XCTAssertEqual(said, [ChunkUploader.limitMessage])
        XCTAssertEqual(outcome.stored, 1)
        XCTAssertEqual(outcome.failed, [])
        XCTAssertEqual(outcome.totalForFinalize, 1)

        // Any other 400 is still a chunk the server does not have (not finalized).
        let other = ChunkUploader(root: root, useBackgroundSession: false)
        other.delay = { _ in 0.001 }
        other.transport = { request, _ in
            (Data(#"{"error":"Missing chunk_index"}"#.utf8),
             HTTPURLResponse(url: request.url!, statusCode: 400, httpVersion: nil, headerFields: nil)!)
        }
        var otherSaid: [String] = []
        other.onLimit = { _, message in otherSaid.append(message) }
        other.open(sessionId: "s22", uploadURL: URL(string: "http://x/c")!)
        other.enqueue(sessionId: "s22", chunk: chunk(0))
        let failed = await other.settle(sessionId: "s22")
        XCTAssertEqual(otherSaid, [])
        XCTAssertEqual(failed.failed, [0])
        XCTAssertEqual(failed.beyond, 0)

        // The four-hour bound without words: the same text the server sends.
        XCTAssertEqual(ChunkUploader.longestRecordingMessage,
                       "This recording has reached the longest Loore takes, about four hours, and has stopped. Everything before this is saved.")
    }

    // A chunk that came after the recording's finalize (`chunk_not_announced`)
    // is not the four-hour bound: nothing is said, it is not sent again, and
    // the chunks after it still go (keyed on the code, not the words).
    func testAChunkAfterTheFinalizeIsDroppedQuietlyNotTheBound() async throws {
        var sentIndexes: [Int] = []
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            sentIndexes.append(i)
            let body = i == 1
                ? #"{"error":"This recording was already finished, so this part was not added to it.","code":"chunk_not_announced"}"#
                : "{}"
            return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: i == 1 ? 400 : 202,
                                                     httpVersion: nil, headerFields: nil)!)
        }
        var said: [String] = []
        uploader.onLimit = { _, message in said.append(message) }
        uploader.onRefused = { _, message in said.append(message) }
        uploader.open(sessionId: "s23", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<2 { uploader.enqueue(sessionId: "s23", chunk: chunk(i)) }
        let deadline = Date().addingTimeInterval(3)
        while uploader.outcome("s23").beyond == 0 && Date() < deadline {
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        // A chunk handed over after the drop is still sent.
        uploader.enqueue(sessionId: "s23", chunk: chunk(2))
        let outcome = await uploader.settle(sessionId: "s23")
        XCTAssertEqual(said, [])
        XCTAssertEqual(sentIndexes, [0, 1, 2], "sent once, not again at Stop; later chunks go")
        XCTAssertEqual(outcome.stored, 2)
        XCTAssertEqual(outcome.failed, [])

        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.delay = { _ in 0.001 }
        var resent: [Int] = []
        relaunched.transport = { [unowned self] request, file in
            resent.append(self.index(of: request, file: file))
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        relaunched.resumePending()
        _ = await relaunched.settle(sessionId: "s23")
        XCTAssertEqual(resent, [])
    }

    func testRetriesThenGivesUpAndReportsStoredCount() async throws {
        var attempts: [Int: Int] = [:]
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            attempts[i, default: 0] += 1
            let status = i == 1 ? 503 : 202
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s2", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<3 { uploader.enqueue(sessionId: "s2", chunk: chunk(i)) }
        let outcome = await uploader.settle(sessionId: "s2")
        XCTAssertEqual(attempts[1], 6, "4 retries = 5 attempts (web uploadChunkWithRetry), plus one more at Stop")
        XCTAssertEqual(attempts[0], 1)
        XCTAssertEqual(outcome.failed, [1])
        XCTAssertEqual(outcome.stored, 2)
    }

    func testOfflineStopDoesNotRetryEveryChunkInFull() async throws {
        var attempts: [Int: Int] = [:]
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            attempts[i, default: 0] += 1
            if i == 0 {
                return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
            }
            throw URLError(.notConnectedToInternet)
        }
        uploader.open(sessionId: "s7", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<4 { uploader.enqueue(sessionId: "s7", chunk: chunk(i)) }
        let outcome = await uploader.settle(sessionId: "s7")
        XCTAssertEqual(attempts[1], 6, "5 attempts, then one more at Stop")
        XCTAssertEqual(attempts[2], 2, "one attempt once the network is known down, one more at Stop")
        XCTAssertEqual(attempts[3], 2)
        XCTAssertEqual(outcome.failed, [1, 2, 3])
    }

    // B1: a later chunk must never reach the server before chunk 0 (the init segment).
    func testChunkZeroIsNeverOvertakenOrGivenUpWhileRecording() async throws {
        var sentOrder: [Int] = []
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            sentOrder.append(i)
            let failing = i == 0 && sentOrder.filter { $0 == 0 }.count <= 7
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: failing ? 503 : 202, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s8", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s8", chunk: chunk(0))
        uploader.enqueue(sessionId: "s8", chunk: chunk(1))
        let deadline = Date().addingTimeInterval(3)
        while uploader.outcome("s8").stored < 2 && Date() < deadline {
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        let outcome = await uploader.settle(sessionId: "s8")
        XCTAssertEqual(sentOrder, Array(repeating: 0, count: 8) + [1], "chunk 0 retried past the 5 attempts; chunk 1 waited")
        XCTAssertEqual(outcome.stored, 2)
        XCTAssertEqual(outcome.failed, [])
    }

    // B1: chunk 0 still failing at Stop: chunk 1 (which would succeed) is held
    // back, both are reported missing and kept on disk, and the next launch sends them in order.
    func testChunkZeroFailingAtStopHoldsBackLaterChunksAndKeepsThem() async throws {
        var attempts: [Int: Int] = [:]
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            attempts[i, default: 0] += 1
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: i == 0 ? 503 : 202, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s9", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s9", chunk: chunk(0))
        uploader.enqueue(sessionId: "s9", chunk: chunk(1))
        let outcome = await uploader.settle(sessionId: "s9")
        XCTAssertEqual(attempts[0], 6, "5 attempts, then one more at Stop")
        XCTAssertNil(attempts[1], "never sent before chunk 0")
        XCTAssertEqual(outcome.stored, 0)
        XCTAssertEqual(outcome.failed, [0, 1], "missing: the caller must not finalize")
        let files = try FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent("s9").path).sorted()
        XCTAssertEqual(files, ["chunk_0.body", "chunk_1.body", "manifest.json"])

        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.delay = { _ in 0.001 }
        var sentIndexes: [Int] = []
        relaunched.transport = { [unowned self] request, file in
            sentIndexes.append(self.index(of: request, file: file))
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        relaunched.resumePending()
        let resumed = await relaunched.settle(sessionId: "s9")
        XCTAssertEqual(sentIndexes, [0, 1])
        XCTAssertEqual(resumed.stored, 2)
        XCTAssertEqual(resumed.failed, [])
    }

    func testTransportErrorsAreRetried() async throws {
        var calls = 0
        uploader.transport = { request, _ in
            calls += 1
            if calls < 3 { throw URLError(.networkConnectionLost) }
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s3", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s3", chunk: chunk(0))
        let outcome = await uploader.settle(sessionId: "s3")
        XCTAssertEqual(calls, 3)
        XCTAssertEqual(outcome.stored, 1)
    }

    func testInitParseFailedIsFatalAndStopsTheSession() async throws {
        var fatal: String?
        uploader.onFatal = { _, message in fatal = message }
        var calls = 0
        uploader.transport = { request, _ in
            calls += 1
            let body = #"{"error":"Could not parse init segment from first chunk","detail":"No moof/mdat","code":"init_parse_failed"}"#
            return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: 400, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s4", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s4", chunk: chunk(0))
        uploader.enqueue(sessionId: "s4", chunk: chunk(1))
        let outcome = await uploader.settle(sessionId: "s4")
        XCTAssertEqual(calls, 1, "no retry, and chunk 1 is not sent into a dead session")
        XCTAssertEqual(fatal, "Your audio recording could not be processed. Please try recording again. (No moof/mdat)")
        XCTAssertNotNil(outcome.fatalMessage)
    }

    // #388: another device continued the session: no retry, the recorder is told.
    func testTakenOverIsFatalAndSaysTheRecordingContinuedElsewhere() async throws {
        var fatal: String?
        uploader.onFatal = { _, message in fatal = message }
        var calls = 0
        var bodies: [String] = []
        uploader.transport = { request, file in
            calls += 1
            bodies.append((try? String(contentsOf: file, encoding: .isoLatin1)) ?? "")
            let body = #"{"error":"This recording was continued in another tab or on another device.","code":"session_taken_over"}"#
            return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: 409, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s5", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s5", chunk: chunk(0))
        uploader.enqueue(sessionId: "s5", chunk: chunk(1))
        let outcome = await uploader.settle(sessionId: "s5")
        XCTAssertEqual(calls, 1, "no retry, and nothing more is sent into the session")
        XCTAssertEqual(fatal, RecordingDevice.takenOverMessage)
        XCTAssertEqual(outcome.fatalMessage, RecordingDevice.takenOverMessage)
        // Every chunk carries this phone's key.
        XCTAssertTrue(bodies.first?.contains("name=\"device_key\"\r\n\r\n\(RecordingDevice.key)") == true)
    }

    // #388: the phone lost the session to another device, then claimed it back in
    // the same run: the continuation's chunks are sent, not dropped as a dead session's.
    func testASessionClaimedBackAfterATakeoverUploadsAgain() async throws {
        var takenOver = true
        var sent: [Int] = []
        uploader.transport = { [unowned self] request, file in
            if takenOver {
                let body = #"{"error":"x","code":"session_taken_over"}"#
                return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: 409, httpVersion: nil, headerFields: nil)!)
            }
            sent.append(self.index(of: request, file: file))
            return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s7", uploadURL: URL(string: "http://x/c")!, firstIndex: 3)
        uploader.enqueue(sessionId: "s7", chunk: chunk(3))
        let lost = await uploader.settle(sessionId: "s7")
        XCTAssertEqual(lost.fatalMessage, RecordingDevice.takenOverMessage)

        takenOver = false
        uploader.open(sessionId: "s7", uploadURL: URL(string: "http://x/c")!, firstIndex: 5)
        for i in 5...7 { uploader.enqueue(sessionId: "s7", chunk: chunk(i)) }
        let outcome = await uploader.settle(sessionId: "s7")
        XCTAssertEqual(sent, [5, 6, 7])
        XCTAssertNil(outcome.fatalMessage)
        XCTAssertEqual(outcome.produced, 3)
        XCTAssertEqual(outcome.totalForFinalize, 8)
    }

    // M2: a dead session keeps no audio on disk and is not "resumed" at every launch.
    func testFatalSessionKeepsNoAudioAndIsNotResumed() async throws {
        uploader.transport = { request, _ in
            let body = #"{"error":"Could not parse init segment","detail":"x","code":"init_parse_failed"}"#
            return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: 400, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s10", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s10", chunk: chunk(0))
        uploader.enqueue(sessionId: "s10", chunk: chunk(1))
        _ = await uploader.settle(sessionId: "s10")
        uploader.enqueue(sessionId: "s10", chunk: chunk(2))
        let dir = root.appendingPathComponent("s10")
        let bodies = ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []).filter { $0.hasSuffix(".body") }
        XCTAssertEqual(bodies, [], "no audio of a dead session stays on disk")

        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        var sent = 0
        relaunched.transport = { request, _ in
            sent += 1
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        relaunched.resumePending()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(sent, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.path))
    }

    // M2: sign-out deletes the queue; the next user never sends (or keeps) these chunks.
    func testResetDeletesTheQueueSoTheNextUserSendsNothing() async throws {
        uploader.transport = { _, _ in
            try await Task.sleep(nanoseconds: 30_000_000_000)
            throw URLError(.timedOut)
        }
        uploader.open(sessionId: "s11", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s11", chunk: chunk(0))
        uploader.enqueue(sessionId: "s11", chunk: chunk(1))
        try await Task.sleep(nanoseconds: 20_000_000)
        uploader.reset(account: nil)
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), [])

        let nextUser = ChunkUploader(root: root, useBackgroundSession: false)
        var sent = 0
        nextUser.transport = { request, _ in
            sent += 1
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        nextUser.resumePending()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(sent, 0)
    }

    private func answer(_ request: URLRequest, _ status: Int, _ body: String = "{}") -> (Data, HTTPURLResponse) {
        (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }

    private func files(_ sessionId: String) -> [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent(sessionId).path)) ?? []).sorted()
    }

    private func waitUntil(timeout: TimeInterval = 3, _ condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline { try await Task.sleep(nanoseconds: 5_000_000) }
    }

    // #763: the server refused the sign-in (401) and the app signed out on its
    // own. The pieces not sent stay on the phone, also one the recorder hands
    // over after the sign-out; another account that signs in never sends them
    // (not after a relaunch either); the account they were recorded under does.
    func testASignOutTheServerCausedKeepsTheAudioForTheSameAccount() async throws {
        uploader.resumePending(account: 7)
        var refused = true
        var refusals = 0
        var sent: [Int] = []
        uploader.transport = { [unowned self] request, file in
            if refused {
                refusals += 1
                return self.answer(request, 401)
            }
            sent.append(self.index(of: request, file: file))
            return self.answer(request, 202)
        }
        uploader.open(sessionId: "s30", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s30", chunk: chunk(0))
        uploader.enqueue(sessionId: "s30", chunk: chunk(1))
        // Chunk 0 refused; chunk 1 waits behind it (B1).
        try await waitUntil { refusals == 1 }
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertEqual(refusals, 1)

        uploader.hold()
        refused = false
        // The recorder's last piece, handed over after the sign-out.
        uploader.enqueue(sessionId: "s30", chunk: chunk(2))
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(sent, [], "nothing goes while nobody is signed in")
        XCTAssertEqual(files("s30"), ["chunk_0.body", "chunk_1.body", "chunk_2.body", "manifest.json"])
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 7))
        XCTAssertFalse(uploader.holdsUnsentAudio(account: 8))

        // Another account signs in, in this run and after a relaunch.
        uploader.resumePending(account: 8)
        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.transport = { [unowned self] request, file in
            sent.append(self.index(of: request, file: file))
            return self.answer(request, 202)
        }
        XCTAssertEqual(relaunched.resumePending(account: 8), [])
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(sent, [], "never sent with another account's sign-in")
        XCTAssertEqual(files("s30").count, 4, "and kept for its own")

        // The account it was recorded under signs in again.
        uploader.hold()
        uploader.resumePending(account: 7)
        let outcome = await uploader.settle(sessionId: "s30")
        XCTAssertEqual(sent, [0, 1, 2])
        XCTAssertEqual(outcome.stored, 3)
        XCTAssertEqual(outcome.failed, [])
        XCTAssertFalse(uploader.holdsUnsentAudio(account: 7))
    }

    // #763: Logout deletes the audio of the account that logs out, and only
    // that; without a known account (it could not be loaded), all of it.
    func testLogoutDeletesTheAudioOfTheAccountThatLogsOut() async throws {
        uploader.transport = { _, _ in throw URLError(.notConnectedToInternet) }
        uploader.resumePending(account: 7)
        uploader.open(sessionId: "s31", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s31", chunk: chunk(0))
        _ = await uploader.settle(sessionId: "s31")
        uploader.hold()
        uploader.resumePending(account: 8)
        uploader.open(sessionId: "s32", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s32", chunk: chunk(0))
        _ = await uploader.settle(sessionId: "s32")
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 7))
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 8))
        XCTAssertFalse(uploader.holdsUnsentAudio(account: 9))
        XCTAssertTrue(uploader.holdsUnsentAudio(account: nil))

        uploader.reset(account: 8)
        XCTAssertEqual(files("s32"), [])
        XCTAssertEqual(files("s31"), ["chunk_0.body", "manifest.json"], "another account's audio stays")
        XCTAssertFalse(uploader.holdsUnsentAudio(account: 8))
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 7))

        uploader.reset(account: nil)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: root.path), [])
        XCTAssertFalse(uploader.holdsUnsentAudio(account: nil))
    }

    // #763: a queue left by a build that did not record accounts goes to the
    // first account that signs in (as that build would have sent it), and then
    // belongs to it.
    func testAQueueFromAnEarlierBuildGoesToTheFirstAccountThatSignsIn() async throws {
        uploader.transport = { _, _ in throw URLError(.notConnectedToInternet) }
        uploader.open(sessionId: "s33", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s33", chunk: chunk(0))
        _ = await uploader.settle(sessionId: "s33")
        let manifest = root.appendingPathComponent("s33").appendingPathComponent("manifest.json")
        XCTAssertFalse(try String(contentsOf: manifest, encoding: .utf8).contains("account"))

        let first = ChunkUploader(root: root, useBackgroundSession: false)
        first.transport = { request, _ in (Data(), HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: nil, headerFields: nil)!) }
        first.resumePending(account: 7)
        _ = await first.settle(sessionId: "s33")

        var sent = 0
        let other = ChunkUploader(root: root, useBackgroundSession: false)
        other.transport = { request, _ in
            sent += 1
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        other.resumePending(account: 8)
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(sent, 0, "it is account 7's now")
        let owner = ChunkUploader(root: root, useBackgroundSession: false)
        owner.transport = other.transport
        owner.resumePending(account: 7)
        let outcome = await owner.settle(sessionId: "s33")
        XCTAssertEqual(sent, 1)
        XCTAssertEqual(outcome.stored, 1)
    }

    // #754: Stop could not send chunks 2 and 3; "Continue recording" claims the
    // session, whose server holds 0 and 1. The new chunks are numbered after
    // the ones still on the phone, those go first, and their files are the
    // audio that was recorded for them.
    func testAContinuedRecordingIsNumberedAfterThePiecesStillOnThePhone() async throws {
        var online = false
        var sent: [String] = []
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            if i >= 2 && !online { throw URLError(.notConnectedToInternet) }
            let body = try Data(contentsOf: file)
            let byte = (0...255).first { body.range(of: Data(repeating: UInt8($0), count: 32)) != nil } ?? -1
            sent.append("\(i):\(byte)")
            return self.answer(request, 202)
        }
        uploader.open(sessionId: "s40", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<4 { uploader.enqueue(sessionId: "s40", chunk: chunk(i)) }
        let stopped = await uploader.settle(sessionId: "s40")
        XCTAssertEqual(stopped.failed, [2, 3])
        XCTAssertEqual(files("s40"), ["chunk_2.body", "chunk_3.body", "manifest.json"])

        online = true
        sent = []
        let first = uploader.open(sessionId: "s40", uploadURL: URL(string: "http://x/c")!, firstIndex: 2)
        XCTAssertEqual(first, 4, "after the pieces still on the phone, not the server's 2")
        for i in first..<(first + 2) {
            uploader.enqueue(sessionId: "s40", chunk: RecordedChunk(index: i, data: Data(repeating: UInt8(0x40 + i), count: 32),
                                                                     duration: 15))
        }
        let outcome = await uploader.settle(sessionId: "s40")
        XCTAssertEqual(sent, ["2:2", "3:3", "4:68", "5:69"], "the queued pieces first, with their own audio")
        XCTAssertEqual(outcome.failed, [])
        XCTAssertEqual(outcome.totalForFinalize, 6)
    }

    // #754: another device recorded into the session meanwhile, so the server
    // is ahead of the phone: the new chunks follow the server's, and finalize
    // counts the chunks the phone never had.
    func testAContinuationAfterTheServerMovedOnCountsItsChunks() async throws {
        var online = false
        var sent: [Int] = []
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            if i >= 2 && !online { throw URLError(.notConnectedToInternet) }
            sent.append(i)
            return i == 2 || i == 3 ? self.answer(request, 200, #"{"message":"Chunk already uploaded"}"#)
                                    : self.answer(request, 202)
        }
        uploader.open(sessionId: "s41", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<4 { uploader.enqueue(sessionId: "s41", chunk: chunk(i)) }
        _ = await uploader.settle(sessionId: "s41")
        online = true
        let first = uploader.open(sessionId: "s41", uploadURL: URL(string: "http://x/c")!, firstIndex: 6)
        XCTAssertEqual(first, 6)
        for i in 6..<9 { uploader.enqueue(sessionId: "s41", chunk: chunk(i)) }
        let outcome = await uploader.settle(sessionId: "s41")
        XCTAssertEqual(sent.suffix(5), [2, 3, 6, 7, 8])
        XCTAssertEqual(outcome.failed, [])
        XCTAssertEqual(outcome.totalForFinalize, 9, "0-1 and 6-8 from this phone, 2-5 on the server")
    }

    // #754: a session whose queued pieces are on disk only (a relaunch that has
    // not resumed them yet) is read before it is continued, so they are kept.
    func testContinuingASessionKeptOnDiskKeepsItsQueuedPieces() async throws {
        uploader.transport = { [unowned self] request, file in
            if self.index(of: request, file: file) == 1 { throw URLError(.notConnectedToInternet) }
            return self.answer(request, 202)
        }
        uploader.open(sessionId: "s42", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s42", chunk: chunk(0))
        uploader.enqueue(sessionId: "s42", chunk: chunk(1))
        _ = await uploader.settle(sessionId: "s42")

        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.delay = { _ in 0.001 }
        var sent: [Int] = []
        relaunched.transport = { [unowned self] request, file in
            sent.append(self.index(of: request, file: file))
            return self.answer(request, 202)
        }
        let first = relaunched.open(sessionId: "s42", uploadURL: URL(string: "http://x/c")!, firstIndex: 1)
        XCTAssertEqual(first, 2)
        relaunched.enqueue(sessionId: "s42", chunk: chunk(2))
        let outcome = await relaunched.settle(sessionId: "s42")
        XCTAssertEqual(sent, [1, 2])
        XCTAssertEqual(outcome.totalForFinalize, 3)
    }

    // #754: a number already queued is never written again.
    func testAQueuedChunkIsNeverWrittenOver() async throws {
        uploader.transport = { _, _ in throw URLError(.notConnectedToInternet) }
        uploader.open(sessionId: "s43", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s43", chunk: chunk(1))
        _ = await uploader.settle(sessionId: "s43")
        uploader.enqueue(sessionId: "s43", chunk: RecordedChunk(index: 1, data: Data(repeating: 0xEE, count: 32), duration: 15))
        let body = try Data(contentsOf: root.appendingPathComponent("s43").appendingPathComponent("chunk_1.body"))
        XCTAssertNotNil(body.range(of: Data(repeating: 1, count: 32)), "the queued audio is still there")
        XCTAssertNil(body.range(of: Data(repeating: 0xEE, count: 32)))
        XCTAssertEqual(uploader.outcome("s43").produced, 1)
    }

    private let notFoundBody = #"{"error":"Streaming session not found","code":"session_not_found"}"#

    // Review of #765: the server has no such recording for the account signed
    // in (it was discarded on the web, or cleaned up). Its pieces leave the
    // phone and no longer count as unsent audio, nothing more is sent into it,
    // and a relaunch does not bring it back.
    func testARecordingTheServerNoLongerHasLeavesThePhone() async throws {
        uploader.resumePending(account: 7)
        var sent: [Int] = []
        uploader.transport = { [unowned self] request, file in
            let i = self.index(of: request, file: file)
            sent.append(i)
            return i == 0 ? self.answer(request, 202) : self.answer(request, 404, self.notFoundBody)
        }
        uploader.open(sessionId: "s34", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<3 { uploader.enqueue(sessionId: "s34", chunk: chunk(i)) }
        let outcome = await uploader.settle(sessionId: "s34")
        XCTAssertEqual(sent, [0, 1], "not retried, and nothing more is sent into it")
        XCTAssertEqual(outcome.fatalMessage, ChunkUploader.sessionNotFoundMessage)
        XCTAssertEqual(files("s34"), ["manifest.json"])
        XCTAssertFalse(uploader.holdsUnsentAudio(account: 7))

        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.transport = { [unowned self] request, file in
            sent.append(self.index(of: request, file: file))
            return self.answer(request, 202)
        }
        XCTAssertEqual(relaunched.resumePending(account: 7), [])
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(sent, [0, 1])
        XCTAssertEqual(files("s34"), [])

        // A 404 without the code (not the server's answer about the recording) deletes nothing.
        uploader.transport = { [unowned self] request, _ in self.answer(request, 404, "<html>Not Found</html>") }
        uploader.open(sessionId: "s37", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s37", chunk: chunk(0))
        let plain = await uploader.settle(sessionId: "s37")
        XCTAssertNil(plain.fatalMessage)
        XCTAssertEqual(files("s37"), ["chunk_0.body", "manifest.json"])
    }

    // ...but only for a recording made under the account signed in now, while
    // it still is. A queue an earlier build left may be another account's; a
    // recording of another account is never sent, so it gets no answer; an
    // answer that comes after a sign-out the server caused deletes nothing.
    func testANotFoundAnswerDeletesNothingUnlessTheRecordingsAccountIsSignedIn() async throws {
        // 1. A queue from an earlier build, taken by account 8.
        uploader.transport = { _, _ in throw URLError(.notConnectedToInternet) }
        uploader.open(sessionId: "s35", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s35", chunk: chunk(0))
        _ = await uploader.settle(sessionId: "s35")
        let adopter = ChunkUploader(root: root, useBackgroundSession: false)
        adopter.delay = { _ in 0.001 }
        var answered = 0
        adopter.transport = { [unowned self] request, _ in
            answered += 1
            return self.answer(request, 404, self.notFoundBody)
        }
        adopter.resumePending(account: 8)
        let adopted = await adopter.settle(sessionId: "s35")
        XCTAssertGreaterThan(answered, 0)
        XCTAssertNil(adopted.fatalMessage)
        XCTAssertEqual(files("s35"), ["chunk_0.body", "manifest.json"], "kept: it may be another account's")
        uploader.forget(sessionId: "s35")

        // 2. Account 7's recording while account 8 is signed in: never sent.
        uploader.resumePending(account: 7)
        uploader.open(sessionId: "s36", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s36", chunk: chunk(0))
        _ = await uploader.settle(sessionId: "s36")
        uploader.hold()
        var sentUnderEight = 0
        uploader.transport = { [unowned self] request, _ in
            sentUnderEight += 1
            return self.answer(request, 404, self.notFoundBody)
        }
        uploader.resumePending(account: 8)
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(sentUnderEight, 0)
        XCTAssertEqual(files("s36"), ["chunk_0.body", "manifest.json"])

        // 3. Account 7 again; its request is in flight when the server refuses
        // the sign-in and the app holds the queue; then the answer comes.
        uploader.hold()
        var inFlight = false
        uploader.transport = { [unowned self] request, _ in
            inFlight = true
            await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { done.resume() }
            }
            return self.answer(request, 404, self.notFoundBody)
        }
        uploader.resumePending(account: 7)
        try await waitUntil { inFlight }
        uploader.hold()
        try await Task.sleep(nanoseconds: 250_000_000)
        XCTAssertEqual(files("s36"), ["chunk_0.body", "manifest.json"])
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 7))
    }

    // MARK: The recorder's numbering (#754)

    /// A tone file the Debug file source reads in real time.
    private func toneFile(seconds: Double) throws -> URL {
        let url = root.appendingPathComponent("tone-\(UUID().uuidString).caf")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let format = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 1)!
        let frames = AVAudioFrameCount(seconds * 44_100)
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
        buffer.frameLength = frames
        for i in 0..<Int(frames) {
            buffer.floatChannelData![0][i] = Float(sin(Double(i) * 2 * .pi * 440 / 44_100) * 0.2)
        }
        do {
            let file = try AVAudioFile(forWriting: url, settings: format.settings)
            try file.write(from: buffer)
        }
        return url
    }

    /// The first-run piece of a chunk body: the chunk's own byte when it is
    /// one of `chunk(i)`'s, else only its number.
    private func label(_ request: URLRequest, _ file: URL) -> String {
        let i = index(of: request, file: file)
        let body = (try? Data(contentsOf: file)) ?? Data()
        let own = body.range(of: Data(repeating: UInt8(truncatingIfNeeded: i), count: 32)) != nil
        return own ? "\(i):own" : "\(i)"
    }

    private func makeRecorder(_ file: URL, ended: @escaping () -> Void,
                          observed: @escaping (RecordedChunk) -> Void) -> VoiceRecorder {
        let recorder = VoiceRecorder(uploader: uploader, debugFile: file,
                                     recordingLog: RecordingLog(directory: root.appendingPathComponent("log")))
        recorder.onSourceEnded = ended
        recorder.chunkObserver = observed
        return recorder
    }

    // #754 through the real recorder: "Continue recording" holds its pieces
    // until the claim answers 2 (the server holds 0 and 1); this phone still
    // holds 2 and 3. The recorder's first piece becomes 4 (`releaseHeldChunks`),
    // and 2 and 3 go first with their own audio.
    func testTheRecorderNumbersAContinuationPastThePiecesStillOnThePhone() async throws {
        var online = false
        var sent: [String] = []
        uploader.transport = { [unowned self] request, file in
            if self.index(of: request, file: file) >= 2 && !online { throw URLError(.notConnectedToInternet) }
            sent.append(self.label(request, file))
            return self.answer(request, 202)
        }
        uploader.open(sessionId: "s60", uploadURL: URL(string: "http://x/c")!)
        for i in 0..<4 { uploader.enqueue(sessionId: "s60", chunk: chunk(i)) }
        let stopped = await uploader.settle(sessionId: "s60")
        XCTAssertEqual(stopped.failed, [2, 3])
        online = true
        sent = []

        var ended = false
        var observed: [RecordedChunk] = []
        let recorder = makeRecorder(try toneFile(seconds: 1), ended: { ended = true }, observed: { observed.append($0) })
        try recorder.startHoldingChunks(sessionId: "s60", uploadURL: URL(string: "http://x/c")!,
                                        firstChunkIndex: 2, elapsedOffset: 30)
        try await waitUntil(timeout: 15) { ended }
        recorder.pause()
        XCTAssertEqual(sent, [], "held until the claim answers")
        recorder.releaseHeldChunks(firstChunkIndex: 2)
        let outcome = await recorder.stop()

        XCTAssertEqual(observed.first?.index, 4)
        XCTAssertTrue(observed.first.map { MP4Boxes.startsWithFtyp($0.data) } ?? false, "a new stream's first piece")
        XCTAssertEqual(Array(sent.prefix(3)), ["2:own", "3:own", "4"])
        XCTAssertEqual(outcome.failed, [])
        XCTAssertEqual(outcome.totalForFinalize, (observed.map(\.index).max() ?? 0) + 1)
    }

    // The same for a recorder that starts without holding (`begin`): a
    // session of which this phone still holds 0 and 1 is numbered from 2.
    func testTheRecorderStartsPastThePiecesStillOnThePhone() async throws {
        var online = false
        var sent: [String] = []
        uploader.transport = { [unowned self] request, file in
            if !online { throw URLError(.notConnectedToInternet) }
            sent.append(self.label(request, file))
            return self.answer(request, 202)
        }
        uploader.open(sessionId: "s61", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s61", chunk: chunk(0))
        uploader.enqueue(sessionId: "s61", chunk: chunk(1))
        let stopped = await uploader.settle(sessionId: "s61")
        XCTAssertEqual(stopped.failed, [0, 1])
        online = true

        var ended = false
        var observed: [RecordedChunk] = []
        let recorder = makeRecorder(try toneFile(seconds: 1), ended: { ended = true }, observed: { observed.append($0) })
        try recorder.start(sessionId: "s61", uploadURL: URL(string: "http://x/c")!, firstChunkIndex: 0, elapsedOffset: 0)
        try await waitUntil(timeout: 15) { ended }
        let outcome = await recorder.stop()

        XCTAssertEqual(observed.first?.index, 2)
        XCTAssertEqual(Array(sent.prefix(3)), ["0:own", "1:own", "2"])
        XCTAssertEqual(outcome.failed, [])
        XCTAssertEqual(outcome.totalForFinalize, (observed.map(\.index).max() ?? 0) + 1)
    }

    func testDuplicateAnswerCountsAsStored() async throws {
        uploader.transport = { request, _ in
            (Data(#"{"message":"Chunk already uploaded"}"#.utf8),
             HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: "s5", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s5", chunk: chunk(0))
        let outcome = await uploader.settle(sessionId: "s5")
        XCTAssertEqual(outcome.stored, 1)
    }

    /// A session whose recorder was running when "the app was killed": chunk 0
    /// stored, the manifest left open on disk.
    private func leaveRecordingSession(_ sid: String) async throws {
        uploader.transport = { request, _ in
            (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        uploader.open(sessionId: sid, uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: sid, chunk: chunk(0))
        for _ in 0..<200 where uploader.outcome(sid).stored < 1 {
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTAssertEqual(uploader.outcome(sid).stored, 1)
    }

    private func relaunch(at offset: TimeInterval = 0) -> ChunkUploader {
        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.now = { Date().addingTimeInterval(offset) }
        return relaunched
    }

    // #423 walk test: the app crashed mid-recording and relaunched inside the
    // server's window. Its session is released (nothing records into it any
    // more), once; a recording that was stopped is not.
    func testRelaunchInsideTheWindowReleasesTheRecordingTheKilledAppLeft() async throws {
        try await leaveRecordingSession("s10")
        uploader.open(sessionId: "s11", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s11", chunk: chunk(0))
        _ = await uploader.settle(sessionId: "s11")
        XCTAssertEqual(relaunch().resumePending(releaseMargin: AudioCenter.releaseTimeout), ["s10"])
        XCTAssertEqual(relaunch().resumePending(releaseMargin: AudioCenter.releaseTimeout), [], "released once")
    }

    // Review of #424: after the window another device may have continued the
    // session; releasing it then would let the phone discard or clash with it.
    func testRelaunchAfterTheWindowDoesNotRelease() async throws {
        try await leaveRecordingSession("s13")
        let later = relaunch(at: ChunkUploader.serverLiveWindow - AudioCenter.releaseTimeout)
        XCTAssertEqual(later.resumePending(releaseMargin: AudioCenter.releaseTimeout), [])
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("s13").path),
                       "nothing left to upload: the session's files go either way")
    }

    // A manifest from an earlier build has no upload record: no release.
    func testRelaunchWithNoUploadRecordDoesNotRelease() async throws {
        try await leaveRecordingSession("s14")
        let file = root.appendingPathComponent("s14").appendingPathComponent("manifest.json")
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        XCTAssertNotNil(json.removeValue(forKey: "lastUploadAt"))
        try JSONSerialization.data(withJSONObject: json).write(to: file)
        XCTAssertEqual(relaunch().resumePending(releaseMargin: AudioCenter.releaseTimeout), [])
    }

    func testMayReleaseOnlyInsideTheWindowLessTheMargin() {
        let stored = Date()
        let window = ChunkUploader.serverLiveWindow
        func may(after age: TimeInterval, margin: TimeInterval = 5) -> Bool {
            ChunkUploader.mayRelease(lastUploadAt: stored, now: stored.addingTimeInterval(age), margin: margin)
        }
        XCTAssertTrue(may(after: 0))
        XCTAssertTrue(may(after: window - 5 - 0.5))
        XCTAssertFalse(may(after: window - 5))
        XCTAssertFalse(may(after: window + 60))
        XCTAssertTrue(may(after: window - 0.5, margin: 0))
        XCTAssertFalse(may(after: -1), "a clock that went back")
        XCTAssertFalse(ChunkUploader.mayRelease(lastUploadAt: nil, now: stored, margin: 0), "no record")
    }

    // Its chunks still uploading: not reported. Recovered before they land, the
    // continuation would reuse their indexes.
    func testRelaunchDoesNotReportARecordingWithChunksStillToUpload() async throws {
        uploader.transport = { _, _ in
            try await Task.sleep(nanoseconds: 30_000_000_000)
            throw URLError(.timedOut)
        }
        uploader.open(sessionId: "s12", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s12", chunk: chunk(0))
        try await Task.sleep(nanoseconds: 20_000_000)
        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.delay = { _ in 0.001 }
        relaunched.transport = { request, _ in
            (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        XCTAssertEqual(relaunched.resumePending(), [])
        let outcome = await relaunched.settle(sessionId: "s12")
        XCTAssertEqual(outcome.stored, 1)
        uploader.forget(sessionId: "s12")
    }

    func testRelaunchResumesPendingChunks() async throws {
        // The first upload never answers (the app is killed mid-request).
        uploader.transport = { _, _ in
            try await Task.sleep(nanoseconds: 30_000_000_000)
            throw URLError(.timedOut)
        }
        uploader.open(sessionId: "s6", uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: "s6", chunk: chunk(0))
        try await Task.sleep(nanoseconds: 20_000_000)
        // The "app is killed": a new uploader over the same directory.
        let relaunched = ChunkUploader(root: root, useBackgroundSession: false)
        relaunched.delay = { _ in 0.001 }
        var sentIndexes: [Int] = []
        relaunched.transport = { [unowned self] request, file in
            sentIndexes.append(self.index(of: request, file: file))
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        relaunched.resumePending()
        let outcome = await relaunched.settle(sessionId: "s6")
        XCTAssertEqual(sentIndexes, [0])
        XCTAssertEqual(outcome.stored, 1)
        uploader.forget(sessionId: "s6")
    }
}

import MediaPlayer

@MainActor
final class NowPlayingTests: XCTestCase {
    func testLockScreenCommandsFollowThePhase() {
        let controller = NowPlayingController()
        let center = MPRemoteCommandCenter.shared()
        var calls: [String] = []
        controller.handlers = .init(play: { calls.append("play") }, pause: { calls.append("pause") },
                                    next: { calls.append("next") })

        controller.update(.recording(elapsed: 83, paused: false))
        XCTAssertEqual(MPNowPlayingInfoCenter.default().nowPlayingInfo?[MPMediaItemPropertyTitle] as? String, "Recording 1:23")
        XCTAssertTrue(center.nextTrackCommand.isEnabled && center.pauseCommand.isEnabled && center.playCommand.isEnabled)
        XCTAssertFalse(center.skipForwardCommand.isEnabled)

        controller.update(.thinking(title: "Voice…"))
        XCTAssertEqual(MPNowPlayingInfoCenter.default().nowPlayingInfo?[MPMediaItemPropertyTitle] as? String, "Voice…")
        XCTAssertFalse(center.nextTrackCommand.isEnabled, "no lock-screen cancel while thinking (#397)")
        XCTAssertFalse(center.playCommand.isEnabled || center.pauseCommand.isEnabled || center.skipForwardCommand.isEnabled)

        controller.update(.playback(title: "Voice", elapsed: 3, duration: 20, rate: 1.5, playing: true))
        let info = MPNowPlayingInfoCenter.default().nowPlayingInfo
        XCTAssertEqual(info?[MPMediaItemPropertyPlaybackDuration] as? Double, 20)
        XCTAssertEqual(info?[MPNowPlayingInfoPropertyPlaybackRate] as? Double, 1.5)
        XCTAssertTrue(center.skipForwardCommand.isEnabled && center.changePlaybackRateCommand.isEnabled)
        XCTAssertFalse(center.nextTrackCommand.isEnabled)
        XCTAssertEqual(center.skipBackwardCommand.preferredIntervals, [10])

        controller.update(.none)
        XCTAssertNil(MPNowPlayingInfoCenter.default().nowPlayingInfo)
        XCTAssertFalse(center.playCommand.isEnabled)
    }
}

// M14: unplayable audio (WebM, 404, network) is reported and skipped, not silent.
@MainActor
final class PlaybackFailureTests: XCTestCase {
    func testWebMIsRecognisedAndMessagesFollowTheWeb() {
        XCTAssertTrue(ListenFormats.isWebM("/media/nodes/5/9/chunk_0001.webm"))
        XCTAssertTrue(ListenFormats.isWebM("/media/nodes/5/9/original.WEBM.enc?v=2"))
        XCTAssertFalse(ListenFormats.isWebM("/media/nodes/5/9/batch_0-19.mp4"))
        XCTAssertEqual(ChunkQueuePlayer.playbackErrorMessage(url: URL(string: "http://x/media/a.webm"), error: nil),
                       ListenFormats.webMMessage)
        XCTAssertEqual(ChunkQueuePlayer.playbackErrorMessage(url: URL(string: "http://x/media/a.mp3"),
                                                             error: URLError(.notConnectedToInternet)),
                       "Network error loading audio. Try again.")
        XCTAssertEqual(ChunkQueuePlayer.playbackErrorMessage(url: URL(string: "http://x/media/a.mp3"), error: nil),
                       "Audio playback failed.")
    }

    func testAnUnplayableItemIsReportedOnceAndTheQueueFinishes() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("playback-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let a = dir.appendingPathComponent("chunk_0000.webm")
        let b = dir.appendingPathComponent("chunk_0001.webm")
        try Data(repeating: 0x42, count: 4096).write(to: a)
        try Data(repeating: 0x42, count: 4096).write(to: b)

        let player = ChunkQueuePlayer()
        var errors: [String] = []
        let finished = expectation(description: "the queue skips unplayable chunks and ends (a voice turn reaches done)")
        player.onPlaybackError = { errors.append($0) }
        player.onFinished = { finished.fulfill() }
        player.load(urls: [a.absoluteString, b.absoluteString], durations: [15, 15], title: "Rec", source: .node(1))
        // Waits for the end itself. Under Xcode, CoreMedia's "FigFilePlayer
        // signalled err" log line goes to stderr from the player's queue before
        // the item fails; on CI that stderr stalled and the end came over 5 s
        // late. The timeout only stops a queue that never ends.
        await fulfillment(of: [finished], timeout: 60)
        XCTAssertEqual(errors, [ListenFormats.webMMessage], "one toast per queue")
        XCTAssertFalse(player.isPlaying)
    }
}

// M2: what a killed app left in tmp/ is swept at launch and sign-out.
final class PrivateFilesTests: XCTestCase {
    func testSweepRemovesOnlyTheAppsTemporaryFiles() throws {
        let fm = FileManager.default
        let dir = fm.temporaryDirectory.appendingPathComponent("sweep-test-\(UUID().uuidString)")
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: dir) }
        let ours = ["loore-dictation.m4a", "loore-export-2026-10-01.txt", "import-\(UUID().uuidString)",
                    UUID().uuidString, "CFNetworkDownload_abc.tmp", "org.loore.app-Inbox"]
        for name in ours + ["keep.txt"] {
            try Data("x".utf8).write(to: dir.appendingPathComponent(name))
        }
        PrivateFiles.sweepTemporary(in: dir)
        XCTAssertEqual(try fm.contentsOfDirectory(atPath: dir.path), ["keep.txt"])
    }

    func testRemovingADownloadAlsoRemovesItsFolder() throws {
        let fm = FileManager.default
        let folder = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("node-1-tts.mp3")
        try Data("x".utf8).write(to: file)
        PrivateFiles.remove(file)
        XCTAssertFalse(fm.fileExists(atPath: folder.path))
    }
}

import SwiftUI

// #763, through the app: which sign-outs keep recorded audio that has not
// reached the server, and which delete it (after saying so).
@MainActor
final class SignOutUnsentAudioTests: StubbedAppTestCase {
    private var root: URL!
    private var uploader: ChunkUploader!

    override func setUp() async throws {
        try await super.setUp()
        root = FileManager.default.temporaryDirectory.appendingPathComponent("signout-\(UUID().uuidString)")
        uploader = ChunkUploader(root: root, useBackgroundSession: false)
        uploader.delay = { _ in 0.001 }
        app.audio.uploader = uploader
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: root)
        try await super.tearDown()
    }

    private func status(_ code: Int) -> (URLRequest, URL) async throws -> (Data, HTTPURLResponse) {
        { request, _ in (Data(), HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: nil)!) }
    }

    /// A recording whose only piece the server refused with a 401.
    private func recordWhileTheSignInIsRefused(_ sessionId: String) async {
        uploader.transport = status(401)
        uploader.open(sessionId: sessionId, uploadURL: URL(string: "http://x/c")!)
        uploader.enqueue(sessionId: sessionId, chunk: RecordedChunk(index: 0, data: Data(repeating: 1, count: 32), duration: 15))
        _ = await uploader.settle(sessionId: sessionId)
    }

    private func signInAgain() throws {
        app.useForTesting(api: app.api, user: try decode(CurrentUser.self, Self.userJSON))
        app.audio.didSignIn(account: 5)
    }

    func testA401KeepsTheAudioUntilTheSameAccountSignsInAgain() async throws {
        app.audio.didSignIn(account: 5)
        await recordWhileTheSignInIsRefused("s50")
        XCTAssertTrue(app.signOutDeletesUnsentAudio)

        // Any request answered 401 signs the app out on its own (`handle(.unauthorized)`).
        await app.signOutLocally()
        XCTAssertEqual(app.phase, .signedOut)
        XCTAssertTrue(uploader.isHeld)
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 5), "kept on this phone")

        // The same account signs in again: the piece goes.
        var sent = 0
        uploader.transport = { request, _ in
            sent += 1
            return (Data(), HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!)
        }
        try signInAgain()
        let outcome = await uploader.settle(sessionId: "s50")
        XCTAssertEqual(sent, 1)
        XCTAssertEqual(outcome.stored, 1)
        XCTAssertFalse(app.signOutDeletesUnsentAudio)
    }

    func testA401ToWhoAmIAndLeavingTheRestoreQuestionKeepTheAudioToo() async throws {
        app.audio.didSignIn(account: 5)
        await recordWhileTheSignInIsRefused("s53")
        StubURLProtocol.install { _ in .json(401, #"{"error":"Unauthorized"}"#) }
        await app.loadUser()
        XCTAssertEqual(app.phase, .signedOut)
        XCTAssertTrue(uploader.isHeld)
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 5))

        await app.leaveAccountDeletionScreen()
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 5))
    }

    func testLogoutSaysTheAudioIsDeletedAndDeletesIt() async throws {
        app.audio.didSignIn(account: 5)
        // Nothing waiting: a sign-out button signs out at once.
        var asked = false
        let question = Binding(get: { asked }, set: { asked = $0 })
        StubURLProtocol.install { _ in .json(200, "{}") }
        await app.requestSignOut(question: question)?.value
        XCTAssertEqual(app.phase, .signedOut)
        XCTAssertFalse(asked)

        try signInAgain()
        await recordWhileTheSignInIsRefused("s51")
        app.requestSignOut(question: question)
        XCTAssertTrue(asked, "the screen asks first")
        XCTAssertEqual(app.phase, .signedIn, "and nothing happens before the answer")
        XCTAssertTrue(uploader.holdsUnsentAudio(account: 5))

        await app.signOut()
        XCTAssertEqual(app.phase, .signedOut)
        XCTAssertFalse(uploader.holdsUnsentAudio(account: nil), "confirmed: deleted")
        // Where the account is not known, the question says every account's audio goes.
        XCTAssertTrue(SignOutQuestion.everyAccount.message.contains("deletes all of it from this phone, for every account"))
    }

    func testDeletingTheAccountDeletesItsAudio() async throws {
        app.audio.didSignIn(account: 5)
        await recordWhileTheSignInIsRefused("s52")
        await app.accountDeletionScheduled(deleteOn: nil)
        XCTAssertFalse(uploader.holdsUnsentAudio(account: nil))
    }
}
