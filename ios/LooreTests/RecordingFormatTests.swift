import AVFoundation
import XCTest
@testable import Loore

/// The fMP4 chunk contract (design doc §9.2, map C §2.1): chunk 0 = init segment
/// + first media segment; later chunks are bare media segments (no `ftyp`).
final class RecordingFormatTests: XCTestCase {
    private func box(_ type: String, payload: Int = 8) -> Data {
        var d = Data()
        let size = UInt32(8 + payload)
        d.append(contentsOf: withUnsafeBytes(of: size.bigEndian, Array.init))
        d.append(Data(type.utf8))
        d.append(Data(repeating: 0, count: payload))
        return d
    }

    // MARK: Packager (pure)

    func testChunkZeroIsInitPlusFirstMediaSegment() throws {
        var packager = SegmentPackager()
        let initSeg = box("ftyp") + box("moov", payload: 40)
        let media1 = box("moof", payload: 20) + box("mdat", payload: 100)
        let media2 = box("moof", payload: 20) + box("mdat", payload: 90)

        XCTAssertNil(packager.add(initSeg, kind: .initialization))
        XCTAssertTrue(packager.hasPendingInit)
        let c0 = try XCTUnwrap(packager.add(media1, kind: .media, duration: 15))
        XCTAssertEqual(c0.index, 0)
        XCTAssertEqual(c0.data, initSeg + media1)
        XCTAssertEqual(c0.duration, 15)
        let c1 = try XCTUnwrap(packager.add(media2, kind: .media))
        XCTAssertEqual(c1.index, 1)
        XCTAssertEqual(c1.data, media2)
        XCTAssertFalse(MP4Boxes.startsWithFtyp(c1.data))
        XCTAssertTrue(MP4Boxes.startsWithFtyp(c0.data))
        XCTAssertEqual(try MP4Boxes.serverInitSegmentLength(c0.data), initSeg.count)
    }

    func testNewWriterInitOpensSubsessionChunk() throws {
        var packager = SegmentPackager(firstIndex: 7)
        let initSeg = box("ftyp") + box("moov")
        let media = box("moof") + box("mdat")
        _ = packager.add(initSeg, kind: .initialization)
        let chunk = try XCTUnwrap(packager.add(media, kind: .media))
        XCTAssertEqual(chunk.index, 7)
        XCTAssertTrue(MP4Boxes.startsWithFtyp(chunk.data), "a later writer's first chunk opens a subsession")
    }

    func testEmptyMediaSegmentIsSkipped() {
        var packager = SegmentPackager()
        XCTAssertNil(packager.add(Data(), kind: .media))
        XCTAssertEqual(packager.nextIndex, 0)
    }

    func testServerRuleRejectsMissingMoov() {
        let bad = box("ftyp") + box("moof") + box("mdat")
        XCTAssertThrowsError(try MP4Boxes.serverInitSegmentLength(bad)) { error in
            XCTAssertEqual(error as? MP4Boxes.InitParseError, .missingMoov)
        }
        XCTAssertThrowsError(try MP4Boxes.serverInitSegmentLength(box("ftyp") + box("moov"))) { error in
            XCTAssertEqual(error as? MP4Boxes.InitParseError, .noFragment)
        }
    }

    // MARK: Real encoder

    /// 40 s of a tone at 44.1 kHz stereo float (so the converter resamples and
    /// downmixes) → writer with 15 s segments → 3 chunks in the server's shape.
    func testWriterProducesServerCompatibleChunks() async throws {
        let collector = ChunkCollector()
        let writer = try FMP4SegmentWriter(segmentSeconds: 15, realTime: false) { collector.add($0) }
        let converter = PCMConverter()
        let source = AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 2)!
        let seconds = 40.0
        let step = 4_410
        var phase = 0.0
        var written = 0
        while Double(written) < seconds * 44_100 {
            let buffer = AVAudioPCMBuffer(pcmFormat: source, frameCapacity: AVAudioFrameCount(step))!
            buffer.frameLength = AVAudioFrameCount(step)
            for i in 0..<step {
                let v = Float(sin(phase) * 0.2)
                buffer.floatChannelData![0][i] = v
                buffer.floatChannelData![1][i] = v
                phase += 2 * .pi * 440 / 44_100
            }
            if let out = converter.convert(buffer) { writer.append(out) }
            written += step
        }
        await writer.finish()

        let chunks = collector.chunks
        XCTAssertEqual(chunks.map(\.index), [0, 1, 2])
        XCTAssertTrue(MP4Boxes.startsWithFtyp(chunks[0].data))
        let initLength = try MP4Boxes.serverInitSegmentLength(chunks[0].data)
        XCTAssertGreaterThan(initLength, 0)
        let types0 = try MP4Boxes.topLevel(chunks[0].data).map(\.type)
        XCTAssertEqual(Array(types0.prefix(2)), ["ftyp", "moov"])
        XCTAssertTrue(types0.contains("moof") && types0.contains("mdat"))
        for chunk in chunks.dropFirst() {
            XCTAssertFalse(MP4Boxes.startsWithFtyp(chunk.data), "chunk \(chunk.index) must not start with ftyp")
            let types = try MP4Boxes.topLevel(chunk.data).map(\.type)
            XCTAssertFalse(types.contains("moov"))
            XCTAssertTrue(types.contains("moof") && types.contains("mdat"), "chunk \(chunk.index): \(types)")
        }
        let total = chunks.map(\.duration).reduce(0, +)
        XCTAssertEqual(total, seconds, accuracy: 0.3)
        XCTAssertEqual(chunks[0].duration, 15, accuracy: 0.2)
    }

    /// A pause flushes a short segment; timestamps stay continuous (no new init).
    func testFlushCutsASegmentWithoutNewInit() async throws {
        let collector = ChunkCollector()
        let writer = try FMP4SegmentWriter(segmentSeconds: 15, realTime: false) { collector.add($0) }
        func feed(_ seconds: Double) {
            let format = FMP4SegmentWriter.pcmFormat
            let frames = AVAudioFrameCount(seconds * format.sampleRate)
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
            buffer.frameLength = frames
            for i in 0..<Int(frames) { buffer.int16ChannelData![0][i] = Int16((i % 100) * 50) }
            writer.append(buffer)
        }
        feed(3)
        writer.flush()
        feed(4)
        await writer.finish()
        let chunks = collector.chunks
        XCTAssertEqual(chunks.count, 2)
        XCTAssertFalse(MP4Boxes.startsWithFtyp(chunks[1].data))
        XCTAssertEqual(chunks[0].duration, 3, accuracy: 0.1)
        XCTAssertEqual(chunks[1].duration, 4, accuracy: 0.1)
    }

    /// Format probing helper (not a regular test): with `TEST_RUNNER_LOORE_AUDIO_FILE`
    /// and `TEST_RUNNER_LOORE_CHUNK_DIR` set, encodes that file through the app's
    /// pipeline and writes `chunk_N.mp4` files for a manual upload.
    func testDumpChunksForFormatProbe() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let path = env["LOORE_AUDIO_FILE"], let outDir = env["LOORE_CHUNK_DIR"] else {
            throw XCTSkip("format probe inputs not set")
        }
        let seconds = Double(env["LOORE_SEGMENT_SECONDS"] ?? "15") ?? 15
        let collector = ChunkCollector()
        let writer = try FMP4SegmentWriter(segmentSeconds: seconds, realTime: false) { collector.add($0) }
        let converter = PCMConverter()
        let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
        while file.framePosition < file.length {
            let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 4_096)!
            try file.read(into: buffer, frameCount: 4_096)
            if buffer.frameLength == 0 { break }
            if let out = converter.convert(buffer) { writer.append(out) }
        }
        await writer.finish()
        try FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
        for chunk in collector.chunks {
            try chunk.data.write(to: URL(fileURLWithPath: outDir).appendingPathComponent("chunk_\(chunk.index).mp4"))
        }
        XCTAssertFalse(collector.chunks.isEmpty)
    }
}

final class ChunkCollector: @unchecked Sendable {
    private let lock = NSLock()
    private var _chunks: [RecordedChunk] = []

    func add(_ chunk: RecordedChunk) {
        lock.lock()
        _chunks.append(chunk)
        lock.unlock()
    }

    var chunks: [RecordedChunk] {
        lock.lock()
        defer { lock.unlock() }
        return _chunks
    }
}
