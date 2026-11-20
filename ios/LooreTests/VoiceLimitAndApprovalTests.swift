import XCTest
@testable import Loore

// Merge train 2026-11-12 (2): two reasons to refuse a new Voice turn go through
// the one gate (`VoiceTurnController.newTurnRefusal`): a change the AI proposed
// that waits for approval (Peter, 2026-11-12) and a thread too long for another
// reply (#648). When both apply the waiting change comes first: it needs a
// decision before anything else, and the thread's length is said once it is
// decided (the server answers `approval_pending` before `thread_too_long` too).

private func waitingCall(_ id: Int, turn: Int) -> ToolApproval {
    ToolApproval(id: id, nodeId: turn, turnNodeId: turn, tool: "update_artifact", kinds: [.otherUser],
                 title: "Change your Memory", kind: "memory", artifactTitle: "Memory")
}

@MainActor
final class VoiceLimitAndApprovalTurnTests: XCTestCase {
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

    private var inits: Int { backend.log.filter { $0.hasPrefix("init") }.count }
    private var spoken: [String] {
        audio.events.filter { $0.hasPrefix("speak: ") }.map { String($0.dropFirst("speak: ".count)) }
    }

    func testBothTheWaitingChangeComesFirstThenTheLength() async throws {
        backend.approvalLists[50] = [waitingCall(31, turn: 49)]
        await turn.refreshApprovals(nodeId: 50, openDialog: true)
        turn.noteThreadLength(.limit)
        XCTAssertTrue(turn.approvalWaiting)
        XCTAssertEqual(turn.aiBlock?.isThreadTooLong, true)

        XCTAssertEqual(turn.newTurnRefusalReason, .approvalWaiting(count: 1))
        XCTAssertEqual(turn.newTurnRefusal, VoiceApproval.recordRefusal(count: 1))
        turn.start()
        XCTAssertEqual(turn.state, .idle)
        XCTAssertEqual(inits, 0, "no session was asked for")
        XCTAssertTrue(turn.approvalDialogDue, "the change is decided first")

        // Decided: the length is what refuses now.
        turn.approvalsChanged([])
        XCTAssertEqual(turn.newTurnRefusalReason, .threadTooLong(.threadTooLong()))
        XCTAssertEqual(turn.newTurnRefusal, ThreadLength.limitText)
        turn.start()
        XCTAssertEqual(turn.state, .idle)
        XCTAssertEqual(inits, 0)
        XCTAssertTrue(spoken.isEmpty, "the screen shows the block; nothing was said for a press it hides")
    }

    func testEachAlone() async throws {
        // The length alone.
        turn.noteThreadLength(.limit)
        XCTAssertEqual(turn.newTurnRefusalReason, .threadTooLong(.threadTooLong()))
        turn.start()
        XCTAssertEqual(inits, 0)

        // A new thread clears it; a change waiting there alone refuses.
        backend.approvalLists[60] = [waitingCall(40, turn: 58)]
        turn.clearAIBlock()
        turn.startNewConversation(parentId: 60)
        XCTAssertNil(turn.newTurnRefusalReason)
        await wait("listed") { turn.approvalWaiting && self.backend.approvalRequests == [60] }
        XCTAssertEqual(turn.newTurnRefusalReason, .approvalWaiting(count: 1))
        turn.start()
        XCTAssertEqual(inits, 0)

        // Neither: the turn starts.
        turn.approvalsChanged([])
        XCTAssertNil(turn.newTurnRefusalReason)
        turn.start()
        await wait("recording") { turn.state == .recording }
        XCTAssertEqual(inits, 1)
    }

    func testAReplyThatWaitsAndReachesTheLimitPlaysOutThenTheDialogThenTheLimit() async throws {
        let clip = "/api/voice/approval-announcement/one.mp3?v=k1"
        backend.approvalLists[101] = [waitingCall(31, turn: 101)]
        backend.announcements[101] = ApprovalAnnouncement(count: 1, url: clip, duration: 4.5)
        backend.statuses = [try decode(StreamingSessionStatus.self,
            #"{"session_id":"sid-1","streaming_status":"completed","content":"hello there","chunks":[],"llm_node_id":101}"#)]
        backend.llmStatuses[101] = [try decode(LLMStatus.self, #"""
            {"node_id":101,"status":"completed","tts_streaming":false,"tts_task_status":"pending",
             "content":"I noted it.","warnings":[],"thread_length":"limit"}
            """#)]
        turn.start()
        await wait("recording") { turn.state == .recording }
        turn.stop()
        await wait("attached") { backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/r0.mp3","duration":3}"#)
        await wait("playing") { turn.state == .playing }
        backend.push(101, "all_complete", #"{"continuation_node_id":null,"preview":"I noted it."}"#)
        await wait("generated") { !audio.fakeQueue.generatingTTS }
        await wait("listed") { turn.approvalWaiting }

        // The reply plays on, with the announcement after it: the limit is shown,
        // not said, and it does not cut the reply.
        XCTAssertEqual(turn.aiBlock?.isThreadTooLong, true)
        XCTAssertEqual(turn.state, .playing)
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/r0.mp3", clip])
        XCTAssertTrue(spoken.isEmpty)
        XCTAssertEqual(turn.newTurnRefusalReason, .approvalWaiting(count: 1))
        XCTAssertFalse(turn.approvalDialogDue)

        // Heard to its end: the dialog (the change first).
        turn.queueFinished()
        XCTAssertTrue(turn.approvalDialogDue)
        turn.continueConversation()
        XCTAssertEqual(turn.state, .done, "no new turn")

        // Decided: the limit remains, and Continue stays refused.
        turn.approvalsChanged([])
        XCTAssertEqual(turn.newTurnRefusalReason, .threadTooLong(.threadTooLong()))
        turn.continueConversation()
        XCTAssertEqual(turn.state, .done)
        XCTAssertEqual(inits, 1, "only the first turn's session")
    }
}

/// The lock screen's Record, in the app: the same order.
@MainActor
final class VoiceLimitAndApprovalAppTests: StubbedAppTestCase {
    private var backend: FakeVoiceBackend!
    private var audio: FakeAudio!
    private var turn: VoiceTurnController!

    override func setUp() async throws {
        try await super.setUp()
        backend = FakeVoiceBackend()
        let recorder = FakeRecorder()
        audio = FakeAudio()
        recorder.audio = audio
        turn = VoiceTurnController(backend: backend, recorder: recorder, audio: audio, notices: FakeNotices(),
                                   parentId: nil)
    }

    private var spoken: [String] {
        audio.events.filter { $0.hasPrefix("speak: ") }.map { String($0.dropFirst("speak: ".count)) }
    }

    func testLockScreenRecordSaysTheWaitingChangeFirstThenTheLength() async throws {
        try XCTSkipUnless(NetworkStatus.shared.isOnline, "the lock screen's Record checks the connection first")
        backend.approvalLists[60] = [waitingCall(40, turn: 58)]
        turn.startNewConversation(parentId: 60)
        for _ in 0..<200 where !(turn.approvalWaiting && backend.approvalRequests == [60]) {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertTrue(turn.approvalWaiting)
        turn.noteThreadLength(.limit)

        let first = await app.audio.recordFromLockScreen(turn)
        XCTAssertEqual(first, VoiceApproval.recordRefusal(count: 1))
        // Said aloud as the waiting change, not as the length (#651).
        XCTAssertEqual(spoken, [SpokenRefusal.approvalWaiting.text])
        XCTAssertEqual(turn.state, .idle)
        XCTAssertFalse(backend.log.contains { $0.hasPrefix("init") })

        // Decided on another device: the next press meets the length, said aloud.
        backend.approvalLists[60] = []
        let second = await app.audio.recordFromLockScreen(turn)
        XCTAssertEqual(second, ThreadLength.limitText)
        XCTAssertEqual(spoken, [SpokenRefusal.approvalWaiting.text, ThreadLength.spokenText])
        XCTAssertEqual(turn.state, .idle)
        XCTAssertFalse(backend.log.contains { $0.hasPrefix("init") }, "no session either time")
        turn.tearDown()
    }
}
