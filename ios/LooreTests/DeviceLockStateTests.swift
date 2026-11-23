import AVFoundation
import MediaPlayer
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

/// Silent audio files for the player's tests, made once per name.
enum SilentAudio {
    /// The name has no `loore-` in front: every new `AppState` sweeps such files
    /// out of the temporary folder (`PrivateFiles.sweepTemporary`).
    static func file(_ name: String, seconds: Double) -> String {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("silent-\(name).wav")
        if !FileManager.default.fileExists(atPath: url.path) {
            let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 8000, channels: 1, interleaved: true)!
            let frames = AVAudioFrameCount(seconds * 8000)
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
            buffer.frameLength = frames
            if let file = try? AVAudioFile(forWriting: url, settings: format.settings, commonFormat: .pcmFormatInt16,
                                           interleaved: true) {
                try? file.write(from: buffer)
            }
        }
        return url.absoluteString
    }
}

/// `LockTimeline`: what the app concludes from the events iOS gives it.
final class LockTimelineTests: XCTestCase {
    private func playingInFront() -> LockTimeline {
        var timeline = LockTimeline(inBackground: false, dataAvailable: true)
        timeline.playbackChanged(playing: true)
        return timeline
    }

    private func playingBehindAReportedLock() -> LockTimeline {
        var timeline = playingInFront()
        timeline.enteredBackground()
        timeline.dataWillBecomeUnavailable()
        return timeline
    }

    func testOnScreenTheAppIsUnlocked() {
        var timeline = LockTimeline(inBackground: false, dataAvailable: true)
        XCTAssertFalse(timeline.isLocked())
        timeline.playbackChanged(playing: true)
        XCTAssertFalse(timeline.isLocked())
        timeline.playbackChanged(playing: false)
        XCTAssertFalse(timeline.isLocked())
    }

    func testOnScreenTheSystemsAnswerStillCounts() {
        var timeline = LockTimeline(inBackground: true, dataAvailable: false)
        timeline.becameActive(dataAvailable: false)
        XCTAssertTrue(timeline.isLocked(systemDataAvailable: false))
        XCTAssertTrue(timeline.isLocked(systemDataAvailable: true), "what the app was last told counts too")
        XCTAssertTrue(playingInFront().isLocked(systemDataAvailable: false))
        timeline.dataDidBecomeAvailable()
        XCTAssertFalse(timeline.isLocked())
    }

    func testOffScreenTheAppCountsAsLockedHoweverLongNothingIsReported() {
        var timeline = playingInFront()
        timeline.enteredBackground()                    // the side button, or only another app
        XCTAssertTrue(timeline.isLocked())
        // Hearing nothing changes nothing: the same answer for the rest of the playback.
        timeline.playbackChanged(playing: true)
        XCTAssertTrue(timeline.isLocked())
        timeline.dataWillBecomeUnavailable()            // iOS reports the lock some seconds later
        XCTAssertTrue(timeline.isLocked())
    }

    func testAReportedUnlockShowsTheHeadingAtOnceWhileAudioPlays() {
        var timeline = playingBehindAReportedLock()
        timeline.dataDidBecomeAvailable()
        XCTAssertFalse(timeline.isLocked())
    }

    func testUnlockAtTheLockScreenThenRelockIsLockedFromTheReportOn() {
        var timeline = playingBehindAReportedLock()
        timeline.dataDidBecomeAvailable()               // Face ID at the lock screen
        XCTAssertFalse(timeline.isLocked())
        timeline.dataWillBecomeUnavailable()            // the report of the relock
        XCTAssertTrue(timeline.isLocked())
    }

    func testAPauseOffScreenEndsTheUnlockedState() {
        var timeline = playingBehindAReportedLock()
        timeline.dataDidBecomeAvailable()
        XCTAssertFalse(timeline.isLocked())
        timeline.playbackChanged(playing: false)
        XCTAssertTrue(timeline.isLocked())
        // Play again (Control Center, or the lock screen): still no new unlock report.
        timeline.playbackChanged(playing: true)
        XCTAssertTrue(timeline.isLocked())
        timeline.dataWillBecomeUnavailable()
        timeline.dataDidBecomeAvailable()
        XCTAssertFalse(timeline.isLocked())
    }

    func testAnUnlockWhilePausedOffScreenKeepsTheFixedTitle() {
        var timeline = LockTimeline(inBackground: true, dataAvailable: false)
        timeline.dataDidBecomeAvailable()
        XCTAssertTrue(timeline.isLocked())
        timeline.playbackChanged(playing: true)
        XCTAssertTrue(timeline.isLocked())
    }

    func testACallWhileLockedAndTheResumeAfterItStayLocked() {
        var timeline = playingBehindAReportedLock()
        timeline.playbackChanged(playing: false)        // interruption began
        XCTAssertTrue(timeline.isLocked())
        timeline.playbackChanged(playing: true)         // interruption ended
        XCTAssertTrue(timeline.isLocked())
    }

    func testTheNextQueueItemWhileLockedStaysLocked() {
        var timeline = playingBehindAReportedLock()
        for _ in 0..<5 {
            timeline.playbackChanged(playing: false)
            timeline.playbackChanged(playing: true)
            XCTAssertTrue(timeline.isLocked())
        }
    }

    func testALockTheAppWasNotToldAboutShowsInTheSystemsOwnAnswer() {
        // Unlocked at the lock screen, then suspended and locked with no report.
        var timeline = playingBehindAReportedLock()
        timeline.dataDidBecomeAvailable()
        XCTAssertFalse(timeline.isLocked(systemDataAvailable: true))
        XCTAssertTrue(timeline.isLocked(systemDataAvailable: false))
    }

    func testPlayFromTheLockScreenAfterAMissedReportStaysLocked() {
        var timeline = LockTimeline(inBackground: false, dataAvailable: true)
        timeline.enteredBackground()                    // paused, then suspended; the phone locks
        timeline.playbackChanged(playing: true)         // play on the lock screen or headphones
        XCTAssertTrue(timeline.isLocked(systemDataAvailable: false))
        XCTAssertTrue(timeline.isLocked(systemDataAvailable: true))
    }

    func testStartedOffScreenTheAppCountsAsLockedUntilItIsOnScreen() {
        var timeline = LockTimeline(inBackground: true, dataAvailable: true)
        timeline.playbackChanged(playing: true)
        XCTAssertTrue(timeline.isLocked())
        timeline.becameActive(dataAvailable: true)
        XCTAssertFalse(timeline.isLocked())
    }

    func testBackOnScreenAfterALockTheAppIsUnlocked() {
        var timeline = playingBehindAReportedLock()
        timeline.dataDidBecomeAvailable()
        timeline.becameActive(dataAvailable: true)
        XCTAssertFalse(timeline.isLocked())
    }

    func testLeavingTheScreenAgainEndsAnEarlierUnlock() {
        var timeline = playingBehindAReportedLock()
        timeline.dataDidBecomeAvailable()
        timeline.becameActive(dataAvailable: true)
        timeline.enteredBackground()
        XCTAssertTrue(timeline.isLocked())
    }
}

/// The real `SystemLockState` inside `AudioCenter`, driven by the UIKit
/// notifications it observes (posted by hand: the simulator cannot lock).
@MainActor
final class SystemLockStateTests: StubbedAppTestCase {
    private var title: String? {
        MPNowPlayingInfoCenter.default().nowPlayingInfo?[MPMediaItemPropertyTitle] as? String
    }
    private var info: [String: Any]? { MPNowPlayingInfoCenter.default().nowPlayingInfo }

    private static let headings = [QueueChapter(title: "First heading", chunkIndex: 0, startTime: 0),
                                   QueueChapter(title: "Second heading", chunkIndex: 1, startTime: 6)]
    private static let one = SilentAudio.file("lock-one", seconds: 6)
    private static let two = SilentAudio.file("lock-two", seconds: 4.5)

    private func post(_ name: Notification.Name) {
        NotificationCenter.default.post(name: name, object: UIApplication.shared)
    }

    private func load(voice: Bool = false, title: String = "A morning entry", autoplay: Bool = false) {
        app.audio.player.load(urls: [Self.one, Self.two], durations: [6, 4.5], title: voice ? "Voice" : title,
                              source: voice ? .voice : .node(77), chapters: Self.headings, autoplay: autoplay)
    }

    override func tearDown() async throws {
        await MainActor.run {
            app.audio.player.stop()
            NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: UIApplication.shared)
        }
        try await super.tearDown()
    }

    private static let fixed: Set<String?> = ["Listening", "Voice", "Voice…", nil]

    /// Records the title after every player change.
    private func recordTitles() -> () -> [String?] {
        var titles: [String?] = []
        let previous = app.audio.player.onStateChange
        app.audio.player.onStateChange = { [unowned self] in
            previous?()
            titles.append(self.title)
        }
        return { titles }
    }

    func testTheSystemsLockStateIsInstalledAndOnScreenShowsTheHeading() {
        XCTAssertTrue(app.audio.lockState is SystemLockState)
        load()
        XCTAssertEqual(title, "A morning entry — First heading")
    }

    func testLockWithTheAppInFrontEverySequenceKeepsTheFixedTitle() {
        load()
        app.audio.player.play()
        XCTAssertEqual(title, "A morning entry — First heading")
        let titles = recordTitles()

        post(UIApplication.willResignActiveNotification)
        post(UIApplication.didEnterBackgroundNotification)          // side button
        XCTAssertEqual(title, "Listening", "fixed title at once, before any report")
        app.audio.player.seek(to: 7)                                // next heading
        XCTAssertEqual(app.audio.player.currentChapterIndex, 1)
        XCTAssertEqual(title, "Listening")
        post(UIApplication.protectedDataWillBecomeUnavailableNotification)

        // A call: interruption, then its end.
        app.audio.session.onEvent?(.interruptionBegan)
        XCTAssertFalse(app.audio.player.isPlaying)
        XCTAssertEqual(title, "Listening")
        app.audio.session.onEvent?(.interruptionEnded(shouldResume: true))
        XCTAssertEqual(title, "Listening")

        // Lock-screen and headphone buttons.
        app.audio.nowPlaying.handlers.pause()
        XCTAssertEqual(title, "Listening")
        app.audio.nowPlaying.handlers.play()
        app.audio.nowPlaying.handlers.skip(-10)
        app.audio.nowPlaying.handlers.seek(8)
        app.audio.nowPlaying.handlers.rate(1.5)
        XCTAssertEqual(title, "Listening")

        // The next queue item, another entry, and a Voice reply.
        load(title: "Another entry", autoplay: true)
        XCTAssertEqual(title, "Listening")
        load(voice: true, autoplay: true)
        XCTAssertEqual(title, "Voice")
        app.audio.refreshNowPlaying()
        XCTAssertEqual(title, "Voice")

        XCTAssertTrue(titles().allSatisfy { Self.fixed.contains($0) }, "\(titles())")

        // Unlock at the lock screen: the heading at once; the relock's report: fixed again.
        post(UIApplication.protectedDataDidBecomeAvailableNotification)
        XCTAssertEqual(title, "Voice — First heading")
        post(UIApplication.protectedDataWillBecomeUnavailableNotification)
        XCTAssertEqual(title, "Voice")

        // Unlock and open the app.
        post(UIApplication.protectedDataDidBecomeAvailableNotification)
        post(UIApplication.didBecomeActiveNotification)
        XCTAssertEqual(title, "Voice — First heading")
    }

    func testOffScreenWithNoReportTheTitleStaysFixed() {
        load()
        app.audio.player.play()
        post(UIApplication.didEnterBackgroundNotification)          // another app, or a lock not yet reported
        XCTAssertEqual(title, "Listening")
        app.audio.player.seek(to: 7)
        app.audio.refreshNowPlaying()
        XCTAssertEqual(title, "Listening")
        post(UIApplication.didBecomeActiveNotification)
        XCTAssertEqual(title, "A morning entry — Second heading")
    }

    func testAPauseAfterAnUnlockOffScreenPutsTheFixedTitleBack() {
        load()
        app.audio.player.play()
        post(UIApplication.didEnterBackgroundNotification)
        post(UIApplication.protectedDataWillBecomeUnavailableNotification)
        post(UIApplication.protectedDataDidBecomeAvailableNotification)
        XCTAssertEqual(title, "A morning entry — First heading")
        app.audio.player.pause()
        XCTAssertEqual(title, "Listening")
        app.audio.nowPlaying.handlers.play()
        XCTAssertEqual(title, "Listening")
    }

    func testAnUnlockWhilePausedBehindTheLockScreenKeepsTheFixedTitle() {
        load()
        post(UIApplication.didEnterBackgroundNotification)
        post(UIApplication.protectedDataWillBecomeUnavailableNotification)
        post(UIApplication.protectedDataDidBecomeAvailableNotification)
        XCTAssertEqual(title, "Listening")
    }

    func testTheRestOfTheInfoIsTheSameLockedAndUnlocked() {
        load()
        app.audio.player.seek(to: 3)
        let unlocked = info ?? [:]
        post(UIApplication.didEnterBackgroundNotification)
        let locked = info ?? [:]
        XCTAssertEqual(unlocked[MPMediaItemPropertyTitle] as? String, "A morning entry — First heading")
        XCTAssertEqual(locked[MPMediaItemPropertyTitle] as? String, "Listening")
        XCTAssertEqual(Set(unlocked.keys), Set(locked.keys))
        for key in [MPMediaItemPropertyPlaybackDuration, MPNowPlayingInfoPropertyElapsedPlaybackTime,
                    MPNowPlayingInfoPropertyPlaybackRate, MPNowPlayingInfoPropertyDefaultPlaybackRate] {
            XCTAssertEqual(unlocked[key] as? Double, locked[key] as? Double, key)
        }
        XCTAssertEqual(locked[MPMediaItemPropertyPlaybackDuration] as? Double, 10.5)
        XCTAssertEqual(locked[MPNowPlayingInfoPropertyElapsedPlaybackTime] as? Double, 3)
        XCTAssertEqual(locked[MPMediaItemPropertyArtist] as? String, "Loore")
        let center = MPRemoteCommandCenter.shared()
        for command in [center.playCommand, center.pauseCommand, center.togglePlayPauseCommand, center.skipForwardCommand,
                        center.skipBackwardCommand, center.changePlaybackPositionCommand, center.changePlaybackRateCommand] {
            XCTAssertTrue(command.isEnabled)
        }
        XCTAssertFalse(center.nextTrackCommand.isEnabled)
    }
}
