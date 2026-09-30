import AVFoundation
import UniformTypeIdentifiers
import os

/// Encodes 48 kHz mono PCM to AAC in fragmented MP4 and hands out upload-ready
/// chunks (design doc §9.2, map C §8.1).
///
/// - `AACEncoder` (`AVAudioConverter`, AAC-LC 64 kbps) encodes the PCM; the
///   compressed packets go to `AVAssetWriter(contentType: .mpeg4Movie)` with no
///   output file, `outputFileTypeProfile = .mpeg4AppleHLS`, in passthrough.
///   (AVFoundation refuses to encode itself when the segment interval is
///   `.indefinite`: error -11875, "only supports passthrough".)
/// - `preferredOutputSegmentInterval = .indefinite`: a segment is cut when
///   `flushSegment()` is called — every 15 s of audio and on pause, interruption
///   and stop (the web's `requestData()`).
/// - Timestamps come from the packet count, so a pause leaves no gap and needs no
///   new initialization segment: one recording is one stream for the server.
/// - Segments go through `SegmentPackager` (chunk 0 = init + first media segment).
///
/// Thread-safe: appends, flushes and finish run on the writer's serial queue.
/// `onChunk` may run on that queue: it must not call back into the writer synchronously.
final class FMP4SegmentWriter: NSObject, AVAssetWriterDelegate, @unchecked Sendable {
    static let sampleRate: Double = 48_000
    /// The fixed format every source is converted to.
    static let pcmFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate,
                                         channels: 1, interleaved: true)!
    static let bitRate = 64_000

    enum WriterError: Error {
        case encoderUnavailable
        case cannotAddInput
        case cannotStart(String)
    }

    private let writer: AVAssetWriter
    private let input: AVAssetWriterInput
    private let encoder: AACEncoder
    private let queue = DispatchQueue(label: "org.loore.audio.fmp4-writer")
    private let lock = NSLock()
    private var packager: SegmentPackager
    private let segmentFrames: Int64
    private let realTime: Bool
    private var pcmFramesIn: Int64 = 0
    private var framesSinceFlush: Int64 = 0
    private var finished = false
    private let onChunk: @Sendable (RecordedChunk) -> Void
    private let log = Logger(subsystem: "org.loore.app", category: "recorder")

    /// - Parameters:
    ///   - firstChunkIndex: index of the first chunk this writer produces (a
    ///     writer created after a reset continues the numbering; its first chunk
    ///     starts with `ftyp`, which the server treats as a new subsession).
    ///   - realTime: true for live sources (drop a buffer if the writer is busy);
    ///     false for files in tests (wait for the writer).
    init(firstChunkIndex: Int = 0, segmentSeconds: Double = 15, realTime: Bool = true,
         onChunk: @escaping @Sendable (RecordedChunk) -> Void) throws {
        packager = SegmentPackager(firstIndex: firstChunkIndex)
        segmentFrames = Int64(segmentSeconds * Self.sampleRate)
        self.realTime = realTime
        self.onChunk = onChunk
        guard let encoder = AACEncoder(bitRate: Self.bitRate) else { throw WriterError.encoderUnavailable }
        self.encoder = encoder

        writer = AVAssetWriter(contentType: .mpeg4Movie)
        writer.outputFileTypeProfile = .mpeg4AppleHLS
        writer.preferredOutputSegmentInterval = .indefinite
        writer.initialSegmentStartTime = .zero

        input = AVAssetWriterInput(mediaType: .audio, outputSettings: nil,
                                   sourceFormatHint: encoder.formatDescription)
        input.expectsMediaDataInRealTime = realTime
        guard writer.canAdd(input) else { throw WriterError.cannotAddInput }
        writer.add(input)
        super.init()
        writer.delegate = self
        guard writer.startWriting() else {
            throw WriterError.cannotStart(writer.error.map { "\($0)" } ?? "unknown")
        }
        writer.startSession(atSourceTime: .zero)
    }

    /// Seconds of audio appended so far (the recording's duration).
    var secondsWritten: Double {
        queue.sync { Double(pcmFramesIn) / Self.sampleRate }
    }

    /// Appends converted PCM (`pcmFormat`). Cuts a segment every `segmentSeconds`.
    func append(_ buffer: AVAudioPCMBuffer) {
        queue.sync {
            guard !finished, buffer.frameLength > 0 else { return }
            pcmFramesIn += Int64(buffer.frameLength)
            for sample in encoder.encode(buffer) {
                appendLocked(sample)
            }
            if framesSinceFlush >= segmentFrames {
                flushLocked()
            }
        }
    }

    private func appendLocked(_ sample: CMSampleBuffer) {
        if !input.isReadyForMoreMediaData {
            if realTime {
                // A live source cannot wait; the encoder produces ~47 packets/s,
                // so this is rare (and logged).
                var waited = 0
                while !input.isReadyForMoreMediaData && writer.status == .writing && waited < 50 {
                    usleep(1_000)
                    waited += 1
                }
            } else {
                while !input.isReadyForMoreMediaData && writer.status == .writing {
                    usleep(1_000)
                }
            }
        }
        guard writer.status == .writing, input.append(sample) else {
            log.error("append failed: status \(self.writer.status.rawValue) \(self.writer.error.map { "\($0)" } ?? "", privacy: .public)")
            return
        }
        framesSinceFlush += Int64(CMSampleBufferGetNumSamples(sample)) * Int64(AACEncoder.framesPerPacket)
    }

    /// Closes the current segment now (pause, interruption, app backgrounding).
    func flush() {
        queue.sync { flushLocked() }
    }

    private func flushLocked() {
        guard !finished, framesSinceFlush > 0, writer.status == .writing else { return }
        writer.flushSegment()
        framesSinceFlush = 0
    }

    /// Ends the recording: drains the encoder; the last segment arrives through
    /// `onChunk` before this returns.
    func finish() async {
        let shouldFinish: Bool = queue.sync {
            guard !finished else { return false }
            for sample in encoder.drain() {
                appendLocked(sample)
            }
            finished = true
            return writer.status == .writing
        }
        guard shouldFinish else { return }
        input.markAsFinished()
        await writer.finishWriting()
        if writer.status == .failed {
            log.error("writer failed at finish: \(self.writer.error.map { "\($0)" } ?? "?", privacy: .public)")
        }
    }

    /// The index the next chunk will get.
    var nextChunkIndex: Int {
        lock.lock()
        defer { lock.unlock() }
        return packager.nextIndex
    }

    // MARK: AVAssetWriterDelegate

    func assetWriter(_ writer: AVAssetWriter, didOutputSegmentData segmentData: Data,
                     segmentType: AVAssetSegmentType, segmentReport: AVAssetSegmentReport?) {
        let kind: SegmentPackager.SegmentKind = segmentType == .initialization ? .initialization : .media
        let duration = segmentReport?.trackReports.first.map { CMTimeGetSeconds($0.duration) } ?? 0
        lock.lock()
        let chunk = packager.add(segmentData, kind: kind, duration: duration.isFinite ? duration : 0)
        lock.unlock()
        if let chunk {
            log.debug("chunk \(chunk.index) ready: \(chunk.data.count) bytes, \(chunk.duration, format: .fixed(precision: 2)) s")
            onChunk(chunk)
        }
    }
}

/// PCM (48 kHz mono Int16) → AAC-LC packets wrapped as `CMSampleBuffer`s with
/// timestamps from the packet count. Keeps the encoder's state across calls.
final class AACEncoder {
    static let framesPerPacket: AVAudioFrameCount = 1_024

    let formatDescription: CMAudioFormatDescription
    private let converter: AVAudioConverter
    private let outputFormat: AVAudioFormat
    private var packetsOut: Int64 = 0

    init?(bitRate: Int) {
        var asbd = AudioStreamBasicDescription(
            mSampleRate: FMP4SegmentWriter.sampleRate, mFormatID: kAudioFormatMPEG4AAC,
            mFormatFlags: AudioFormatFlags(MPEG4ObjectID.AAC_LC.rawValue), mBytesPerPacket: 0,
            mFramesPerPacket: Self.framesPerPacket, mBytesPerFrame: 0, mChannelsPerFrame: 1,
            mBitsPerChannel: 0, mReserved: 0)
        guard let aac = AVAudioFormat(streamDescription: &asbd),
              let converter = AVAudioConverter(from: FMP4SegmentWriter.pcmFormat, to: aac) else { return nil }
        converter.bitRate = bitRate
        self.converter = converter
        outputFormat = converter.outputFormat
        var fullASBD = outputFormat.streamDescription.pointee
        let cookie = converter.magicCookie
        var description: CMAudioFormatDescription?
        let status: OSStatus
        if let cookie, !cookie.isEmpty {
            status = cookie.withUnsafeBytes { raw in
                CMAudioFormatDescriptionCreate(allocator: kCFAllocatorDefault, asbd: &fullASBD,
                                               layoutSize: 0, layout: nil,
                                               magicCookieSize: cookie.count, magicCookie: raw.baseAddress,
                                               extensions: nil, formatDescriptionOut: &description)
            }
        } else {
            status = CMAudioFormatDescriptionCreate(allocator: kCFAllocatorDefault, asbd: &fullASBD,
                                                    layoutSize: 0, layout: nil, magicCookieSize: 0,
                                                    magicCookie: nil, extensions: nil,
                                                    formatDescriptionOut: &description)
        }
        guard status == noErr, let description else { return nil }
        formatDescription = description
    }

    /// Encodes `pcm`; returns whatever complete packets the encoder released.
    func encode(_ pcm: AVAudioPCMBuffer) -> [CMSampleBuffer] {
        var delivered = false
        return run { _, status in
            if delivered {
                status.pointee = .noDataNow
                return nil
            }
            delivered = true
            status.pointee = .haveData
            return pcm
        }
    }

    /// Flushes the encoder at the end of the recording.
    func drain() -> [CMSampleBuffer] {
        run { _, status in
            status.pointee = .endOfStream
            return nil
        }
    }

    private func run(_ inputBlock: @escaping AVAudioConverterInputBlock) -> [CMSampleBuffer] {
        var out: [CMSampleBuffer] = []
        let maxPacket = max(converter.maximumOutputPacketSize, 1_536)
        while true {
            let compressed = AVAudioCompressedBuffer(format: outputFormat, packetCapacity: 16,
                                                     maximumPacketSize: maxPacket)
            var error: NSError?
            let status = converter.convert(to: compressed, error: &error, withInputFrom: inputBlock)
            if compressed.packetCount > 0, let sample = makeSampleBuffer(compressed) {
                out.append(sample)
            }
            // `.haveData`: the output filled up and more may follow.
            if status != .haveData || compressed.packetCount == 0 { break }
        }
        return out
    }

    private func makeSampleBuffer(_ buffer: AVAudioCompressedBuffer) -> CMSampleBuffer? {
        let count = Int(buffer.packetCount)
        let byteLength = Int(buffer.byteLength)
        guard count > 0, byteLength > 0, let descriptions = buffer.packetDescriptions else { return nil }
        var block: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(allocator: kCFAllocatorDefault, memoryBlock: nil,
                                                 blockLength: byteLength, blockAllocator: kCFAllocatorDefault,
                                                 customBlockSource: nil, offsetToData: 0, dataLength: byteLength,
                                                 flags: kCMBlockBufferAssureMemoryNowFlag,
                                                 blockBufferOut: &block) == noErr, let block,
              CMBlockBufferReplaceDataBytes(with: buffer.data, blockBuffer: block, offsetIntoDestination: 0,
                                            dataLength: byteLength) == noErr else { return nil }
        var sample: CMSampleBuffer?
        let pts = CMTime(value: packetsOut * Int64(Self.framesPerPacket),
                         timescale: CMTimeScale(FMP4SegmentWriter.sampleRate))
        let status = CMAudioSampleBufferCreateReadyWithPacketDescriptions(
            allocator: kCFAllocatorDefault, dataBuffer: block, formatDescription: formatDescription,
            sampleCount: count, presentationTimeStamp: pts, packetDescriptions: descriptions,
            sampleBufferOut: &sample)
        guard status == noErr, let sample else { return nil }
        packetsOut += Int64(count)
        return sample
    }
}

/// Converts any PCM buffer to `FMP4SegmentWriter.pcmFormat` (48 kHz mono Int16),
/// keeping the resampler's state across buffers. One per source, used from one thread.
final class PCMConverter {
    let outputFormat = FMP4SegmentWriter.pcmFormat
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?

    func convert(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard buffer.frameLength > 0 else { return nil }
        if inputFormat != buffer.format || converter == nil {
            converter = AVAudioConverter(from: buffer.format, to: outputFormat)
            converter?.downmix = true
            inputFormat = buffer.format
        }
        guard let converter else { return nil }
        let ratio = outputFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1_024
        guard let out = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { return nil }
        var delivered = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if delivered {
                inputStatus.pointee = .noDataNow
                return nil
            }
            delivered = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, out.frameLength > 0 else { return nil }
        return out
    }
}
