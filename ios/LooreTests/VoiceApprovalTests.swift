import AVFoundation
import SwiftUI
import XCTest
@testable import Loore

// A Voice conversation whose thread has write tool calls waiting for the user's
// approval (Peter, 2026-11-12): the dialog on the Voice screen after the reply's
// audio, no new turn (on screen, from the lock screen) until every call is
// decided, and nothing new for a thread with only the user's own text.

private func waitingCall(_ id: Int, turn: Int, tool: String = "update_artifact") -> ToolApproval {
    ToolApproval(id: id, nodeId: turn, turnNodeId: turn, tool: tool, kinds: [.otherUser],
                 title: "Change your Memory", kind: "memory", artifactTitle: "Memory")
}

/// The turn's state machine against the fakes of `VoiceTurnTests`.
@MainActor
final class VoiceApprovalTurnTests: XCTestCase {
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

    private func status(_ s: String, llm: Int? = nil) throws -> StreamingSessionStatus {
        var json = #"{"session_id":"sid-1","streaming_status":"\#(s)","content":"hello there","chunks":[]"#
        if let llm { json += #","llm_node_id":\#(llm)"# }
        return try decode(StreamingSessionStatus.self, json + "}")
    }

    private func llm(_ id: Int, _ status: String, content: String? = nil) throws -> LLMStatus {
        var json = #"{"node_id":\#(id),"status":"\#(status)","tts_streaming":false,"warnings":[],"tts_task_status":"pending""#
        if let content { json += #","content":"\#(content)""# }
        return try decode(LLMStatus.self, json + "}")
    }

    private var inits: Int { backend.log.filter { $0.hasPrefix("init") }.count }

    /// A recorded turn whose reply (node 101) is written, spoken and played to its end.
    private func runTurnToItsEnd() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "completed", content: "I noted it.")]
        turn.start()
        await wait("recording") { turn.state == .recording }
        turn.stop()
        await wait("attached") { backend.streams[101] != nil }
        backend.push(101, "chunk_ready", #"{"chunk_index":0,"audio_url":"/media/r0.mp3","duration":3}"#)
        await wait("playing") { turn.state == .playing }
        backend.push(101, "chunk_ready", #"{"chunk_index":1,"audio_url":"/media/r1.mp3","duration":4}"#)
        await wait("second chunk") { audio.fakeQueue.urls.count == 2 }
        backend.push(101, "all_complete", #"{"continuation_node_id":null,"preview":"I noted it."}"#)
        await wait("generated") { !audio.fakeQueue.generatingTTS }
    }

    private static let clip = "/api/voice/approval-announcement/one.mp3?v=k1"

    func testTheAnnouncementFollowsTheReplyAndThenTheDialogOpens() async throws {
        backend.approvalLists[101] = [waitingCall(31, turn: 101)]
        backend.announcements[101] = ApprovalAnnouncement(count: 1, url: Self.clip, duration: 4.5)
        try await runTurnToItsEnd()
        await wait("listed") { turn.approvalWaiting }
        XCTAssertEqual(backend.approvalRequests, [101], "asked once, when the reply was written")
        XCTAssertEqual(backend.announcementRequests, [101])
        // The reply's chunks, then the announcement as one more chunk of the same queue.
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/r0.mp3", "/media/r1.mp3", Self.clip])
        // Not a chapter: the reply's chapters stay as they were.
        XCTAssertEqual(audio.fakeQueue.chapters.map(\.0), [0])
        XCTAssertEqual(audio.fakeQueue.chapters.map(\.1), ["I noted it."])
        XCTAssertTrue(audio.events.contains("announcement 101 \(Self.clip)"))
        XCTAssertEqual(turn.state, .playing)
        XCTAssertFalse(turn.approvalDialogDue, "not over the reply while it plays (or is paused)")

        // The queue (announcement included) played to its end: the dialog.
        turn.queueFinished()
        XCTAssertEqual(turn.state, .done)
        XCTAssertTrue(turn.approvalDialogDue)
        turn.approvalDialogOpened()
        XCTAssertFalse(turn.approvalDialogDue)
        // Replaying the loaded queue while the change waits says it again.
        XCTAssertEqual(audio.fakeQueue.urls.last, Self.clip)
    }

    func testWithoutAClipTheReplyEndsAsBeforeAndTheDialogStillOpens() async throws {
        backend.approvalLists[101] = [waitingCall(31, turn: 101)]
        // The clip could not be made (no TTS key, a provider error).
        backend.announcements[101] = ApprovalAnnouncement(count: 1, url: nil)
        try await runTurnToItsEnd()
        await wait("listed") { turn.approvalWaiting }
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/r0.mp3", "/media/r1.mp3"])
        XCTAssertFalse(audio.events.contains { $0.hasPrefix("announcement") })
        turn.queueFinished()
        XCTAssertTrue(turn.approvalDialogDue)
    }

    func testASlowAnswerEndsTheQueueWithoutTheAnnouncement() async throws {
        turn.timings.announcementWait = 0.2
        backend.announcementDelay = 1
        backend.announcements[101] = ApprovalAnnouncement(count: 1, url: Self.clip, duration: 4.5)
        let started = Date()
        try await runTurnToItsEnd()
        XCTAssertLessThan(Date().timeIntervalSince(started), 0.9, "the queue did not wait for the answer")
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/r0.mp3", "/media/r1.mp3"])
        // The late answer adds nothing afterwards.
        try? await Task.sleep(nanoseconds: 1_000_000_000)
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/r0.mp3", "/media/r1.mp3"])
        XCTAssertEqual(backend.announcementRequests, [101])
    }

    func testAReplyWithNothingToSpeakStillSaysAChangeWaits() async throws {
        backend.statuses = [try status("completed", llm: 101)]
        backend.llmStatuses[101] = [try llm(101, "completed", content: "")]
        backend.announcements[101] = ApprovalAnnouncement(count: 1, url: Self.clip, duration: 4.5)
        turn.start()
        await wait("recording") { turn.state == .recording }
        turn.stop()
        await wait("announcement loaded") { audio.fakeQueue.urls == [Self.clip] }
        XCTAssertTrue(audio.fakeQueue.chapters.isEmpty)
        XCTAssertEqual(turn.state, .playing)
        await wait("generated") { !audio.fakeQueue.generatingTTS }
    }

    func testAThreadWithOnlyTheUsersTextGetsNoExtraChunk() async throws {
        try await runTurnToItsEnd()
        XCTAssertEqual(backend.announcementRequests, [101])
        XCTAssertEqual(audio.fakeQueue.urls, ["/media/r0.mp3", "/media/r1.mp3"])
    }

    func testRecordAndContinueAreRefusedWhileAChangeWaitsAndWorkAfter() async throws {
        backend.approvalLists[101] = [waitingCall(31, turn: 101), waitingCall(32, turn: 101, tool: "apply_todo_changes")]
        try await runTurnToItsEnd()
        await wait("listed") { turn.approvalWaiting }
        XCTAssertEqual(turn.newTurnRefusal, VoiceApproval.recordRefusal(count: 2))

        // Continue while the reply plays: refused, and the reply keeps playing.
        let stopped = audio.fakeQueue.stopped
        turn.continueConversation()
        XCTAssertEqual(turn.state, .playing)
        XCTAssertEqual(audio.fakeQueue.stopped, stopped)
        turn.queueFinished()
        turn.continueConversation()
        turn.start()
        XCTAssertEqual(turn.state, .done)
        XCTAssertEqual(inits, 1, "no new session was asked for")

        // One call decided: still refused.
        turn.approvalsChanged([waitingCall(32, turn: 101, tool: "apply_todo_changes")])
        turn.continueConversation()
        XCTAssertEqual(turn.state, .done)
        XCTAssertEqual(turn.newTurnRefusal, VoiceApproval.recordRefusal(count: 1))

        // Every call decided: the next turn starts.
        turn.approvalsChanged([])
        XCTAssertNil(turn.newTurnRefusal)
        XCTAssertFalse(turn.approvalDialogDue)
        turn.continueConversation()
        await wait("recording again") { turn.state == .recording }
        XCTAssertEqual(inits, 2)
    }

    func testAThreadOpenedWithChangesWaitingAsksAtOnce() async throws {
        backend.approvalLists[60] = [waitingCall(40, turn: 58)]
        turn.startNewConversation(parentId: 60)
        await wait("dialog due") { turn.approvalDialogDue }
        XCTAssertEqual(turn.phase, .ready)
        XCTAssertEqual(backend.approvalRequests, [60])
        turn.start()
        XCTAssertEqual(turn.state, .idle)
        XCTAssertEqual(inits, 0)

        // A new conversation elsewhere drops the list.
        turn.startNewConversation(parentId: nil)
        XCTAssertFalse(turn.approvalWaiting)
        XCTAssertFalse(turn.approvalDialogDue)
        XCTAssertEqual(backend.approvalRequests, [60], "a fresh conversation asks nothing")
    }

    func testTheServersRefusalShowsTheWaitingChangesNotAnError() async throws {
        backend.startResult = .failure(APIError.from(
            status: 409, contentType: "application/json",
            data: Data(#"{"error":"A change to your data waits for your approval.","code":"approval_pending","pending":1}"#.utf8)))
        backend.approvalLists[50] = [waitingCall(31, turn: 49)]
        turn.start()
        await wait("back to ready") { turn.state == .idle && turn.approvalWaiting }
        XCTAssertTrue(turn.approvalDialogDue)
        XCTAssertEqual(backend.approvalRequests, [50])
        XCTAssertTrue(notices.toasts.isEmpty, "the dialog says it, not a toast")
        XCTAssertFalse(turn.hasError)
        XCTAssertFalse(audio.events.contains("sound.error"))
        XCTAssertNil(turn.aiBlock)
        XCTAssertTrue(recorder.calls.isEmpty, "the microphone never started")
    }

    func testTheServersRefusalUnlistedSaysWhyInAToast() async throws {
        backend.startResult = .failure(APIError.from(
            status: 409, contentType: "application/json",
            data: Data(#"{"error":"A change to your data waits for your approval.","code":"approval_pending","pending":1}"#.utf8)))
        backend.approvalError = APIError.transport(code: -1009, description: "offline")
        turn.start()
        await wait("back to ready") { turn.state == .idle }
        XCTAssertEqual(notices.toasts, ["A change to your data waits for your approval."])
        XCTAssertFalse(turn.approvalWaiting)
        XCTAssertFalse(audio.events.contains("sound.error"))
    }

    func testAThreadWithOnlyTheUsersTextShowsNothing() async throws {
        try await runTurnToItsEnd()
        await wait("asked") { backend.approvalRequests == [101] }
        turn.queueFinished()
        XCTAssertFalse(turn.approvalWaiting)
        XCTAssertFalse(turn.approvalDialogDue)
        XCTAssertNil(turn.newTurnRefusal)
        // The app coming back asks nothing more.
        turn.appDidBecomeActive()
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(backend.approvalRequests, [101])
        turn.continueConversation()
        await wait("recording") { turn.state == .recording }
    }

    func testDecidedOnAnotherDeviceShowsWhenTheAppComesBack() async throws {
        backend.approvalLists[60] = [waitingCall(40, turn: 58)]
        turn.startNewConversation(parentId: 60)
        await wait("waiting") { turn.approvalWaiting }
        backend.approvalLists[60] = []
        turn.appDidBecomeActive()
        await wait("refreshed") { !turn.approvalWaiting }
        XCTAssertEqual(backend.approvalRequests, [60, 60])
        turn.start()
        await wait("recording") { turn.state == .recording }
    }

    func testTheLockScreenCardHasNoRecordWhileAChangeWaits() {
        typealias ContentState = VoiceActivityAttributes.ContentState
        for state in [VoiceTurnController.State.done, .playing, .idle] {
            let waiting = VoiceLiveActivity.state(turn: state, isPaused: false, isInterrupted: false,
                                                  awaitingNextNode: false, elapsed: 0, approvalWaiting: true)
            XCTAssertTrue(waiting.approvalWaiting, "\(state)")
            XCTAssertFalse(waiting.offersRecord, "\(state)")
            let free = VoiceLiveActivity.state(turn: state, isPaused: false, isInterrupted: false,
                                               awaitingNextNode: false, elapsed: 0)
            XCTAssertFalse(free.approvalWaiting)
            XCTAssertTrue(free.offersRecord, "\(state)")
        }
        // Recording and thinking have no Record button either way.
        let recording = VoiceLiveActivity.state(turn: .recording, isPaused: false, isInterrupted: false,
                                                awaitingNextNode: false, elapsed: 4, approvalWaiting: true)
        XCTAssertFalse(recording.approvalWaiting)
        XCTAssertFalse(recording.offersRecord)
        // The Glean button stays as it is.
        let glean = VoiceLiveActivity.state(turn: .done, isPaused: false, isInterrupted: false,
                                            awaitingNextNode: false, elapsed: 0, glean: .ready, approvalWaiting: true)
        XCTAssertEqual(glean.glean, .ready)
        XCTAssertEqual(ContentState.approvalWaitingLine, "Approve or reject the change in Loore")
    }

    func testTheWords() {
        XCTAssertEqual(VoiceApproval.waitingLine(count: 1), "A change to your data waits for your approval")
        XCTAssertEqual(VoiceApproval.waitingLine(count: 3), "3 changes to your data wait for your approval")
        XCTAssertEqual(VoiceApproval.recordRefusal(count: 1),
                       "A change to your data waits for your approval. Open Loore to approve or reject it before you record again.")
        XCTAssertEqual(VoiceApproval.recordRefusal(count: 2),
                       "2 changes to your data wait for your approval. Open Loore to approve or reject them before you record again.")
        XCTAssertTrue(VoiceApproval.isRefusal(APIError.server(status: 409, body: ServerErrorBody(code: "approval_pending"))))
        XCTAssertFalse(VoiceApproval.isRefusal(APIError.server(status: 403, body: ServerErrorBody(code: "ai_usage_none"))))
    }
}

/// The lock screen's Record and the Voice screen's dialog model, in the app.
@MainActor
final class VoiceApprovalAppTests: StubbedAppTestCase {
    private var backend: FakeVoiceBackend!
    private var turn: VoiceTurnController!

    override func setUp() async throws {
        try await super.setUp()
        backend = FakeVoiceBackend()
        let recorder = FakeRecorder()
        let audio = FakeAudio()
        recorder.audio = audio
        turn = VoiceTurnController(backend: backend, recorder: recorder, audio: audio, notices: FakeNotices(),
                                   parentId: nil)
    }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }

    func testLockScreenRecordIsRefusedWhileAChangeWaitsAndWorksOnceDecided() async throws {
        try XCTSkipUnless(NetworkStatus.shared.isOnline, "the lock screen's Record checks the connection first")
        backend.approvalLists[60] = [waitingCall(40, turn: 58)]
        turn.startNewConversation(parentId: 60)
        let listed = await eventually { self.turn.approvalWaiting }
        XCTAssertTrue(listed)

        let refusal = await app.audio.recordFromLockScreen(turn)
        XCTAssertEqual(refusal, VoiceApproval.recordRefusal(count: 1))
        XCTAssertEqual(turn.state, .idle, "no recording started")
        XCTAssertFalse(backend.log.contains { $0.hasPrefix("init") })
        XCTAssertEqual(backend.approvalRequests, [60, 60], "asked once more before refusing")

        // Approved or rejected on another device meanwhile: the same tap records.
        backend.approvalLists[60] = []
        let started = await app.audio.recordFromLockScreen(turn)
        XCTAssertNil(started)
        XCTAssertEqual(turn.state, .recording)
        XCTAssertTrue(backend.log.contains { $0.hasPrefix("init parent=60") })
        turn.tearDown()
    }

    func testTheVoiceDialogShowsEachReplyInTurnAndFreesRecordWhenAllAreDecided() async throws {
        let first = #"{"id":1,"node_id":70,"turn_node_id":70,"tool":"apply_share","title":"Save the proposed share as a private draft","proposal_text":"x"}"#
        let second = #"{"id":31,"node_id":77,"turn_node_id":77,"tool":"update_artifact","title":"Change your Memory","lines":[]}"#
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/tool-approvals": return .json(200, #"{"pending":[\#(first),\#(second)]}"#)
            case "/api/tool-approvals/1/reject": return .json(200, #"{"outcome":"rejected","pending":[]}"#)
            case "/api/tool-approvals/31/approve": return .json(200, #"{"outcome":"approved","pending":[]}"#)
            default: return .json(500, "{}")
            }
        }
        let voice = app.audio.voice
        await voice.refreshApprovals(nodeId: 77, openDialog: true)
        XCTAssertEqual(voice.waitingApprovals.map(\.id), [1, 31])
        XCTAssertTrue(voice.approvalDialogDue, "nothing plays: at once")
        let model = try XCTUnwrap(app.audio.voiceApprovals)
        XCTAssertEqual(model.nodeId, 77)
        XCTAssertEqual(model.pending.map(\.id), [1, 31])

        model.review()
        XCTAssertEqual(model.shownTurnId, 70, "the oldest reply first")
        await model.decide(model.pending[0], approve: false)
        XCTAssertEqual(model.shownTurnId, 77, "the next reply's calls follow, no Later")
        XCTAssertEqual(voice.waitingApprovals.map(\.id), [31])
        XCTAssertNotNil(voice.newTurnRefusal)

        await model.decide(model.pending[0], approve: true)
        XCTAssertNil(model.shownTurnId)
        XCTAssertTrue(voice.waitingApprovals.isEmpty)
        XCTAssertNil(voice.newTurnRefusal, "Record (and the lock screen's) is offered again")
        XCTAssertEqual(app.signals.artifactsChanged, 1)
    }

    func testADecisionOnTheThreadScreenFreesTheVoiceConversation() async throws {
        let item = #"{"id":31,"node_id":77,"turn_node_id":77,"tool":"update_artifact","title":"Change your Memory","lines":[]}"#
        var decided = false
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/tool-approvals": return .json(200, decided ? #"{"pending":[]}"# : #"{"pending":[\#(item)]}"#)
            case "/api/tool-approvals/31/reject":
                decided = true
                return .json(200, #"{"outcome":"rejected","pending":[]}"#)
            default: return .json(500, "{}")
            }
        }
        let voice = app.audio.voice
        await voice.refreshApprovals(nodeId: 77, openDialog: false)
        XCTAssertTrue(voice.approvalWaiting)

        let thread = ThreadModel(nodeId: 77, awaitLLM: nil, app: app)
        thread.approvals.update(voice.waitingApprovals, autoOpen: false)
        thread.approvals.review()
        await thread.approvals.decide(voice.waitingApprovals[0], approve: false)
        let freed = await eventually { !voice.approvalWaiting }
        XCTAssertTrue(freed)
    }

    /// The real Voice screen against a stubbed server, drawn in a window: opened
    /// on a thread with a change waiting, the dialog comes by itself (no
    /// "Later"); without it, the record button is off with the waiting line;
    /// once the change is decided, the button is back. With
    /// `TEST_RUNNER_LOORE_SNAPSHOT_DIR` set the three states are written as PNGs.
    func testTheVoiceScreenShowsTheDialogThenFreesRecord() async throws {
        let item = #"{"id":31,"node_id":58,"turn_node_id":58,"tool":"update_artifact","title":"Change your Memory","kinds":["other_user"],"kind":"memory","artifact_title":"Memory","base_version":3,"current_version":3,"lines":[{"type":"same","text":"Rivers"},{"type":"del","text":"I walk alone."},{"type":"add","text":"I trust Anna's judgement."}]}"#
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/voice/availability": return .json(200, #"{"allowed":true}"#)
            case "/api/drafts/interrupted": return .json(200, "[]")
            case "/api/tool-approvals": return .json(200, #"{"pending":[\#(item)]}"#)
            case "/api/tool-approvals/31/reject": return .json(200, #"{"outcome":"rejected","pending":[]}"#)
            default: return .json(404, #"{"error":"Not found"}"#)
            }
        }
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = scene.screen.bounds
        window.overrideUserInterfaceStyle = .dark
        window.rootViewController = UIHostingController(rootView: VoiceView(parentId: 60, resumeLLMId: nil)
            .environment(app))
        window.isHidden = false
        defer { window.isHidden = true }

        // Suspends (the screen's tasks run on the main actor meanwhile).
        func settle(_ seconds: Double = 0.8) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9)) }
        func snapshot(_ name: String) throws {
            let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
                _ = window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
            }
            if let dir = ProcessInfo.processInfo.environment["LOORE_SNAPSHOT_DIR"], !dir.isEmpty {
                try image.pngData()?.write(to: URL(fileURLWithPath: dir).appendingPathComponent("voice-approval-\(name).png"))
            }
        }

        let voice = app.audio.voice
        for _ in 0..<20 where app.audio.voiceApprovals?.shownTurnId == nil { await settle(0.2) }
        XCTAssertEqual(voice.waitingApprovals.map(\.id), [31])
        XCTAssertEqual(app.audio.voiceApprovals?.shownTurnId, 58, "the dialog opened by itself")
        XCTAssertNotNil(voice.newTurnRefusal)
        XCTAssertTrue(calls.contains("GET /api/tool-approvals"))
        await settle()
        try snapshot("1-dialog")

        // As after "Open the thread": the dialog closed, nothing decided.
        app.audio.voiceApprovals?.later()
        await settle()
        try snapshot("2-waiting-line")
        XCTAssertTrue(voice.approvalWaiting)

        let model = try XCTUnwrap(app.audio.voiceApprovals)
        await model.decide(model.pending[0], approve: false)
        await settle()
        try snapshot("3-decided")
        XCTAssertFalse(voice.approvalWaiting)
        XCTAssertNil(voice.newTurnRefusal)
        voice.tearDown()
    }

    // MARK: The announcement in the shared player (no network: file URLs)

    /// Silent local files of the given lengths, so the player loads them without
    /// any network and keeps their durations (a missing file would fail and be
    /// counted as 0 s).
    private static let reply = silence("reply-77", seconds: 6)
    private static let clip = silence("approval-announcement-one", seconds: 4.5)

    private static func silence(_ name: String, seconds: Double) -> String {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("loore-tests-\(name).wav")
        if !FileManager.default.fileExists(atPath: url.path) {
            let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 8000, channels: 1, interleaved: true)!
            let frames = AVAudioFrameCount(seconds * 8000)
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
            buffer.frameLength = frames
            if let file = try? AVAudioFile(forWriting: url, settings: format.settings, commonFormat: .pcmFormatInt16,
                                           interleaved: true) {
                try? file.write(from: buffer)
            }
        }
        return url.absoluteString
    }

    /// The Voice queue as a turn leaves it: the reply's chunk (a chapter) and the
    /// announcement after it.
    private func loadVoiceQueue() {
        app.audio.player.load(urls: [Self.reply, Self.clip], durations: [6, 4.5], title: "Voice", source: .voice,
                              chapters: [QueueChapter(title: "I noted it.", chunkIndex: 0, startTime: 0)],
                              autoplay: false)
        app.audio.announcementQueued(nodeId: 77, url: Self.clip)
    }

    private var queued: [String] { app.audio.player.entries.map(\.url.absoluteString) }

    func testApprovedOrRejectedTheAnnouncementLeavesTheQueue() async throws {
        let item = #"{"id":31,"node_id":77,"turn_node_id":77,"tool":"update_artifact","title":"Change your Memory","lines":[]}"#
        var waiting = 1
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/tool-approvals": return .json(200, #"{"pending":[\#(item)]}"#)
            case "/api/tool-approvals/31/reject":
                waiting = 0
                return .json(200, #"{"outcome":"rejected","pending":[]}"#)
            case "/api/voice/approval-announcement":
                return .json(200, waiting > 0 ? #"{"count":1,"url":"\#(Self.clip)","duration":4.5}"#
                                               : #"{"count":0,"url":null,"duration":null}"#)
            default: return .json(500, "{}")
            }
        }
        loadVoiceQueue()
        XCTAssertEqual(queued, [Self.reply, Self.clip])
        let voice = app.audio.voice
        await voice.refreshApprovals(nodeId: 77, openDialog: false)

        // Asked again while the change still waits (the app came back): it stays.
        app.audio.recheckQueuedAnnouncement()
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(queued, [Self.reply, Self.clip], "replay while it waits says it again")

        let model = try XCTUnwrap(app.audio.voiceApprovals)
        model.review()
        await model.decide(model.pending[0], approve: false)
        let dropped = await eventually { self.queued == [Self.reply] }
        XCTAssertTrue(dropped, "the next replay has no announcement")
        XCTAssertEqual(app.audio.player.chapters, [QueueChapter(title: "I noted it.", chunkIndex: 0, startTime: 0)])
        XCTAssertEqual(app.audio.player.totalDuration, 6, accuracy: 0.01)
        XCTAssertNil(app.audio.queuedAnnouncement)
    }

    func testTheSpeakerOfAReplyGetsTheAnnouncementAndLosesItOnceDecidedOnTheThread() async throws {
        let item = #"{"id":31,"node_id":77,"turn_node_id":77,"tool":"update_artifact","title":"Change your Memory","lines":[]}"#
        var waiting = 1
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/tool-approvals/31/approve":
                waiting = 0
                return .json(200, #"{"outcome":"approved","pending":[]}"#)
            case "/api/voice/approval-announcement" where request.url?.query == "node=77":
                return .json(200, waiting > 0 ? #"{"count":1,"url":"\#(Self.clip)","duration":4.5}"#
                                               : #"{"count":0,"url":null,"duration":null}"#)
            default: return .json(500, "{}")
            }
        }
        // The thread screen's speaker loaded the reply's spoken audio.
        app.audio.player.load(urls: [Self.reply], durations: [6], title: "Reply", source: .node(77), autoplay: false)
        await app.audio.appendApprovalAnnouncement(nodeId: 77)
        XCTAssertEqual(queued, [Self.reply, Self.clip])
        XCTAssertTrue(app.audio.player.chapters.isEmpty)

        let thread = ThreadModel(nodeId: 77, awaitLLM: nil, app: app)
        let call = try decode(ToolApproval.self, item)
        thread.approvals.update([call], autoOpen: false)
        thread.approvals.review()
        await thread.approvals.decide(call, approve: true)
        let dropped = await eventually { self.queued == [Self.reply] }
        XCTAssertTrue(dropped)

        // Loaded again with nothing waiting: no extra chunk.
        app.audio.player.load(urls: [Self.reply], durations: [6], title: "Reply", source: .node(77), autoplay: false)
        await app.audio.appendApprovalAnnouncement(nodeId: 77)
        XCTAssertEqual(queued, [Self.reply])
    }

    func testDropTrailingTakesOffOnlyThatLastChunk() {
        let player = app.audio.player
        player.load(urls: [Self.reply, Self.clip], durations: [6, 4.5], title: "Voice", source: .voice,
                    chapters: [QueueChapter(title: "I noted it.", chunkIndex: 0, startTime: 0)], autoplay: false)
        player.dropTrailing(url: Self.reply)
        XCTAssertEqual(queued, [Self.reply, Self.clip], "not the last chunk: kept")
        player.seek(to: 10.5)  // the end: as after the queue played through
        player.dropTrailing(url: Self.clip)
        XCTAssertEqual(queued, [Self.reply])
        XCTAssertEqual(player.cumulativeTime, 6, accuracy: 0.01)
        XCTAssertTrue(player.atEnd, "play then starts over, without the announcement")
        XCTAssertEqual(player.chapters.count, 1)
    }

    func testAnAnnouncementThatIsPlayingFinishesBeforeItGoes() async throws {
        let reply = Self.silence("short-reply", seconds: 0.3)
        let clip = Self.silence("short-clip", seconds: 0.8)
        let player = app.audio.player
        player.load(urls: [reply, clip], durations: [0.3, 0.8], title: "Reply", source: .node(5), autoplay: true)
        let inClip = await eventually { player.currentIndex == 1 && player.isPlaying }
        try XCTSkipUnless(inClip, "no audio playback in this simulator")
        player.dropTrailing(url: clip)
        XCTAssertEqual(player.entries.count, 2, "it is not cut off")
        let gone = await eventually { player.entries.count == 1 }
        XCTAssertTrue(gone, "gone once it has played")
        XCTAssertTrue(player.atEnd)
        player.close()
    }

    func testOwnOnlyThreadsCostNoRequestOnReturn() async throws {
        StubURLProtocol.install { _ in .json(500, "{}") }
        app.audio.toolApprovalsDecidedElsewhere()
        await app.audio.voice.refreshApprovalsIfWaiting(openDialog: true)
        try? await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertTrue(calls.isEmpty)
    }
}
