import AVFoundation
import os

/// The live recorder: a PCM source (microphone, or the Debug file) → converter →
/// fMP4 writer → persisted upload queue. Used by voice mode and, from M2's
/// writing form, by dictation (design doc §8 "Dictation"). Stop, leave and
/// cancel end a recording: Stop queues everything and settles the uploads for
/// finalize; leave queues everything for recovery (#753); cancel drops the
/// segment not yet cut.
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
    var onLimit: ((String) -> Void)?
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
    /// One per recording (`begin`, `leave`): a recording left behind finishes
    /// its own chunks while the next one starts (#753).
    private var inbox = ChunkInbox()
    private let log = Logger(subsystem: "org.loore.app", category: "recorder")
    private let recordingLog: RecordingLog
    /// #388: a continued recording keeps its chunks on the phone until its claim
    /// answered (`releaseHeldChunks`). The uploader is not opened before, so
    /// nothing reaches the server while another device is the session's writer.
    private var holdingChunks = false
    /// The index the writer numbers from while holding (the listed chunk count);
    /// the claim's index replaces it, and the difference is added to every chunk.
    private var heldFirstIndex = 0
    private var indexShift = 0
    /// Which recording owns each session on this phone (#753 review). Every
    /// start takes a new token, in any recorder (Voice's, a dictation's). A
    /// recording left mid-way keeps its token until its uploads settle; its
    /// clean-up (forget, release, discard) runs only if the token is still the
    /// session's, so it never touches a session continued meanwhile.
    private static var owners: [String: UUID] = [:]
    private var ownerToken: UUID?

    /// INTRODUCED HEURISTIC (#753 review): the least audio a recording left
    /// mid-way keeps when it has no earlier piece (none cut yet, not a
    /// continued session). Below it the leave is taken as a Record tapped by
    /// accident: the session is discarded as before #753, so Voice does not
    /// offer it and Text mode does not transcribe it. 2 s tells a tap followed
    /// by Back from a spoken sentence. Peter's call (PR #764).
    static let shortestKeptRecording: Double = 2

    init(uploader: ChunkUploader? = nil, debugFile: URL? = nil, recordingLog: RecordingLog = .shared) {
        self.uploader = uploader ?? .shared
        self.debugFile = debugFile
        self.recordingLog = recordingLog
    }

    var elapsed: Double {
        elapsedOffset + secondsBeforeWriter + (writer?.secondsWritten ?? 0)
    }

    var isRecording: Bool { source != nil }

    func start(sessionId: String, uploadURL: URL, firstChunkIndex: Int, elapsedOffset: Double) throws {
        try begin(sessionId: sessionId, uploadURL: uploadURL, firstChunkIndex: firstChunkIndex,
                  elapsedOffset: elapsedOffset, holdChunks: false)
    }

    func startHoldingChunks(sessionId: String, uploadURL: URL, firstChunkIndex: Int,
                            elapsedOffset: Double) throws {
        try begin(sessionId: sessionId, uploadURL: uploadURL, firstChunkIndex: firstChunkIndex,
                  elapsedOffset: elapsedOffset, holdChunks: true)
    }

    func releaseHeldChunks(firstChunkIndex: Int) {
        guard holdingChunks, let sessionId, let uploadURL else { return }
        holdingChunks = false
        // Past the pieces of the session still queued on this phone, if any (#754).
        let first = uploader.open(sessionId: sessionId, uploadURL: uploadURL, firstIndex: firstChunkIndex)
        indexShift = first - heldFirstIndex
        recordingLog.note("claim answered: chunks from \(firstChunkIndex)"
                          + (first == firstChunkIndex ? "" : "; this phone still holds earlier ones, so from \(first)"))
        drainInbox()
    }

    private func begin(sessionId: String, uploadURL: URL, firstChunkIndex: Int, elapsedOffset: Double,
                       holdChunks: Bool) throws {
        cancel()
        inbox = ChunkInbox()
        self.sessionId = sessionId
        let token = UUID()
        ownerToken = token
        Self.owners[sessionId] = token
        self.uploadURL = uploadURL
        self.elapsedOffset = elapsedOffset
        secondsBeforeWriter = 0
        holdingChunks = holdChunks
        heldFirstIndex = firstChunkIndex
        indexShift = 0
        uploader.onFatal = { [weak self] sid, message in
            guard let self, sid == self.sessionId else { return }
            self.onFatal?(message)
        }
        uploader.onLimit = { [weak self] sid, message in
            guard let self, sid == self.sessionId else { return }
            self.onLimit?(message)
        }
        if !holdChunks {
            indexShift = uploader.open(sessionId: sessionId, uploadURL: uploadURL, firstIndex: firstChunkIndex)
                - firstChunkIndex
        }
        recordingLog.note("recorder start: session \(sessionId.prefix(8)), first chunk \(firstChunkIndex)"
                          + (holdChunks ? " (held until the claim answers)" : ""))
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
            releaseOwnership()
            throw error
        }
        self.source = source
    }

    /// This recorder no longer records its session (stopped, cancelled).
    private func releaseOwnership() {
        if let sessionId, let ownerToken, Self.owners[sessionId] == ownerToken {
            Self.owners[sessionId] = nil
        }
        ownerToken = nil
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
        guard let sessionId, !holdingChunks else { return }
        for var chunk in inbox.popAll() {
            chunk.index += indexShift
            recordingLog.note("chunk \(chunk.index): \(String(format: "%.1f", chunk.duration)) s, \(chunk.data.count / 1024) KB")
            chunkObserver?(chunk)
            uploader.enqueue(sessionId: sessionId, chunk: chunk)
        }
    }

    func pause() {
        recordingLog.note("recorder pause (samples dropped, microphone on)")
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
        recordingLog.note("recorder resume")
    }

    func interrupt() {
        recordingLog.note("recorder interrupt (microphone stopped)")
        gate.set(open: false)
        writer?.flush()
        source?.pause()
    }

    func stop() async -> ChunkUploader.Outcome {
        gate.set(open: false)
        source?.stop()
        source = nil
        if let writer { await writer.finish() }
        if holdingChunks {
            // Stopped before the claim answered (the turn does not allow it):
            // nothing was sent, nothing is finalized (#388).
            cancel()
            return ChunkUploader.Outcome(produced: 0, stored: 0, failed: [],
                                         fatalMessage: VoiceTurnController.claimFailedMessage,
                                         prior: heldFirstIndex)
        }
        drainInbox()
        guard let sessionId else {
            return ChunkUploader.Outcome(produced: 0, stored: 0, failed: [], fatalMessage: nil)
        }
        let outcome = await uploader.settle(sessionId: sessionId)
        recordingLog.note("recorder stop: \(outcome.produced) chunks produced, \(outcome.stored) stored, failed \(outcome.failed)")
        writer = nil
        currentWriterUnsafe = nil
        releaseOwnership()
        return outcome
    }

    func cancel() {
        if source != nil { recordingLog.note("recorder cancel") }
        gate.set(open: false)
        source?.stop()
        source = nil
        writer = nil
        currentWriterUnsafe = nil
        if holdingChunks {
            // Never sent: a continue whose claim failed or was abandoned (#388).
            _ = inbox.popAll()
            holdingChunks = false
        }
        if let sessionId { uploader.close(sessionId: sessionId) }
        releaseOwnership()
    }

    /// Leaving mid-recording (the Voice screen or the writing form went away,
    /// #753): capture stops, and the unfinished segment is cut and queued with
    /// the others, as Stop does, so recovery gets everything that was said.
    /// Nothing is finalized. `cancel()` dropped that segment: up to 15 s.
    ///
    /// The recorder is free for a new recording at once; the returned task
    /// finishes this one (its writer, inbox and session go with it) and says
    /// what became of it. Then, unless a recorder on this phone has opened the
    /// session again meanwhile (`.reopened`), its upload state is forgotten and
    /// - `discard` is called for a recording under `shortestKeptRecording`
    ///   with no earlier piece (`.empty`, as before #753);
    /// - `release` is called once the server has every piece (`.stored`).
    /// Chunks still on the phone stay queued (`.pending`). A continued
    /// recording still held for its claim sends nothing (#388), and nothing
    /// running is only cancelled: `.nothing`.
    @discardableResult
    func leave(release: @escaping @MainActor (String) async -> Void,
               discard: @escaping @MainActor (String) async -> Void) -> Task<RecordingLeft, Never> {
        guard source != nil, !holdingChunks, let sessionId, let token = ownerToken else {
            cancel()
            return Task { .nothing }
        }
        recordingLog.note("recorder leave: the unfinished segment is queued")
        gate.set(open: false)
        source?.stop()
        source = nil
        let writer = writer
        let inbox = inbox
        let shift = indexShift
        let uploader = uploader
        let recordingLog = recordingLog
        // Worth keeping: an earlier piece (cut already, or a continued session,
        // which numbers after its stored chunks), or enough audio not cut yet.
        let recorded = secondsBeforeWriter + (writer?.secondsWritten ?? 0)
        let keep = (writer?.nextChunkIndex ?? 0) > 0 || recorded >= Self.shortestKeptRecording
        // Detached: a chunk the writer hands over from now on lands in its own
        // inbox, never in the next recording's. The session stays this leave's
        // (its token) until the task below ends.
        self.writer = nil
        currentWriterUnsafe = nil
        self.inbox = ChunkInbox()
        self.sessionId = nil
        ownerToken = nil
        return Task { @MainActor in
            let left: RecordingLeft
            if keep {
                if let writer { await writer.finish() }
                for var chunk in inbox.popAll() {
                    chunk.index += shift
                    recordingLog.note("chunk \(chunk.index): \(String(format: "%.1f", chunk.duration)) s, \(chunk.data.count / 1024) KB (left)")
                    uploader.enqueue(sessionId: sessionId, chunk: chunk)
                }
                let outcome = await uploader.settle(sessionId: sessionId)
                recordingLog.note("recorder left: \(outcome.produced) chunks produced, \(outcome.stored) stored, failed \(outcome.failed)")
                if outcome.fatalMessage != nil || outcome.produced == 0 {
                    // Nothing queued at all: the queue was deleted meanwhile (sign-out).
                    left = .nothing
                } else {
                    left = outcome.failed.isEmpty ? .stored(sessionId: sessionId) : .pending(sessionId: sessionId)
                }
            } else {
                recordingLog.note("recorder left: \(String(format: "%.1f", recorded)) s, not kept")
                left = .empty(sessionId: sessionId)
            }
            // Opened again on this phone while the uploads settled (Continue
            // recording): the session is that recording's now.
            guard Self.owners[sessionId] == token else {
                recordingLog.note("recorder left: the session was opened again; left as it is")
                return .reopened(sessionId: sessionId)
            }
            Self.owners[sessionId] = nil
            switch left {
            case .empty:
                uploader.forget(sessionId: sessionId)
                await discard(sessionId)
            case .stored:
                uploader.forget(sessionId: sessionId)
                await release(sessionId)
            case .pending, .nothing, .reopened:
                break
            }
            return left
        }
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
