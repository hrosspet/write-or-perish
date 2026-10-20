import AVFoundation
import os

/// Where recorded PCM comes from: the microphone, or (Debug builds) a file.
protocol PCMSource: AnyObject {
    /// Called with each captured buffer, on the source's own thread.
    var onBuffer: ((AVAudioPCMBuffer) -> Void)? { get set }
    /// Called when a file source reaches its end (never for the microphone).
    /// Delivered on the main queue.
    var onEnd: (() -> Void)? { get set }
    func start() throws
    func pause()
    func resume() throws
    func stop()
}

/// The parts of `AVAudioEngine` the microphone uses. A seam, so tests can drive
/// the restart paths that crashed on the 2026-10-07 walk (#423).
protocol CaptureEngine: AnyObject {
    /// The object `AVAudioEngineConfigurationChange` is posted for.
    var notificationObject: AnyObject { get }
    var isRunning: Bool { get }
    /// The input hardware's format, and the input node's output format (what a
    /// tap receives). After a route change the second can lag the first.
    func inputFormats() throws -> (hardware: AVAudioFormat, output: AVAudioFormat)
    func installTap(format: AVAudioFormat, bufferSize: AVAudioFrameCount,
                    block: @escaping (AVAudioPCMBuffer) -> Void) throws
    func removeTap()
    /// Prepares and starts the engine.
    func start() throws
    func pause()
    func stop()
}

/// `AVAudioEngine`'s input. AVFAudio answers a format it cannot use with an
/// Objective-C exception, which Swift cannot catch: those calls throw here instead.
final class AVCaptureEngine: CaptureEngine {
    private let engine = AVAudioEngine()

    var notificationObject: AnyObject { engine }
    var isRunning: Bool { engine.isRunning }

    func inputFormats() throws -> (hardware: AVAudioFormat, output: AVAudioFormat) {
        var formats: (hardware: AVAudioFormat, output: AVAudioFormat)?
        try ObjCException.catching {
            let input = engine.inputNode
            formats = (input.inputFormat(forBus: 0), input.outputFormat(forBus: 0))
        }
        guard let formats else { throw MicrophoneSource.SourceError.noInput }
        return formats
    }

    func installTap(format: AVAudioFormat, bufferSize: AVAudioFrameCount,
                    block: @escaping (AVAudioPCMBuffer) -> Void) throws {
        try ObjCException.catching {
            engine.inputNode.installTap(onBus: 0, bufferSize: bufferSize, format: format) { buffer, _ in
                block(buffer)
            }
        }
    }

    func removeTap() {
        try? ObjCException.catching { engine.inputNode.removeTap(onBus: 0) }
    }

    func start() throws {
        var startError: Error?
        try ObjCException.catching {
            engine.prepare()
            do { try engine.start() } catch { startError = error }
        }
        if let startError { throw startError }
    }

    func pause() {
        try? ObjCException.catching { engine.pause() }
    }

    func stop() {
        try? ObjCException.catching { engine.stop() }
    }
}

/// An Objective-C exception caught on its way into Swift.
struct ObjCExceptionError: Error, CustomStringConvertible {
    let name: String
    let reason: String
    var description: String { "\(name): \(reason)" }
}

enum ObjCException {
    /// Runs `body`. An Objective-C exception it raises comes back as an
    /// `ObjCExceptionError` instead of ending the app.
    static func catching(_ body: () -> Void) throws {
        if let exception = LooreCatchException(body) {
            throw ObjCExceptionError(name: exception.name.rawValue, reason: exception.reason ?? "")
        }
    }
}

/// The microphone through `AVAudioEngine` (map C §8.1). The engine is restarted
/// after a configuration change (route change, category switch); the converter
/// after it keeps the output format fixed. A microphone that stops delivering
/// audio while it should be running is restarted too (#423); when the restarts
/// fail, `onFailure` reports it.
///
/// Every restart builds a new engine and taps it only with a format the input
/// hardware has now. On the 2026-10-07 walk (#423) the headphones went off
/// mid-recording; the reused engine's input node still reported the headset
/// mic's 16 kHz while the phone's mic runs at 48 kHz. It started but delivered
/// nothing, and the watchdog's restart then installed a tap on it that AVFAudio
/// refused with an exception (most likely the sample-rate mismatch; the crash
/// report does not keep the reason): the app aborted.
final class MicrophoneSource: PCMSource {
    var onBuffer: ((AVAudioPCMBuffer) -> Void)?
    var onEnd: (() -> Void)?
    /// Called when the engine cannot be restarted after a configuration change
    /// or a stall (after the retries). Delivered on the main queue.
    var onFailure: ((Error) -> Void)?
    /// INTRODUCED HEURISTIC: waits before each further restart attempt. A new
    /// Bluetooth route is often not ready at once; one retry after 0.5 s gave up
    /// too early (#423). Gives up after about 3.5 s.
    static let restartRetryDelays: [TimeInterval] = [0.5, 1, 2]
    /// INTRODUCED HEURISTIC: no buffer for this long while capturing means a
    /// dead input (a 4 096-frame tap delivers about 4 buffers a second even at
    /// 16 kHz).
    static let stallTimeout: TimeInterval = 2
    /// Restarts of a stalled microphone that still bring no audio before it is
    /// reported as failed (INTRODUCED HEURISTIC).
    static let stallRestartLimit = 2
    static let tapBufferSize: AVAudioFrameCount = 4_096

    enum SourceError: Error, CustomStringConvertible {
        case stalled
        /// The hardware reports no input (0 Hz or no channels).
        case noInput
        /// The input node's format lags the hardware's (a route change); a tap
        /// with it would raise.
        case formatMismatch(hardware: Double, output: Double)

        var description: String {
            switch self {
            case .stalled: return "no audio"
            case .noInput: return "no input"
            case .formatMismatch(let hardware, let output):
                return "input node at \(Int(output)) Hz, hardware at \(Int(hardware)) Hz"
            }
        }
    }

    /// The waits above (tests shorten them).
    var retryDelays = MicrophoneSource.restartRetryDelays
    var stallTimeout = MicrophoneSource.stallTimeout

    private let makeEngine: () -> CaptureEngine
    private var engine: CaptureEngine
    /// A tap is on the current engine.
    private var tapInstalled = false
    /// Between a successful `start()` and `stop()`.
    private var active = false
    /// Capture should be running: not stopped, not paused for an interruption.
    private var capturing = false
    private var restartGeneration = 0
    private var restarting = false
    private var stallRestarts = 0
    private var configObserver: NSObjectProtocol?
    private var watchdog: Timer?
    private let clock = BufferClock()
    private let recordingLog: RecordingLog
    private let log = Logger(subsystem: "org.loore.app", category: "recorder")

    init(recordingLog: RecordingLog = .shared, makeEngine: @escaping () -> CaptureEngine = { AVCaptureEngine() }) {
        self.recordingLog = recordingLog
        self.makeEngine = makeEngine
        engine = makeEngine()
    }

    func start() throws {
        do {
            try installTap()
            try engine.start()
        } catch {
            recordingLog.note("microphone did not start: \(AudioSessionController.describe(error))")
            discardEngine()
            throw error
        }
        active = true
        capturing = true
        clock.reset()
        recordingLog.note("microphone started: \(formatSummary)")
        observeConfigurationChanges()
        startWatchdog()
    }

    private var formatSummary: String {
        guard let formats = try? engine.inputFormats() else { return "format unknown" }
        let output = formats.output, hardware = formats.hardware
        var summary = "\(Int(output.sampleRate)) Hz, \(output.channelCount) ch"
        if hardware.sampleRate != output.sampleRate { summary += " (hardware \(Int(hardware.sampleRate)) Hz)" }
        return summary
    }

    /// The format to tap with: the input node's output format, when the hardware
    /// has an input and the node agrees with it. AVFAudio raises an exception
    /// for a tap with no channels or whose sample rate differs from the
    /// hardware's.
    static func tapFormat(hardware: AVAudioFormat, output: AVAudioFormat) throws -> AVAudioFormat {
        guard hardware.sampleRate > 0, hardware.channelCount > 0 else { throw SourceError.noInput }
        guard output.sampleRate == hardware.sampleRate, output.channelCount > 0 else {
            throw SourceError.formatMismatch(hardware: hardware.sampleRate, output: output.sampleRate)
        }
        return output
    }

    private func installTap() throws {
        let formats = try engine.inputFormats()
        let format = try Self.tapFormat(hardware: formats.hardware, output: formats.output)
        if tapInstalled {
            engine.removeTap()
            tapInstalled = false
        }
        let clock = clock
        let recordingLog = recordingLog
        try engine.installTap(format: format, bufferSize: Self.tapBufferSize) { [weak self] buffer in
            clock.tick()
            recordingLog.measure(buffer)
            self?.onBuffer?(buffer)
        }
        tapInstalled = true
    }

    private func observeConfigurationChanges() {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine.notificationObject, queue: .main
        ) { [weak self] _ in
            self?.restartAfterConfigurationChange()
        }
    }

    private func restartAfterConfigurationChange() {
        guard active else { return }
        log.info("engine configuration changed; restarting input")
        recordingLog.note("engine configuration changed (running: \(engine.isRunning))")
        restart()
    }

    /// Restarts now, then retries on `retryDelays`; a newer restart (another
    /// configuration change) replaces a pending one.
    private func restart() {
        restartGeneration += 1
        attemptRestart(attempt: 0, generation: restartGeneration)
    }

    private func attemptRestart(attempt: Int, generation: Int) {
        guard active, generation == restartGeneration else { return }
        if attempt > 0 && engine.isRunning && !clock.stalled(after: stallTimeout) {
            restarting = false
            return
        }
        restarting = true
        do {
            try restartEngine()
            restarting = false
            capturing = true
            clock.reset()
            recordingLog.note("microphone restarted (attempt \(attempt + 1)): \(formatSummary)")
        } catch {
            recordingLog.note("microphone restart failed (attempt \(attempt + 1)): \(AudioSessionController.describe(error))")
            guard attempt < retryDelays.count else {
                restarting = false
                log.error("engine restart failed")
                onFailure?(error)
                return
            }
            log.error("engine restart failed; retrying")
            DispatchQueue.main.asyncAfter(deadline: .now() + retryDelays[attempt]) { [weak self] in
                self?.attemptRestart(attempt: attempt + 1, generation: generation)
            }
        }
    }

    /// A new engine, tapped with the format the hardware has now (see the type's
    /// comment for why the old one is not reused).
    private func restartEngine() throws {
        discardEngine()
        engine = makeEngine()
        observeConfigurationChanges()
        try installTap()
        try engine.start()
    }

    private func discardEngine() {
        if tapInstalled {
            engine.removeTap()
            tapInstalled = false
        }
        engine.stop()
    }

    /// A running engine whose tap stopped delivering is restarted (the web found
    /// iOS capture that dies silently, #209).
    private func startWatchdog() {
        watchdog?.invalidate()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.checkForStall() }
        RunLoop.main.add(timer, forMode: .common)
        watchdog = timer
    }

    /// The watchdog's check, once a second.
    func checkForStall() {
        guard active, capturing, !restarting else { return }
        // Only real audio clears the count (a restart also restarts the wait).
        if clock.takeDelivered() > 0 { stallRestarts = 0 }
        guard clock.stalled(after: stallTimeout) else { return }
        guard stallRestarts < Self.stallRestartLimit else {
            // Restarting brings no audio: say so rather than record nothing.
            recordingLog.note("no audio after \(stallRestarts) restarts: reporting a failure")
            capturing = false
            stallRestarts = 0
            onFailure?(SourceError.stalled)
            return
        }
        stallRestarts += 1
        recordingLog.note("no audio from the microphone for \(Int(stallTimeout)) s (running: \(engine.isRunning)): restarting")
        log.error("microphone stalled; restarting")
        restart()
    }

    /// Stops capture (the OS mic indicator goes off). `resume()` starts it again.
    func pause() {
        capturing = false
        stallRestarts = 0
        engine.pause()
    }

    /// After a user pause the engine is still running (the recorder only drops
    /// samples): nothing to do. Preparing or starting it again could only fail,
    /// and from the background iOS refuses to start a microphone (#397).
    /// After an interruption, a media-services reset or a failed restart the
    /// route may have changed meanwhile: a new engine, as for a restart.
    func resume() throws {
        if engine.isRunning {
            capturing = true
            return
        }
        // A retry still pending would replace the engine this starts.
        restartGeneration += 1
        restarting = false
        do {
            try restartEngine()
        } catch {
            recordingLog.note("microphone did not resume: \(AudioSessionController.describe(error))")
            throw error
        }
        capturing = true
        stallRestarts = 0
        clock.reset()
        recordingLog.note("microphone resumed: \(formatSummary)")
    }

    func stop() {
        capturing = false
        active = false
        restartGeneration += 1
        watchdog?.invalidate()
        watchdog = nil
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        discardEngine()
    }
}

/// When the tap last delivered a buffer, and how many arrived since the last
/// look (written on the capture thread).
final class BufferClock: @unchecked Sendable {
    private let lock = NSLock()
    private var last = ProcessInfo.processInfo.systemUptime
    private var delivered = 0

    func tick() {
        lock.lock()
        last = ProcessInfo.processInfo.systemUptime
        delivered += 1
        lock.unlock()
    }

    /// Starts the wait afresh (after a start or restart); not a delivery.
    func reset() {
        lock.lock()
        last = ProcessInfo.processInfo.systemUptime
        lock.unlock()
    }

    /// Buffers delivered since the previous call.
    func takeDelivered() -> Int {
        lock.lock()
        defer { lock.unlock() }
        let count = delivered
        delivered = 0
        return count
    }

    func stalled(after seconds: TimeInterval) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return ProcessInfo.processInfo.systemUptime - last > seconds
    }
}

#if DEBUG
/// Debug-only: feeds an audio file into the recorder at real-time pace, as if it
/// were the microphone (`-LooreDebugAudioFile <path>`, design doc §12), so the
/// whole upload → transcription → reply → TTS loop runs in the simulator.
/// Calls `onEnd` when the file is exhausted.
final class AudioFileSource: PCMSource {
    var onBuffer: ((AVAudioPCMBuffer) -> Void)?
    var onEnd: (() -> Void)?

    private let url: URL
    private let pace: Double
    private var file: AVAudioFile?
    private var timer: DispatchSourceTimer?
    private let queue = DispatchQueue(label: "org.loore.audio.file-source")
    private var paused = false
    private var ended = false
    private static let tick: Double = 0.1

    /// - Parameter pace: 1 = real time; larger values feed faster (tests).
    init(url: URL, pace: Double = 1) {
        self.url = url
        self.pace = pace
    }

    func start() throws {
        file = try AVAudioFile(forReading: url)
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: Self.tick / pace)
        timer.setEventHandler { [weak self] in self?.readTick() }
        self.timer = timer
        timer.resume()
    }

    private func readTick() {
        guard !paused, !ended, let file else { return }
        let frames = AVAudioFrameCount(file.processingFormat.sampleRate * Self.tick)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: frames) else { return }
        do {
            try file.read(into: buffer, frameCount: frames)
        } catch {
            buffer.frameLength = 0
        }
        if buffer.frameLength > 0 {
            onBuffer?(buffer)
        }
        if buffer.frameLength < frames || file.framePosition >= file.length {
            ended = true
            timer?.cancel()
            timer = nil
            let onEnd = onEnd
            DispatchQueue.main.async { onEnd?() }
        }
    }

    func pause() {
        queue.sync { paused = true }
    }

    func resume() throws {
        queue.sync { paused = false }
    }

    func stop() {
        queue.sync {
            timer?.cancel()
            timer = nil
            ended = true
        }
    }
}
#endif
