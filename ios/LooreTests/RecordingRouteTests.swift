import AVFoundation
import XCTest
@testable import Loore

/// #423: which route changes pause a recording, which mic a recording keeps, and
/// the recording log kept on the phone.
final class AudioRouteTests: XCTestCase {
    private let hfp = AudioRoute.Port(type: .bluetoothHFP, name: "WH-XB910N", uid: "AA:BB:CC:DD:EE:FF-tsco")
    private let a2dp = AudioRoute.Port(type: .bluetoothA2DP, name: "WH-XB910N", uid: "AA:BB:CC:DD:EE:FF-tacl")
    private let builtIn = AudioRoute.Port(type: .builtInMic, name: "iPhone Microphone", uid: "Built-In Microphone")
    private let speaker = AudioRoute.Port(type: .builtInSpeaker, name: "Speaker", uid: "Speaker")
    private let wired = AudioRoute.Port(type: .headsetMic, name: "Headset Microphone", uid: "Wired Headset")

    // The #423 route: the headphones keep playing, the input moves to the phone.
    func testHeadsetMicToPhoneMicIsALoss() {
        let before = AudioRoute(inputs: [hfp], outputs: [hfp])
        XCTAssertTrue(AudioRoute.headsetMicLost(from: before, to: AudioRoute(inputs: [builtIn], outputs: [a2dp])))
        XCTAssertTrue(AudioRoute.headsetMicLost(from: before, to: AudioRoute(inputs: [builtIn], outputs: [speaker])))
        XCTAssertTrue(AudioRoute.headsetMicLost(from: before, to: AudioRoute(inputs: [], outputs: [speaker])))
    }

    func testOtherChangesAreNotALoss() {
        let phone = AudioRoute(inputs: [builtIn], outputs: [speaker])
        let headset = AudioRoute(inputs: [hfp], outputs: [hfp])
        XCTAssertFalse(AudioRoute.headsetMicLost(from: phone, to: headset), "headphones arriving")
        XCTAssertFalse(AudioRoute.headsetMicLost(from: headset, to: AudioRoute(inputs: [wired], outputs: [speaker])),
                       "one headset for another")
        XCTAssertFalse(AudioRoute.headsetMicLost(from: phone, to: AudioRoute(inputs: [builtIn], outputs: [a2dp])))
        XCTAssertFalse(AudioRoute.headsetMicLost(from: headset, to: headset))
    }

    func testPreferredInputIsTheCurrentHeadsetMic() {
        let current = AudioRoute(inputs: [hfp], outputs: [hfp])
        XCTAssertEqual(AudioRoute.preferredHeadsetInput(current: current, available: [builtIn, hfp]), hfp)
    }

    // Right after activation iOS may still use the phone's mic while the headphones play.
    func testPreferredInputIsTheHandsFreeMicOfThePlayingHeadphones() {
        let current = AudioRoute(inputs: [builtIn], outputs: [a2dp])
        XCTAssertEqual(AudioRoute.preferredHeadsetInput(current: current, available: [builtIn, hfp]), hfp)
    }

    func testNoPreferredInputWithoutHeadphones() {
        let current = AudioRoute(inputs: [builtIn], outputs: [speaker])
        XCTAssertNil(AudioRoute.preferredHeadsetInput(current: current, available: [builtIn]))
        let otherDevice = AudioRoute.Port(type: .bluetoothHFP, name: "Car", uid: "11:22:33:44:55:66-tsco")
        XCTAssertNil(AudioRoute.preferredHeadsetInput(current: AudioRoute(inputs: [builtIn], outputs: [a2dp]),
                                                      available: [builtIn, otherDevice]),
                     "only the mic of the headphones that play")
    }

    func testSummaryHasTypesAndNamesButNoAddresses() {
        let summary = AudioRoute(inputs: [hfp], outputs: [hfp]).summary
        XCTAssertTrue(summary.contains("BluetoothHFP"))
        XCTAssertTrue(summary.contains("WH-XB910N"))
        XCTAssertFalse(summary.contains("AA:BB"))
    }
}

final class RecordingLogTests: XCTestCase {
    private var directory: URL!

    override func setUp() {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent("loore-test-logs-\(UUID().uuidString)")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: directory)
    }

    func testBeginWritesAFileThatEndCloses() throws {
        let log = RecordingLog(directory: directory)
        log.begin("recording")
        log.note("route change (oldDeviceUnavailable)")
        let file = try XCTUnwrap(log.currentFile)
        log.end("session deactivated")
        XCTAssertNil(log.currentFile)
        let text = try String(contentsOf: file, encoding: .utf8)
        XCTAssertTrue(text.contains("recording"))
        XCTAssertTrue(text.contains("route change (oldDeviceUnavailable)"))
        XCTAssertTrue(text.contains("session deactivated"))
        log.note("after the end")
        _ = log.files()  // waits for the log's queue
        XCTAssertFalse(try String(contentsOf: file, encoding: .utf8).contains("after the end"))
    }

    func testKeepsTheNewestLogs() throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let base = Date(timeIntervalSince1970: 1_790_000_000)
        for i in 0..<25 {
            let name = RecordingLog.fileName(for: base.addingTimeInterval(Double(i) * 60))
            FileManager.default.createFile(atPath: directory.appendingPathComponent(name).path, contents: Data())
        }
        let log = RecordingLog(directory: directory)
        log.begin("recording")
        let files = log.files().map(\.lastPathComponent)
        XCTAssertEqual(files.count, RecordingLog.keep)
        XCTAssertFalse(files.contains(RecordingLog.fileName(for: base)), "the oldest went")
        XCTAssertTrue(files.contains(try XCTUnwrap(log.currentFile).lastPathComponent))
        log.deleteAll()
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
    }

    func testFileNamesSortInTimeOrder() {
        let early = RecordingLog.fileName(for: Date(timeIntervalSince1970: 1_790_000_000))
        let late = RecordingLog.fileName(for: Date(timeIntervalSince1970: 1_790_000_000 + 3_600 * 13))
        XCTAssertLessThan(early, late)
        XCTAssertTrue(early.hasSuffix(".log"))
    }

    func testMeasureLogsTheFormatAndOneLineASecond() throws {
        let log = RecordingLog(directory: directory)
        log.begin("recording")
        let format = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1))
        for _ in 0..<5 {
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4_000))
            buffer.frameLength = 4_000
            for i in 0..<4_000 { buffer.floatChannelData![0][i] = 0.1 }
            log.measure(buffer)
        }
        let file = try XCTUnwrap(log.currentFile)
        log.end()
        let lines = try String(contentsOf: file, encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(lines.filter { $0.contains("input format 16000 Hz, 1 ch") }.count, 1)
        XCTAssertEqual(lines.filter { $0.contains("level -20.0 dBFS rms, peak -20.0 dBFS (1.0 s)") }.count, 1)
    }
}

final class LevelWindowTests: XCTestCase {
    func testAFormatChangeClosesTheSecondSoFar() {
        var window = LevelWindow()
        XCTAssertEqual(window.add(frames: 8_000, rms: 0.01, peak: 0.02, sampleRate: 16_000, channels: 1),
                       ["input format 16000 Hz, 1 ch"])
        let lines = window.add(frames: 4_800, rms: 0.01, peak: 0.02, sampleRate: 48_000, channels: 1)
        XCTAssertEqual(lines.count, 2)
        XCTAssertTrue(lines[0].hasPrefix("level -40.0 dBFS rms, peak -34.0 dBFS (0.5 s)"))
        XCTAssertEqual(lines[1], "input format 48000 Hz, 1 ch")
    }

    func testSilenceIsMinusInfinity() {
        XCTAssertEqual(LevelWindow.dBFS(0), "-inf dBFS")
        XCTAssertEqual(LevelWindow.dBFS(1), "0.0 dBFS")
    }
}
