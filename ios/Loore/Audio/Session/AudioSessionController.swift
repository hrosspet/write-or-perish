import AVFoundation
import os

/// Owns `AVAudioSession` configuration and its notifications (design doc §9.1).
///
/// - Voice turn: `.playAndRecord`, mode `.videoChat`, Bluetooth HFP + A2DP, speaker
///   by default — what Safari sets when the web app opens the mic. The call-style
///   mode keeps a headset's mic as the input; mode `.default` let iOS move the
///   input to the phone's own mic mid-recording while the headphones kept playing
///   (#423). The headset mic is also set as the preferred input. Activated at the
///   record tap and kept active until the turn's audio is finished (a non-mixable
///   session cannot be re-activated from the background).
/// - At Stop: the still-active session switches to `.playback` / `.spokenAudio`
///   so the reply plays over A2DP rather than narrowband HFP.
/// - Listening outside voice mode: `.playback` / `.spokenAudio`.
/// - Deactivated with `.notifyOthersOnDeactivation` when audio ends.
@MainActor
final class AudioSessionController {
    enum Mode: Equatable { case inactive, recording, playback }

    enum Event {
        case interruptionBegan
        case interruptionEnded(shouldResume: Bool)
        /// Any route change. `.oldDeviceUnavailable` pauses playback (HIG); a
        /// recording whose headset mic went away is paused (#423).
        case routeChanged(reason: AVAudioSession.RouteChangeReason, from: AudioRoute, to: AudioRoute)
        case mediaServicesReset
    }

    private(set) var mode: Mode = .inactive
    var onEvent: ((Event) -> Void)?

    private let session = AVAudioSession.sharedInstance()
    private var observers: [NSObjectProtocol] = []
    private let log = Logger(subsystem: "org.loore.app", category: "audio-session")
    private let recordingLog: RecordingLog

    init(recordingLog: RecordingLog = .shared) {
        self.recordingLog = recordingLog
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: AVAudioSession.interruptionNotification,
                                            object: session, queue: .main) { [weak self] note in
            let info = note.userInfo ?? [:]
            let rawType = info[AVAudioSessionInterruptionTypeKey] as? UInt
            let rawOptions = info[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            MainActor.assumeIsolated {
                guard let self, let rawType, let type = AVAudioSession.InterruptionType(rawValue: rawType) else { return }
                switch type {
                case .began:
                    self.log.info("interruption began")
                    let reason = (info[AVAudioSessionInterruptionReasonKey] as? UInt).map { " (reason \($0))" } ?? ""
                    self.recordingLog.note("interruption began\(reason)")
                    self.onEvent?(.interruptionBegan)
                case .ended:
                    let resume = AVAudioSession.InterruptionOptions(rawValue: rawOptions).contains(.shouldResume)
                    self.log.info("interruption ended (shouldResume \(resume))")
                    self.recordingLog.note("interruption ended (shouldResume \(resume))")
                    self.onEvent?(.interruptionEnded(shouldResume: resume))
                @unknown default:
                    break
                }
            }
        })
        observers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification,
                                            object: session, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            let previous = note.userInfo?[AVAudioSessionRouteChangePreviousRouteKey] as? AVAudioSessionRouteDescription
            MainActor.assumeIsolated {
                guard let self else { return }
                let reason = raw.flatMap(AVAudioSession.RouteChangeReason.init(rawValue:)) ?? .unknown
                let from = previous.map(AudioRoute.init) ?? AudioRoute(inputs: [], outputs: [])
                let to = AudioRoute(self.session.currentRoute)
                self.recordingLog.note("route change (\(reason.name)): \(from.summary)  →  \(to.summary)")
                self.onEvent?(.routeChanged(reason: reason, from: from, to: to))
            }
        })
        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
                                            object: session, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.log.error("media services were reset")
                self?.recordingLog.note("media services were reset")
                self?.mode = .inactive
                self?.onEvent?(.mediaServicesReset)
            }
        })
    }

    /// At the record tap (foreground).
    /// - Parameter microphone: false for the Debug audio-file source, which needs
    ///   no input: in the simulator a `.playAndRecord` session opens the Mac's
    ///   microphone and waits on macOS's consent prompt.
    func activateForRecording(microphone: Bool = true) throws {
        recordingLog.begin(microphone ? "recording" : "recording (debug audio file)")
        do {
            if microphone {
                try session.setCategory(.playAndRecord, mode: .videoChat,
                                        options: [.allowBluetoothHFP, .allowBluetoothA2DP, .defaultToSpeaker])
            } else {
                try session.setCategory(.playback, mode: .spokenAudio, options: [])
            }
            try? session.setPrefersNoInterruptionsFromSystemAlerts(true)
            try session.setActive(true)
        } catch {
            recordingLog.end("activation failed: \(Self.describe(error))")
            throw error
        }
        mode = .recording
        recordingLog.note("session \(session.category.rawValue) / \(session.mode.rawValue), "
                          + "\(Int(session.sampleRate)) Hz: \(AudioRoute(session.currentRoute).summary)")
        if microphone { preferHeadsetInput() }
    }

    /// Keeps the headset's mic as the input for this recording (#423), as Safari
    /// does for the web app: after a Bluetooth glitch iOS returns to a preferred
    /// input rather than staying on the phone's own mic.
    private func preferHeadsetInput() {
        let available = (session.availableInputs ?? []).map(AudioRoute.Port.init)
        guard let port = AudioRoute.preferredHeadsetInput(current: AudioRoute(session.currentRoute), available: available),
              let description = session.availableInputs?.first(where: { $0.uid == port.uid }) else {
            recordingLog.note("no headset mic to prefer")
            return
        }
        do {
            try session.setPreferredInput(description)
            recordingLog.note("preferred input: \(port.type.rawValue) “\(port.name)”")
        } catch {
            recordingLog.note("preferred input refused: \(Self.describe(error))")
        }
    }

    private func clearPreferredInput() {
        guard session.preferredInput != nil else { return }
        try? session.setPreferredInput(nil)
    }

    /// For the recording log: the error's domain and code, or the reason of a
    /// caught AVFAudio exception (a format condition, no user content).
    nonisolated static func describe(_ error: Error) -> String {
        if let exception = error as? ObjCExceptionError { return "exception \(exception)" }
        if let sourceError = error as? MicrophoneSource.SourceError { return sourceError.description }
        let ns = error as NSError
        return "\(ns.domain) \(ns.code)"
    }

    /// At Stop, on the still-active session: the reply over A2DP (design §9.1).
    /// Falls back to staying in `.playAndRecord` if the switch is refused.
    func switchToPlaybackAfterRecording() {
        try? session.setPrefersNoInterruptionsFromSystemAlerts(false)
        clearPreferredInput()
        recordingLog.note("switching to playback for the reply")
        do {
            try session.setCategory(.playback, mode: .spokenAudio, options: [])
            try session.setActive(true)
            mode = .playback
        } catch {
            log.error("switch to playback refused; staying in playAndRecord")
        }
    }

    /// Listening outside voice mode, or a replay after the turn ended.
    func activateForPlayback() {
        guard mode != .playback else { return }
        do {
            if mode != .recording {
                clearPreferredInput()
                try session.setCategory(.playback, mode: .spokenAudio, options: [])
            }
            try session.setActive(true)
            if mode == .inactive { mode = .playback }
        } catch {
            log.error("playback activation failed")
        }
    }

    /// Re-activates after an interruption (resume recording or playback).
    func reactivate() throws {
        try session.setActive(true)
    }

    func deactivate() {
        guard mode != .inactive else { return }
        try? session.setPrefersNoInterruptionsFromSystemAlerts(false)
        clearPreferredInput()
        recordingLog.end("session deactivated")
        do {
            try session.setActive(false, options: .notifyOthersOnDeactivation)
        } catch {
            log.info("deactivate: session busy")
        }
        mode = .inactive
    }

    static var recordPermission: AVAudioApplication.recordPermission {
        AVAudioApplication.shared.recordPermission
    }

    static func requestRecordPermission() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }
}
