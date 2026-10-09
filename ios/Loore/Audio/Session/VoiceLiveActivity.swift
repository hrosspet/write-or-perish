import ActivityKit
import Foundation
import os

/// The voice conversation as a Live Activity (#397): Pause/Resume and Stop while
/// recording, and a Record button once Loore replies, so a turn can start
/// without unlocking the phone.
///
/// Why not the Now Playing controls: their three slots are previous / play-pause
/// / next, so Stop could only be "next track" next to a greyed-out "previous",
/// and their commands cannot start the microphone from the background. A Live
/// Activity button runs an `AudioRecordingIntent`, which can (iOS 18).
///
/// One activity per conversation: requested at a record tap (the app is in the
/// foreground then), updated when the phase changes, ended when the Voice
/// screen closes. Where there is none (iOS 17, Live Activities switched off in
/// Settings, or swiped away) the Now Playing recording controls stand in.
@MainActor
final class VoiceLiveActivity {
    typealias State = VoiceActivityAttributes.ContentState

    /// The activity went away by itself (swiped away, or ended by iOS).
    var onEnded: (() -> Void)?

    private var activity: Activity<VoiceActivityAttributes>?
    private var last: State?
    private var updates: Task<Void, Never>?
    private var watch: Task<Void, Never>?
    private let log = Logger(subsystem: "org.loore.app", category: "live-activity")

    var isShowing: Bool { activity != nil }

    /// The lock screen for the conversation's state. In a Glean-card conversation
    /// (#475) a glean in flight is its own phase, and `glean` is the Glean button
    /// where Record shows (it is not offered while recording or thinking).
    nonisolated static func state(turn: VoiceTurnController.State, isPaused: Bool, isInterrupted: Bool,
                                  awaitingNextNode: Bool, elapsed: Double, now: Date = Date(),
                                  glean: State.GleanButton? = nil, gleaning: Bool = false,
                                  gleanAccepted: Bool = false, gleaned: Bool = false) -> State {
        if gleaning {
            var state = State(phase: .gleaning)
            state.gleanAccepted = gleanAccepted
            return state
        }
        var state = turnState(turn: turn, isPaused: isPaused, isInterrupted: isInterrupted,
                              awaitingNextNode: awaitingNextNode, elapsed: elapsed, now: now)
        switch state.phase {
        case .ready, .replying, .finished:
            state.glean = glean
            state.gleaned = gleaned && state.phase == .ready
        default:
            break
        }
        return state
    }

    private nonisolated static func turnState(turn: VoiceTurnController.State, isPaused: Bool, isInterrupted: Bool,
                                              awaitingNextNode: Bool, elapsed: Double, now: Date) -> State {
        switch turn {
        case .idle:
            return State(phase: .ready)
        case .starting:
            // No clock and no buttons until the microphone is on: the clock would
            // run ahead, and Pause and Stop do nothing yet.
            return State(phase: .starting)
        case .recording:
            if isInterrupted { return State(phase: .interrupted, elapsed: Int(elapsed)) }
            if isPaused { return State(phase: .paused, elapsed: Int(elapsed)) }
            return State(phase: .recording, clockStart: now.addingTimeInterval(-elapsed))
        case .stopping:
            return State(phase: .sending)
        case .transcribing, .awaitingAudio:
            return State(phase: .thinking)
        case .draining:
            return State(phase: awaitingNextNode ? .thinking : .replying)
        case .playing:
            return State(phase: .replying)
        case .done:
            return State(phase: .finished)
        }
    }

    /// Shows `state`. With no activity yet, one is requested only when `start`
    /// (a recording is starting): a conversation whose activity was swiped away
    /// does not bring it back mid-turn.
    func sync(_ state: State, start: Bool) {
        guard #available(iOS 18.0, *) else { return }
        guard let activity else {
            if start { request(state) }
            return
        }
        // While recording the clock runs by itself; only a new phase is news.
        if state.phase == .recording && last?.phase == .recording { return }
        guard state != last else { return }
        last = state
        let previous = updates
        updates = Task {
            await previous?.value
            await activity.update(ActivityContent(state: state, staleDate: nil))
        }
    }

    /// The conversation is over (Voice screen closed, signed out).
    func end() {
        watch?.cancel()
        watch = nil
        guard let activity else { return }
        self.activity = nil
        last = nil
        let previous = updates
        updates = nil
        Task {
            await previous?.value
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }

    /// Activities a killed app left behind (a launch has no conversation yet).
    func endLeftovers() {
        let current = activity?.id
        Task {
            for leftover in Activity<VoiceActivityAttributes>.activities where leftover.id != current {
                await leftover.end(nil, dismissalPolicy: .immediate)
            }
        }
    }

    /// A button on a card whose conversation is gone (iOS ended the app between
    /// turns): remove the card and say why, instead of a tap that does nothing.
    nonisolated static func staleTap() async {
        for leftover in Activity<VoiceActivityAttributes>.activities {
            await leftover.end(nil, dismissalPolicy: .immediate)
        }
        LocalNotifier.post(.recordFailed, body: staleMessage)
    }

    nonisolated static let staleMessage =
        "Loore was closed in the meantime. Open Voice in Loore to continue the conversation."

    @available(iOS 18.0, *)
    private func request(_ state: State) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            log.info("Live Activities are off; the Now Playing controls stand in")
            return
        }
        do {
            let activity = try Activity.request(attributes: VoiceActivityAttributes(),
                                                content: ActivityContent(state: state, staleDate: nil))
            self.activity = activity
            last = state
            watch = Task { [weak self] in
                for await change in activity.activityStateUpdates where change == .dismissed || change == .ended {
                    self?.dropped(activity)
                    return
                }
            }
        } catch {
            log.error("Live Activity request failed: \(String(describing: error), privacy: .public)")
        }
    }

    private func dropped(_ gone: Activity<VoiceActivityAttributes>) {
        guard activity?.id == gone.id else { return }
        log.info("Live Activity dismissed")
        activity = nil
        last = nil
        watch = nil
        onEnded?()
    }
}

/// Where the Live Activity's intents land (their `perform` runs in the app).
/// `AudioCenter` sets the handler. When iOS launches the app in the background
/// for a tap, `perform` runs before SwiftUI creates `AppState`: no handler, and
/// no conversation either.
@MainActor
enum VoiceActivityCommands {
    static var handler: ((VoiceActivityCommand) async -> Void)?

    static func run(_ command: VoiceActivityCommand) async {
        guard let handler else {
            await VoiceLiveActivity.staleTap()
            return
        }
        await handler(command)
    }
}
