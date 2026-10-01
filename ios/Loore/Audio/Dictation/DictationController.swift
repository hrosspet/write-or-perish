import Foundation
import Observation
import os

/// Dictation into a writing form (web `StreamingMicButton` + `useStreamingTranscription`,
/// the text-mode path, map C §3): the same recorder and upload queue as voice
/// mode, but `init` and `finalize` carry no `Voice` label, so the server only
/// transcribes (no reply). The transcript arrives in the form; Send saves it
/// with `save-as-node` (the form's job).
///
/// While recording, the draft's transcription SSE delivers `content_update`
/// (every 5-minute batch); `/status` is never called before finalize (#320).
/// After finalize, `all_complete` on the SSE or a `/status` poll ends it.
@MainActor
@Observable
final class DictationController {
    enum State: Equatable { case idle, initializing, recording, finalizing, error }

    private(set) var state: State = .idle
    private(set) var isInterrupted = false
    private(set) var elapsed: Double = 0
    /// The recording so far as one `.m4a` (fragmented MP4) for "Save audio".
    private(set) var recordingFile: URL?

    struct Callbacks {
        /// Before recording starts (the form keeps its current text).
        var started: () -> Void = {}
        /// The whole transcript so far.
        var transcript: (String) -> Void = { _ in }
        /// Transcription finished: the session id (for `save-as-node`) and the transcript.
        var finished: (String, String) -> Void = { _, _ in }
        /// Failed or refused; `spendCapped` = the cap refused the start (a toast was shown).
        var failed: (String?, Bool) -> Void = { _, _ in }
    }

    @ObservationIgnored var callbacks = Callbacks()
    @ObservationIgnored private weak var app: AppState?
    @ObservationIgnored private var recorder: VoiceRecorder?
    @ObservationIgnored private var sessionId: String?
    @ObservationIgnored private var sseTask: Task<Void, Never>?
    @ObservationIgnored private var flowTask: Task<Void, Never>?
    @ObservationIgnored private var tickTask: Task<Void, Never>?
    @ObservationIgnored private var recordedData = Data()
    @ObservationIgnored private var warned = false
    @ObservationIgnored private var interruptionToast: Int?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private let log = Logger(subsystem: "org.loore.app", category: "dictation")

    init(app: AppState) {
        self.app = app
    }

    var isActive: Bool { state == .initializing || state == .recording || state == .finalizing }

    // MARK: Start

    /// Record press (web `handleClick` in `idle`). The caller disables the button
    /// when the form's AI usage is `none`, offline, or a file is attached.
    func start(parentId: Int?, privacy: String, aiUsage: String) {
        guard let app, state == .idle || state == .error else { return }
        if app.spendCapped {
            app.notifySpendBlocked()
            app.toasts.show(SpendCap.toastMessage(.record), duration: 8)
            return
        }
        if app.audio.hasVoiceController && app.audio.voice.isActive {
            app.toasts.show("Finish the voice conversation first.", duration: 5)
            return
        }
        generation += 1
        let gen = generation
        callbacks.started()
        state = .initializing
        recordingFile = nil
        recordedData = Data()
        warned = false
        elapsed = 0
        flowTask = Task { [weak self] in
            guard let self else { return }
            if !app.audio.usesDebugAudioFile {
                if AudioSessionController.recordPermission == .undetermined {
                    _ = await AudioSessionController.requestRecordPermission()
                }
                guard AudioSessionController.recordPermission == .granted else {
                    self.fail("Microphone access is off for Loore. Turn it on in Settings → Loore → Microphone, then try again.")
                    return
                }
                await LocalNotifier.requestAuthorizationIfNeeded()
            }
            guard gen == self.generation else { return }
            let sid: String
            do {
                let body: JSONValue = .object(["parent_id": .optional(parentId),
                                               "privacy_level": .string(privacy),
                                               "ai_usage": .string(aiUsage)])
                let answer: StreamingInitResponse = try await app.api.post(APIPath.streamingInit, json: body)
                sid = answer.sessionId
            } catch {
                guard gen == self.generation else { return }
                if SpendCap.isSpendCapError(error) {
                    app.toasts.show(SpendCap.toastMessage(.record), duration: 8)
                    self.state = .idle
                    self.callbacks.failed(nil, true)
                } else {
                    self.fail((error as? APIError)?.userMessage(fallback: "Could not start recording.") ?? "Could not start recording.")
                }
                return
            }
            guard gen == self.generation else { return }
            self.sessionId = sid
            do {
                try app.audio.session.activateForRecording(microphone: !app.audio.usesDebugAudioFile)
                let recorder = self.makeRecorder()
                try recorder.start(sessionId: sid, uploadURL: app.api.environment.url(path: APIPath.streamingChunk(sid)),
                                   firstChunkIndex: 0, elapsedOffset: 0)
                self.recorder = recorder
            } catch {
                _ = try? await app.api.data(for: APIRequest(.delete, APIPath.streamingDiscard(sid)))
                app.audio.session.deactivate()
                self.fail("The microphone could not start. Close other apps using it (calls, voice memos) and try again.")
                return
            }
            self.state = .recording
            app.audio.activeDictation = self
            self.listen(sid, gen)
            self.startTicker(gen)
        }
    }

    private func makeRecorder() -> VoiceRecorder {
        let debugFile: URL? = {
            #if DEBUG
            return app?.launch.debugAudioFile.map { URL(fileURLWithPath: $0) }
            #else
            return nil
            #endif
        }()
        let recorder = VoiceRecorder(debugFile: debugFile)
        recorder.onFatal = { [weak self] message in self?.fatalUpload(message) }
        recorder.onSourceEnded = { [weak self] in self?.stop() }
        recorder.onSourceFailed = { [weak self] in
            self?.holdForInterruption("Recording paused — the microphone stopped after an audio device change. Everything up to here is saved. Press Resume to continue.")
        }
        recorder.chunkObserver = { [weak self] chunk in self?.keep(chunk) }
        return recorder
    }

    private func keep(_ chunk: RecordedChunk) {
        recordedData.append(chunk.data)
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("loore-dictation.m4a")
        try? recordedData.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        recordingFile = url
    }

    /// The draft's transcription stream: live `content_update`, then `all_complete`.
    private func listen(_ sid: String, _ gen: Int) {
        guard let app else { return }
        let tracker = LastChunkTracker()
        let stream = app.sse.subscribe(path: APIPath.sseDraftTranscription(sid),
                                       options: .init(stallTimeout: 45, reconnectDelay: 3, maxReconnects: 20,
                                                      terminalEvents: ["all_complete", "close"]),
                                       resumeQuery: { tracker.resumeQuery() })
        sseTask = Task { [weak self] in
            do {
                for try await message in stream {
                    guard let self, gen == self.generation else { return }
                    guard case .event(let event) = message else { continue }
                    tracker.observe(event)
                    switch TranscriptionStreamEvent(event) {
                    case .contentUpdate(let content, _):
                        self.callbacks.transcript(content)
                    case .allComplete(let done):
                        if self.state == .finalizing, let content = done.content {
                            self.complete(sid, content: content)
                        }
                        return
                    default:
                        continue
                    }
                }
            } catch {
                // The stream gave up; the /status poll after finalize still finishes the job.
            }
        }
    }

    private func startTicker(_ gen: Int) {
        tickTask?.cancel()
        tickTask = Task { [weak self] in
            while let self, !Task.isCancelled, gen == self.generation, self.state == .recording {
                self.elapsed = self.recorder?.elapsed ?? 0
                if !self.warned && self.elapsed >= 59 * 60 {
                    self.warned = true
                    self.app?.audio.sounds.playLongRecordingWarning()
                    self.app?.toasts.show("You’ve been recording for 59 minutes — consider stopping soon and continuing in a new recording.", duration: 10)
                    LocalNotifier.post(.longRecording)
                }
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
        }
    }

    // MARK: Stop

    /// Stop press (or "Stop & save" after an interruption).
    func stop() {
        guard state == .recording, let sid = sessionId, let recorder, let app else { return }
        let gen = generation
        state = .finalizing
        tickTask?.cancel()
        endInterruption()
        flowTask = Task { [weak self] in
            guard let self else { return }
            let outcome = await recorder.stop()
            app.audio.session.deactivate()
            if app.audio.activeDictation === self { app.audio.activeDictation = nil }
            guard gen == self.generation else { return }
            self.elapsed = recorder.elapsed
            if let fatal = outcome.fatalMessage {
                app.audio.sounds.playError()
                self.fail(fatal)
                return
            }
            if outcome.produced == 0 {
                _ = try? await app.api.data(for: APIRequest(.delete, APIPath.streamingDiscard(sid)))
                recorder.forget(sessionId: sid)
                self.reset()
                self.callbacks.failed(nil, false)
                return
            }
            if !outcome.failed.isEmpty {
                // Finalizing now would leave those chunks out of the transcript (B1).
                app.audio.sounds.playError()
                self.fail(VoiceTurnController.missingChunksMessage)
                return
            }
            do {
                var request = APIRequest.json(.post, APIPath.streamingFinalize(sid),
                                              .object(["total_chunks": .int(outcome.totalForFinalize)]))
                request.timeout = 120
                _ = try await app.api.data(for: request)
            } catch let error as APIError where VoiceTurnController.isAlreadyFinalizing(error) {
                // A retried finalize whose first answer was lost.
            } catch {
                guard gen == self.generation else { return }
                app.audio.sounds.playError()
                self.fail("Streaming transcription failed")
                return
            }
            recorder.forget(sessionId: sid)
            await self.pollUntilDone(sid, gen)
        }
    }

    /// After finalize (never before, #320): `/status` every 2 s until completed.
    private func pollUntilDone(_ sid: String, _ gen: Int) async {
        guard let app else { return }
        let deadline = Date().addingTimeInterval(15 * 60)
        while gen == generation, state == .finalizing, Date() < deadline, !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard gen == generation, state == .finalizing else { return }
            if let status: StreamingSessionStatus = try? await app.api.get(APIPath.streamingStatus(sid), poll: true) {
                if status.streamingStatus == .completed {
                    complete(sid, content: status.content)
                    return
                }
                if status.streamingStatus == .failed {
                    fail("Streaming transcription failed")
                    return
                }
            }
        }
    }

    private func complete(_ sid: String, content: String) {
        guard state == .finalizing else { return }
        sseTask?.cancel()
        state = .idle
        callbacks.finished(sid, content)
    }

    // MARK: Interruptions, cancel

    /// Resume after an interruption (the button reads "Resume").
    func resume() {
        guard state == .recording, isInterrupted, let recorder, let app else { return }
        do {
            try app.audio.session.reactivate()
            try recorder.resume()
            endInterruption()
        } catch {
            app.toasts.show("The microphone could not restart. Try again in a moment.", duration: 8)
        }
    }

    func systemInterruptionBegan() {
        holdForInterruption("Recording paused — another app took the microphone (phone call?). Everything up to the interruption is saved. Press Resume to continue.")
    }

    /// Also when the microphone could not restart after a route change (M15).
    private func holdForInterruption(_ message: String) {
        guard state == .recording, !isInterrupted, let app else { return }
        recorder?.interrupt()
        isInterrupted = true
        app.audio.sounds.playInterruptionAlert()
        interruptionToast = app.toasts.show(message, duration: 24 * 60 * 60)
        LocalNotifier.post(.recordingPaused)
    }

    func systemInterruptionEnded() {
        if state == .recording && isInterrupted { app?.audio.sounds.playInterruptionAlert() }
    }

    private func endInterruption() {
        isInterrupted = false
        if let id = interruptionToast { app?.toasts.dismiss(id) }
        interruptionToast = nil
        LocalNotifier.withdraw(.recordingPaused)
    }

    /// The form went away (web: unmount). Stored chunks stay on the server; the
    /// Voice page's recovery banner offers them.
    func cancel() {
        generation += 1
        recorder?.cancel()
        [sseTask, flowTask, tickTask].forEach { $0?.cancel() }
        if isActive { app?.audio.session.deactivate() }
        if app?.audio.activeDictation === self { app?.audio.activeDictation = nil }
        endInterruption()
        reset()
    }

    /// "Error - Retry" press.
    func clearError() {
        if state == .error { state = .idle }
    }

    private func reset() {
        state = .idle
        sessionId = nil
        recorder = nil
    }

    private func fatalUpload(_ message: String) {
        guard state == .recording else { return }
        generation += 1
        recorder?.cancel()
        app?.audio.session.deactivate()
        app?.audio.sounds.playError()
        fail(message)
    }

    private func fail(_ message: String) {
        sseTask?.cancel()
        tickTask?.cancel()
        state = .error
        callbacks.failed(message, false)
    }
}
