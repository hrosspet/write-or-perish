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
        VoiceActivityCommands.handler = { [weak self] command in await self?.runLockScreenCommand(command) }
        observers.append(NotificationCenter.default.addObserver(forName: UIApplication.didBecomeActiveNotification,
                                                                object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.voiceController?.appDidBecomeActive() }
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
        voiceController = controller
        return controller
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
        activeDictation?.cancel()
        ChunkUploader.shared.reset()
        PrivateFiles.sweepTemporary()
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
        case .oldDeviceUnavailable:
            if player.isPlaying { player.pause() }
        case .mediaServicesReset:
            sounds.stopCue()
            if voiceActive { voiceController?.systemInterruptionBegan() }
            activeDictation?.systemInterruptionBegan()
            if player.isPlaying { player.pause() }
        }
    }

    @ObservationIgnored private var resumeAfterInterruption = false

    // MARK: Lock screen

    func refreshNowPlaying() {
        if let voice = voiceController {
            liveActivity.sync(VoiceLiveActivity.state(turn: voice.state, isPaused: voice.isPaused,
                                                      isInterrupted: voice.isInterrupted,
                                                      awaitingNextNode: voice.awaitingNextNode,
                                                      elapsed: voice.elapsed),
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
            case .stopping:
                // A second "next" while the last chunks upload does nothing (M11; web
                // stays in recording until the transcript, so next just stops again).
                nowPlaying.handlers = .init(next: { [weak voice] in voice?.stop() })
                nowPlaying.update(.thinking(title: "Voice…"))
                return
            case .transcribing, .awaitingAudio:
                nowPlaying.handlers = .init(next: { [weak voice] in voice?.cancelProcessing() })
                nowPlaying.update(.thinking(title: "Voice…"))
                return
            case .draining where voice.awaitingNextNode:
                nowPlaying.handlers = .init(next: { [weak voice] in voice?.cancelProcessing() })
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
            // Left over from an earlier launch: no conversation to control.
            liveActivity.endLeftovers()
            return
        }
        log.info("lock screen: \(command.rawValue, privacy: .public)")
        switch command {
        case .pause: voice.pauseRecording()
        case .resume: voice.resumeRecording()
        case .stop: voice.stop()
        case .record: await recordFromLockScreen(voice)
        }
    }

    /// Record from the lock screen: the next turn, also while the reply plays
    /// (the Voice screen's Continue), or a new one between turns.
    private func recordFromLockScreen(_ voice: VoiceTurnController) async {
        if app?.spendCapped == true {
            app?.notifySpendBlocked()
            _ = app?.toasts.show(SpendCap.toastMessage(.record), duration: 8)
            return
        }
        switch voice.phase {
        case .playback: voice.continueConversation()
        case .ready where voice.aiBlock == nil: voice.start()
        default: return
        }
        // The intent keeps the app running until it returns: wait for the microphone.
        for _ in 0..<150 where voice.state == .starting {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
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

    func startCue() { sounds.startCue() }
    func stopCue() { sounds.stopCue() }
    func playErrorSound() { sounds.playError() }
    func playInterruptionAlert() { sounds.playInterruptionAlert() }
    func playLongRecordingWarning() { sounds.playLongRecordingWarning() }
}

// MARK: VoiceNotices

extension AudioCenter: VoiceNotices {
    @discardableResult
    func toast(_ text: String, duration: TimeInterval) -> Int {
        app?.toasts.show(text, duration: duration) ?? 0
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

    static func post(_ notice: LocalNotice) {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
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
        ChunkUploader.shared.resumePending()
        #if DEBUG
        // `-LooreDebugListenNode <id>`: play a node's audio in the global player
        // at launch (the speaker icon's path) until the thread view lands (M2).
        let id = UserDefaults.standard.integer(forKey: "LooreDebugListenNode")
        if id > 0 { listen(to: .node(id), content: nil) }
        #endif
    }
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
