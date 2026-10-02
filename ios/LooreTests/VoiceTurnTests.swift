import XCTest
@testable import Loore

// The voice turn state machine against fakes (design doc §12): no network,
// no microphone, no audio output.

@MainActor
final class FakeVoiceBackend: VoiceBackend {
    var log: [String] = []
    var startResult: Result<StreamingInitResponse, Error> = .success(
        StreamingInitResponse(sessionId: "sid-1", draftId: 7, sseURL: nil))
    var finalizeError: Error?
    var statuses: [StreamingSessionStatus] = []
    var llmStatuses: [Int: [LLMStatus]] = [:]
    var ttsTrigger: [Int: TTSTriggerOutcome] = [:]
    var ttsTriggerError: [Int: Error] = [:]
    var ttsStatuses: [Int: TTSStatus] = [:]
    var streams: [Int: AsyncThrowingStream<SSEMessage, Error>.Continuation] = [:]
    var streamRequests: [(Int, Int?)] = []
    var timingPosts: [(Int, [String: Double])] = []
    var legacyResult: VoiceSessionResponse?
    var legacyError: Error?

    func startSession(parentId: Int?, aiUsage: String) async throws -> StreamingInitResponse {
        log.append("init parent=\(parentId.map(String.init) ?? "nil") ai=\(aiUsage)")
        return try startResult.get()
    }

    func uploadURL(sessionId: String) -> URL { URL(string: "http://test/\(sessionId)/audio-chunk")! }

    func finalize(sessionId: String, totalChunks: Int, parentId: Int?, model: String?) async throws {
        log.append("finalize total=\(totalChunks) parent=\(parentId.map(String.init) ?? "nil") model=\(model ?? "nil")")
        if let finalizeError { throw finalizeError }
    }

    func sessionStatus(sessionId: String) async throws -> StreamingSessionStatus {
        log.append("status")
        return statuses.count > 1 ? statuses.removeFirst() : statuses[0]
    }

    func legacyVoice(content: String, model: String?, aiUsage: String?, parentId: Int?,
                     sessionId: String?) async throws -> VoiceSessionResponse {
        log.append("legacy")
        if let legacyError { throw legacyError }
        guard let legacyResult else { throw APIError.server(status: 500, body: nil) }
        return legacyResult
    }

    func llmStatus(nodeId: Int) async throws -> LLMStatus {
        guard var list = llmStatuses[nodeId], !list.isEmpty else { throw APIError.transport(code: -1, description: "none") }
        let next = list.count > 1 ? list.removeFirst() : list[0]
        llmStatuses[nodeId] = list
        return next
    }

    func requestTTS(nodeId: Int) async throws -> TTSTriggerOutcome {
        log.append("tts \(nodeId)")
        if let error = ttsTriggerError[nodeId] { throw error }
        return ttsTrigger[nodeId] ?? .started
    }

    func ttsStatus(nodeId: Int) async throws -> TTSStatus {
        guard let s = ttsStatuses[nodeId] else { throw APIError.transport(code: -1, description: "none") }
        return s
    }

    func ttsStream(nodeId: Int, lastChunk: Int?) -> AsyncThrowingStream<SSEMessage, Error> {
        streamRequests.append((nodeId, lastChunk))
        log.append("attach \(nodeId)")
        return AsyncThrowingStream { continuation in
            self.streams[nodeId] = continuation
        }
    }

    func discard(sessionId: String) async { log.append("discard") }
    func timingClock() async throws -> Double { Date().timeIntervalSince1970 }
    func postTiming(nodeId: Int, marks: [String: Double], offsetMs: Double, rttMs: Double) async {
        timingPosts.append((nodeId, marks))
    }

    // Helpers
    func push(_ nodeId: Int, _ event: String, _ json: String) {
        streams[nodeId]?.yield(.event(SSEEvent(event: event, data: json)))
    }
}

@MainActor
final class FakeRecorder: VoiceRecording {
    var elapsed: Double = 0
    var onSourceEnded: (() -> Void)?
    var onFatal: ((String) -> Void)?
    var onSourceFailed: (() -> Void)?
    var outcome = ChunkUploader.Outcome(produced: 2, stored: 2, failed: [], fatalMessage: nil)
    var startError: Error?
    weak var audio: FakeAudio?
    var calls: [String] = []

    func start(sessionId: String, uploadURL: URL, firstChunkIndex: Int, elapsedOffset: Double) throws {
        calls.append("start \(sessionId) from \(firstChunkIndex)")
        if let startError { throw startError }
        elapsed = elapsedOffset + 3
    }
    var resumeError: Error?
    func pause() { calls.append("pause") }
    func resume() throws {
        calls.append("resume")
        if let resumeError { throw resumeError }
    }
    func interrupt() { calls.append("interrupt") }
    /// Seconds `stop()` takes (the last uploads).
    var stopDelay: Double = 0
    func stop() async -> ChunkUploader.Outcome {
        calls.append("stop")
        audio?.events.append("recorder.stop")
        if stopDelay > 0 { try? await Task.sleep(nanoseconds: UInt64(stopDelay * 1_000_000_000)) }
        return outcome
    }
    func cancel() { calls.append("cancel") }
    func forget(sessionId: String) { calls.append("forget") }
}

@MainActor
final class FakeQueue: VoiceQueue {
    var urls: [String] = []
    var chapters: [(Int, String)] = []
    var generatingTTS = false
    var isPlaying = true
    var waitingForChunks = false
    var stopped = 0
    var onPlaying: (() -> Void)?
    var hasAudio: Bool { !urls.isEmpty }
    var entryCount: Int { urls.count }

    func loadFirst(url: String, duration: Double?, chapterTitle: String?, onPlaying: @escaping () -> Void) {
        urls = [url]
        chapters = chapterTitle.map { [(0, $0)] } ?? []
        self.onPlaying = onPlaying
    }
    func append(url: String, duration: Double?, chapterTitle: String?) -> Bool {
        guard !urls.contains(url) else { return false }
        urls.append(url)
        if let chapterTitle { chapters.append((urls.count - 1, chapterTitle)) }
        return true
    }
    func renameChapter(atChunk index: Int, to title: String) {
        if let i = chapters.firstIndex(where: { $0.0 == index }) { chapters[i].1 = title }
    }
    func stop() { stopped += 1 }
    func close() { urls = [] }
}

@MainActor
final class FakeAudio: VoiceAudio {
    let fakeQueue = FakeQueue()
    var queue: VoiceQueue { fakeQueue }
    var events: [String] = []
    var cueOn = false

    func activateForRecording() throws { events.append("activate.record") }
    func activateForReply() { events.append("activate.reply") }
    func reactivate() throws { events.append("reactivate") }
    func switchToPlayback() { events.append("category.playback") }
    func deactivate() { events.append("deactivate") }
    func startCue() { if !cueOn { events.append("cue.start") }; cueOn = true }
    func stopCue() { if cueOn { events.append("cue.stop") }; cueOn = false }
    func playErrorSound() { events.append("sound.error") }
    func playInterruptionAlert() { events.append("sound.interruption") }
    func playLongRecordingWarning() { events.append("sound.59") }
    func refreshNowPlaying() {}
    func voiceConversationEnded() { events.append("conversation.ended") }
}

@MainActor
final class FakeNotices: VoiceNotices {
    var toasts: [String] = []
    var notified: [LocalNotice] = []
    var capped = 0
    func toast(_ text: String, duration: TimeInterval) -> Int { toasts.append(text); return toasts.count }
    func dismissToast(_ id: Int) {}
    func notify(_ notice: LocalNotice) { notified.append(notice) }
    func withdraw(_ notice: LocalNotice) { withdrawn.append(notice) }
    var withdrawn: [LocalNotice] = []
    func spendCapped() { capped += 1 }
}

@MainActor
final class VoiceTurnTests: XCTestCase {
    var backend: FakeVoiceBackend!
    var recorder: FakeRecorder!
    var audio: FakeAudio!
    var notices: FakeNotices!
    var turn: VoiceTurnController!

    override func setUp() async throws {
        backend = FakeVoiceBackend()
        recorder = FakeRecorder()
        audio = FakeAudio()
        recorder.audio = audio
        notices = FakeNotices()
        turn = VoiceTurnController(backend: backend, recorder: recorder, audio: audio, notices: notices, parentId: 50)
        turn.model = { "gpt-6-luna" }
        turn.aiUsage = { "chat" }
        turn.timings = .init(statusPoll: 0.01, statusGiveUp: 5, llmPoll: 0.01, llmGiveUp: 5, reconcile: 0.05,
                             triggerWatchdog: 0.2, catchUpGrace: 0.05, errorDot: 0.05, tick: 0.01,
                             longRecording: 59 * 60)
    }

    private func wait(_ what: String = "", timeout: Double = 2, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTAssertTrue(condition(), "timed out waiting: \(what)")
    }

    private func status(_ s: String, llm: Int? = nil, content: String = "hello there", warning: String? = nil) throws -> StreamingSessionStatus {
        var json = #"{"session_id":"sid-1","streaming_status":"\#(s)","content":"\#(content)","chunks":[]"#
        if let llm { json += #","llm_node_id":\#(llm)"# }
        if let warning { json += #","warning":"\#(warning)""# }
        return try decode(StreamingSessionStatus.self, json + "}")
    }

    private func llm(_ id: Int, _ status: String, tts: String? = "pending", streaming: Bool = false,
                     content: String? = nil, continuation: Int? = nil, error: String? = nil) throws -> LLMStatus {
        var json = #"{"node_id":\#(id),"status":"\#(status)","tts_streaming":\#(streaming),"warnings":[]"#
        if let tts { json += #","tts_task_status":"\#(tts)""# }
        if let content { json += #","content":"\#(content)""# }
        if let continuation { json += #","continuation_node_id":\#(continuation)"# }
        if let error { json += #","error":"\#(error)""# }
        return try decode(LLMStatus.self, json + "}")
    }

    private func recordAndStop() async {
        turn.start()
        await wait("recording") { turn.state == .recording }
        turn.stop()
    }

    // MARK: Happy path

    func testFullTurnFromRecordToPlaybackToDone() async throws {
        backend.statuses = [try status("finalizing"), try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing"),
                                    try llm(101, "completed", tts: "processing", content: "## Rivers\\nThe ice moves.")]
        XCTAssertEqual(turn.phase, .ready)

        turn.start()
        XCTAssertEqual(turn.phase, .recording)
        await wait("recording") { turn.state == .recording }
        XCTAssertEqual(backend.log.first, "init parent=50 ai=chat")
        XCTAssertEqual(recorder.calls, ["start sid-1 from 0"])

        turn.stop()
        XCTAssertTrue(turn.isStopping)
        await wait("attached") { backend.streams[101] != nil }
        // The cue started before the recorder stopped, and the session switched to playback.
        let cue = try XCTUnwrap(audio.events.firstIndex(of: "cue.start"))
        let stop = try XCTUnwrap(audio.events.firstIndex(of: "recorder.stop"))
        XCTAssertLessThan(cue, stop)
        XCTAssertTrue(audio.events.contains("category.playback"))
        XCTAssertTrue(backend.log.contains("finalize total=2 parent=50 model=gpt-6-luna"))
        XCTAssertEqual(turn.phase, .processing)
        XCTAssertEqual(turn.state, .awaitingAudio)
        XCTAssertTrue(recorder.calls.contains("forget"))

        await wait("completed") { turn.lastReplyNodeId == 101 }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/a0.mp3","duration":4.2}"#)
        await wait("playing") { turn.state == .playing }
        XCTAssertEqual(turn.phase, .playback)
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/a0.mp3"])
        XCTAssertTrue(audio.fakeQueue.generatingTTS)
        XCTAssertEqual(audio.fakeQueue.chapters.first?.1, "Rivers The ice moves.")

        // Replayed chunk (reconnect) is ignored; the next one is appended.
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/a0.mp3","duration":4.2}"#)
        backend.push(101, "chunk_ready", #"{"chunk_index":1,"audio_url":"/media/a1.mp3","duration":3}"#)
        await wait("appended") { audio.fakeQueue.urls.count == 2 }

        // First audio playing: the cue stops and the timing marks go out.
        audio.fakeQueue.onPlaying?()
        turn.queueStartedPlaying()
        XCTAssertFalse(audio.cueOn)
        await wait("timing") { !backend.timingPosts.isEmpty }
        guard let marks = backend.timingPosts.first?.1 else { return }
        for stage in ["rec_stop", "finalize_acked", "llm_node_known", "tts_attach", "chunk_ready", "playing"] {
            XCTAssertNotNil(marks[stage], stage)
        }
        XCTAssertEqual(backend.timingPosts[0].0, 101)

        backend.push(101, "all_complete", #"{"tts_url":"/media/tts.mp3","continuation_node_id":null,"preview":"The ice"}"#)
        await wait("generation over") { !audio.fakeQueue.generatingTTS }
        XCTAssertEqual(turn.state, .playing)
        turn.queueFinished()
        XCTAssertEqual(turn.state, .done)
        XCTAssertEqual(turn.phase, .playback)
        XCTAssertEqual(turn.threadParentId, 101, "the next turn replies to this reply")
        XCTAssertTrue(audio.events.last == "deactivate")
    }

    func testContinueStartsANewTurnOnTheSameThread() async throws {
        try await runToDone()
        backend.startResult = .success(StreamingInitResponse(sessionId: "sid-2", draftId: 8, sseURL: nil))
        turn.continueConversation()
        await wait("recording") { turn.state == .recording }
        XCTAssertEqual(backend.log.filter { $0.hasPrefix("init") }.last, "init parent=101 ai=chat")
        XCTAssertGreaterThan(audio.fakeQueue.stopped, 0)
    }

    private func runToDone() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "completed", content: "Short.")]
        await recordAndStop()
        await wait("attached") { backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/b0.mp3","duration":2}"#)
        backend.push(101, "all_complete", #"{"continuation_node_id":null}"#)
        await wait("not generating") { turn.state == .playing && !audio.fakeQueue.generatingTTS }
        turn.queueFinished()
    }

    // MARK: Chains, drains, streaming placeholders

    func testChainAppendsTheContinuationToTheSameQueue() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing", tts: "processing", streaming: true)]
        backend.llmStatuses[102] = [try llm(102, "processing", tts: "processing", streaming: true)]
        await recordAndStop()
        await wait("attached 101") { backend.streams[101] != nil }

        // Spoken while written: placeholder chapter, renamed at all_complete.
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/i0.mp3","duration":2}"#)
        await wait("playing") { turn.state == .playing }
        XCTAssertEqual(audio.fakeQueue.chapters.first?.1, "…")
        backend.push(101, "all_complete", #"{"continuation_node_id":102,"preview":"On it, looking that up"}"#)
        await wait("advanced") { backend.streams[102] != nil }
        XCTAssertEqual(audio.fakeQueue.chapters.first?.1, "On it, looking that up")
        XCTAssertTrue(turn.awaitingNextNode)
        XCTAssertTrue(audio.fakeQueue.generatingTTS)

        // The interim audio ran out before the answer's audio: Thinking + cue.
        turn.queueDrained()
        XCTAssertEqual(turn.state, .draining)
        XCTAssertEqual(turn.phase, .processing)
        XCTAssertTrue(audio.cueOn)

        backend.push(102, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/c0.mp3","duration":5}"#)
        await wait("second node") { audio.fakeQueue.urls.count == 2 }
        XCTAssertEqual(turn.phase, .playback)
        XCTAssertEqual(audio.fakeQueue.chapters.map(\.0), [0, 1])
        turn.queueStartedPlaying()
        XCTAssertFalse(audio.cueOn)
        backend.push(102, "all_complete", #"{"continuation_node_id":null,"preview":"The answer"}"#)
        await wait("done generating") { !audio.fakeQueue.generatingTTS }
        XCTAssertEqual(audio.fakeQueue.chapters.map(\.1), ["On it, looking that up", "The answer"])
    }

    func testDrainMidReplyKeepsThePlayerAndPlaysTheCue() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing", tts: "processing", streaming: true)]
        await recordAndStop()
        await wait("attached") { backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/d0.mp3","duration":2}"#)
        await wait("playing") { turn.state == .playing }
        turn.queueStartedPlaying()
        turn.queueDrained()
        XCTAssertEqual(turn.phase, .playback, "a drain inside one node keeps the player")
        XCTAssertTrue(audio.cueOn)
        backend.push(101, "chunk_ready", #"{"chunk_index":1,"audio_url":"/media/d1.mp3","duration":2}"#)
        await wait("resumed") { turn.state == .playing }
        turn.queueStartedPlaying()
        XCTAssertFalse(audio.cueOn)
    }

    // MARK: Stream answers JSON; REST delivery

    func testStreamJSONAnswerDeliversTheWholeFile() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing")]
        await recordAndStop()
        await wait("attached") { backend.streams[101] != nil }
        backend.streams[101]?.yield(.response(status: 200, body: Data(#"{"status":"completed","tts_url":"/media/full.mp3"}"#.utf8)))
        await wait("delivered") { audio.fakeQueue.urls == ["/media/full.mp3"] }
        XCTAssertEqual(turn.state, .playing)
        XCTAssertFalse(audio.fakeQueue.generatingTTS)
    }

    func testPostTTSReadyDeliversWhenTheStreamIsNotAttached() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "completed", tts: nil, content: "Done.")]
        backend.ttsTrigger[101] = .ready(url: "/media/ready.mp3")
        await recordAndStop()
        await wait("delivered") { audio.fakeQueue.urls == ["/media/ready.mp3"] }
        XCTAssertTrue(backend.log.contains("tts 101"))
        XCTAssertFalse(backend.log.contains("attach 101"))
    }

    // M9: POST /tts at completion must not duplicate or cut a reply whose stream is attached.

    func testStreamedReplySkipsPostTTS() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing", tts: "processing", streaming: true),
                                    try llm(101, "completed", tts: "processing", streaming: true, content: "Done.")]
        await recordAndStop()
        await wait("attached") { backend.streams[101] != nil }
        await wait("completed") { turn.lastReplyNodeId == 101 }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertFalse(backend.log.contains("tts 101"), "the web skips POST /tts for a streamed node")
    }

    func testPostTTSAnsweringReadyAfterStreamedChunksDoesNotDuplicateTheReply() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing", tts: "pending")]
        backend.ttsTrigger[101] = .ready(url: "/media/full.mp3")
        await recordAndStop()
        await wait("attached") { backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/e0.mp3","duration":2}"#)
        await wait("playing") { turn.state == .playing }
        // The TTS finished before the stream's all_complete reached the app.
        backend.llmStatuses[101] = [try llm(101, "completed", tts: "completed", content: "Done.")]
        await wait("triggered") { backend.log.contains("tts 101") }
        await wait("reattached") { backend.streamRequests.count == 2 }
        XCTAssertEqual(backend.streamRequests.last?.1, 0, "resumes after the last chunk it has")
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/e0.mp3"], "the whole file is not appended after the chunks")
        XCTAssertTrue(audio.fakeQueue.generatingTTS)
        backend.push(101, "chunk_ready", #"{"chunk_index":1,"audio_url":"/media/e1.mp3","duration":2}"#)
        backend.push(101, "all_complete", #"{"tts_url":"/media/full.mp3","continuation_node_id":null}"#)
        await wait("done generating") { !audio.fakeQueue.generatingTTS }
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/e0.mp3", "/media/e1.mp3"])
    }

    func testPostTTSErrorWhileTheStreamIsAttachedKeepsTheReply() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing", tts: "pending")]
        backend.ttsTriggerError[101] = APIError.spendCap(message: "capped")
        await recordAndStop()
        await wait("attached") { backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/f0.mp3","duration":2}"#)
        await wait("playing") { turn.state == .playing }
        backend.llmStatuses[101] = [try llm(101, "completed", tts: "processing", content: "Done.")]
        await wait("triggered") { backend.log.contains("tts 101") }
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(turn.state, .playing, "a 402 on POST /tts does not cut a reply that is streaming")
        XCTAssertFalse(turn.hasError)
        backend.push(101, "chunk_ready", #"{"chunk_index":1,"audio_url":"/media/f1.mp3","duration":2}"#)
        await wait("second chunk") { audio.fakeQueue.urls.count == 2 }
    }

    // M13: a first chunk that arrives long after completion (slow TTS) still plays.
    func testLateFirstChunkAfterCompletionStillPlays() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing", tts: "pending"),
                                    try llm(101, "completed", tts: "processing", content: "Slow.")]
        backend.ttsTrigger[101] = .started
        await recordAndStop()
        await wait("completed") { turn.lastReplyNodeId == 101 }
        // Longer than the old 60 s net at test scale (1 s); reconcile keeps running.
        try await Task.sleep(nanoseconds: 1_300_000_000)
        XCTAssertEqual(turn.state, .awaitingAudio)
        XCTAssertTrue(audio.cueOn, "still thinking, still audible")
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/late0.mp3","duration":2}"#)
        await wait("playing") { turn.state == .playing }
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/late0.mp3"])
    }

    // M12: the thinking cue cannot loop forever.

    func testReplyDeadlineEndsTheTurn() async throws {
        turn.timings.llmGiveUp = 0.15
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing")]
        await recordAndStop()
        await wait("idle") { turn.state == .idle }
        XCTAssertFalse(audio.cueOn)
        XCTAssertEqual(notices.toasts, ["The reply is taking too long. It will be in the thread once it's ready."])
    }

    func testUnreachableReplyStatusEndsTheTurn() async throws {
        turn.timings.llmErrorGiveUp = 0.15
        backend.statuses = [try status("completed", llm: 101)]
        // No llm-status answers for 101: every poll fails.
        await recordAndStop()
        await wait("idle") { turn.state == .idle }
        XCTAssertFalse(audio.cueOn)
        XCTAssertEqual(notices.toasts, ["Can't reach Loore to get the reply. It will be in the thread once it's ready."])
    }

    func testCueTimeIsCappedPerWait() async throws {
        turn.timings.cueCap = 0.2
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing")]
        await recordAndStop()
        await wait("thinking") { turn.state == .awaitingAudio }
        XCTAssertTrue(audio.cueOn)
        await wait("cue capped") { !audio.cueOn }
        XCTAssertEqual(turn.state, .awaitingAudio, "the turn still waits, silently")
        turn.systemInterruptionEnded()
        XCTAssertFalse(audio.cueOn, "the budget is spent for this wait")
    }

    func testAWaitAfterPlaybackGetsAFreshCueBudget() async throws {
        turn.timings.cueCap = 0.2
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing", tts: "processing", streaming: true)]
        await recordAndStop()
        await wait("cue capped") { !audio.cueOn && turn.state == .awaitingAudio }
        await wait("attached") { backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/g0.mp3","duration":2}"#)
        await wait("playing") { turn.state == .playing }
        turn.queueStartedPlaying()
        audio.fakeQueue.waitingForChunks = true
        turn.queueDrained()
        XCTAssertTrue(audio.cueOn, "a new wait after audio played gets its own cue budget")
    }

    func testPausingDuringADrainSilencesTheCue() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing", tts: "processing", streaming: true)]
        await recordAndStop()
        await wait("attached") { backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/g0.mp3","duration":2}"#)
        await wait("playing") { turn.state == .playing }
        turn.queueStartedPlaying()
        audio.fakeQueue.waitingForChunks = true
        turn.queueDrained()
        XCTAssertTrue(audio.cueOn)
        // AirPods out / lock-screen pause.
        audio.fakeQueue.isPlaying = false
        turn.playbackPaused()
        XCTAssertFalse(audio.cueOn)
        turn.systemInterruptionEnded()
        XCTAssertFalse(audio.cueOn, "an interruption ending does not restart the cue under a paused reply")
        audio.fakeQueue.isPlaying = true
        turn.playbackResumed()
        XCTAssertTrue(audio.cueOn, "play while still waiting: the cue again")
    }

    // M11: a "next" (cancel) while Stop uploads the last chunks must not drop the recording.
    func testCancelDuringStopIsIgnoredAndTheTurnFinalizes() async throws {
        recorder.stopDelay = 0.1
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing")]
        await recordAndStop()
        XCTAssertEqual(turn.state, .stopping)
        turn.cancelProcessing()
        XCTAssertEqual(turn.state, .stopping)
        await wait("finalized") { backend.log.contains { $0.hasPrefix("finalize") } }
        await wait("thinking") { turn.state == .awaitingAudio }
    }

    func testLeavingDuringStopDoesNotReactivateTheSession() async throws {
        recorder.stopDelay = 0.1
        await recordAndStop()
        turn.tearDown()
        XCTAssertEqual(Array(audio.events.suffix(2)), ["deactivate", "conversation.ended"])
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertFalse(audio.events.contains("category.playback"))
        XCTAssertFalse(backend.log.contains { $0.hasPrefix("finalize") })
    }

    // M10: returning to the Voice screen must not apply its route parameters again.

    // Device test: a new Voice screen continued the last thread, which had been deleted.
    func testANewVoiceScreenWithoutParentStartsANewConversation() async throws {
        var first = VoiceRouteParameters(parentId: 7, resumeLLMId: nil)
        first.applyOnce(to: turn)
        try await runToDone()
        XCTAssertEqual(turn.threadParentId, 101)
        var fresh = VoiceRouteParameters(parentId: nil, resumeLLMId: nil)
        fresh.applyOnce(to: turn)
        XCTAssertNil(turn.threadParentId, "the next recording must not reply to the old thread")
    }

    // Device test: "Voice Mode" on an earlier node showed the old session instead of branching.
    func testVoiceModeOnAnEarlierNodeBranchesFromIt() async throws {
        try await runToDone()
        XCTAssertEqual(turn.state, .done)
        backend.llmStatuses[201] = [try llm(201, "completed", content: "Branch.")]
        var branch = VoiceRouteParameters(parentId: 50, resumeLLMId: 201)
        branch.applyOnce(to: turn)
        XCTAssertEqual(turn.state, .awaitingAudio, "the new reply is awaited, not the old session shown")
        XCTAssertEqual(turn.threadParentId, 50)
        XCTAssertTrue(audio.fakeQueue.urls.isEmpty, "the old reply is not part of the new conversation")
        await wait("the new reply's audio is requested") { backend.log.contains("tts 201") }
    }

    func testANewVoiceScreenKeepsARecordingInProgress() async throws {
        var first = VoiceRouteParameters(parentId: 7, resumeLLMId: nil)
        first.applyOnce(to: turn)
        turn.start()
        await wait("recording") { turn.state == .recording }
        var other = VoiceRouteParameters(parentId: 9, resumeLLMId: nil)
        other.applyOnce(to: turn)
        XCTAssertEqual(turn.state, .recording)
        XCTAssertEqual(turn.threadParentId, 7)
    }

    func testLeavingVoiceForgetsTheThread() async throws {
        try await runToDone()
        XCTAssertNotNil(turn.lastReplyNodeId)
        turn.tearDown()
        XCTAssertNil(turn.threadParentId)
        XCTAssertNil(turn.lastReplyNodeId)
    }

    // Device test: after a cancel, the lock screen offered the previous reply and played it.
    func testCancelUnloadsThePreviousReply() async throws {
        try await runToDone()
        XCTAssertFalse(audio.fakeQueue.urls.isEmpty)
        turn.continueConversation()
        await wait("recording") { turn.state == .recording }
        turn.stop()
        await wait("thinking") { turn.state == .awaitingAudio }
        turn.cancelProcessing()
        XCTAssertTrue(audio.fakeQueue.urls.isEmpty, "nothing left to replay")
    }

    func testRouteParentIsAppliedOncePerScreen() async throws {
        var route = VoiceRouteParameters(parentId: 7, resumeLLMId: nil)
        route.applyOnce(to: turn)
        XCTAssertEqual(turn.threadParentId, 7)
        try await runToDone()
        XCTAssertEqual(turn.threadParentId, 101)
        // Turn 2 is cancelled, then the screen appears again (tab switch).
        turn.continueConversation()
        await wait("recording") { turn.state == .recording }
        turn.stop()
        await wait("thinking") { turn.state == .awaitingAudio }
        turn.cancelProcessing()
        XCTAssertEqual(turn.state, .idle)
        route.applyOnce(to: turn)
        XCTAssertEqual(turn.threadParentId, 101, "turn 3 still replies under turn 1's reply")
    }

    func testRouteResumeIsNotReplayedOnReturn() async throws {
        backend.llmStatuses[300] = [try llm(300, "processing", tts: "processing")]
        var route = VoiceRouteParameters(parentId: 9, resumeLLMId: 300)
        route.applyOnce(to: turn)
        await wait("attached") { backend.streams[300] != nil }
        XCTAssertEqual(turn.threadParentId, 9)
        turn.cancelProcessing()
        let requests = backend.streamRequests.count
        route.applyOnce(to: turn)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(turn.state, .idle, "no cue, no replay of reply 300")
        XCTAssertEqual(backend.streamRequests.count, requests)
        XCTAssertFalse(audio.cueOn)
    }

    // MARK: Endings

    func testEmptyTranscriptReturnsToReady() async throws {
        backend.statuses = [try status("completed", content: " ")]
        await recordAndStop()
        await wait("idle") { turn.state == .idle }
        XCTAssertFalse(audio.cueOn)
        XCTAssertTrue(notices.toasts.isEmpty)
    }

    func testServerWarningIsToasted() async throws {
        backend.statuses = [try status("completed", warning: "Monthly limit reached")]
        await recordAndStop()
        await wait("idle") { turn.state == .idle }
        XCTAssertEqual(notices.toasts, ["Monthly limit reached"])
    }

    func testSpendCapOnInitNeverOpensTheMic() async throws {
        backend.startResult = .failure(APIError.spendCap(message: "capped"))
        turn.start()
        await wait("idle") { turn.state == .idle }
        XCTAssertTrue(recorder.calls.isEmpty)
        XCTAssertEqual(notices.capped, 1)
        XCTAssertTrue(notices.toasts.first?.hasPrefix("You've reached your monthly usage limit") == true)
    }

    // AI usage `none` (server code ai_usage_none): the screen explains, nothing records.

    private func aiUsageNone(scope: String?) -> APIError {
        var body = #"{"error":"Voice mode needs AI to listen and reply.","code":"ai_usage_none""#
        if let scope { body += #","scope":"\#(scope)""# }
        return APIError.from(status: 403, contentType: "application/json", data: Data((body + "}").utf8))
    }

    func testAIUsageNoneOnInitNeverOpensTheMic() async throws {
        backend.startResult = .failure(aiUsageNone(scope: "thread"))
        turn.start()
        await wait("idle") { turn.state == .idle }
        XCTAssertTrue(recorder.calls.isEmpty, "the mic never opens")
        XCTAssertEqual(turn.aiBlock, VoiceAIBlock(scope: .thread))
        XCTAssertTrue(notices.toasts.isEmpty, "the screen explains instead of a toast")
        XCTAssertFalse(turn.hasError)
        XCTAssertEqual(turn.phase, .ready)
        XCTAssertFalse(backend.log.contains("discard"), "no draft was created")
    }

    func testAIUsageNoneWithoutScopeFallsBackByThread() async throws {
        let fresh = VoiceTurnController(backend: backend, recorder: recorder, audio: audio, notices: notices)
        fresh.aiUsage = { "none" }
        backend.startResult = .failure(aiUsageNone(scope: nil))
        fresh.start()
        await wait("idle") { fresh.state == .idle && fresh.aiBlock != nil }
        XCTAssertEqual(fresh.aiBlock, VoiceAIBlock(scope: .account))
        XCTAssertEqual(backend.log.first, "init parent=nil ai=none")

        backend.startResult = .failure(aiUsageNone(scope: nil))
        turn.start()
        await wait("idle") { turn.state == .idle && turn.aiBlock != nil }
        XCTAssertEqual(turn.aiBlock, VoiceAIBlock(scope: .thread), "a turn under a parent is a thread")
    }

    func testAIBlockClearsForAFreshCheckAndOnTearDown() async throws {
        backend.startResult = .failure(aiUsageNone(scope: "account"))
        turn.start()
        await wait("blocked") { turn.aiBlock != nil }
        turn.clearAIBlock()
        XCTAssertNil(turn.aiBlock)

        turn.start()
        await wait("blocked again") { turn.aiBlock != nil }
        turn.tearDown()
        XCTAssertNil(turn.aiBlock)

        // Allowed again (the setting changed): the next start records.
        backend.startResult = .success(StreamingInitResponse(sessionId: "sid-2", draftId: 8, sseURL: nil))
        turn.start()
        await wait("recording") { turn.state == .recording }
        XCTAssertNil(turn.aiBlock)
    }

    func testLegacyReplyRefusedForAIUsageNoneBlocksQuietly() async throws {
        backend.statuses = [try status("completed")]
        backend.legacyError = aiUsageNone(scope: "thread")
        await recordAndStop()
        await wait("idle") { turn.state == .idle && turn.aiBlock != nil }
        XCTAssertTrue(backend.log.contains("legacy"))
        XCTAssertEqual(turn.aiBlock, VoiceAIBlock(scope: .thread))
        XCTAssertTrue(notices.toasts.isEmpty)
        XCTAssertFalse(audio.cueOn)
    }

    func testRecordingSavedWithoutAReplyIsToasted() async throws {
        // Finalize of a Voice draft whose reply is not allowed (the setting changed
        // mid-recording): the entry is saved, no reply node, the server's warning.
        let warning = "Your recording is saved. Loore didn't reply because AI usage is set to None."
        backend.statuses = [try status("completed", warning: warning)]
        await recordAndStop()
        await wait("idle") { turn.state == .idle }
        XCTAssertEqual(notices.toasts, [warning])
        XCTAssertFalse(backend.log.contains("legacy"))
        XCTAssertNil(turn.aiBlock)
    }

    func testMicrophoneFailureDiscardsTheDraft() async throws {
        recorder.startError = NSError(domain: "mic", code: 1)
        turn.start()
        await wait("idle") { turn.state == .idle }
        XCTAssertTrue(backend.log.contains("discard"))
        XCTAssertTrue(turn.hasError)
    }

    func testReplyFailureToastsTheServerMessage() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "failed", error: "Please break the request into smaller steps")]
        await recordAndStop()
        await wait("idle") { turn.state == .idle }
        XCTAssertEqual(notices.toasts, ["Please break the request into smaller steps"])
        XCTAssertFalse(audio.cueOn)
    }

    func testFatalUploadEndsTheTurn() async throws {
        recorder.outcome = .init(produced: 1, stored: 0, failed: [0], fatalMessage: "Your audio recording could not be processed.")
        await recordAndStop()
        await wait("idle") { turn.state == .idle }
        XCTAssertEqual(notices.toasts, ["Your audio recording could not be processed."])
        XCTAssertTrue(audio.events.contains("sound.error"))
        XCTAssertFalse(backend.log.contains { $0.hasPrefix("finalize") })
    }

    // B1: a chunk the server does not have is never left out of a finalize.
    func testMissingChunksKeepTheRecordingInsteadOfFinalizing() async throws {
        recorder.outcome = .init(produced: 4, stored: 3, failed: [2], fatalMessage: nil)
        backend.statuses = [try status("completed", content: "")]
        await recordAndStop()
        await wait("idle") { turn.state == .idle }
        XCTAssertFalse(backend.log.contains { $0.hasPrefix("finalize") })
        XCTAssertFalse(backend.log.contains("discard"))
        XCTAssertFalse(recorder.calls.contains("forget"), "the queued chunks stay on the phone")
        XCTAssertEqual(notices.toasts, [VoiceTurnController.missingChunksMessage])
        XCTAssertFalse(audio.cueOn)
    }

    func testRetriedFinalizeAnsweringNotRecordingCountsAsSuccess() async throws {
        backend.finalizeError = APIError.server(status: 400,
                                                body: ServerErrorBody(error: "Streaming session is not in recording state"))
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing")]
        await recordAndStop()
        await wait("awaiting audio") { turn.state == .awaitingAudio }
    }

    func testCancelWhileThinkingIgnoresLateEvents() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "processing")]
        await recordAndStop()
        await wait("attached") { backend.streams[101] != nil }
        turn.cancelProcessing()
        XCTAssertEqual(turn.state, .idle)
        XCTAssertFalse(audio.cueOn)
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/late.mp3","duration":2}"#)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(audio.fakeQueue.urls.isEmpty)
        XCTAssertEqual(turn.state, .idle)
    }

    // MARK: Recording controls

    func testInterruptionPausesAlertsAndNotifies() async throws {
        turn.start()
        await wait("recording") { turn.state == .recording }
        turn.systemInterruptionBegan()
        XCTAssertTrue(turn.isPaused)
        XCTAssertTrue(turn.isInterrupted)
        XCTAssertEqual(recorder.calls.last, "interrupt")
        XCTAssertEqual(notices.notified, [.recordingPaused])
        XCTAssertTrue(audio.events.contains("sound.interruption"))
        XCTAssertTrue(notices.toasts.last?.hasPrefix("Recording paused") == true)
        turn.resumeRecording()
        XCTAssertFalse(turn.isPaused)
        XCTAssertFalse(turn.isInterrupted)
        XCTAssertTrue(audio.events.contains("reactivate"))
    }

    // M15: a microphone that cannot restart after a route change is reported, not silent.
    func testFailedMicrophoneRestartIsReportedAsAnInterruption() async throws {
        turn.start()
        await wait("recording") { turn.state == .recording }
        recorder.onSourceFailed?()
        XCTAssertTrue(turn.isInterrupted)
        XCTAssertTrue(turn.isPaused, "the screen shows Resume, not Recording")
        XCTAssertEqual(recorder.calls.last, "interrupt")
        XCTAssertTrue(audio.events.contains("sound.interruption"))
        XCTAssertEqual(notices.notified, [.recordingPaused])
        XCTAssertTrue(notices.toasts.last?.hasPrefix("Recording paused — the microphone stopped") == true)
        turn.resumeRecording()
        XCTAssertFalse(turn.isInterrupted)
        XCTAssertEqual(recorder.calls.last, "resume")
    }

    // #397: a resume pressed on the lock screen that fails is not only a toast
    // (nobody sees it there): a notification says so, and goes once it works.
    func testFailedResumeNotifiesAndAWorkingOneWithdrawsIt() async throws {
        turn.start()
        await wait("recording") { turn.state == .recording }
        turn.pauseRecording()
        recorder.resumeError = NSError(domain: NSOSStatusErrorDomain, code: 561_145_187)
        turn.resumeRecording()
        XCTAssertTrue(turn.isPaused, "still paused: the microphone did not restart")
        XCTAssertEqual(notices.notified, [.resumeFailed])
        XCTAssertTrue(notices.toasts.last?.hasPrefix("The microphone could not restart") == true)
        recorder.resumeError = nil
        turn.resumeRecording()
        XCTAssertFalse(turn.isPaused)
        XCTAssertTrue(notices.withdrawn.contains(.resumeFailed))
    }

    func testLeavingVoiceEndsTheLockScreenActivity() async throws {
        turn.start()
        await wait("recording") { turn.state == .recording }
        turn.tearDown()
        XCTAssertTrue(audio.events.contains("conversation.ended"))
    }

    func testLongRecordingWarningAt59Minutes() async throws {
        turn.timings.longRecording = 2
        turn.start()
        await wait("recording") { turn.state == .recording }
        await wait("warned") { notices.notified.contains(.longRecording) }
        XCTAssertTrue(audio.events.contains("sound.59"))
        XCTAssertEqual(audio.events.filter { $0 == "sound.59" }.count, 1)
    }

    func testResumingAnInterruptedDraftContinuesItsNumbering() async throws {
        let draft = try decode(InterruptedDraft.self,
            #"{"id":3,"session_id":"old","parent_id":9,"label":"Voice","content":"","chunk_count":4,"has_stored_chunks":true,"streaming_mime_type":"audio/mp4"}"#)
        turn.resumeInterrupted(draft)
        await wait("recording") { turn.state == .recording }
        XCTAssertEqual(recorder.calls, ["start old from 4"])
        XCTAssertFalse(backend.log.contains { $0.hasPrefix("init") })
        XCTAssertEqual(turn.threadParentId, 9)
    }

    func testWebMDraftCannotBeResumedNatively() throws {
        let draft = try decode(InterruptedDraft.self,
            #"{"id":3,"session_id":"old","chunk_count":4,"streaming_mime_type":"audio/webm"}"#)
        turn.resumeInterrupted(draft)
        XCTAssertEqual(turn.state, .idle)
        XCTAssertEqual(notices.toasts.count, 1)
    }

    func testChapterTitlesMatchTheWeb() {
        XCTAssertEqual(ChapterTitle.from(content: "## Hello *world*"), "Hello world")
        XCTAssertNil(ChapterTitle.from(content: "  # "))
        let long = "The river carried the ice past the bridge and on toward the sea at dusk"
        XCTAssertEqual(ChapterTitle.from(content: long), "The river carried the ice past the bridge…")
        XCTAssertEqual(ChapterTitle.numeral(2), "III")
        XCTAssertEqual(ChapterTitle.numeral(9), "10")
    }
}

@MainActor
final class ResumedSessionTotalsTests: XCTestCase {
    func testResumedSessionCountsTheChunksAlreadyStored() {
        let outcome = ChunkUploader.Outcome(produced: 2, stored: 2, failed: [], fatalMessage: nil, prior: 1)
        XCTAssertEqual(outcome.totalForFinalize, 3)
        let partial = ChunkUploader.Outcome(produced: 3, stored: 2, failed: [5], fatalMessage: nil, prior: 4)
        XCTAssertEqual(partial.totalForFinalize, 6)
    }
}

/// The voice Live Activity's phase for each turn state (#397).
final class VoiceLiveActivityStateTests: XCTestCase {
    private typealias Phase = VoiceActivityAttributes.ContentState.Phase

    private func phase(_ turn: VoiceTurnController.State, paused: Bool = false, interrupted: Bool = false,
                       awaiting: Bool = false) -> Phase {
        VoiceLiveActivity.state(turn: turn, isPaused: paused, isInterrupted: interrupted,
                                awaitingNextNode: awaiting, elapsed: 0).phase
    }

    func testRecordingPhases() {
        XCTAssertEqual(phase(.starting), .starting, "no clock or buttons before the microphone is on")
        XCTAssertEqual(phase(.recording), .recording)
        XCTAssertEqual(phase(.recording, paused: true), .paused)
        XCTAssertEqual(phase(.recording, paused: true, interrupted: true), .interrupted)
        XCTAssertEqual(phase(.stopping), .sending)
    }

    func testReplyPhasesOfferRecordOnlyOnceLooreSpeaks() {
        XCTAssertEqual(phase(.transcribing), .thinking)
        XCTAssertEqual(phase(.awaitingAudio), .thinking)
        XCTAssertEqual(phase(.draining, awaiting: true), .thinking, "between a chain's nodes: still thinking")
        XCTAssertEqual(phase(.draining), .replying)
        XCTAssertEqual(phase(.playing), .replying)
        XCTAssertEqual(phase(.done), .finished)
        XCTAssertEqual(phase(.idle), .ready)
    }

    func testTheRecordingClockStartsWhereTheRecordingIs() {
        let now = Date(timeIntervalSince1970: 1_000)
        let state = VoiceLiveActivity.state(turn: .recording, isPaused: false, isInterrupted: false,
                                            awaitingNextNode: false, elapsed: 42.6, now: now)
        XCTAssertEqual(state.clockStart, now.addingTimeInterval(-42.6))
        let paused = VoiceLiveActivity.state(turn: .recording, isPaused: true, isInterrupted: false,
                                             awaitingNextNode: false, elapsed: 42.6, now: now)
        XCTAssertNil(paused.clockStart)
        XCTAssertEqual(paused.elapsed, 42)
    }
}
