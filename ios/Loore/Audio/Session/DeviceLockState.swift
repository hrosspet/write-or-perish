import UIKit

/// Whether the phone is locked, as far as the app can tell. `AudioCenter` reads it
/// for the Now Playing title: the entry's title and heading while unlocked, a fixed
/// title while locked.
@MainActor
protocol DeviceLockState: AnyObject {
    /// The phone is locked, or the app has not been told that it is unlocked.
    var isLocked: Bool { get }
    /// `isLocked` may have changed.
    var onChange: (() -> Void)? { get set }
    /// Audio started or stopped playing. Never calls `onChange` before it returns.
    func playbackChanged(playing: Bool)
}

/// The rules behind `SystemLockState`, without the system: events in, one answer out.
///
/// iOS has no "the screen locked" event for apps. What an app gets:
/// - `protectedDataWillBecomeUnavailable`, which arrives some seconds *after* the
///   lock (10 s with "Require Passcode: Immediately", Apple Platform Security,
///   "Data Protection classes"; later with a longer passcode delay), and only to an
///   app that is running: a paused app in the background is suspended and misses it;
/// - `protectedDataDidBecomeAvailable` at the unlock;
/// - `didEnterBackground`, which comes at once when the phone locks with the app in
///   front, and also when the person only leaves the app.
///
/// So: on screen the phone is unlocked. Off screen the answer is "locked" until
/// iOS reports an unlock while audio plays; hearing nothing never counts as
/// unlocked. A lock report, a pause (iOS may then suspend the app, which would
/// miss the next report) and leaving the screen each end the unlocked state.
/// What stays open is the time between a relock and its report: the heading
/// shown after an unlock at the lock screen stays until iOS reports the relock.
struct LockTimeline: Equatable {
    private(set) var inBackground: Bool
    private(set) var dataAvailable: Bool
    private(set) var playing = false
    /// Off screen: iOS reported an unlock, and no lock report or pause came since.
    private(set) var unlockReported = false

    init(inBackground: Bool, dataAvailable: Bool) {
        self.inBackground = inBackground
        self.dataAvailable = dataAvailable
    }

    mutating func enteredBackground() {
        inBackground = true
        unlockReported = false
    }

    mutating func becameActive(dataAvailable: Bool) {
        inBackground = false
        self.dataAvailable = dataAvailable
        unlockReported = false
    }

    mutating func dataWillBecomeUnavailable() {
        dataAvailable = false
        unlockReported = false
    }

    /// The unlock itself: the heading shows at once if audio plays.
    mutating func dataDidBecomeAvailable() {
        dataAvailable = true
        unlockReported = inBackground && playing
    }

    mutating func playbackChanged(playing: Bool) {
        self.playing = playing
        if !playing { unlockReported = false }
    }

    /// `systemDataAvailable` is `UIApplication.isProtectedDataAvailable` read now:
    /// it catches a lock whose notification the suspended app missed.
    func isLocked(systemDataAvailable: Bool = true) -> Bool {
        guard dataAvailable, systemDataAvailable else { return true }
        return inBackground && !unlockReported
    }
}

/// `LockTimeline` fed by UIKit's notifications.
///
/// A phone without a passcode never reports an unlock: it has the fixed title
/// whenever the app is off screen.
@MainActor
final class SystemLockState: DeviceLockState {
    var onChange: (() -> Void)?

    private var timeline: LockTimeline
    private var observers: [NSObjectProtocol] = []

    init() {
        let application = UIApplication.shared
        timeline = LockTimeline(inBackground: application.applicationState == .background,
                                dataAvailable: application.isProtectedDataAvailable)
        observe(UIApplication.didEnterBackgroundNotification) { $0.enteredBackground() }
        observe(UIApplication.didBecomeActiveNotification) {
            $0.becameActive(dataAvailable: UIApplication.shared.isProtectedDataAvailable)
        }
        observe(UIApplication.protectedDataWillBecomeUnavailableNotification) { $0.dataWillBecomeUnavailable() }
        observe(UIApplication.protectedDataDidBecomeAvailableNotification) { $0.dataDidBecomeAvailable() }
    }

    deinit {
        observers.forEach(NotificationCenter.default.removeObserver)
    }

    var isLocked: Bool {
        timeline.isLocked(systemDataAvailable: UIApplication.shared.isProtectedDataAvailable)
    }

    func playbackChanged(playing: Bool) {
        timeline.playbackChanged(playing: playing)
    }

    private func observe(_ name: Notification.Name, _ apply: @escaping (inout LockTimeline) -> Void) {
        observers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                apply(&self.timeline)
                self.onChange?()
            }
        })
    }
}
