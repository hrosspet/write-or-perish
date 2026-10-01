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
/// after it keeps the output format fixed.
final class MicrophoneSource: PCMSource {
    var onBuffer: ((AVAudioPCMBuffer) -> Void)?
    var onEnd: (() -> Void)?
    /// Called when the engine cannot be restarted after a configuration change
    /// (after one retry). Delivered on the main queue.
    var onFailure: ((Error) -> Void)?
    /// Wait before the one retry: a new route is often not ready at once.
    static let restartRetryDelay: TimeInterval = 0.5

    private let engine = AVAudioEngine()
    private var tapInstalled = false
    private var configObserver: NSObjectProtocol?
    private let log = Logger(subsystem: "org.loore.app", category: "recorder")

    func start() throws {
        installTap()
        engine.prepare()
        try engine.start()
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            self?.restartAfterConfigurationChange()
        }
    }

    private func installTap() {
        let input = engine.inputNode
        if tapInstalled { input.removeTap(onBus: 0) }
        let format = input.outputFormat(forBus: 0)
        input.installTap(onBus: 0, bufferSize: 4_096, format: format) { [weak self] buffer, _ in
            self?.onBuffer?(buffer)
        }
        tapInstalled = true
    }

    private func restartAfterConfigurationChange() {
        guard tapInstalled else { return }
        log.info("engine configuration changed; restarting input")
        do {
            try restartEngine()
        } catch {
            // One retry shortly after; then the recorder reports it (M15).
            log.error("engine restart failed; retrying once")
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.restartRetryDelay) { [weak self] in
                guard let self, self.tapInstalled, !self.engine.isRunning else { return }
                do {
                    try self.restartEngine()
                } catch {
                    self.log.error("engine restart failed")
                    self.onFailure?(error)
                }
            }
        }
    }

    private func restartEngine() throws {
        engine.stop()
        installTap()
        engine.prepare()
        try engine.start()
    }

    /// Stops capture (the OS mic indicator goes off). `resume()` starts it again.
    func pause() {
        engine.pause()
    }

    func resume() throws {
        if !tapInstalled { installTap() }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        engine.stop()
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
