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
        uploader.reset()
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
        var finished = false
        player.onPlaybackError = { errors.append($0) }
        player.onFinished = { finished = true }
        player.load(urls: [a.absoluteString, b.absoluteString], durations: [15, 15], title: "Rec", source: .node(1))
        let deadline = Date().addingTimeInterval(5)
        while !finished && Date() < deadline {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertTrue(finished, "the queue skips unplayable chunks and ends (a voice turn reaches done)")
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
