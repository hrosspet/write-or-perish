import AVFoundation
import XCTest
@testable import Loore

/// An engine whose input formats a test sets. Like AVFAudio, it refuses a tap
/// whose sample rate is not the hardware's (AVFAudio raises an exception there,
/// which `AVCaptureEngine` turns into an `ObjCExceptionError`).
final class FakeCaptureEngine: CaptureEngine {
    var hardware: AVAudioFormat
    var output: AVAudioFormat
    /// Thrown by the next taps, as a caught AVFAudio exception would be.
    var tapError: Error?
    private(set) var isRunning = false
    private(set) var stopped = false
    private(set) var tapFormat: AVAudioFormat?
    private(set) var tapAttempts = 0
    private var tap: ((AVAudioPCMBuffer) -> Void)?

    init(hardware: Double, output: Double? = nil) {
        self.hardware = Self.format(hardware)
        self.output = Self.format(output ?? hardware)
    }

    static func format(_ rate: Double) -> AVAudioFormat {
        if rate == 0 { return AVAudioFormat() }
        return AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
    }

    var notificationObject: AnyObject { self }

    func inputFormats() throws -> (hardware: AVAudioFormat, output: AVAudioFormat) { (hardware, output) }

    func installTap(format: AVAudioFormat, bufferSize: AVAudioFrameCount,
                    block: @escaping (AVAudioPCMBuffer) -> Void) throws {
        tapAttempts += 1
        if let tapError { throw tapError }
        guard tap == nil, format.sampleRate == hardware.sampleRate, hardware.channelCount > 0 else {
            throw ObjCExceptionError(name: "com.apple.coreaudio.avfaudio",
                                     reason: "required condition is false: format.sampleRate == hwFormat.sampleRate")
        }
        tapFormat = format
        tap = block
    }

    func removeTap() { tap = nil }
    func start() throws { isRunning = true }
    func pause() { isRunning = false }

    func stop() {
        isRunning = false
        stopped = true
    }

    var hasTap: Bool { tap != nil }

    /// One buffer through the tap.
    func deliver() {
        guard let buffer = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: 1_600) else { return }
        buffer.frameLength = 1_600
        tap?(buffer)
    }
}

/// The paths that crashed on the 2026-10-07 walk (#423): the headphones went off
/// mid-recording, the mic restarted on a stale 16 kHz format and delivered
/// nothing, and the watchdog's restart installed a tap AVFAudio refused with an
/// exception (`MicrophoneSource.installTap()`, AudioSources.swift:90).
final class MicrophoneSourceTests: XCTestCase {
    private var directory: URL!
    private var recordingLog: RecordingLog!
    /// Engines the source gets next; after them, one at 48 kHz.
    private var upcoming: [FakeCaptureEngine] = []
    private var made: [FakeCaptureEngine] = []
    private var failures: [Error] = []

    override func setUp() {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("loore-mic-\(UUID().uuidString)")
        recordingLog = RecordingLog(directory: directory)
        recordingLog.begin("test")
        upcoming = []
        made = []
        failures = []
    }

    override func tearDown() {
        recordingLog.end()
        try? FileManager.default.removeItem(at: directory)
    }

    private func makeSource() -> MicrophoneSource {
        let source = MicrophoneSource(recordingLog: recordingLog) { [unowned self] in
            let engine = self.upcoming.isEmpty ? FakeCaptureEngine(hardware: 48_000) : self.upcoming.removeFirst()
            self.made.append(engine)
            return engine
        }
        source.retryDelays = [0.01, 0.01, 0.01]
        source.onFailure = { [unowned self] in self.failures.append($0) }
        return source
    }

    private func logText() throws -> String {
        let file = try XCTUnwrap(recordingLog.currentFile)
        _ = recordingLog.files()  // waits for the log's queue
        return try String(contentsOf: file, encoding: .utf8)
    }

    private func spin(until condition: () -> Bool, timeout: TimeInterval = 2) {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
        }
    }

    // MARK: The tap's format

    func testTapFormatIsTheInputNodesWhenItMatchesTheHardware() throws {
        let format = try MicrophoneSource.tapFormat(hardware: FakeCaptureEngine.format(48_000),
                                                    output: FakeCaptureEngine.format(48_000))
        XCTAssertEqual(format.sampleRate, 48_000)
    }

    // The walk: the input node kept the headset mic's 16 kHz, the phone's mic ran at 48 kHz.
    func testTapFormatRefusesAnInputNodeThatLagsTheHardware() {
        XCTAssertThrowsError(try MicrophoneSource.tapFormat(hardware: FakeCaptureEngine.format(48_000),
                                                            output: FakeCaptureEngine.format(16_000))) { error in
            guard case MicrophoneSource.SourceError.formatMismatch(48_000, 16_000) = error else {
                return XCTFail("\(error)")
            }
        }
    }

    func testTapFormatRefusesAHardwareWithNoInput() {
        XCTAssertThrowsError(try MicrophoneSource.tapFormat(hardware: FakeCaptureEngine.format(0),
                                                            output: FakeCaptureEngine.format(0))) { error in
            guard case MicrophoneSource.SourceError.noInput = error else { return XCTFail("\(error)") }
        }
    }

    // MARK: Exceptions

    // The crash itself: an Objective-C exception raised under Swift ended the app.
    func testAnObjectiveCExceptionBecomesAnError() {
        let name = NSExceptionName("com.apple.coreaudio.avfaudio")
        let reason = "required condition is false: format.sampleRate == hwFormat.sampleRate"
        XCTAssertThrowsError(try ObjCException.catching { NSException(name: name, reason: reason).raise() }) { error in
            let caught = error as? ObjCExceptionError
            XCTAssertEqual(caught?.name, name.rawValue)
            XCTAssertEqual(caught?.reason, reason)
            XCTAssertEqual(AudioSessionController.describe(error), "exception \(name.rawValue): \(reason)")
        }
        XCTAssertNoThrow(try ObjCException.catching {})
    }

    // MARK: Restarts

    // Headphones off: the configuration change restarts on a new engine, tapped at
    // the phone mic's rate, and the old engine is stopped with its tap removed.
    func testRestartAfterAConfigurationChangeUsesANewEngineAtTheCurrentRate() throws {
        let headset = FakeCaptureEngine(hardware: 16_000)
        upcoming = [headset, FakeCaptureEngine(hardware: 48_000)]
        let source = makeSource()
        try source.start()
        XCTAssertEqual(headset.tapFormat?.sampleRate, 16_000)
        NotificationCenter.default.post(name: .AVAudioEngineConfigurationChange, object: headset)
        spin { made.count == 2 && made[1].isRunning }
        let phone = made[1]
        XCTAssertTrue(phone.isRunning)
        XCTAssertEqual(phone.tapFormat?.sampleRate, 48_000)
        XCTAssertTrue(headset.stopped)
        XCTAssertFalse(headset.hasTap)
        XCTAssertTrue(failures.isEmpty)
        XCTAssertTrue(try logText().contains("microphone restarted (attempt 1): 48000 Hz, 1 ch"))
        // Only the current engine's changes count.
        NotificationCenter.default.post(name: .AVAudioEngineConfigurationChange, object: headset)
        spin(until: { false }, timeout: 0.05)
        XCTAssertEqual(made.count, 2)
        source.stop()
        XCTAssertTrue(phone.stopped)
    }

    // The crash path: watchdog → restart → tap. An input node that still lags the
    // hardware is not tapped; the next attempt, on another new engine, works.
    func testWatchdogRestartOnALaggingFormatRetriesInsteadOfCrashing() throws {
        let headset = FakeCaptureEngine(hardware: 16_000)
        let lagging = FakeCaptureEngine(hardware: 48_000, output: 16_000)
        upcoming = [headset, lagging, FakeCaptureEngine(hardware: 48_000)]
        let source = makeSource()
        try source.start()
        source.stallTimeout = 0  // no buffer since the start: stalled
        source.checkForStall()
        XCTAssertEqual(lagging.tapAttempts, 0, "never tapped with a format the hardware does not have")
        spin { made.count == 3 && made[2].isRunning }
        XCTAssertEqual(made[2].tapFormat?.sampleRate, 48_000)
        XCTAssertTrue(failures.isEmpty)
        let log = try logText()
        XCTAssertTrue(log.contains("no audio from the microphone"))
        XCTAssertTrue(log.contains("microphone restart failed (attempt 1): input node at 16000 Hz, hardware at 48000 Hz"))
        XCTAssertTrue(log.contains("microphone restarted (attempt 2)"))
        source.stop()
    }

    // A tap AVFAudio refuses every time is retried, then reported (the turn holds
    // with an alert), not a crash.
    func testATapRefusedEveryTimeIsReportedAfterTheRetries() throws {
        upcoming = [FakeCaptureEngine(hardware: 16_000)]
        let source = makeSource()
        try source.start()
        let refused = ObjCExceptionError(name: "com.apple.coreaudio.avfaudio", reason: "required condition is false")
        upcoming = (0..<4).map { _ in
            let engine = FakeCaptureEngine(hardware: 48_000)
            engine.tapError = refused
            return engine
        }
        NotificationCenter.default.post(name: .AVAudioEngineConfigurationChange, object: made[0])
        spin { !failures.isEmpty }
        XCTAssertEqual(failures.count, 1)
        XCTAssertTrue(failures.first is ObjCExceptionError)
        XCTAssertEqual(made.count, 5, "the start, then 1 + 3 retries")
        XCTAssertTrue(try logText().contains("microphone restart failed (attempt 4): exception com.apple.coreaudio.avfaudio"))
        source.stop()
    }

    // MARK: Resume

    // The lock-screen Resume during a headset hold: the engine kept running, so
    // Resume starts nothing (iOS refuses a new mic from the background, #397).
    func testResumeWithTheEngineRunningKeepsIt() throws {
        let source = makeSource()
        try source.start()
        try source.resume()
        XCTAssertEqual(made.count, 1)
        XCTAssertTrue(made[0].isRunning)
        source.stop()
    }

    // After a call (or a microphone the watchdog gave up on) the route may have
    // changed: Resume starts a new engine at the current rate.
    func testResumeAfterAnInterruptionStartsANewEngine() throws {
        upcoming = [FakeCaptureEngine(hardware: 16_000), FakeCaptureEngine(hardware: 48_000)]
        let source = makeSource()
        try source.start()
        source.pause()
        XCTAssertFalse(made[0].isRunning)
        try source.resume()
        XCTAssertEqual(made.count, 2)
        XCTAssertTrue(made[1].isRunning)
        XCTAssertEqual(made[1].tapFormat?.sampleRate, 48_000)
        XCTAssertTrue(made[0].stopped)
        made[1].deliver()
        source.stallTimeout = 60
        source.checkForStall()
        XCTAssertEqual(made.count, 2, "a delivering microphone is left alone")
        source.stop()
    }

    func testResumeWithNoInputThrows() throws {
        upcoming = [FakeCaptureEngine(hardware: 16_000), FakeCaptureEngine(hardware: 0)]
        let source = makeSource()
        try source.start()
        source.pause()
        XCTAssertThrowsError(try source.resume())
        XCTAssertTrue(try logText().contains("microphone did not resume: no input"))
        source.stop()
    }
}
