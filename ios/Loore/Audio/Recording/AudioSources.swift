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

/// The microphone through `AVAudioEngine` (map C §8.1). The engine is restarted
/// after a configuration change (route change, category switch); the converter
/// after it keeps the output format fixed. A microphone that stops delivering
/// audio while it should be running is restarted too (#423); when the restarts
/// fail, `onFailure` reports it.
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

    enum SourceError: Error { case stalled }

    private let engine = AVAudioEngine()
    private var tapInstalled = false
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

    init(recordingLog: RecordingLog = .shared) {
        self.recordingLog = recordingLog
    }

    func start() throws {
        installTap()
        engine.prepare()
        do {
            try engine.start()
        } catch {
            recordingLog.note("microphone did not start: \(AudioSessionController.describe(error))")
            throw error
        }
        capturing = true
        clock.reset()
        recordingLog.note("microphone started: \(formatSummary)")
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            self?.restartAfterConfigurationChange()
        }
        startWatchdog()
    }

    private var formatSummary: String {
        let format = engine.inputNode.outputFormat(forBus: 0)
        return "\(Int(format.sampleRate)) Hz, \(format.channelCount) ch"
    }

    private func installTap() {
        let input = engine.inputNode
        if tapInstalled { input.removeTap(onBus: 0) }
        let format = input.outputFormat(forBus: 0)
        let clock = clock
        let recordingLog = recordingLog
        input.installTap(onBus: 0, bufferSize: 4_096, format: format) { [weak self] buffer, _ in
            clock.tick()
            recordingLog.measure(buffer)
            self?.onBuffer?(buffer)
        }
        tapInstalled = true
    }

    private func restartAfterConfigurationChange() {
        guard tapInstalled else { return }
        log.info("engine configuration changed; restarting input")
        recordingLog.note("engine configuration changed (running: \(engine.isRunning))")
        restart()
    }

    /// Restarts now, then retries on `restartRetryDelays`; a newer restart
    /// (another configuration change) replaces a pending one.
    private func restart() {
        restartGeneration += 1
        attemptRestart(attempt: 0, generation: restartGeneration)
    }

    private func attemptRestart(attempt: Int, generation: Int) {
        guard tapInstalled, generation == restartGeneration else { return }
        if attempt > 0 && engine.isRunning && !clock.stalled(after: Self.stallTimeout) {
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
            guard attempt < Self.restartRetryDelays.count else {
                restarting = false
                log.error("engine restart failed")
                onFailure?(error)
                return
            }
            log.error("engine restart failed; retrying")
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.restartRetryDelays[attempt]) { [weak self] in
                self?.attemptRestart(attempt: attempt + 1, generation: generation)
            }
        }
    }

    private func restartEngine() throws {
        engine.stop()
        installTap()
        engine.prepare()
        try engine.start()
    }

    /// A running engine whose tap stopped delivering is restarted (the web found
    /// iOS capture that dies silently, #209).
    private func startWatchdog() {
        watchdog?.invalidate()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            guard let self, self.capturing, !self.restarting, self.tapInstalled else { return }
            // Only real audio clears the count (a restart also restarts the wait).
            if self.clock.takeDelivered() > 0 { self.stallRestarts = 0 }
            guard self.clock.stalled(after: Self.stallTimeout) else { return }
            guard self.stallRestarts < Self.stallRestartLimit else {
                // Restarting brings no audio: say so rather than record nothing.
                self.recordingLog.note("no audio after \(self.stallRestarts) restarts: reporting a failure")
                self.capturing = false
                self.stallRestarts = 0
                self.onFailure?(SourceError.stalled)
                return
            }
            self.stallRestarts += 1
            self.recordingLog.note("no audio from the microphone for \(Int(Self.stallTimeout)) s (running: \(self.engine.isRunning)): restarting")
            self.log.error("microphone stalled; restarting")
            self.restart()
        }
        RunLoop.main.add(timer, forMode: .common)
        watchdog = timer
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
    func resume() throws {
        if engine.isRunning {
            capturing = true
            return
        }
        if !tapInstalled { installTap() }
        engine.prepare()
        try engine.start()
        capturing = true
        clock.reset()
        recordingLog.note("microphone resumed: \(formatSummary)")
    }

    func stop() {
        capturing = false
        restartGeneration += 1
        watchdog?.invalidate()
        watchdog = nil
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        engine.stop()
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
