import XCTest

/// Voice pieces inside M2's screens, against the local backend.
///
/// Environment (`TEST_RUNNER_` prefix): `LOORE_SESSION_COOKIE`; `LOORE_NODE_ID`
/// (a test-user node that already has TTS, so listening is not billed);
/// `LOORE_AUDIO_FILE` for dictation (transcription is billed);
/// `LOORE_SCREENSHOT_DIR` optional.
final class VoiceWiringUITests: XCTestCase {
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

    private func launch(route: String, extra: [String] = []) throws -> XCUIApplication {
        let cookie = try XCTUnwrap(env["LOORE_SESSION_COOKIE"].flatMap { $0.isEmpty ? nil : $0 })
        let app = XCUIApplication()
        app.launchArguments += ["-LooreEnvironment", "local", "-LooreSessionCookie", cookie,
                                "-LooreSkipUpdates", "YES", "-LooreTheme", "dark", "-LooreRoute", route] + extra
        app.launch()
        return app
    }

    /// Speaker → the node's audio plays in the mini-player; download → share sheet.
    func testSpeakerAndDownloadOnAThread() throws {
        let nodeId = try XCTUnwrap(env["LOORE_NODE_ID"].flatMap(Int.init), "set TEST_RUNNER_LOORE_NODE_ID")
        let app = try launch(route: "/node/\(nodeId)")
        let speaker = app.buttons["speaker.\(nodeId)"]
        XCTAssertTrue(speaker.waitForExistence(timeout: 20), "no speaker icon")
        snapshot("wiring-01-thread-footer")
        speaker.tap()
        let playPause = app.buttons["miniPlayer.playPause"]
        XCTAssertTrue(playPause.waitForExistence(timeout: 20), "the mini-player did not appear")
        sleep(3)
        XCTAssertEqual(playPause.label, "Pause")
        snapshot("wiring-02-mini-player")
        app.buttons["miniPlayer.playPause"].tap()
        XCTAssertEqual(playPause.label, "Play")

        app.buttons["download.\(nodeId)"].tap()
        sleep(4)
        snapshot("wiring-03-download-share-sheet")
    }

    /// Record into the reply form with the debug audio file: the transcript
    /// lands in the editor (text-mode path, no reply). Discards the draft after.
    func testDictationIntoTheReplyForm() throws {
        let nodeId = try XCTUnwrap(env["LOORE_NODE_ID"].flatMap(Int.init), "set TEST_RUNNER_LOORE_NODE_ID")
        let clip = try XCTUnwrap(env["LOORE_AUDIO_FILE"].flatMap { $0.isEmpty ? nil : $0 })
        let app = try launch(route: "/node/\(nodeId)", extra: ["-LooreDebugAudioFile", clip])
        let record = app.buttons["nodeForm.record"]
        XCTAssertTrue(record.waitForExistence(timeout: 20), "no Record button")
        record.tap()
        sleep(5)
        snapshot("wiring-04-dictation-recording")
        let editor = app.textViews["nodeForm.text"].exists ? app.textViews["nodeForm.text"] : app.textFields["nodeForm.text"]
        let deadline = Date().addingTimeInterval(120)
        while Date() < deadline {
            if let value = editor.value as? String, value.localizedCaseInsensitiveContains("river") { break }
            sleep(2)
        }
        snapshot("wiring-05-dictation-transcript")
        let value = (editor.value as? String) ?? ""
        print("DICTATION-UITEST transcript-length=\(value.count)")
        XCTAssertTrue(value.localizedCaseInsensitiveContains("river"), "the transcript did not reach the form")
        let discard = app.buttons["Discard draft"]
        if discard.exists { discard.tap() }
    }
}
