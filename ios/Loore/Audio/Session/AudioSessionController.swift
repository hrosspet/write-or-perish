import AVFoundation
import os

/// Owns `AVAudioSession` configuration and its notifications (design doc §9.1).
///
/// - Voice turn: `.playAndRecord`, mode `.default`, Bluetooth HFP + A2DP, speaker
///   by default; activated at the record tap and kept active until the turn's
///   audio is finished (a non-mixable session cannot be re-activated from the
///   background).
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
        /// Headphones or another output went away (HIG: pause playback).
        case oldDeviceUnavailable
        case mediaServicesReset
    }

    private(set) var mode: Mode = .inactive
    var onEvent: ((Event) -> Void)?

    private let session = AVAudioSession.sharedInstance()
    private var observers: [NSObjectProtocol] = []
    private let log = Logger(subsystem: "org.loore.app", category: "audio-session")

    init() {
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
                    self.onEvent?(.interruptionBegan)
                case .ended:
                    let resume = AVAudioSession.InterruptionOptions(rawValue: rawOptions).contains(.shouldResume)
                    self.log.info("interruption ended (shouldResume \(resume))")
                    self.onEvent?(.interruptionEnded(shouldResume: resume))
                @unknown default:
                    break
                }
            }
        })
        observers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification,
                                            object: session, queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            MainActor.assumeIsolated {
                guard let raw, AVAudioSession.RouteChangeReason(rawValue: raw) == .oldDeviceUnavailable else { return }
                self?.onEvent?(.oldDeviceUnavailable)
            }
        })
        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
                                            object: session, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.log.error("media services were reset")
                self?.mode = .inactive
                self?.onEvent?(.mediaServicesReset)
            }
        })
    }

    /// At the record tap (foreground).
    func activateForRecording() throws {
        try session.setCategory(.playAndRecord, mode: .default,
                                options: [.allowBluetoothHFP, .allowBluetoothA2DP, .defaultToSpeaker])
        try? session.setPrefersNoInterruptionsFromSystemAlerts(true)
        try session.setActive(true)
        mode = .recording
    }

    /// At Stop, on the still-active session: the reply over A2DP (design §9.1).
    /// Falls back to staying in `.playAndRecord` if the switch is refused.
    func switchToPlaybackAfterRecording() {
        try? session.setPrefersNoInterruptionsFromSystemAlerts(false)
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
