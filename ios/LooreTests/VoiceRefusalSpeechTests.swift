import XCTest
@testable import Loore

// #651: every refusal of a Voice turn is said aloud, as the thread's length is
// (#648): the monthly usage limit, AI usage None, a change waiting for approval.
// Its sentence comes from a cached clip in the replies' voice, else the device's
// speech; never over the reply, and a new recording stops it. The toasts, the
// banner and the blocks stay.

private func apiError(_ status: Int, _ body: String) -> APIError {
    APIError.from(status: status, contentType: "application/json", data: Data(body.utf8))
}

private let spendCap = #"{"error":"monthly_spend_limit_reached","message":"You've reached your monthly usage limit."}"#
private let aiUsageNone = #"{"error":"Voice mode needs AI.","code":"ai_usage_none","scope":"thread"}"#
private let approvalPending = #"{"error":"A change to your data waits for your approval.","code":"approval_pending","pending":1}"#

@MainActor
final class VoiceRefusalSpeechTurnTests: XCTestCase {
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
                             longRecording: 59 * 60, errorSound: 0.05)
    }

    private func wait(_ what: String = "", timeout: Double = 2, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTAssertTrue(condition(), "timed out waiting: \(what)")
    }

    private var spoken: [String] {
        audio.events.filter { $0.hasPrefix("speak: ") }.map { String($0.dropFirst("speak: ".count)) }
    }

    private var errorSounds: Int { audio.events.filter { $0 == "sound.error" }.count }

    private func status(warning: String, warningCode: String?) throws -> StreamingSessionStatus {
        var json = #"{"session_id":"sid-1","streaming_status":"completed","content":"hello there","chunks":[]"#
        json += #","warning":"\#(warning)""#
        if let warningCode { json += #","warning_code":"\#(warningCode)""# }
        return try decode(StreamingSessionStatus.self, json + "}")
    }

    private func recordAndStop() async {
        turn.start()
        await wait("recording") { self.turn.state == .recording }
        turn.stop()
    }

    // MARK: A refused start

    func testAStartRefusedForTheUsageLimitIsSaid() async throws {
        backend.startResult = .failure(apiError(402, spendCap))
        turn.start()
        await wait("said") { !self.spoken.isEmpty }
        XCTAssertEqual(spoken, [SpokenRefusal.usageLimit.text])
        XCTAssertEqual(notices.toasts, [SpendCap.toastMessage(.record)], "the toast stays")
        XCTAssertEqual(notices.capped, 1, "and the banner")
        XCTAssertTrue(recorder.calls.isEmpty, "the mic never opens")
        XCTAssertEqual(turn.state, .idle)
        XCTAssertFalse(turn.hasError)
        XCTAssertEqual(errorSounds, 0, "a refusal, not a failure")
        // Said first, then the session ends.
        await wait("deactivated") { self.audio.events.last == "deactivate" }
    }

    func testAStartRefusedForAIUsageIsSaidAndBlocks() async throws {
        backend.startResult = .failure(apiError(403, aiUsageNone))
        turn.start()
        await wait("said") { !self.spoken.isEmpty }
        XCTAssertEqual(spoken, [SpokenRefusal.aiUsage.text])
        XCTAssertEqual(turn.aiBlock, VoiceAIBlock(scope: .thread))
        XCTAssertTrue(notices.toasts.isEmpty, "the screen explains")
        XCTAssertEqual(errorSounds, 0)
    }

    func testAStartRefusedForAWaitingChangeIsSaidAndListsIt() async throws {
        backend.startResult = .failure(apiError(409, approvalPending))
        backend.approvalLists[50] = [ToolApproval(id: 31, nodeId: 49, turnNodeId: 49, tool: "update_artifact",
                                                  kinds: [.otherUser], title: "Change your Memory", kind: "memory",
                                                  artifactTitle: "Memory")]
        turn.start()
        await wait("said") { !self.spoken.isEmpty }
        XCTAssertEqual(spoken, [SpokenRefusal.approvalWaiting.text])
        XCTAssertTrue(turn.approvalWaiting)
        XCTAssertEqual(errorSounds, 0)
    }

    func testAStartThatFailedForAnotherReasonSaysNothing() async throws {
        backend.startResult = .failure(apiError(500, #"{"error":"boom"}"#))
        turn.start()
        await wait("idle") { self.turn.state == .idle }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(spoken.isEmpty)
    }

    // MARK: A recording saved without a reply

    func testEachSkippedReplyIsSaidByItsCode() async throws {
        for refusal in [SpokenRefusal.usageLimit, .aiUsage, .approvalWaiting] {
            audio.events = []
            notices.toasts = []
            backend.statuses = [try status(warning: "Your recording is saved.", warningCode: refusal.rawValue)]
            await recordAndStop()
            await wait("said \(refusal)") { !self.spoken.isEmpty }
            XCTAssertEqual(spoken, [refusal.text])
            XCTAssertEqual(notices.toasts, ["Your recording is saved."], "the toast stays")
            XCTAssertEqual(turn.state, .idle)
            XCTAssertNil(turn.aiBlock, "a refused recording shows no block: the next press asks again")
            XCTAssertEqual(errorSounds, 0)
            XCTAssertFalse(audio.cueOn)
        }
    }

    func testARecordingSkippedForAWaitingChangeBringsItsDialog() async throws {
        backend.approvalLists[50] = [ToolApproval(id: 31, nodeId: 49, turnNodeId: 49, tool: "update_artifact",
                                                  kinds: [.otherUser], title: "Change your Memory", kind: "memory",
                                                  artifactTitle: "Memory")]
        backend.statuses = [try status(warning: "Your recording is saved.", warningCode: "approval_pending")]
        await recordAndStop()
        await wait("listed") { self.turn.approvalWaiting }
        await wait("dialog") { self.turn.approvalDialogDue }
    }

    // MARK: The legacy reply request

    func testTheLegacyReplyRefusedForEachReasonIsSaid() async throws {
        let cases: [(APIError, SpokenRefusal)] = [
            (apiError(402, spendCap), .usageLimit),
            (apiError(403, aiUsageNone), .aiUsage),
            (apiError(409, approvalPending), .approvalWaiting),
        ]
        for (error, refusal) in cases {
            audio.events = []
            turn.clearAIBlock()
            backend.statuses = [try decode(StreamingSessionStatus.self,
                #"{"session_id":"sid-1","streaming_status":"completed","content":"hello there","chunks":[]}"#)]
            backend.legacyError = error
            await recordAndStop()
            await wait("said \(refusal)") { !self.spoken.isEmpty }
            XCTAssertEqual(spoken, [refusal.text])
            XCTAssertEqual(errorSounds, 0, "\(refusal) is a refusal, not a failure")
            XCTAssertFalse(turn.hasError)
            XCTAssertEqual(turn.state, .idle)
        }
    }

    // MARK: A press refused on the phone

    func testAPressRefusedForTheKnownLimitIsSaid() async {
        await turn.announceUsageLimit()
        XCTAssertEqual(spoken, [SpokenRefusal.usageLimit.text])
        XCTAssertTrue(backend.log.isEmpty, "no request")
        XCTAssertEqual(turn.state, .idle)
    }

    func testContinueRefusedForTheLimitStopsTheReplyThenSaysIt() async throws {
        backend.statuses = [try decode(StreamingSessionStatus.self,
            #"{"session_id":"sid-1","streaming_status":"completed","content":"hello there","chunks":[],"llm_node_id":101}"#)]
        backend.llmStatuses[101] = [try decode(LLMStatus.self,
            #"{"node_id":101,"status":"completed","tts_task_status":"processing","content":"The ice moves.","warnings":[]}"#)]
        await recordAndStop()
        await wait("attached") { self.backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/a0.mp3","duration":4.2}"#)
        await wait("playing") { self.turn.phase == .playback }

        await turn.announceUsageLimit()

        XCTAssertEqual(spoken, [SpokenRefusal.usageLimit.text])
        XCTAssertEqual(turn.state, .idle, "the reply stopped, as Continue stops it")
        XCTAssertTrue(audio.fakeQueue.urls.isEmpty, "unloaded before the sentence")
        XCTAssertEqual(backend.log.filter { $0.hasPrefix("init") }.count, 1, "no new turn")
    }

    func testTheLockScreensWaitingChangeIsSaidOnlyWhenNothingPlays() async throws {
        backend.approvalLists[60] = [ToolApproval(id: 40, nodeId: 58, turnNodeId: 58, tool: "update_artifact",
                                                  kinds: [.otherUser], title: "Change your Memory", kind: "memory",
                                                  artifactTitle: "Memory")]
        await turn.refreshApprovals(nodeId: 60, openDialog: false)
        XCTAssertTrue(turn.approvalWaiting)

        // A reply still plays (a replay after the announcement): it goes on.
        audio.fakeQueue.urls = ["/media/r0.mp3"]
        audio.fakeQueue.isPlaying = true
        await turn.announceApprovalWaiting()
        XCTAssertTrue(spoken.isEmpty)
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/r0.mp3"])

        // Nothing plays: said.
        audio.fakeQueue.isPlaying = false
        await turn.announceApprovalWaiting()
        XCTAssertEqual(spoken, [SpokenRefusal.approvalWaiting.text])
    }

    func testANewRecordingStopsTheSentenceBeforeTheMicrophone() async throws {
        audio.holdSpeech = true
        backend.startResult = .failure(apiError(402, spendCap))
        turn.start()
        await wait("said") { self.audio.isSpeaking }

        backend.startResult = .success(StreamingInitResponse(sessionId: "sid-2", draftId: 8, sseURL: nil))
        turn.start()
        await wait("recording") { self.turn.state == .recording }
        XCTAssertFalse(audio.isSpeaking)
        let stop = try XCTUnwrap(audio.events.lastIndex(of: "speak.stop"))
        let record = try XCTUnwrap(audio.events.lastIndex(of: "activate.record"))
        XCTAssertLessThan(stop, record)
    }

    // MARK: The sentences

    func testTheSentencesAreTheServersInPlainWords() {
        // backend/utils/voice_refusals.py SPOKEN_REFUSALS, and the web's.
        XCTAssertEqual(SpokenRefusal.usageLimit.text,
                       "You've reached your monthly usage limit, so Loore can't reply until it resets.")
        XCTAssertEqual(SpokenRefusal.aiUsage.text, "AI usage is set to None, so Loore can't reply here.")
        XCTAssertEqual(SpokenRefusal.approvalWaiting.text,
                       "A change to your data is waiting for your approval. Approve or reject it on the screen to continue.")
        XCTAssertEqual(SpokenRefusal.threadTooLong.text, ThreadLength.spokenText)
        for refusal in SpokenRefusal.allCases {
            XCTAssertNil(refusal.text.rangeOfCharacter(from: .decimalDigits), refusal.rawValue)
            for word in ["$", "€", "buy", "pay", "price", "subscri", "upgrade", "plan"] {
                XCTAssertFalse(refusal.text.lowercased().contains(word), "\(refusal.rawValue): \(word)")
            }
        }
        XCTAssertEqual(SpokenRefusal(code: "approval_pending"), .approvalWaiting)
        XCTAssertNil(SpokenRefusal(code: "model_read_only"))
        XCTAssertNil(SpokenRefusal(code: nil))
        XCTAssertEqual(SpokenRefusal.usageLimit.clipPath, "/api/voice/refusal/monthly_spend_limit_reached.mp3")
    }
}

/// The clip, else the device's speech (`RefusalVoice`, live in `AudioCenter`).
@MainActor
final class RefusalVoiceTests: XCTestCase {
    private var voice: RefusalVoice!
    private var log: [String] = []
    private var clips: [SpokenRefusal: Data] = [:]
    private var clipPlays = true
    private var fetchGate: CheckedContinuation<Void, Never>?
    private var holdFetch = false

    override func setUp() async throws {
        log = []
        clips = [.usageLimit: Data("mp3-limit".utf8), .aiUsage: Data("mp3-ai".utf8)]
        clipPlays = true
        holdFetch = false
        voice = RefusalVoice()
        voice.fetch = { [unowned self] refusal in
            self.log.append("fetch \(refusal.rawValue)")
            if self.holdFetch { await withCheckedContinuation { self.fetchGate = $0 } }
            return self.clips[refusal]
        }
        voice.playClip = { [unowned self] data in
            self.log.append("clip \(String(decoding: data, as: UTF8.self))")
            return self.clipPlays
        }
        voice.stopClip = { [unowned self] in self.log.append("clip.stop") }
        voice.sayOnDevice = { [unowned self] text in self.log.append("device \(text)") }
        voice.stopDevice = { [unowned self] in self.log.append("device.stop") }
    }

    private var said: [String] { log.filter { $0.hasPrefix("clip mp3") || $0.hasPrefix("device ") && $0 != "device.stop" } }

    func testTheClipIsPlayedAndAskedForOncePerRun() async {
        await voice.say(.usageLimit)
        await voice.say(.usageLimit)
        XCTAssertEqual(log.filter { $0.hasPrefix("fetch") }, ["fetch monthly_spend_limit_reached"])
        XCTAssertEqual(said, ["clip mp3-limit", "clip mp3-limit"])
    }

    func testNoClipMeansTheDevicesSpeech() async {
        await voice.say(.approvalWaiting)
        XCTAssertEqual(said, ["device \(SpokenRefusal.approvalWaiting.text)"])
    }

    func testAClipThatCannotPlayIsSaidByTheDevice() async {
        clipPlays = false
        await voice.say(.aiUsage)
        XCTAssertEqual(said, ["clip mp3-ai", "device \(SpokenRefusal.aiUsage.text)"])
    }

    func testStoppedWhileTheClipIsAskedForSaysNothing() async throws {
        holdFetch = true
        let saying = Task { await voice.say(.usageLimit) }
        for _ in 0..<100 where fetchGate == nil { try await Task.sleep(nanoseconds: 5_000_000) }
        voice.stop()
        fetchGate?.resume()
        await saying.value
        XCTAssertTrue(said.isEmpty, "a new recording started meanwhile")
        XCTAssertTrue(log.contains("clip.stop") && log.contains("device.stop"))
    }
}

/// The lock screen's Record and the clip's request, in the app.
@MainActor
final class VoiceRefusalSpeechAppTests: StubbedAppTestCase {
    func testTheLockScreensRecordWhileCappedIsSaid() async throws {
        let backend = FakeVoiceBackend()
        let recorder = FakeRecorder()
        let audio = FakeAudio()
        recorder.audio = audio
        let turn = VoiceTurnController(backend: backend, recorder: recorder, audio: audio, notices: FakeNotices(),
                                       parentId: nil)
        app.notifySpendBlocked()

        let body = await app.audio.recordFromLockScreen(turn)

        XCTAssertEqual(body, SpendCap.toastMessage(.record))
        XCTAssertEqual(audio.events.filter { $0.hasPrefix("speak: ") }, ["speak: \(SpokenRefusal.usageLimit.text)"])
        XCTAssertTrue(backend.log.isEmpty, "no session asked for")
        turn.tearDown()
    }

    func testTheClipIsAskedForByItsCode() async throws {
        StubURLProtocol.install { request in
            if request.url?.path(percentEncoded: true) == "/api/voice/refusal/ai_usage_none.mp3" {
                return StubResponse(status: 200, headers: ["Content-Type": "audio/mpeg"], chunks: [Data("ID3".utf8)])
            }
            return .json(503, #"{"error":"The sentence is not available."}"#)
        }
        let clip = await app.audio.refusalClip(.aiUsage)
        XCTAssertEqual(clip, Data("ID3".utf8))
        let missing = await app.audio.refusalClip(.usageLimit)
        XCTAssertNil(missing, "not made: the device says it")
    }
}
