import AVFoundation
import Accelerate

/// A plain-text log per recording, kept on the phone (#423): the audio session's
/// mode and route, every route change and interruption, the microphone's
/// restarts and errors, the chunks written, and the input level once a second.
/// No audio and no words. It exists because iOS's own logs need Apple's newest
/// tools to read; the next time a recording goes wrong, this one says what the
/// audio system did.
///
/// One file per record tap, from activation until the session is deactivated or
/// the next recording begins; the newest `keep` files stay, and sign-out deletes
/// them all. Read them from a development build over the cable:
/// `xcrun devicectl device copy from --domain-type appDataContainer
///  --domain-identifier <bundle id> --source Library/Application\ Support/RecordingLogs …`
///
/// Thread-safe: lines are written on a private serial queue; `measure` runs on
/// the capture thread.
final class RecordingLog: @unchecked Sendable {
    static let shared = RecordingLog(directory: defaultDirectory)
    /// INTRODUCED CONSTANT: how many recordings' logs stay on the phone.
    static let keep = 20

    static var defaultDirectory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("RecordingLogs", isDirectory: true)
    }

    let directory: URL
    private let queue = DispatchQueue(label: "org.loore.audio.recording-log")
    private var handle: FileHandle?
    private var beganAt = Date()
    private(set) var currentFile: URL?

    private let levelLock = NSLock()
    private var level = LevelWindow()

    init(directory: URL) {
        self.directory = directory
    }

    // MARK: Files

    /// Starts a new log (closing the previous one) and prunes old ones.
    func begin(_ title: String) {
        let now = Date()
        queue.sync {
            closeLocked()
            let fileManager = FileManager.default
            try? fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
            var dir = directory
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try? dir.setResourceValues(values)
            pruneLocked(leaving: Self.keep - 1)
            let url = directory.appendingPathComponent(Self.fileName(for: now))
            fileManager.createFile(atPath: url.path, contents: nil)
            handle = try? FileHandle(forWritingTo: url)
            currentFile = handle == nil ? nil : url
            beganAt = now
        }
        levelLock.lock()
        level = LevelWindow()
        levelLock.unlock()
        let device = UIDeviceInfo.current
        note("\(title) · \(device) · time zone \(TimeZone.current.identifier)")
    }

    /// Appends a line, stamped with the local time and the seconds since `begin`.
    /// Does nothing when no log is open.
    func note(_ text: String) {
        let now = Date()
        queue.async { [self] in
            guard let handle else { return }
            let line = "\(Self.clock.string(from: now)) +\(String(format: "%.1f", now.timeIntervalSince(beganAt)))s  \(text)\n"
            handle.write(Data(line.utf8))
        }
    }

    /// Closes the current log (the session was deactivated).
    func end(_ text: String? = nil) {
        if let text { note(text) }
        queue.sync { closeLocked() }
    }

    /// Deletes every log (sign-out).
    func deleteAll() {
        queue.sync {
            closeLocked()
            try? FileManager.default.removeItem(at: directory)
        }
    }

    func files() -> [URL] {
        queue.sync { sortedFilesLocked() }
    }

    private func closeLocked() {
        try? handle?.close()
        handle = nil
        currentFile = nil
    }

    private func sortedFilesLocked() -> [URL] {
        let items = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return items.filter { $0.pathExtension == "log" }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    private func pruneLocked(leaving count: Int) {
        let files = sortedFilesLocked()
        for url in files.dropLast(max(count, 0)) {
            try? FileManager.default.removeItem(at: url)
        }
    }

    /// `recording-2026-10-05T11-05-27Z.log` (UTC, so names sort in time order).
    static func fileName(for date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyy-MM-dd'T'HH-mm-ss'Z'"
        return "recording-\(formatter.string(from: date)).log"
    }

    private static let clock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm:ss.SSS"
        return formatter
    }()

    // MARK: Input level

    /// Called with every captured buffer (capture thread). Writes a line for each
    /// second of audio: the RMS and peak level, so a log shows when the voice
    /// went quiet; and a line whenever the input format changes (16 kHz is a
    /// Bluetooth hands-free mic, 48 kHz usually the phone's own).
    func measure(_ buffer: AVAudioPCMBuffer) {
        guard buffer.frameLength > 0 else { return }
        let format = buffer.format
        var rms: Float = 0
        var peak: Float = 0
        if let channel = buffer.floatChannelData?[0] {
            vDSP_rmsqv(channel, 1, &rms, vDSP_Length(buffer.frameLength))
            vDSP_maxmgv(channel, 1, &peak, vDSP_Length(buffer.frameLength))
        } else if let channel = buffer.int16ChannelData?[0] {
            var floats = [Float](repeating: 0, count: Int(buffer.frameLength))
            vDSP_vflt16(channel, 1, &floats, 1, vDSP_Length(buffer.frameLength))
            var scale = Float(1) / Float(Int16.max)
            vDSP_vsmul(floats, 1, &scale, &floats, 1, vDSP_Length(buffer.frameLength))
            vDSP_rmsqv(floats, 1, &rms, vDSP_Length(buffer.frameLength))
            vDSP_maxmgv(floats, 1, &peak, vDSP_Length(buffer.frameLength))
        }
        levelLock.lock()
        let lines = level.add(frames: Int(buffer.frameLength), rms: rms, peak: peak,
                              sampleRate: format.sampleRate, channels: Int(format.channelCount))
        levelLock.unlock()
        for line in lines { note(line) }
    }
}

/// Sums one second of levels at a time (pure, for tests).
struct LevelWindow {
    private(set) var sampleRate: Double = 0
    private(set) var channels = 0
    private var frames = 0
    private var sumSquares: Double = 0
    private var peak: Float = 0

    /// Adds a buffer; returns the lines to log (a format change, a finished second).
    mutating func add(frames count: Int, rms: Float, peak bufferPeak: Float,
                      sampleRate rate: Double, channels channelCount: Int) -> [String] {
        var lines: [String] = []
        if rate != sampleRate || channelCount != channels {
            if frames > 0 { lines.append(flush()) }
            sampleRate = rate
            channels = channelCount
            lines.append("input format \(Int(rate)) Hz, \(channelCount) ch")
        }
        frames += count
        sumSquares += Double(rms) * Double(rms) * Double(count)
        peak = max(peak, bufferPeak)
        if Double(frames) >= sampleRate {
            lines.append(flush())
        }
        return lines
    }

    private mutating func flush() -> String {
        let rms = frames > 0 ? (sumSquares / Double(frames)).squareRoot() : 0
        let line = "level \(Self.dBFS(rms)) rms, peak \(Self.dBFS(Double(peak))) (\(String(format: "%.1f", Double(frames) / max(sampleRate, 1))) s)"
        frames = 0
        sumSquares = 0
        peak = 0
        return line
    }

    static func dBFS(_ value: Double) -> String {
        guard value > 0 else { return "-inf dBFS" }
        return String(format: "%.1f dBFS", 20 * log10(value))
    }
}

/// Model and OS for the log's first line.
enum UIDeviceInfo {
    static var current: String {
        var info = utsname()
        uname(&info)
        let model = withUnsafeBytes(of: &info.machine) { raw in
            String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
        }
        return "\(model), iOS \(ProcessInfo.processInfo.operatingSystemVersionString)"
    }
}
