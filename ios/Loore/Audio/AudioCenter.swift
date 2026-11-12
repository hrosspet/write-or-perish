import AVFoundation
import Observation
import UIKit
import UserNotifications
import os

/// The app's audio, owned by `AppState` (design doc §3 "the shared audio player"):
/// one audio session, one queue player (voice mode and the mini-player share it,
/// C §8.4 option A), the thinking cue and chimes, the lock screen (Now Playing
/// and the voice Live Activity), and the voice conversation.
@MainActor
@Observable
final class AudioCenter {
    let player = ChunkQueuePlayer()
    @ObservationIgnored let sounds = SoundPlayer()
    @ObservationIgnored let session = AudioSessionController()
    @ObservationIgnored let nowPlaying = NowPlayingController()
    @ObservationIgnored let liveActivity = VoiceLiveActivity()
    /// A refused Voice turn's sentence (#651): its clip, else the device's speech.
    @ObservationIgnored let refusalVoice = RefusalVoice()
    @ObservationIgnored private let refusalClips = ClipSpeaker()

    /// The Voice screen is on screen (the mini-player hides there).
    var voiceScreenVisible = false
    /// Listen-aloud: the node/profile/item whose audio is loading (spinner).
    private(set) var loadingSource: ChunkQueuePlayer.Source?

    @ObservationIgnored private weak var app: AppState?
    /// Listen-aloud results per target and content (web SpeakerIcon's cached URLs).
    @ObservationIgnored var listenCache: [ListenCacheKey: ListenCacheEntry] = [:]
    var appState: AppState? { app }
    /// The writing form's recorder while it runs (interruptions go to it).
    @ObservationIgnored weak var activeDictation: DictationController?
    @ObservationIgnored private var voiceController: VoiceTurnController?
    @ObservationIgnored private var observers: [NSObjectProtocol] = []
    @ObservationIgnored var listenTask: Task<Void, Never>?
    /// A read-aloud failed after part of its audio arrived: announce it when that audio ends.
    @ObservationIgnored var pendingListenFailure: ChunkQueuePlayer.Source?
    @ObservationIgnored let log = Logger(subsystem: "org.loore.app", category: "audio")

    init() {
        player.willPlay = { [weak self] in self?.prepareSessionForPlayback() }
        player.onStateChange = { [weak self] in self?.refreshNowPlaying() }
        player.onDrained = { [weak self] in
            guard let self, self.player.source == .voice else { return }
            self.voiceController?.queueDrained()
        }
        player.onFinished = { [weak self] in
            guard let self else { return }
            if self.player.source == .voice {
                self.voiceController?.queueFinished()
            } else if self.finishedQueue() {
                // Deactivating now would cut the error sound off.
                Task { [weak self] in
                    try? await Task.sleep(nanoseconds: UInt64(ToneSynth.errorSoundLength * 1_000_000_000))
                    self?.deactivate()
                }
            } else {
                self.session.deactivate()
            }
        }
        player.onTransport = { [weak self] playing in
            guard let self, self.player.source == .voice else { return }
            if playing { self.voiceController?.playbackResumed() } else { self.voiceController?.playbackPaused() }
        }
        player.onPlaybackError = { [weak self] message in
            _ = self?.app?.toasts.show(message, duration: 6)
        }
        player.onStartedPlaying = { [weak self] in
            guard let self, self.player.source == .voice else { return }
            self.voiceController?.queueStartedPlaying()
        }
        session.onEvent = { [weak self] event in self?.handleSessionEvent(event) }
        liveActivity.onEnded = { [weak self] in self?.refreshNowPlaying() }
        refusalVoice.fetch = { [weak self] refusal in await self?.refusalClip(refusal) }
        refusalVoice.playClip = { [refusalClips] data in await refusalClips.play(data) }
        refusalVoice.stopClip = { [refusalClips] in refusalClips.stop() }
        refusalVoice.sayOnDevice = { [sounds] text in await sounds.speak(text) }
        refusalVoice.stopDevice = { [sounds] in sounds.stopSpeaking() }
        VoiceActivityCommands.handler = { [weak self] command in await self?.runLockScreenCommand(command) }
        observers.append(NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification,
                                                                object: nil, queue: .main) { [weak self] _ in
            RecordingLog.shared.note("app active")
            MainActor.assumeIsolated {
                self?.voiceController?.appDidBecomeActive()
                // A change may have been decided on another device meanwhile.
                self?.recheckQueuedAnnouncement()
            }
        })
        observers.append(NotificationCenter.default.addObserver(forName: UIApplication.didEnterBackgroundNotification,
                                                                object: nil, queue: .main) { [weak self] _ in
            RecordingLog.shared.note("app in background")
            MainActor.assumeIsolated { self?.voiceController?.appDidEnterBackground() }
        })
    }

    /// Connects to the app (API client, cookies, toasts). Called once by `AppState`.
    func attach(_ app: AppState) {
        self.app = app
        // What a killed app left in tmp/ (exports, downloads, imports, dictation).
        PrivateFiles.sweepTemporary()
        // …and on the lock screen: a conversation does not survive a relaunch.
        liveActivity.endLeftovers()
        player.cookiesProvider = { [weak app] in app?.api.backendCookies() ?? [] }
        player.urlResolver = { [weak app] raw in
            if raw.hasPrefix("http://") || raw.hasPrefix("https://") || raw.hasPrefix("file://") { return URL(string: raw) }
            return app?.environment.url(path: raw)
        }
    }

    // MARK: Voice conversation

    /// The voice conversation (created on first use; one per app, like the web page's hook).
    var voice: VoiceTurnController {
        if let voiceController { return voiceController }
        let backend = LiveVoiceBackend(api: { [weak self] in self!.app!.api },
                                       sse: { [weak self] in self!.app!.sse })
        let debugFile: URL? = {
            #if DEBUG
            return app?.launch.debugAudioFile.map { URL(fileURLWithPath: $0) }
            #else
            return nil
            #endif
        }()
        let controller = VoiceTurnController(backend: backend,
                                             recorder: VoiceRecorder(debugFile: debugFile),
                                             audio: self, notices: self)
        controller.model = { [weak self] in self?.app?.user?.preferredModel }
        controller.aiUsage = { [weak self] in self?.app?.user?.defaultAIUsage.rawString ?? "none" }
        controller.onGleaningReady = { [weak self] id in self?.openGleaning(id) }
        controller.appIsActive = { UIApplication.shared.applicationState == .active }
        controller.onApprovalsListed = { [weak self] node, items in
            guard let model = self?.makeVoiceApprovals() else { return }
            model.nodeId = node
            model.update(items, autoOpen: false)
        }
        voiceController = controller
        return controller
    }

    // MARK: Changes waiting for approval in the voice conversation (2026-11-12)

    /// The Voice screen's approval dialog: the thread screen's model and dialog,
    /// fed by the conversation's list (`VoiceTurnController.waitingApprovals`).
    /// Kept here, with the conversation, so a decision reaches the lock screen
    /// whichever screen is showing.
    private(set) var voiceApprovals: ToolApprovalsModel?

    @discardableResult
    func makeVoiceApprovals() -> ToolApprovalsModel? {
        if let voiceApprovals { return voiceApprovals }
        guard let app else { return nil }
        let model = ToolApprovalsModel(api: app.api, nodeId: voiceController?.approvalsNodeId ?? 0)
        model.onApproved = { [weak app] item in ToolApprovalsModel.signalApproved(item, signals: app?.signals) }
        model.onPendingChanged = { [weak self] items in
            self?.voiceController?.approvalsChanged(items)
            self?.recheckQueuedAnnouncement()
        }
        model.onGroupDone = { [weak model] in
            // The next reply's calls, if any, follow at once: the Voice screen's
            // dialog has no "Later", and it closes when nothing waits.
            if model?.pending.isEmpty == false { model?.review() }
        }
        voiceApprovals = model
        return model
    }

    /// A call was decided on a thread screen: a voice conversation waiting for
    /// it asks the server again (only while something waits there), and so does
    /// an announcement at the end of the loaded queue.
    func toolApprovalsDecidedElsewhere() {
        recheckQueuedAnnouncement()
        guard let voice = voiceController, voice.approvalWaiting else { return }
        Task { await voice.refreshApprovalsIfWaiting() }
    }

    // MARK: The spoken approval announcement in the queue (2026-11-12)

    struct QueuedAnnouncement: Equatable {
        var nodeId: Int
        var url: String
    }

    /// Peter, 2026-11-12: "the announcement should be TTSed as a separate chunk
    /// and appended to the queue. So that when it's not pending, it's not voiced
    /// out anymore". The announcement at the end of the loaded queue (voice turn
    /// or a reply's speaker), and its reply.
    var queuedAnnouncement: QueuedAnnouncement? { queuedAnnouncementValue }
    @ObservationIgnored private var queuedAnnouncementValue: QueuedAnnouncement?

    func noteQueuedAnnouncement(nodeId: Int, url: String) {
        queuedAnnouncementValue = QueuedAnnouncement(nodeId: nodeId, url: url)
    }

    /// After a decision (any screen) or a return to the app: asks whether the
    /// reply of the announcement at the end of the queue still has changes
    /// waiting; when none do, the announcement leaves the queue (if it is
    /// playing, after it ends), so a replay no longer says it. Costs a request
    /// only while such an announcement is loaded.
    func recheckQueuedAnnouncement() {
        guard let queued = queuedAnnouncementValue, let api = app?.api else { return }
        guard let url = player.urlResolver(queued.url), player.entries.last?.url == url else {
            queuedAnnouncementValue = nil  // the queue moved on
            return
        }
        Task { [weak self] in
            guard let answer: ApprovalAnnouncement = try? await api.get(APIPath.voiceApprovalAnnouncement, query: [
                URLQueryItem(name: "node", value: String(queued.nodeId)),
            ]), let self, self.queuedAnnouncementValue == queued, answer.count == 0 else { return }
            self.player.dropTrailing(url: queued.url)
            self.queuedAnnouncementValue = nil
        }
    }

    /// A reply's spoken audio was loaded from its speaker: its announcement
    /// joins the end while one of its changes waits. Waits at most
    /// `VoiceApproval.announcementWait` for the answer.
    func appendApprovalAnnouncement(nodeId: Int) async {
        guard let api = app?.api else { return }
        var request = APIRequest(.get, APIPath.voiceApprovalAnnouncement,
                                 query: [URLQueryItem(name: "node", value: String(nodeId))])
        request.timeout = VoiceApproval.announcementWait
        guard let (data, http) = try? await api.data(for: request), http.statusCode == 200,
              let answer = try? api.decode(ApprovalAnnouncement.self, from: data), let clip = answer.clip,
              player.source == .node(nodeId) else { return }
        if player.append(url: clip, duration: answer.duration) {
            noteQueuedAnnouncement(nodeId: nodeId, url: clip)
        }
    }

    /// A glean from the Voice screen (or its lock screen) landed (#475): the view
    /// switches to text mode, the gleaning's thread page pushed over the Voice
    /// screen, in the tab where it is. Back returns to the conversation, which
    /// continues under the gleaning. Nothing moves when the Voice screen is not on
    /// top (the user went on elsewhere): the gleaning is in the thread.
    private func openGleaning(_ id: Int) {
        guard let app else { return }
        let router = app.router
        let isVoice: (AppRoute?) -> Bool = { route in
            if case .voice? = route { return true }
            return false
        }
        guard let tab = AppTab.allCases.first(where: { isVoice(router.path(for: $0).last) }) else { return }
        if router.selectedTab == tab {
            NodePrefetch.shared.openThread(id, app: app)
        } else {
            router.setPath(router.path(for: tab) + [.thread(id: id, awaitLLM: nil)], for: tab)
        }
    }

    var hasVoiceController: Bool { voiceController != nil }

    /// Debug builds with `-LooreDebugAudioFile`: the recorder reads a file, not the mic.
    var usesDebugAudioFile: Bool {
        #if DEBUG
        return app?.launch.debugAudioFile != nil
        #else
        return false
        #endif
    }

    /// Sign-out: stop everything, forget the conversation, and delete this user's
    /// audio and temporary files (upload queue, dictation, exports, downloads; M2).
    func signedOut() {
        voiceController?.tearDown()
        voiceController = nil
        voiceApprovals = nil
        queuedAnnouncementValue = nil
        activeDictation?.cancel()
        ChunkUploader.shared.reset()
        PrivateFiles.sweepTemporary()
        RecordingLog.shared.deleteAll()
        listenTask?.cancel()
        listenCache = [:]
        loadingSource = nil
        player.close()
        sounds.stopCue()
        session.deactivate()
        nowPlaying.update(.none)
    }

    // MARK: Listening outside voice mode

    /// The mini-player's height with its padding, measured by `MiniPlayerView`, so
    /// toasts and the spend-cap banner sit above it (web `--floating-player-offset`).
    var miniPlayerHeight: CGFloat = 0

    /// Mini-player visibility: a queue is loaded and the Voice screen is not showing.
    var showsMiniPlayer: Bool {
        player.isLoaded && !voiceScreenVisible && player.source != .voice
    }

    func setLoading(_ source: ChunkQueuePlayer.Source?) {
        loadingSource = source
    }

    private func prepareSessionForPlayback() {
        if player.source == .voice, session.mode != .inactive { return }
        session.activateForPlayback()
    }

    // MARK: Session events

    private func handleSessionEvent(_ event: AudioSessionController.Event) {
        let voiceActive = voiceController?.isActive == true
        switch event {
        case .interruptionBegan:
            if voiceActive { voiceController?.systemInterruptionBegan() }
            activeDictation?.systemInterruptionBegan()
            if player.isPlaying { player.pause(); resumeAfterInterruption = true }
        case .interruptionEnded(let shouldResume):
            if voiceActive { voiceController?.systemInterruptionEnded() }
            activeDictation?.systemInterruptionEnded()
            if resumeAfterInterruption && shouldResume {
                try? session.reactivate()
                player.play()
            }
            resumeAfterInterruption = false
        case .routeChanged(let reason, let from, let to):
            if reason == .oldDeviceUnavailable && player.isPlaying { player.pause() }
            if AudioRoute.headsetMicLost(from: from, to: to) {
                // Also at every Stop (the switch to playback drops the input): only
                // a recording that was paused gets a line.
                let voiceHeld = voiceActive && voiceController?.headsetMicLost() == true
                let dictationHeld = activeDictation?.headsetMicLost() == true
                if voiceHeld || dictationHeld { RecordingLog.shared.note("headset mic lost: recording paused") }
            }
        case .mediaServicesReset:
            sounds.stopCue()
            if voiceActive { voiceController?.systemInterruptionBegan() }
            activeDictation?.systemInterruptionBegan()
            if player.isPlaying { player.pause() }
        }
    }

    @ObservationIgnored private var resumeAfterInterruption = false
    /// The releases of recordings a killed app left (`releaseAbandonedRecordings`);
    /// the Voice screen waits for them before it asks for unfinished recordings.
    @ObservationIgnored var releasingAbandoned: Task<Void, Never>?
    /// The voice conversation's last toast (a lock-screen Record that failed repeats it).
    @ObservationIgnored private var lastVoiceToast: (text: String, at: Date)?

    // MARK: Lock screen

    func refreshNowPlaying() {
        if let voice = voiceController {
            liveActivity.sync(VoiceLiveActivity.state(turn: voice.state, isPaused: voice.isPaused,
                                                      isInterrupted: voice.isInterrupted,
                                                      awaitingNextNode: voice.awaitingNextNode,
                                                      elapsed: voice.elapsed,
                                                      glean: voice.lockScreenGlean,
                                                      gleaning: voice.isGleaning,
                                                      gleanAccepted: voice.gleanAccepted,
                                                      gleaned: voice.gleaningDone,
                                                      approvalWaiting: voice.approvalWaiting),
                              start: voice.state == .starting)
        }
        if let voice = voiceController, voice.isActive || (player.source == .voice && player.isLoaded) {
            switch voice.state {
            case .starting, .recording:
                if liveActivity.isShowing {
                    // The Live Activity carries the recording controls (#397): Now
                    // Playing's previous/next slots would only confuse them.
                    nowPlaying.update(.none)
                    return
                }
                nowPlaying.handlers = .init(play: { [weak voice] in voice?.resumeRecording() },
                                            pause: { [weak voice] in voice?.pauseRecording() },
                                            next: { [weak voice] in voice?.stop() })
                nowPlaying.update(.recording(elapsed: voice.elapsed, paused: voice.isPaused))
                return
            case .stopping, .transcribing, .awaitingAudio, .draining where voice.awaitingNextNode:
                // Nothing to control until the reply plays. No lock-screen cancel
                // (the web's next = cancel, #397): it looked like "next track", and the
                // server finishes the reply anyway. The Voice screen's ✕ remains.
                nowPlaying.handlers = .init()
                nowPlaying.update(.thinking(title: "Voice…"))
                return
            default:
                break
            }
        }
        guard player.isLoaded else {
            nowPlaying.update(.none)
            return
        }
        nowPlaying.handlers = .init(play: { [weak self] in self?.player.play() },
                                    pause: { [weak self] in self?.player.pause() },
                                    skip: { [weak self] in self?.player.skip(by: $0) },
                                    seek: { [weak self] in self?.player.seek(to: $0) },
                                    rate: { [weak self] in self?.player.setRate($0) })
        var title = player.title.isEmpty ? "Loore" : player.title
        if player.chapters.count > 1, let i = player.currentChapterIndex {
            title += " — " + player.chapters[i].title
        }
        nowPlaying.update(.playback(title: title, elapsed: player.cumulativeTime, duration: player.totalDuration,
                                    rate: player.rate, playing: player.isPlaying))
    }
}

// MARK: Live Activity buttons

extension AudioCenter {
    /// A Live Activity button (its intent runs here, in the app). The phone may
    /// be locked and the app was possibly suspended until now.
    func runLockScreenCommand(_ command: VoiceActivityCommand) async {
        guard let voice = voiceController, liveActivity.isShowing else {
            // Left over from an earlier launch (iOS ended the app between turns):
            // no conversation to control.
            await VoiceLiveActivity.staleTap()
            return
        }
        log.info("lock screen: \(command.rawValue, privacy: .public)")
        switch command {
        case .pause: voice.pauseRecording()
        case .resume: voice.resumeRecording()
        case .stop: voice.stop()
        case .record: await recordFromLockScreen(voice)
        case .glean: await gleanFromLockScreen(voice)
        }
    }

    /// Glean from the lock screen (#475): the same as the Voice screen's button.
    /// The intent waits for the request (the app may be suspended after it); the
    /// gleaning opens in text when the app runs again. A refusal or a failure
    /// also notifies, since a toast is not seen on a locked phone.
    private func gleanFromLockScreen(_ voice: VoiceTurnController) async {
        guard voice.canGlean else { return }
        if app?.spendCapped == true {
            app?.notifySpendBlocked()
            _ = app?.toasts.show(SpendCap.toastMessage(.glean), duration: 8)
            LocalNotifier.post(.gleanFailed, body: SpendCap.toastMessage(.glean))
            return
        }
        guard NetworkStatus.shared.isOnline else {
            LocalNotifier.post(.gleanFailed, body: "You're offline. Glean again when you're back online.")
            return
        }
        let tapped = Date()
        if await !voice.glean() {
            let reason = lastVoiceToast.flatMap { $0.at >= tapped ? $0.text : nil }
            LocalNotifier.post(.gleanFailed, body: reason ?? LocalNotice.gleanFailed.body)
        }
    }

    /// Most time a lock-screen Record waits for the session and the microphone
    /// before its intent returns (heuristic: the intent keeps the app running
    /// only until then, and the microphone may start only inside it).
    static let lockScreenStartWait: Double = 15

    /// Record from the lock screen: the next turn, also while the reply plays
    /// (the Voice screen's Continue), or a new one between turns. On a locked
    /// phone a toast is not seen, so a refusal or a failed start also notifies.
    /// Returns what that notification said (nil: none was posted).
    @discardableResult
    func recordFromLockScreen(_ voice: VoiceTurnController) async -> String? {
        // A glean runs: its card shows no Record button (a stale tap does nothing).
        guard !voice.isGleaning else { return nil }
        if app?.spendCapped == true {
            app?.notifySpendBlocked()
            _ = app?.toasts.show(SpendCap.toastMessage(.record), duration: 8)
            let body = refuseLockScreenRecord(SpendCap.toastMessage(.record))
            // ...and said aloud (#651); the intent waits until it has been said,
            // as the app may be suspended once it returns.
            await voice.announceUsageLimit()
            return body
        }
        // Checked before Continue stops the reply (the Voice screen disables it offline).
        guard NetworkStatus.shared.isOnline else {
            return refuseLockScreenRecord("You're offline. Record again when you're back online.")
        }
        // The one gate for a new turn (`newTurnRefusal`), the waiting change first:
        // - A change in the thread waits for approval (Peter, 2026-11-12): no new
        //   turn, and the reply keeps playing. The card has no Record button then;
        //   a tap on a card not yet updated asks the server once more, since the
        //   change may have been decided on another device.
        // - A thread too long for another reply (#648), known before the press (the
        //   last reply reached the limit): it is said aloud, and the intent waits
        //   until it has been said, as the app may be suspended once it returns.
        if voice.approvalWaiting { await voice.refreshApprovalsIfWaiting() }
        switch voice.newTurnRefusalReason {
        case .approvalWaiting?:
            let body = refuseLockScreenRecord(voice.newTurnRefusal ?? LocalNotice.recordFailed.body)
            // Said aloud too when nothing plays (#651); a reply that plays goes on,
            // and its own announcement says it at its end.
            await voice.announceApprovalWaiting()
            return body
        case .threadTooLong(let block)? where voice.phase == .ready || voice.phase == .playback:
            LocalNotifier.post(.recordFailed, body: block.body)
            await voice.announceBlock()
            return block.body
        default:
            break
        }
        switch voice.phase {
        case .playback:
            voice.continueConversation()
        case .ready:
            if let block = voice.aiBlock {
                return refuseLockScreenRecord(block.body)
            }
            voice.start()
        default:
            return nil
        }
        let tapped = Date()
        let deadline = tapped.addingTimeInterval(Self.lockScreenStartWait)
        while voice.state == .starting && Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if voice.state == .idle {
            // The start failed; its toast says why. Two refusals have no toast: a
            // change waiting for approval that the server named (the dialog waits
            // in the app) and the thread's length (the block says it, aloud).
            let reason = voice.newTurnRefusal ?? lastVoiceToast.flatMap { $0.at >= tapped ? $0.text : nil }
            let body = refuseLockScreenRecord(reason ?? LocalNotice.recordFailed.body)
            // A refused start is being said (#648, #651): keep the app running
            // until it has been.
            await voice.untilSpoken()
            return body
        }
        return nil
    }

    private func refuseLockScreenRecord(_ body: String) -> String {
        LocalNotifier.post(.recordFailed, body: body)
        return body
    }
}

// MARK: VoiceAudio

extension AudioCenter: VoiceAudio {
    var queue: VoiceQueue { player }

    func activateForRecording() throws {
        // A listen-aloud queue stops when a recording starts (web: audio.stop()).
        if player.source != .voice && player.isLoaded { player.close() }
        try session.activateForRecording(microphone: !usesDebugAudioFile)
        sounds.prepareCue()
    }

    func activateForReply() {
        session.activateForPlayback()
    }

    func reactivate() throws {
        try session.reactivate()
    }

    func switchToPlayback() {
        session.switchToPlaybackAfterRecording()
    }

    func deactivate() {
        guard !player.isPlaying else { return }
        session.deactivate()
    }

    func voiceConversationEnded() {
        liveActivity.end()
    }

    func announcementQueued(nodeId: Int, url: String) {
        noteQueuedAnnouncement(nodeId: nodeId, url: url)
    }

    func startCue() { sounds.startCue() }
    func stopCue() { sounds.stopCue() }
    func playErrorSound() { sounds.playError() }

    func speak(_ refusal: SpokenRefusal) async {
        // Recording's session stays as it is (it plays too); otherwise playback.
        session.activateForPlayback()
        // The clip may have to be fetched before anything plays: a locked phone
        // keeps the app running for that (as for a draft's save, #651).
        let application = UIApplication.shared
        final class Token { var id = UIBackgroundTaskIdentifier.invalid }
        let token = Token()
        token.id = application.beginBackgroundTask(withName: "loore.refusal") {
            application.endBackgroundTask(token.id)
            token.id = .invalid
        }
        await refusalVoice.say(refusal)
        if token.id != .invalid {
            application.endBackgroundTask(token.id)
            token.id = .invalid
        }
    }

    func stopSpeaking() { refusalVoice.stop() }

    /// The refusal's clip (`GET /api/voice/refusal/<code>.mp3`), or nil: offline,
    /// refused (an account without Voice mode), not made (503), or slower than
    /// `SpokenRefusal.clipWait`.
    func refusalClip(_ refusal: SpokenRefusal) async -> Data? {
        guard let api = app?.api else { return nil }
        var request = APIRequest(.get, refusal.clipPath)
        request.timeout = SpokenRefusal.clipWait
        guard let (data, http) = try? await api.data(for: request), http.statusCode == 200,
              !data.isEmpty else { return nil }
        return data
    }

    func playInterruptionAlert() { sounds.playInterruptionAlert() }
    func playLongRecordingWarning() { sounds.playLongRecordingWarning() }
}

// MARK: VoiceNotices

extension AudioCenter: VoiceNotices {
    @discardableResult
    func toast(_ text: String, duration: TimeInterval) -> Int {
        lastVoiceToast = (text, Date())
        return app?.toasts.show(text, duration: duration) ?? 0
    }

    func dismissToast(_ id: Int) {
        app?.toasts.dismiss(id)
    }

    func notify(_ notice: LocalNotice) {
        LocalNotifier.post(notice)
    }

    func withdraw(_ notice: LocalNotice) {
        LocalNotifier.withdraw(notice)
    }

    func spendCapped() {
        app?.notifySpendBlocked()
    }
}

// MARK: ChunkQueuePlayer as the turn's queue

extension ChunkQueuePlayer: VoiceQueue {
    var entryCount: Int { entries.count }

    func loadFirst(url: String, duration: Double?, chapterTitle: String?, onPlaying: @escaping () -> Void) {
        load(urls: [url], durations: [duration], title: "Voice", source: .voice,
             chapters: chapterTitle.map { [QueueChapter(title: $0, chunkIndex: 0, startTime: 0)] } ?? [],
             autoplay: true, onPlaying: onPlaying)
    }
}

/// Local notifications (no backend needed): "Recording paused — tap to resume"
/// and the 59-minute warning (design doc §9.5).
enum LocalNotifier {
    /// Asked once, at the first record tap and before recording starts, so the
    /// system alert never covers a recording in progress.
    static func requestAuthorizationIfNeeded() async {
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        guard settings.authorizationStatus == .notDetermined else { return }
        _ = try? await center.requestAuthorization(options: [.alert, .sound])
    }

    static func post(_ notice: LocalNotice, body: String? = nil) {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = body ?? notice.body
        content.sound = .default
        let request = UNNotificationRequest(identifier: notice.rawValue, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    static func withdraw(_ notice: LocalNotice) {
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [notice.rawValue])
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: [notice.rawValue])
    }
}

extension AudioCenter {
    /// Signed in with cookies restored: finish uploads a killed app left behind.
    func didSignIn() {
        releaseAbandonedRecordings(ChunkUploader.shared.resumePending(releaseMargin: AudioCenter.releaseTimeout))
        #if DEBUG
        // `-LooreDebugListenNode <id>`: play a node's audio in the global player
        // at launch (the speaker icon's path) until the thread view lands (M2).
        let id = UserDefaults.standard.integer(forKey: "LooreDebugListenNode")
        if id > 0 { listen(to: .node(id), content: nil) }
        #endif
    }
}

extension AudioCenter {
    /// Recordings this app was making when it was killed or crashed. The server
    /// counts a recording session as live for `ChunkUploader.serverLiveWindow`
    /// after its last chunk and keeps it out of the Voice screen's recovery
    /// banner until then; after the crash on the 2026-10-07 walk (#423) the
    /// Voice screen opened inside that window and offered nothing. Nothing
    /// records into these any more, so they are released now (the web's
    /// pagehide beacon does the same), but only while still inside the window
    /// (`ChunkUploader.mayRelease`).
    func releaseAbandonedRecordings(_ sessionIds: [String]) {
        guard !sessionIds.isEmpty, let api = app?.api else { return }
        releasingAbandoned = Task {
            await withTaskGroup(of: Void.self) { group in
                for sessionId in sessionIds {
                    group.addTask {
                        var request = APIRequest(.post, APIPath.streamingRelease(sessionId))
                        request.timeout = AudioCenter.releaseTimeout
                        _ = try? await api.data(for: request)
                    }
                }
            }
        }
    }

    /// INTRODUCED HEURISTIC: the longest a release may take. The Voice screen
    /// waits for the releases before it asks for unfinished recordings, so
    /// offline it stays blank for at most this long. Also taken off the window
    /// in which a relaunch releases (`ChunkUploader.mayRelease`), so a release
    /// still in flight lands while the server hides the session.
    static let releaseTimeout: TimeInterval = 5
}

/// App delegate for the background upload session (design doc §9.2): a relaunch
/// for its events hands the system's completion handler to the uploader.
final class LooreAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard identifier == ChunkUploader.backgroundIdentifier else {
            completionHandler()
            return
        }
        MainActor.assumeIsolated {
            ChunkUploader.shared.backgroundCompletionHandler = completionHandler
            ChunkUploader.shared.reconnectBackgroundSession()
        }
    }
}
