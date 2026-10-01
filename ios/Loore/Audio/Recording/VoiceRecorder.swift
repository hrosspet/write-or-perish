import AVFoundation
import os

/// The live recorder: a PCM source (microphone, or the Debug file) → converter →
/// fMP4 writer → persisted upload queue. Used by voice mode and, from M2's
/// writing form, by dictation (design doc §8 "Dictation").
///
/// Pauses keep the capture running and drop the samples (map C §8.5: the audio
/// session stays alive, so a lock-screen resume works); a system interruption
/// stops capture, and `resume()` restarts it. Timestamps stay continuous, so a
/// pause needs no new initialization segment. If the writer died (media
/// services reset), `resume()` starts a new one: its first chunk carries `ftyp`
/// and the server opens a subsession (#124).
@MainActor
final class VoiceRecorder: VoiceRecording {
    var onSourceEnded: (() -> Void)?
    var onFatal: ((String) -> Void)?
    var onSourceFailed: (() -> Void)?
    /// Sees every chunk in order (dictation keeps a local copy for "Save audio").
    var chunkObserver: ((RecordedChunk) -> Void)?

    private let uploader: ChunkUploader
    private let debugFile: URL?
    private var source: PCMSource?
    private var writer: FMP4SegmentWriter?
    private var sessionId: String?
    private var uploadURL: URL?
    private var elapsedOffset: Double = 0
    private var secondsBeforeWriter: Double = 0
    private let gate = SampleGate()
    private let inbox = ChunkInbox()
    private let log = Logger(subsystem: "org.loore.app", category: "recorder")

    init(uploader: ChunkUploader? = nil, debugFile: URL? = nil) {
        self.uploader = uploader ?? .shared
        self.debugFile = debugFile
    }

    var elapsed: Double {
        elapsedOffset + secondsBeforeWriter + (writer?.secondsWritten ?? 0)
    }

    var isRecording: Bool { source != nil }

    func start(sessionId: String, uploadURL: URL, firstChunkIndex: Int, elapsedOffset: Double) throws {
        cancel()
        self.sessionId = sessionId
        self.uploadURL = uploadURL
        self.elapsedOffset = elapsedOffset
        secondsBeforeWriter = 0
        uploader.onFatal = { [weak self] sid, message in
            guard let self, sid == self.sessionId else { return }
            self.onFatal?(message)
        }
        uploader.open(sessionId: sessionId, uploadURL: uploadURL, firstIndex: firstChunkIndex)
        writer = try makeWriter(firstIndex: firstChunkIndex)
        let source = makeSource()
        let converter = PCMConverter()
        let gate = gate
        source.onBuffer = { [weak self] buffer in
            guard gate.isOpen, let out = converter.convert(buffer) else { return }
            self?.currentWriterUnsafe?.append(out)
        }
        source.onEnd = { [weak self] in self?.onSourceEnded?() }
        gate.set(open: true)
        do {
            try source.start()
        } catch {
            gate.set(open: false)
            writer = nil
            throw error
        }
        self.source = source
    }

    /// Read from the capture thread; replaced only on the main actor while the gate is shut.
    nonisolated(unsafe) private var currentWriterUnsafe: FMP4SegmentWriter?

    private func makeWriter(firstIndex: Int) throws -> FMP4SegmentWriter {
        let inbox = inbox
        let writer = try FMP4SegmentWriter(firstChunkIndex: firstIndex, realTime: true) { chunk in
            inbox.push(chunk)
            DispatchQueue.main.async { [weak self] in
                MainActor.assumeIsolated { self?.drainInbox() }
            }
        }
        currentWriterUnsafe = writer
        return writer
    }

    private func makeSource() -> PCMSource {
        #if DEBUG
        if let debugFile { return AudioFileSource(url: debugFile) }
        #endif
        let mic = MicrophoneSource()
        mic.onFailure = { [weak self] _ in
            self?.interrupt()
            self?.onSourceFailed?()
        }
        return mic
    }

    private func drainInbox() {
        guard let sessionId else { return }
        for chunk in inbox.popAll() {
            chunkObserver?(chunk)
            uploader.enqueue(sessionId: sessionId, chunk: chunk)
        }
    }

    func pause() {
        gate.set(open: false)
        writer?.flush()
        if debugFile != nil { source?.pause() }
    }

    func resume() throws {
        guard let source else { return }
        if writer == nil || writerFailed {
            // The old writer is gone (media services reset): a new stream.
            secondsBeforeWriter += writer?.secondsWritten ?? 0
            writer = try makeWriter(firstIndex: nextIndex)
        }
        try source.resume()
        gate.set(open: true)
    }

    func interrupt() {
        gate.set(open: false)
        writer?.flush()
        source?.pause()
    }

    func stop() async -> ChunkUploader.Outcome {
        gate.set(open: false)
        source?.stop()
        source = nil
        if let writer { await writer.finish() }
        drainInbox()
        guard let sessionId else {
            return ChunkUploader.Outcome(produced: 0, stored: 0, failed: [], fatalMessage: nil)
        }
        let outcome = await uploader.settle(sessionId: sessionId)
        writer = nil
        currentWriterUnsafe = nil
        return outcome
    }

    func cancel() {
        gate.set(open: false)
        source?.stop()
        source = nil
        writer = nil
        currentWriterUnsafe = nil
        if let sessionId { uploader.close(sessionId: sessionId) }
    }

    func forget(sessionId: String) {
        uploader.forget(sessionId: sessionId)
        if self.sessionId == sessionId { self.sessionId = nil }
    }

    private var nextIndex: Int { writer?.nextChunkIndex ?? 0 }
    private var writerFailed: Bool { writer?.hasFailed ?? true }
}

/// Open/closed flag read on the capture thread.
final class SampleGate: @unchecked Sendable {
    private let lock = NSLock()
    private var open = false

    var isOpen: Bool {
        lock.lock()
        defer { lock.unlock() }
        return open
    }

    func set(open value: Bool) {
        lock.lock()
        open = value
        lock.unlock()
    }
}

/// Chunks produced on the writer's queue, handed to the main actor in order.
final class ChunkInbox: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [RecordedChunk] = []

    func push(_ chunk: RecordedChunk) {
        lock.lock()
        items.append(chunk)
        lock.unlock()
    }

    func popAll() -> [RecordedChunk] {
        lock.lock()
        defer { lock.unlock() }
        let out = items
        items = []
        return out.sorted { $0.index < $1.index }
    }
}
