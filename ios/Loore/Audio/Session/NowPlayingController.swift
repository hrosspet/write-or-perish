import MediaPlayer
import SwiftUI

/// Lock-screen metadata and remote commands per phase (design doc §9.5, map C
/// §6.5): the web's Media Session mapping, done with `MPNowPlayingInfoCenter` and
/// `MPRemoteCommandCenter`.
///
/// | Phase | Title | Commands |
/// |---|---|---|
/// | recording | `Recording m:ss` / `Paused m:ss` | play = resume, pause = pause, next = stop and send |
/// | thinking | `Voice…` | none (#397: the web's next = cancel read as "next track" and only saved a TTS call) |
/// | playback | `Voice` (+ chapter) or the listen-aloud title | play/pause, ±10 s, seek, rate |
///
/// The recording row is the fallback for when the voice Live Activity is not shown.
/// "Next track" has a handler only while that row uses it, so iOS has no reason to
/// lay out previous/next buttons in the other phases.
@MainActor
final class NowPlayingController {
    enum Phase: Equatable {
        case none
        case recording(elapsed: Double, paused: Bool)
        case thinking(title: String)
        case playback(title: String, elapsed: Double, duration: Double, rate: Float, playing: Bool)
    }

    struct Handlers {
        var play: () -> Void = {}
        var pause: () -> Void = {}
        var next: () -> Void = {}
        var skip: (Double) -> Void = { _ in }
        var seek: (Double) -> Void = { _ in }
        var rate: (Float) -> Void = { _ in }
    }

    private(set) var phase: Phase = .none
    var handlers = Handlers()

    private let center = MPRemoteCommandCenter.shared()
    private var registered = false
    private var nextTarget: Any?

    private lazy var artwork: MPMediaItemArtwork = {
        let renderer = ImageRenderer(content:
            ZStack {
                Color(UIColor(hex: 0x0E0D0B, alpha: 1))
                LooreLogo(size: 360, color: Color(UIColor(hex: 0xC4956A, alpha: 1)))
            }
            .frame(width: 512, height: 512)
        )
        renderer.scale = 1
        let image = renderer.uiImage ?? UIImage()
        return MPMediaItemArtwork(boundsSize: image.size) { _ in image }
    }()

    nonisolated static func recordingTitle(elapsed: Double, paused: Bool) -> String {
        let time = AudioTimeFormat.clock(elapsed)
        return paused ? "Paused \(time)" : "Recording \(time)"
    }

    func update(_ newPhase: Phase) {
        registerIfNeeded()
        phase = newPhase
        let info = MPNowPlayingInfoCenter.default()
        switch newPhase {
        case .none:
            info.nowPlayingInfo = nil
            info.playbackState = .stopped
            setNextTrackHandler(false)
            enable([])
        case .recording(let elapsed, let paused):
            info.nowPlayingInfo = [
                MPMediaItemPropertyTitle: Self.recordingTitle(elapsed: elapsed, paused: paused),
                MPMediaItemPropertyArtist: "Loore",
                MPMediaItemPropertyArtwork: artwork,
                MPNowPlayingInfoPropertyIsLiveStream: true,
                MPNowPlayingInfoPropertyPlaybackRate: paused ? 0.0 : 1.0,
            ]
            info.playbackState = paused ? .paused : .playing
            setNextTrackHandler(true)
            enable([center.playCommand, center.pauseCommand, center.togglePlayPauseCommand, center.nextTrackCommand])
        case .thinking(let title):
            // Nothing to control until the reply plays: every command is off.
            // Not a live stream, which iOS shows without ±10 s.
            info.nowPlayingInfo = [
                MPMediaItemPropertyTitle: title,
                MPMediaItemPropertyArtist: "Loore",
                MPMediaItemPropertyArtwork: artwork,
                MPNowPlayingInfoPropertyPlaybackRate: 1.0,
            ]
            info.playbackState = .playing
            setNextTrackHandler(false)
            enable([])
        case .playback(let title, let elapsed, let duration, let rate, let playing):
            info.nowPlayingInfo = [
                MPMediaItemPropertyTitle: title,
                MPMediaItemPropertyArtist: "Loore",
                MPMediaItemPropertyArtwork: artwork,
                MPMediaItemPropertyPlaybackDuration: duration,
                MPNowPlayingInfoPropertyElapsedPlaybackTime: elapsed,
                MPNowPlayingInfoPropertyPlaybackRate: playing ? Double(rate) : 0.0,
                MPNowPlayingInfoPropertyDefaultPlaybackRate: Double(rate),
            ]
            info.playbackState = playing ? .playing : .paused
            setNextTrackHandler(false)
            enable([center.playCommand, center.pauseCommand, center.togglePlayPauseCommand,
                    center.skipForwardCommand, center.skipBackwardCommand,
                    center.changePlaybackPositionCommand, center.changePlaybackRateCommand])
        }
    }

    private func enable(_ commands: [MPRemoteCommand]) {
        let all: [MPRemoteCommand] = [
            center.playCommand, center.pauseCommand, center.togglePlayPauseCommand, center.nextTrackCommand,
            center.previousTrackCommand, center.skipForwardCommand, center.skipBackwardCommand,
            center.changePlaybackPositionCommand, center.changePlaybackRateCommand, center.stopCommand,
        ]
        let on = Set(commands.map(ObjectIdentifier.init))
        for command in all {
            command.isEnabled = on.contains(ObjectIdentifier(command))
        }
    }

    private func setNextTrackHandler(_ on: Bool) {
        if on, nextTarget == nil {
            nextTarget = center.nextTrackCommand.addTarget { [weak self] _ in
                MainActor.assumeIsolated { self?.handlers.next() }
                return .success
            }
        } else if !on, let target = nextTarget {
            center.nextTrackCommand.removeTarget(target)
            nextTarget = nil
        }
    }

    private func registerIfNeeded() {
        guard !registered else { return }
        registered = true
        center.skipForwardCommand.preferredIntervals = [10]
        center.skipBackwardCommand.preferredIntervals = [10]
        center.changePlaybackRateCommand.supportedPlaybackRates = ChunkQueuePlayer.rates.map { NSNumber(value: $0) }

        center.playCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.handlers.play() }
            return .success
        }
        center.pauseCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.handlers.pause() }
            return .success
        }
        center.togglePlayPauseCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                switch self.phase {
                case .recording(_, let paused): paused ? self.handlers.play() : self.handlers.pause()
                case .playback(_, _, _, _, let playing): playing ? self.handlers.pause() : self.handlers.play()
                default: break
                }
            }
            return .success
        }
        center.skipForwardCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.handlers.skip(10) }
            return .success
        }
        center.skipBackwardCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.handlers.skip(-10) }
            return .success
        }
        center.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            let position = event.positionTime
            MainActor.assumeIsolated { self?.handlers.seek(position) }
            return .success
        }
        center.changePlaybackRateCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackRateCommandEvent else { return .commandFailed }
            let rate = event.playbackRate
            MainActor.assumeIsolated { self?.handlers.rate(rate) }
            return .success
        }
    }
}
