import Foundation
import Observation
import os

/// One voice conversation (the native `useVoiceSession`): recording → upload →
/// finalize → transcript → reply node → TTS chunks → queue, turn after turn.
///
/// States follow map C §8.5; the Voice screen reads `phase` (the web's four).
/// Audio never stops between the record tap and the end of the reply: the
/// thinking cue starts before the mic stops and whenever the queue drains
/// while TTS is still generating (design doc §9.4), so the app keeps running
/// with the phone locked.
///
/// Every async continuation checks `generation`: cancel and continue bump it,
/// so late answers from an abandoned turn change nothing.
@MainActor
@Observable
final class VoiceTurnController {
    enum State: Equatable {
        case idle
        case starting
        case recording
        case stopping
        case transcribing
        case awaitingAudio
        case playing
        /// The queue ran out while more audio is being generated.
        case draining
        case done
    }

    /// The web Voice page's phases.
    enum Phase: Equatable { case ready, recording, processing, playback }

    // MARK: Observable state

    private(set) var state: State = .idle
    private(set) var isPaused = false
    private(set) var isInterrupted = false
    /// Red dot on the ready screen, cleared after 3 s (web `hasError`).
    private(set) var hasError = false
    private(set) var elapsed: Double = 0
    /// Final reply of the last turn (for the proposal card and "Text Mode").
    private(set) var lastReplyNodeId: Int?
    private(set) var replyContent: String?
    private(set) var toolCallsMeta: [ToolCallMeta]?
    private(set) var threadParentId: Int?
    /// Between an interim node's audio and its continuation's first chunk.
    private(set) var awaitingNextNode = false

    var phase: Phase {
        switch state {
        case .idle: return .ready
        case .starting, .recording, .stopping: return .recording
        case .transcribing, .awaitingAudio: return .processing
        case .draining: return awaitingNextNode ? .processing : .playback
        case .playing, .done: return .playback
        }
    }

    var isStopping: Bool { state == .stopping }
    var isActive: Bool { state != .idle && state != .done }

    // MARK: Configuration

    @ObservationIgnored var model: () -> String? = { nil }
    @ObservationIgnored var aiUsage: () -> String = { "none" }
    /// Intervals (tests shorten them).
    @ObservationIgnored var timings = Timings()

    struct Timings {
        var statusPoll: Double = 1
        var statusGiveUp: Double = 15 * 60
        var llmPoll: Double = 1.5
        var llmGiveUp: Double = 30 * 60
        var reconcile: Double = 7
        var triggerWatchdog: Double = 20
        var catchUpGrace: Double = 3
        var errorDot: Double = 3
        var tick: Double = 0.25
        var longRecording: Double = 59 * 60
        /// No llm-status answer for this long ends the turn (M12; heuristic).
        var llmErrorGiveUp: Double = 2 * 60
        /// Most thinking-cue time per turn; after it the wait is silent (M12; heuristic).
        var cueCap: Double = 5 * 60
    }

    // MARK: Dependencies

    @ObservationIgnored let backend: VoiceBackend
    @ObservationIgnored let recorder: VoiceRecording
    @ObservationIgnored let audio: VoiceAudio
    @ObservationIgnored let notices: VoiceNotices
    @ObservationIgnored private let log = Logger(subsystem: "org.loore.app", category: "voice")

    // MARK: Per-turn bookkeeping

    private struct NodeTrack {
        var completed = false
        var content: String?
        var continuation: Int?
        var attached = false
        var allComplete = false
        var sseDelivered = false
        var restDelivered = false
        var hadAudio = false
        var highestChunk: Int?
        var provisionalChunk: Int?
        var ttsAttemptAt: Date?
        var ttsCompletedSeenAt: Date?
    }

    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var sessionId: String?
    @ObservationIgnored private var currentNodeId: Int?
    @ObservationIgnored private var nodes: [Int: NodeTrack] = [:]
    @ObservationIgnored private var receivedChunks: Set<String> = []
    @ObservationIgnored private var turnHasAudio = false
    @ObservationIgnored private var initialResume = false
    @ObservationIgnored private var toastedWarnings: Set<String> = []
    @ObservationIgnored private var failureToasted: Set<Int> = []
    @ObservationIgnored private var interruptionToast: Int?
    @ObservationIgnored private var longRecordingWarned = false
    @ObservationIgnored private var flowTask: Task<Void, Never>?
    @ObservationIgnored private var tickTask: Task<Void, Never>?
    @ObservationIgnored private var llmTask: Task<Void, Never>?
    @ObservationIgnored private var sseTask: Task<Void, Never>?
    @ObservationIgnored private var reconcileTask: Task<Void, Never>?
    @ObservationIgnored private var errorDotTask: Task<Void, Never>?
    @ObservationIgnored private var cueCapTask: Task<Void, Never>?
    /// When the cue started (nil while off) and the cue time this turn used up.
    @ObservationIgnored private var cueSince: Date?
    @ObservationIgnored private var cueUsed: Double = 0
    @ObservationIgnored let timing: VoiceTiming

    init(backend: VoiceBackend, recorder: VoiceRecording, audio: VoiceAudio, notices: VoiceNotices,
         parentId: Int? = nil) {
        self.backend = backend
        self.recorder = recorder
        self.audio = audio
        self.notices = notices
        threadParentId = parentId
        timing = VoiceTiming(backend: backend)
        recorder.onSourceEnded = { [weak self] in self?.stop() }
        recorder.onFatal = { [weak self] message in self?.uploadFailedFatally(message) }
    }

    // MARK: Record

    /// The Voice screen opened with `?parent=`: the next recording replies there.
    func setThreadParent(_ id: Int?) {
        guard state == .idle || state == .done else { return }
        threadParentId = id
    }

    /// The proposal card changed the reply's text (it saves the node itself).
    func setReplyContent(_ content: String) {
        replyContent = content
    }

    /// Keeps `tool_calls_meta` in step after an accept on the proposal card.
    func updateToolMeta(_ name: String, _ updates: [String: JSONValue]) {
        guard var meta = toolCallsMeta else { return }
        for i in meta.indices where meta[i].name == name {
            meta[i].raw.merge(updates) { _, new in new }
        }
        toolCallsMeta = meta
    }

    /// Record tap (web `handleStart`). The caller checks the spend cap first.
    func start() {
        guard state == .idle || state == .done else { return }
        beginRecording(resume: nil)
    }

    /// Record button in playback (web `handleContinue`): new turn, same thread.
    func continueConversation() {
        guard phase == .playback else { return }
        timing.endTurn()
        resetTurn(keepQueue: false)
        beginRecording(resume: nil)
    }

    /// Recovery banner "Continue recording" (web `handleResumeSession`).
    func resumeInterrupted(_ draft: InterruptedDraft) {
        guard state == .idle || state == .done else { return }
        if let mime = draft.streamingMimeType, mime != "audio/mp4" {
            notices.toast("This recording was started in a browser that records in another format, so it can only be continued there.", duration: 8)
            return
        }
        if let parent = draft.parentId { threadParentId = parent }
        beginRecording(resume: draft)
    }

    private func beginRecording(resume draft: InterruptedDraft?) {
        generation += 1
        let gen = generation
        state = .starting
        hasError = false
        isPaused = false
        isInterrupted = false
        longRecordingWarned = false
        elapsed = draft.map { Double($0.chunkCount) * 15 } ?? 0
        audio.refreshNowPlaying()
        do {
            try audio.activateForRecording()
        } catch {
            failStart("Could not start recording. Please try again.")
            return
        }
        flowTask = Task { [weak self] in
            guard let self else { return }
            let sid: String
            if let draft {
                sid = draft.sessionId
            } else {
                do {
                    sid = try await self.backend.startSession(parentId: self.threadParentId,
                                                              aiUsage: self.aiUsage()).sessionId
                } catch {
                    guard gen == self.generation else { return }
                    if SpendCap.isSpendCapError(error) {
                        self.notices.spendCapped()
                        self.notices.toast(SpendCap.toastMessage(.record), duration: 8)
                        self.state = .idle
                        self.audio.deactivate()
                        self.audio.refreshNowPlaying()
                    } else {
                        self.failStart((error as? APIError)?.userMessage(fallback: "Could not start recording. Please try again.")
                                       ?? "Could not start recording. Please try again.")
                    }
                    return
                }
            }
            guard gen == self.generation else { return }
            self.sessionId = sid
            do {
                try self.recorder.start(sessionId: sid, uploadURL: self.backend.uploadURL(sessionId: sid),
                                        firstChunkIndex: draft?.chunkCount ?? 0,
                                        elapsedOffset: draft.map { Double($0.chunkCount) * 15 } ?? 0)
            } catch {
                if draft == nil { await self.backend.discard(sessionId: sid) }
                guard gen == self.generation else { return }
                self.failStart("The microphone could not start. Close other apps using it (calls, voice memos) and try again.")
                return
            }
            self.state = .recording
            self.startTicker(gen)
            self.audio.refreshNowPlaying()
        }
    }

    private func failStart(_ message: String) {
        notices.toast(message, duration: 8)
        audio.deactivate()
        state = .idle
        flagError()
        audio.refreshNowPlaying()
    }

    /// Pause (lock screen or on-screen): the segment so far is flushed and uploaded.
    func pauseRecording() {
        guard state == .recording, !isPaused else { return }
        recorder.pause()
        isPaused = true
        audio.refreshNowPlaying()
    }

    /// Resume after a pause or an interruption (web `handleResumeRecording`).
    func resumeRecording() {
        guard state == .recording, isPaused else { return }
        do {
            if isInterrupted { try audio.reactivate() }
            try recorder.resume()
        } catch {
            log.error("resume failed")
            notices.toast("The microphone could not restart. Unlock your phone and press play again.", duration: 8)
            return
        }
        isPaused = false
        endInterruption()
        audio.refreshNowPlaying()
    }

    /// The OS took the microphone (call, Siri): flush, hold, tell the user (#245).
    func systemInterruptionBegan() {
        switch state {
        case .recording, .starting:
            guard !isInterrupted else { return }
            recorder.interrupt()
            isPaused = true
            isInterrupted = true
            audio.playInterruptionAlert()
            if let id = interruptionToast { notices.dismissToast(id) }
            interruptionToast = notices.toast(
                "Recording paused — another app took the microphone (phone call?). Everything up to the interruption is saved. Press Resume to continue.",
                duration: 24 * 60 * 60)
            notices.notify(.recordingPaused)
            audio.refreshNowPlaying()
        default:
            break
        }
    }

    /// The interruption is over. No automatic resume (web parity); the chime
    /// again, since app audio is often not routed during a call.
    func systemInterruptionEnded() {
        if state == .recording && isInterrupted {
            audio.playInterruptionAlert()
        } else if state == .awaitingAudio || state == .transcribing || state == .stopping {
            cueOn()
        } else if state == .draining && audio.queue.isPlaying {
            // Not when the reply is paused (M12); a resumed player restarts it.
            cueOn()
        }
    }

    private func endInterruption() {
        isInterrupted = false
        if let id = interruptionToast { notices.dismissToast(id) }
        interruptionToast = nil
        notices.withdraw(.recordingPaused)
    }

    private func startTicker(_ gen: Int) {
        tickTask?.cancel()
        tickTask = Task { [weak self] in
            var lastWhole = -1
            while let self, !Task.isCancelled, gen == self.generation, self.state == .recording || self.state == .starting {
                self.elapsed = self.recorder.elapsed
                if !self.longRecordingWarned && self.elapsed >= self.timings.longRecording {
                    self.longRecordingWarned = true
                    self.audio.playLongRecordingWarning()
                    self.notices.toast("You’ve been recording for 59 minutes — consider stopping soon and continuing in a new recording.", duration: 10)
                    self.notices.notify(.longRecording)
                }
                let whole = Int(self.elapsed)
                if whole != lastWhole {
                    lastWhole = whole
                    self.audio.refreshNowPlaying()
                }
                try? await Task.sleep(nanoseconds: UInt64(self.timings.tick * 1_000_000_000))
            }
        }
    }

    // MARK: Stop → finalize → transcript

    /// Stop tap, lock-screen "next track" while recording, or the debug file's end
    /// (web `handleStop`).
    func stop() {
        guard state == .recording, let sid = sessionId else { return }
        let gen = generation
        timing.startTurn()
        state = .stopping
        endInterruption()
        // The cue first, so audio never stops between the mic and the reply.
        cueUsed = 0
        cueOn()
        audio.refreshNowPlaying()
        tickTask?.cancel()
        flowTask = Task { [weak self] in
            guard let self else { return }
            let outcome = await self.recorder.stop()
            self.audio.switchToPlayback()
            guard gen == self.generation else { return }
            self.elapsed = self.recorder.elapsed
            if let fatal = outcome.fatalMessage {
                self.endTurnWithError(fatal, sound: true)
                return
            }
            if outcome.produced == 0 && outcome.prior == 0 {
                await self.backend.discard(sessionId: sid)
                self.recorder.forget(sessionId: sid)
                guard gen == self.generation else { return }
                self.finishQuietly()
                return
            }
            if !outcome.failed.isEmpty {
                // Finalizing now would leave those chunks out of the transcript (B1).
                // They stay queued on the phone; the draft stays on the server.
                self.endTurnWithError(Self.missingChunksMessage, sound: true)
                return
            }
            do {
                try await self.backend.finalize(sessionId: sid, totalChunks: outcome.totalForFinalize,
                                                parentId: self.threadParentId, model: self.model())
            } catch let error as APIError where Self.isAlreadyFinalizing(error) {
                // A retried finalize whose first answer was lost (C §8.1).
            } catch {
                guard gen == self.generation else { return }
                self.endTurnWithError("Your recording could not be sent. It is kept on the server: open Voice again to continue or discard it.", sound: true)
                return
            }
            guard gen == self.generation else { return }
            self.timing.mark("finalize_acked")
            self.state = .transcribing
            self.audio.refreshNowPlaying()
            await self.waitForTranscript(sid, gen)
        }
    }

    /// Stop found chunks the server does not have (no connection): nothing is finalized.
    static let missingChunksMessage = "Part of your recording hasn't reached Loore yet (no connection?). It is kept on this phone and uploads when you're back online: then open Voice to continue or discard it."

    static func isAlreadyFinalizing(_ error: APIError) -> Bool {
        error.status == 400 && (error.userMessage(fallback: "").contains("not in recording state"))
    }

    private func uploadFailedFatally(_ message: String) {
        guard state == .recording || state == .starting else { return }
        generation += 1
        recorder.cancel()
        endTurnWithError(message, sound: true)
    }

    /// Polls `/status` (~1 s) after finalize; never the draft SSE in voice mode
    /// (it deletes the draft at `all_complete`, C §10.3).
    private func waitForTranscript(_ sid: String, _ gen: Int) async {
        let deadline = Date().addingTimeInterval(timings.statusGiveUp)
        var notFound = 0
        while !Task.isCancelled, gen == generation, Date() < deadline {
            do {
                let status = try await backend.sessionStatus(sessionId: sid)
                guard gen == generation else { return }
                switch status.streamingStatus {
                case .completed?:
                    recorder.forget(sessionId: sid)
                    await transcriptReady(status, sid, gen)
                    return
                case .failed?:
                    recorder.forget(sessionId: sid)
                    endTurnWithError("Transcription failed. Please try again.", sound: true)
                    return
                default:
                    break
                }
            } catch let error as APIError where error.status == 404 {
                notFound += 1
                if notFound >= 3 {
                    guard gen == generation else { return }
                    endTurnWithError("Transcription failed. Please try again.", sound: true)
                    return
                }
            } catch {
                // Transient: poll again.
            }
            try? await Task.sleep(nanoseconds: UInt64(timings.statusPoll * 1_000_000_000))
        }
        if gen == generation, !Task.isCancelled {
            endTurnWithError("Transcription is taking too long. Your recording is saved; try again in a moment.", sound: false)
        }
    }

    private func transcriptReady(_ status: StreamingSessionStatus, _ sid: String, _ gen: Int) async {
        if status.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            finishQuietly()
            return
        }
        if let warning = status.warning {
            notices.toast(warning, duration: 8)
            finishQuietly()
            return
        }
        if let nodeId = status.llmNodeId {
            timing.setTurnNode(nodeId)
            timing.mark("llm_node_known")
            beginReply(nodeId, gen)
            return
        }
        // Legacy path: the server chain created no reply node.
        do {
            let answer = try await backend.legacyVoice(content: status.content, model: model(), aiUsage: aiUsage(),
                                                       parentId: threadParentId, sessionId: sid)
            guard gen == generation else { return }
            timing.setTurnNode(answer.llmNodeId)
            timing.mark("llm_node_known")
            beginReply(answer.llmNodeId, gen)
        } catch {
            guard gen == generation else { return }
            if let apiError = error as? APIError, apiError.status == 400 {
                endTurnWithError(apiError.userMessage(fallback: "Could not start the reply."), sound: false)
            } else {
                endTurnWithError(nil, sound: false)
            }
        }
    }

    // MARK: Reply

    /// Deep link `/voice?resume=<llm id>&parent=<id>`: start in the thinking
    /// phase for an existing reply node (its TTS stream replays a completed node).
    func resumeReply(nodeId: Int, parentId: Int?) {
        guard state == .idle || state == .done else { return }
        generation += 1
        initialResume = true
        threadParentId = parentId
        audio.activateForReply()
        cueUsed = 0
        cueOn()
        beginReply(nodeId, generation)
    }

    private func beginReply(_ nodeId: Int, _ gen: Int) {
        state = .awaitingAudio
        currentNodeId = nodeId
        nodes[nodeId] = NodeTrack()
        audio.refreshNowPlaying()
        pollReply(nodeId, gen)
        startReconcile(gen)
    }

    /// llm-status every 1.5 s (the source of truth), with the TTS attach rule (C §8.1).
    private func pollReply(_ nodeId: Int, _ gen: Int) {
        llmTask?.cancel()
        llmTask = Task { [weak self] in
            guard let self else { return }
            let deadline = Date().addingTimeInterval(self.timings.llmGiveUp)
            var lastAnswer = Date()
            while !Task.isCancelled, gen == self.generation, self.currentNodeId == nodeId, Date() < deadline {
                do {
                    let status = try await self.backend.llmStatus(nodeId: nodeId)
                    guard gen == self.generation, self.currentNodeId == nodeId, !Task.isCancelled else { return }
                    lastAnswer = Date()
                    if await self.handleLLMStatus(status, nodeId, gen) { return }
                } catch {
                    guard gen == self.generation, self.currentNodeId == nodeId, !Task.isCancelled else { return }
                    if Date().timeIntervalSince(lastAnswer) > self.timings.llmErrorGiveUp {
                        self.giveUpOnReply("Can't reach Loore to get the reply. It will be in the thread once it's ready.")
                        return
                    }
                }
                try? await Task.sleep(nanoseconds: UInt64(self.timings.llmPoll * 1_000_000_000))
            }
            guard !Task.isCancelled, gen == self.generation, self.currentNodeId == nodeId else { return }
            self.giveUpOnReply("The reply is taking too long. It will be in the thread once it's ready.")
        }
    }

    /// llm-status kept failing or the reply never finished: end the turn instead
    /// of thinking (and cueing) forever (M12). Audio already queued stays playable.
    private func giveUpOnReply(_ message: String) {
        log.error("giving up on the reply")
        closeStream()
        if turnHasAudio {
            notices.toast(message, duration: 8)
            flagError()
            finishGenerating()
        } else {
            endTurnWithError(message, sound: false)
        }
    }

    /// Returns true when polling for this node can stop.
    private func handleLLMStatus(_ status: LLMStatus, _ nodeId: Int, _ gen: Int) async -> Bool {
        guard var track = nodes[nodeId] else { return true }
        let terminal = [TaskStatus.completed, .failed, .cancelled].contains(status.status ?? .pending)
        // Attach as soon as TTS is pending/processing (the server chain sets
        // pending at node creation) or the reply is spoken while written.
        if !track.attached && !terminal &&
            (status.ttsStreaming || status.ttsTaskStatus == .pending || status.ttsTaskStatus == .processing) {
            attachTTS(nodeId, gen)
            track = nodes[nodeId] ?? track
        }
        switch status.status {
        case .completed?:
            track.completed = true
            track.content = status.content ?? ""
            track.continuation = status.continuationNodeId
            nodes[nodeId] = track
            toastWarnings(status.warnings)
            if status.continuationNodeId == nil {
                lastReplyNodeId = nodeId
                replyContent = status.content ?? ""
                toolCallsMeta = status.toolCallsMeta
                if initialResume {
                    initialResume = false
                } else {
                    threadParentId = nodeId
                }
            }
            let content = (status.content ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if content.isEmpty {
                // Nothing to speak (e.g. only a proposal card): end any chain.
                if status.continuationNodeId == nil {
                    closeStream()
                    finishGenerating()
                } else {
                    advanceChain(to: status.continuationNodeId!, gen)
                }
                return true
            }
            // A reply spoken while written already streams its audio: no POST /tts
            // (web useVoiceSession skips it for streamed nodes, M9).
            if !track.allComplete && !track.restDelivered && !(track.attached && status.ttsStreaming) {
                await triggerTTS(nodeId, gen)
            }
            return true
        case .failed?:
            if !failureToasted.contains(nodeId) {
                failureToasted.insert(nodeId)
                notices.toast(status.error ?? "Response generation failed", duration: 8)
            }
            closeStream()
            endTurnWithError(nil, sound: false)
            return true
        case .cancelled?:
            // Terminal (the web ignores it and stays on "Thinking…", C §10.13).
            closeStream()
            if turnHasAudio {
                finishGenerating()
            } else {
                endTurnWithError(nil, sound: false)
            }
            return true
        default:
            return false
        }
    }

    /// `POST /tts` once when the node completes without `all_complete` (idempotent).
    private func triggerTTS(_ nodeId: Int, _ gen: Int) async {
        nodes[nodeId]?.ttsAttemptAt = Date()
        do {
            let outcome = try await backend.requestTTS(nodeId: nodeId)
            guard gen == generation, currentNodeId == nodeId else { return }
            switch outcome {
            case .ready(let url):
                guard let track = nodes[nodeId], !track.restDelivered, !track.allComplete else { return }
                if track.sseDelivered {
                    // Part of the audio already came as chunks: the whole file would
                    // repeat it. Reconnect; the stream replays the rest and all_complete (M9).
                    nodes[nodeId]?.attached = false
                    attachTTS(nodeId, gen)
                } else {
                    deliverFull(nodeId, url: url, gen)
                }
            case .started:
                if nodes[nodeId]?.attached != true { attachTTS(nodeId, gen) }
            }
        } catch let error as APIError where error.status == nil {
            // Lost without an answer: the reconcile watchdog re-fires it.
            nodes[nodeId]?.ttsAttemptAt = Date()
        } catch {
            guard gen == generation, currentNodeId == nodeId else { return }
            if nodes[nodeId]?.attached == true {
                // The stream is delivering this reply (e.g. a 402 after the cap was
                // crossed mid-turn): keep it; reconcile handles a dead stream (M9).
                log.info("POST /tts failed while the stream is attached; keeping the stream")
                return
            }
            closeStream()
            endTurnWithError(nil, sound: false)
        }
    }

    private func attachTTS(_ nodeId: Int, _ gen: Int) {
        guard nodes[nodeId] != nil else { return }
        nodes[nodeId]?.attached = true
        if nodes[nodeId]?.ttsAttemptAt == nil { nodes[nodeId]?.ttsAttemptAt = Date() }
        timing.mark("tts_attach")
        sseTask?.cancel()
        let lastChunk = nodes[nodeId]?.highestChunk
        let stream = backend.ttsStream(nodeId: nodeId, lastChunk: lastChunk)
        sseTask = Task { [weak self] in
            do {
                for try await message in stream {
                    guard let self, gen == self.generation, self.currentNodeId == nodeId, !Task.isCancelled else { return }
                    switch message {
                    case .response(let status, let body):
                        self.handleStreamJSON(nodeId, status: status, body: body, gen)
                        return
                    case .event(let event):
                        switch TTSStreamEvent(event) {
                        case .chunkReady(let chunk):
                            self.handleChunk(nodeId, chunk, gen)
                        case .allComplete(let complete):
                            self.handleAllComplete(nodeId, complete, gen)
                            return
                        case .error:
                            self.handleTTSFailure(nodeId, gen)
                            return
                        case .close:
                            // 30-minute server limit: attach again after the last chunk.
                            self.nodes[nodeId]?.attached = false
                            self.attachTTS(nodeId, gen)
                            return
                        case .heartbeat, .other:
                            break
                        }
                    case .reconnecting:
                        break
                    }
                }
            } catch {
                guard let self, gen == self.generation, self.currentNodeId == nodeId else { return }
                // HTTP error or too many drops: the reconcile loop takes over.
                self.nodes[nodeId]?.attached = false
            }
        }
    }

    /// The stream answered JSON (C §5.4): 200 with a URL = the whole file; 400 =
    /// TTS status still null (legacy path): attach again later.
    private func handleStreamJSON(_ nodeId: Int, status: Int, body: Data, _ gen: Int) {
        let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
        if status == 200, let url = json?["tts_url"] as? String {
            deliverFull(nodeId, url: url, gen)
        } else {
            nodes[nodeId]?.attached = false
        }
    }

    private func handleChunk(_ nodeId: Int, _ chunk: TTSStreamEvent.Chunk, _ gen: Int) {
        guard var track = nodes[nodeId], !track.restDelivered else { return }
        let key = "\(nodeId):\(chunk.chunkIndex)"
        guard !receivedChunks.contains(key) else { return }
        receivedChunks.insert(key)
        track.sseDelivered = true
        track.highestChunk = max(track.highestChunk ?? chunk.chunkIndex, chunk.chunkIndex)
        var chapterTitle: String?
        if !track.hadAudio {
            track.hadAudio = true
            if track.completed, let title = ChapterTitle.from(content: track.content) {
                chapterTitle = title
            } else {
                chapterTitle = ChapterTitle.placeholder
                track.provisionalChunk = audio.queue.entryCount
            }
        }
        nodes[nodeId] = track
        awaitingNextNode = false
        if !turnHasAudio {
            turnHasAudio = true
            timing.mark("chunk_ready", nodeId: nodeId)
            audio.queue.generatingTTS = true
            audio.queue.loadFirst(url: chunk.audioURL, duration: chunk.duration, chapterTitle: chapterTitle) { [weak self] in
                self?.timing.markPlaying()
            }
        } else {
            audio.queue.append(url: chunk.audioURL, duration: chunk.duration, chapterTitle: chapterTitle)
        }
        state = .playing
        audio.refreshNowPlaying()
    }

    private func handleAllComplete(_ nodeId: Int, _ complete: TTSStreamEvent.Complete, _ gen: Int) {
        nodes[nodeId]?.allComplete = true
        if let index = nodes[nodeId]?.provisionalChunk, let title = ChapterTitle.from(content: complete.preview) {
            audio.queue.renameChapter(atChunk: index, to: title)
            nodes[nodeId]?.provisionalChunk = nil
        }
        if let next = complete.continuationNodeId ?? nodes[nodeId]?.continuation {
            advanceChain(to: next, gen)
            return
        }
        finishGenerating()
    }

    /// Nothing more will be appended for this turn.
    private func finishGenerating() {
        sseTask?.cancel()
        reconcileTask?.cancel()
        audio.queue.generatingTTS = false
        if !turnHasAudio || awaitingNextNode || !audio.queue.hasAudio || state == .awaitingAudio {
            // Nothing (more) to play: leave "Thinking…".
            awaitingNextNode = false
            cueOff()
            if state != .playing { state = .done }
            if !turnHasAudio { audio.deactivate() }
        }
        audio.refreshNowPlaying()
    }

    /// Within-turn chain (#158): the continuation's audio joins the same queue.
    private func advanceChain(to next: Int, _ gen: Int) {
        sseTask?.cancel()
        llmTask?.cancel()
        awaitingNextNode = true
        audio.queue.generatingTTS = true
        currentNodeId = next
        nodes[next] = NodeTrack()
        pollReply(next, gen)
        if state == .awaitingAudio || state == .draining {
            cueOn()
        }
        audio.refreshNowPlaying()
    }

    /// The node's whole TTS file (POST /tts 200, a JSON stream answer, or REST recovery).
    private func deliverFull(_ nodeId: Int, url: String, _ gen: Int) {
        nodes[nodeId]?.restDelivered = true
        let title = ChapterTitle.from(content: nodes[nodeId]?.content)
        if turnHasAudio {
            audio.queue.append(url: url, duration: nil, chapterTitle: title)
        } else {
            turnHasAudio = true
            audio.queue.generatingTTS = true
            audio.queue.loadFirst(url: url, duration: nil, chapterTitle: title) { [weak self] in
                self?.timing.markPlaying()
            }
        }
        awaitingNextNode = false
        state = .playing
        if let next = nodes[nodeId]?.continuation {
            advanceChain(to: next, gen)
        } else {
            sseTask?.cancel()
            finishGenerating()
        }
        audio.refreshNowPlaying()
    }

    private func handleTTSFailure(_ nodeId: Int, _ gen: Int) {
        log.error("TTS failed for the reply")
        if turnHasAudio {
            flagError()
            finishGenerating()
        } else {
            endTurnWithError(nil, sound: false)
        }
    }

    private func closeStream() {
        sseTask?.cancel()
        sseTask = nil
    }

    // MARK: Recovery (#242)

    /// Reconciles against REST every 7 s while the turn is undelivered, and on
    /// foreground: lost /tts trigger, dead or lagging stream. No 60 s safety net:
    /// the web's only switches screens for its autoplay block, and ending the
    /// turn there dropped a late first chunk (M13).
    private func startReconcile(_ gen: Int) {
        reconcileTask?.cancel()
        reconcileTask = Task { [weak self] in
            while let self, !Task.isCancelled, gen == self.generation {
                try? await Task.sleep(nanoseconds: UInt64(self.timings.reconcile * 1_000_000_000))
                guard !Task.isCancelled, gen == self.generation else { return }
                await self.reconcile(foreground: false, gen)
            }
        }
    }

    /// Called when the app returns to the foreground.
    func appDidBecomeActive() {
        let gen = generation
        if state == .recording && isInterrupted {
            audio.playInterruptionAlert()
        }
        guard state == .awaitingAudio || state == .draining || (state == .playing && audio.queue.generatingTTS) else { return }
        Task { await reconcile(foreground: true, gen) }
    }

    private func reconcile(foreground: Bool, _ gen: Int) async {
        guard let nodeId = currentNodeId, let track = nodes[nodeId] else { return }
        let undelivered = state == .awaitingAudio || state == .draining || audio.queue.generatingTTS
        guard undelivered else { return }
        // Lost trigger: completed, stream off, last attempt long ago.
        if track.completed && !track.attached && !track.restDelivered && !track.allComplete,
           let at = track.ttsAttemptAt, Date().timeIntervalSince(at) > timings.triggerWatchdog {
            log.info("TTS watchdog: re-firing the trigger")
            await triggerTTS(nodeId, gen)
            return
        }
        if track.completed && !track.attached && !track.restDelivered && !track.allComplete && track.ttsAttemptAt == nil {
            await triggerTTS(nodeId, gen)
            return
        }
        guard track.attached else { return }
        guard let status = try? await backend.ttsStatus(nodeId: nodeId),
              gen == generation, currentNodeId == nodeId else { return }
        if status.status == .completed, let url = status.node?.audioTTSURL {
            if nodes[nodeId]?.ttsCompletedSeenAt == nil { nodes[nodeId]?.ttsCompletedSeenAt = Date() }
            let waited = Date().timeIntervalSince(nodes[nodeId]?.ttsCompletedSeenAt ?? Date())
            if !foreground && waited < timings.catchUpGrace {
                try? await Task.sleep(nanoseconds: UInt64((timings.catchUpGrace - waited) * 1_000_000_000))
                guard gen == generation, currentNodeId == nodeId, nodes[nodeId]?.allComplete == false else { return }
            }
            guard let fresh = nodes[nodeId], !fresh.allComplete, !fresh.restDelivered else { return }
            if fresh.sseDelivered {
                log.info("TTS stream behind a completed node: reconnecting")
                nodes[nodeId]?.attached = false
                attachTTS(nodeId, gen)
            } else {
                log.info("TTS stream silent but complete: delivering the file")
                sseTask?.cancel()
                deliverFull(nodeId, url: url, gen)
            }
        } else if status.status == .failed {
            handleTTSFailure(nodeId, gen)
        }
    }

    // MARK: Queue events (wired by the owner)

    /// The queue ran out while TTS is still generating.
    func queueDrained() {
        guard state == .playing else { return }
        state = .draining
        cueOn()
        audio.refreshNowPlaying()
    }

    /// Audio is playing again (first chunk, or after a drain).
    func queueStartedPlaying() {
        cueOff()
        if state == .draining { state = .playing }
        audio.refreshNowPlaying()
    }

    /// The last chunk ended and nothing more is coming.
    func queueFinished() {
        guard state == .playing || state == .draining else { return }
        state = .done
        cueOff()
        audio.deactivate()
        audio.refreshNowPlaying()
    }

    /// The user paused the reply (screen, lock screen, AirPods out): no cue
    /// while the paused queue waits for chunks (M12).
    func playbackPaused() {
        if state == .draining { cueOff() }
    }

    /// Play again while the queue still waits for chunks: the cue resumes.
    func playbackResumed() {
        if state == .draining && audio.queue.waitingForChunks { cueOn() }
    }

    // MARK: Thinking cue

    /// Starts (or re-asserts, after an interruption) the cue, within the turn's
    /// cue budget (`timings.cueCap`, M12).
    private func cueOn() {
        if cueSince != nil {
            audio.startCue()
            return
        }
        let left = timings.cueCap - cueUsed
        guard left > 0 else { return }
        cueSince = Date()
        audio.startCue()
        cueCapTask?.cancel()
        cueCapTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(left * 1_000_000_000))
            guard !Task.isCancelled, let self else { return }
            self.log.info("thinking cue reached its cap for this turn")
            self.cueOff()
        }
    }

    private func cueOff() {
        cueCapTask?.cancel()
        cueCapTask = nil
        if let since = cueSince {
            cueUsed += Date().timeIntervalSince(since)
            cueSince = nil
        }
        audio.stopCue()
    }

    // MARK: Cancel and reset

    /// The ✕ under "Thinking…", or lock-screen "next track" while thinking
    /// (web `handleCancelProcessing`). The server keeps generating.
    func cancelProcessing() {
        timing.endTurn()
        resetTurn(keepQueue: false)
        state = .idle
        audio.deactivate()
        audio.refreshNowPlaying()
    }

    /// Leaving the Voice screen (the web's unmount cleanup).
    func tearDown() {
        timing.endTurn()
        if state == .recording || state == .starting { recorder.cancel() }
        resetTurn(keepQueue: false)
        state = .idle
        audio.deactivate()
        audio.refreshNowPlaying()
    }

    private func resetTurn(keepQueue: Bool) {
        generation += 1
        [flowTask, tickTask, llmTask, sseTask, reconcileTask].forEach { $0?.cancel() }
        flowTask = nil; tickTask = nil; llmTask = nil; sseTask = nil; reconcileTask = nil
        if state == .recording || state == .starting { recorder.cancel() }
        cueOff()
        if !keepQueue {
            audio.queue.stop()
            audio.queue.generatingTTS = false
        }
        endInterruption()
        sessionId = nil
        currentNodeId = nil
        nodes = [:]
        receivedChunks = []
        turnHasAudio = false
        awaitingNextNode = false
        isPaused = false
        replyContent = nil
        toolCallsMeta = nil
    }

    private func finishQuietly() {
        cueOff()
        state = .idle
        audio.deactivate()
        audio.refreshNowPlaying()
    }

    private func endTurnWithError(_ message: String?, sound: Bool) {
        if let message { notices.toast(message, duration: 8) }
        if sound { audio.playErrorSound() }
        [llmTask, sseTask, reconcileTask, tickTask].forEach { $0?.cancel() }
        cueOff()
        audio.queue.generatingTTS = false
        state = .idle
        flagError()
        audio.deactivate()
        audio.refreshNowPlaying()
    }

    private func flagError() {
        hasError = true
        errorDotTask?.cancel()
        errorDotTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64((self?.timings.errorDot ?? 3) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.hasError = false
        }
    }

    private func toastWarnings(_ warnings: [String]) {
        for warning in warnings where !toastedWarnings.contains(warning) {
            toastedWarnings.insert(warning)
            notices.toast(warning, duration: 8)
        }
    }
}

/// Where a voice turn's wait goes (#371; web `utils/voiceTiming.js`). Marks
/// `rec_stop`, `finalize_acked`, `llm_node_known`, `tts_attach`, `chunk_ready`,
/// `playing` (never `autoplay_blocked`: the app starts the reply itself).
/// Failures are ignored; timing never affects the turn.
@MainActor
final class VoiceTiming {
    private struct Turn {
        var nodeId: Int?
        var marks: [String: Double]
        var sent = false
    }

    private let backend: VoiceBackend
    private var turn: Turn?
    var now: () -> Double = { Date().timeIntervalSince1970 * 1000 }

    init(backend: VoiceBackend) {
        self.backend = backend
    }

    var marks: [String: Double] { turn?.marks ?? [:] }

    func startTurn() {
        turn = Turn(nodeId: nil, marks: ["rec_stop": now()])
    }

    func endTurn() {
        turn = nil
    }

    func setTurnNode(_ nodeId: Int) {
        if turn != nil && turn?.nodeId == nil { turn?.nodeId = nodeId }
    }

    func mark(_ stage: String, nodeId: Int? = nil) {
        guard let current = turn, current.marks[stage] == nil else { return }
        if let nodeId, current.nodeId != nodeId { return }
        turn?.marks[stage] = now()
    }

    /// First audio is playing: send the marks once, with the clock offset.
    func markPlaying() {
        guard var current = turn, !current.sent, current.marks["chunk_ready"] != nil else { return }
        current.marks["playing"] = now()
        current.sent = true
        turn = current
        guard let nodeId = current.nodeId else { return }
        let marks = current.marks
        Task {
            var best: (rtt: Double, offset: Double)?
            for _ in 0..<3 {
                let t0 = now()
                guard let server = try? await backend.timingClock() else { return }
                let t1 = now()
                let rtt = t1 - t0
                if best == nil || rtt < best!.rtt { best = (rtt, server * 1000 - (t0 + t1) / 2) }
            }
            guard let best else { return }
            await backend.postTiming(nodeId: nodeId, marks: marks, offsetMs: best.offset, rttMs: best.rtt)
        }
    }
}
