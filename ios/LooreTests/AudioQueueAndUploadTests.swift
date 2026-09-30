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
        XCTAssertEqual(attempts[1], 5, "4 retries = 5 attempts (web uploadChunkWithRetry)")
        XCTAssertEqual(attempts[0], 1)
        XCTAssertEqual(outcome.failed, [1])
        XCTAssertEqual(outcome.stored, 2)
        XCTAssertEqual(outcome.totalForFinalize, 2)
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
