import XCTest

/// A whole voice turn in the simulator with the Debug audio-file source
/// (`-LooreDebugAudioFile`): record → Thinking… → the reply plays by itself.
/// BILLED on the local backend (transcription, reply, TTS): run it on purpose.
///
/// Environment (`TEST_RUNNER_` prefix for xcodebuild):
/// - `LOORE_SESSION_COOKIE`: session cookie for the test user (`ios/scripts/local_backend.sh session-cookie`).
/// - `LOORE_AUDIO_FILE`: absolute path of a short spoken clip (`say -o clip.m4a …`).
/// - `LOORE_VOICE_RECOVERY`: `continue` or `discard` when an unfinished recording is offered (default `discard`).
/// - `LOORE_VOICE_ROUTE`: optional, e.g. `/voice?parent=123` to reply in a thread (default `/voice`).
/// - `LOORE_VOICE_CONTINUE`: `1` = after the reply, press Continue for a second turn (billed twice).
/// - `LOORE_SCREENSHOT_DIR`: optional; screenshots are written there as PNGs.
final class VoiceUITests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }

    override func setUp() {
        continueAfterFailure = false
    }

    private func snapshot(_ name: String) {
        let shot = XCUIScreen.main.screenshot()
        let attachment = XCTAttachment(screenshot: shot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let dir = env["LOORE_SCREENSHOT_DIR"], !dir.isEmpty {
            try? shot.pngRepresentation.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
    }

    func testVoiceTurnWithDebugAudioFile() throws {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 },
                                   "set TEST_RUNNER_LOORE_SESSION_COOKIE")
        let clip = try XCTUnwrap(env["LOORE_AUDIO_FILE"].flatMap { $0.isEmpty ? nil : $0 },
                                 "set TEST_RUNNER_LOORE_AUDIO_FILE")
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", "dark",
                                "-LooreRoute", env["LOORE_VOICE_ROUTE"] ?? "/voice", "-LooreDebugAudioFile", clip]
        app.launch()

        let record = app.buttons["voice.record"]
        let recoveryContinue = app.buttons["voice.recovery.continue"]
        let deadline = Date().addingTimeInterval(20)
        while !record.exists && !recoveryContinue.exists && Date() < deadline { usleep(200_000) }
        if recoveryContinue.exists {
            snapshot("voice-00-recovery")
            if env["LOORE_VOICE_RECOVERY"] == "continue" {
                recoveryContinue.tap()
            } else {
                app.buttons["voice.recovery.discard"].tap()
                XCTAssertTrue(record.waitForExistence(timeout: 5))
                record.tap()
            }
        } else {
            XCTAssertTrue(record.exists, "no record button")
            snapshot("voice-01-ready")
            record.tap()
        }

        XCTAssertTrue(app.buttons["voice.stop"].waitForExistence(timeout: 10), "recording did not start")
        sleep(6)
        snapshot("voice-02-recording")

        // The clip ends by itself (the debug source acts as "stop and send").
        XCTAssertTrue(app.staticTexts["voice.thinking"].waitForExistence(timeout: 40), "never reached Thinking")
        sleep(2)
        snapshot("voice-03-thinking")

        let playPause = app.buttons["voice.playPause"]
        XCTAssertTrue(playPause.waitForExistence(timeout: 240), "the reply never started")
        snapshot("voice-04-playback-start")
        XCTAssertEqual(playPause.label, "Pause", "the reply should be playing without a tap")
        let firstTime = app.staticTexts.matching(identifier: "voice.time").firstMatch
        sleep(8)
        snapshot("voice-05-playback-8s")
        print("VOICE-UITEST playPause=\(playPause.label) time=\(firstTime.exists ? firstTime.label : "?")")

        // Let the reply finish (or cap the wait), then show the end state.
        waitForEnd(playPause)
        snapshot("voice-06-end")
        XCTAssertTrue(app.buttons["voice.continue"].exists)

        guard env["LOORE_VOICE_CONTINUE"] == "1" else { return }
        app.buttons["voice.continue"].tap()
        XCTAssertTrue(app.buttons["voice.stop"].waitForExistence(timeout: 10), "Continue did not start recording")
        sleep(4)
        snapshot("voice-07-continue-recording")
        XCTAssertTrue(app.staticTexts["voice.thinking"].waitForExistence(timeout: 40), "second turn never reached Thinking")
        XCTAssertTrue(playPause.waitForExistence(timeout: 240), "the second reply never started")
        XCTAssertEqual(playPause.label, "Pause", "the second reply should play without a tap")
        sleep(4)
        snapshot("voice-08-second-reply")
        waitForEnd(playPause)
        snapshot("voice-09-second-end")
    }

    private func waitForEnd(_ playPause: XCUIElement) {
        let end = Date().addingTimeInterval(150)
        while playPause.exists && playPause.label == "Pause" && Date() < end { sleep(3) }
    }
}
