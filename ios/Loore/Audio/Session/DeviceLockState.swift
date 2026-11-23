import UIKit

/// Whether the phone is locked, as far as the app can tell. `AudioCenter` reads it
/// for the Now Playing title: the entry's title and heading while unlocked, a fixed
/// title while locked.
@MainActor
protocol DeviceLockState: AnyObject {
    /// The phone is locked, or the app cannot yet tell that it is not.
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
/// So in the background the answer is "locked" until the app has played without
/// pause for `settleSeconds` and no lock was reported in that time: a lock with
/// the app in front never shows the heading, and a paused app (which iOS may
/// suspend at any moment) leaves the fixed title behind. An unlock is the one
/// event that ends the wait at once. What stays open is a lock while the app
/// plays behind another app: the heading stays until iOS reports the lock.
struct LockTimeline: Equatable {
    /// How long background playback must go on, with no lock reported, before the
    /// heading shows again. A guess that the owner should see: twice the 10 s iOS
    /// waits before it reports a lock when the passcode is required immediately.
    static let settleSeconds: TimeInterval = 20

    private(set) var inBackground: Bool
    private(set) var dataAvailable: Bool
    private(set) var playing = false
    /// In the background: when the heading may show again; nil = not until an event.
    private(set) var revealAt: Date?

    init(inBackground: Bool, dataAvailable: Bool) {
        self.inBackground = inBackground
        self.dataAvailable = dataAvailable
    }

    mutating func enteredBackground(now: Date) {
        inBackground = true
        revealAt = playing ? now.addingTimeInterval(Self.settleSeconds) : nil
    }

    /// The app is on screen, so the phone is unlocked.
    mutating func becameActive(dataAvailable: Bool) {
        inBackground = false
        self.dataAvailable = dataAvailable
        revealAt = nil
    }

    mutating func dataWillBecomeUnavailable() {
        dataAvailable = false
        revealAt = nil
    }

    /// The unlock itself: the heading shows at once if audio plays.
    mutating func dataDidBecomeAvailable(now: Date) {
        dataAvailable = true
        revealAt = playing ? now : nil
    }

    mutating func playbackChanged(playing: Bool, now: Date) {
        guard playing != self.playing else { return }
        self.playing = playing
        if inBackground {
            revealAt = playing ? now.addingTimeInterval(Self.settleSeconds) : nil
        }
    }

    /// `systemDataAvailable` is `UIApplication.isProtectedDataAvailable` read now:
    /// it catches a lock whose notification the suspended app missed.
    func isLocked(now: Date, systemDataAvailable: Bool = true) -> Bool {
        guard inBackground else { return false }
        guard dataAvailable, systemDataAvailable, let revealAt else { return true }
        return now < revealAt
    }
}

/// `LockTimeline` fed by UIKit's notifications.
///
/// A phone without a passcode never reports a lock: after `settleSeconds` of
/// background playback it shows the heading, also on its lock screen.
@MainActor
final class SystemLockState: DeviceLockState {
    var onChange: (() -> Void)?

    private var timeline: LockTimeline
    private var observers: [NSObjectProtocol] = []
    private var settle: Task<Void, Never>?

    init() {
        let application = UIApplication.shared
        timeline = LockTimeline(inBackground: application.applicationState == .background,
                                dataAvailable: application.isProtectedDataAvailable)
        observe(UIApplication.didEnterBackgroundNotification) { $0.enteredBackground(now: Date()) }
        observe(UIApplication.didBecomeActiveNotification) {
            $0.becameActive(dataAvailable: UIApplication.shared.isProtectedDataAvailable)
        }
        observe(UIApplication.protectedDataWillBecomeUnavailableNotification) { $0.dataWillBecomeUnavailable() }
        observe(UIApplication.protectedDataDidBecomeAvailableNotification) { $0.dataDidBecomeAvailable(now: Date()) }
    }

    deinit {
        observers.forEach(NotificationCenter.default.removeObserver)
        settle?.cancel()
    }

    var isLocked: Bool {
        timeline.isLocked(now: Date(), systemDataAvailable: UIApplication.shared.isProtectedDataAvailable)
    }

    func playbackChanged(playing: Bool) {
        let before = timeline
        timeline.playbackChanged(playing: playing, now: Date())
        if timeline != before { scheduleSettle() }
    }

    private func observe(_ name: Notification.Name, _ apply: @escaping (inout LockTimeline) -> Void) {
        observers.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                apply(&self.timeline)
                self.scheduleSettle()
                self.onChange?()
            }
        })
    }

    /// Reports the change when the wait in the background is over.
    private func scheduleSettle() {
        settle?.cancel()
        settle = nil
        guard let revealAt = timeline.revealAt else { return }
        let wait = revealAt.timeIntervalSinceNow
        guard wait > 0 else { return }
        settle = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64((wait + 0.05) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.onChange?()
        }
    }
}
