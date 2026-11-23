import XCTest
@testable import Loore

/// A lock state the tests set by hand.
@MainActor
final class FakeLockState: DeviceLockState {
    private(set) var isLocked: Bool
    var onChange: (() -> Void)?
    private(set) var playing: [Bool] = []

    init(locked: Bool) { isLocked = locked }

    func set(locked: Bool) {
        isLocked = locked
        onChange?()
    }

    func playbackChanged(playing: Bool) { self.playing.append(playing) }
}

/// `LockTimeline`: what the app concludes from the events iOS gives it.
final class LockTimelineTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_000_000)
    private func at(_ seconds: TimeInterval) -> Date { start.addingTimeInterval(seconds) }
    private let settle = LockTimeline.settleSeconds

    private func playingInFront() -> LockTimeline {
        var timeline = LockTimeline(inBackground: false, dataAvailable: true)
        timeline.playbackChanged(playing: true, now: start)
        return timeline
    }

    func testOnScreenTheAppIsUnlocked() {
        var timeline = LockTimeline(inBackground: false, dataAvailable: true)
        XCTAssertFalse(timeline.isLocked(now: start))
        timeline.playbackChanged(playing: true, now: start)
        XCTAssertFalse(timeline.isLocked(now: start))
        timeline.playbackChanged(playing: false, now: at(1))
        XCTAssertFalse(timeline.isLocked(now: at(1)))
    }

    func testLeavingTheAppCountsAsLockedUntilPlaybackWentOnForTheSettleTime() {
        var timeline = playingInFront()
        timeline.enteredBackground(now: at(5))
        XCTAssertTrue(timeline.isLocked(now: at(5)))
        XCTAssertTrue(timeline.isLocked(now: at(5 + settle - 0.1)))
        XCTAssertEqual(timeline.revealAt, at(5 + settle))
        XCTAssertFalse(timeline.isLocked(now: at(5 + settle)))
    }

    func testALockWithTheAppInFrontNeverCountsAsUnlocked() {
        var timeline = playingInFront()
        timeline.enteredBackground(now: at(5))          // the side button
        XCTAssertTrue(timeline.isLocked(now: at(6)))
        timeline.dataWillBecomeUnavailable()            // iOS reports the lock some seconds later
        XCTAssertNil(timeline.revealAt)
        XCTAssertTrue(timeline.isLocked(now: at(5 + settle + 1)))
        XCTAssertTrue(timeline.isLocked(now: at(3600)))
    }

    func testTheUnlockEndsTheWaitAtOnceWhileAudioPlays() {
        var timeline = playingInFront()
        timeline.enteredBackground(now: at(5))
        timeline.dataWillBecomeUnavailable()
        timeline.dataDidBecomeAvailable(now: at(100))
        XCTAssertFalse(timeline.isLocked(now: at(100)))
    }

    func testPausedInTheBackgroundCountsAsLocked() {
        var timeline = playingInFront()
        timeline.enteredBackground(now: at(0))
        XCTAssertFalse(timeline.isLocked(now: at(settle + 1)))
        timeline.playbackChanged(playing: false, now: at(settle + 2))
        XCTAssertTrue(timeline.isLocked(now: at(settle + 2)))
        XCTAssertTrue(timeline.isLocked(now: at(3600)))
        // Play again (Control Center, or the lock screen): the wait starts over.
        timeline.playbackChanged(playing: true, now: at(4000))
        XCTAssertTrue(timeline.isLocked(now: at(4000 + settle - 1)))
        XCTAssertFalse(timeline.isLocked(now: at(4000 + settle)))
    }

    func testAnUnlockWhilePausedInTheBackgroundKeepsTheFixedTitle() {
        var timeline = LockTimeline(inBackground: true, dataAvailable: false)
        timeline.dataDidBecomeAvailable(now: at(0))
        XCTAssertTrue(timeline.isLocked(now: at(60)))
    }

    func testALockTheAppWasNotToldAboutShowsInTheSystemsOwnAnswer() {
        var timeline = playingInFront()
        timeline.enteredBackground(now: at(0))
        XCTAssertFalse(timeline.isLocked(now: at(settle + 1), systemDataAvailable: true))
        XCTAssertTrue(timeline.isLocked(now: at(settle + 1), systemDataAvailable: false))
    }

    func testStartedInTheBackgroundTheAppCountsAsLockedUntilItIsOnScreen() {
        var timeline = LockTimeline(inBackground: true, dataAvailable: true)
        XCTAssertTrue(timeline.isLocked(now: at(3600)))
        timeline.becameActive(dataAvailable: true)
        XCTAssertFalse(timeline.isLocked(now: at(3601)))
    }

    func testBackOnScreenAfterALockTheAppIsUnlocked() {
        var timeline = playingInFront()
        timeline.enteredBackground(now: at(0))
        timeline.dataWillBecomeUnavailable()
        timeline.becameActive(dataAvailable: true)
        XCTAssertFalse(timeline.isLocked(now: at(30)))
    }

    func testRepeatedReportsOfTheSamePlaybackStateDoNotRestartTheWait() {
        var timeline = playingInFront()
        timeline.enteredBackground(now: at(0))
        timeline.playbackChanged(playing: true, now: at(settle - 1))
        XCTAssertFalse(timeline.isLocked(now: at(settle)))
    }
}
