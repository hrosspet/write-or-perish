import Foundation

/// One upload-ready piece of a recording (map C §2.1 "Byte-level contract").
struct RecordedChunk: Equatable, Sendable {
    /// 0-based, continuing across pauses and resumes.
    var index: Int
    var data: Data
    /// Seconds of audio in this chunk (from the sample count).
    var duration: Double
}

/// Turns the writer's segment stream into the chunks the server expects:
///
/// - chunk 0 = the initialization segment (`ftyp` + `moov`) **concatenated with**
///   the first media segment (`moof` + `mdat`);
/// - chunks 1…N = each following media segment alone;
/// - if a new writer starts later (after a media-services reset), its
///   initialization segment is prepended to its first media segment, so that
///   chunk starts with `ftyp` and the server opens a subsession (#124).
///
/// Pure value type: the writer feeds it, tests feed it directly.
struct SegmentPackager {
    enum SegmentKind: Equatable { case initialization, media }

    private(set) var nextIndex: Int
    private var pendingInit: Data?

    init(firstIndex: Int = 0) {
        nextIndex = firstIndex
    }

    /// Feeds one segment; returns the chunk it completes, if any.
    mutating func add(_ data: Data, kind: SegmentKind, duration: Double = 0) -> RecordedChunk? {
        switch kind {
        case .initialization:
            pendingInit = data
            return nil
        case .media:
            guard !data.isEmpty else { return nil }
            var bytes = Data()
            if let initSegment = pendingInit {
                bytes.append(initSegment)
                pendingInit = nil
            }
            bytes.append(data)
            let chunk = RecordedChunk(index: nextIndex, data: bytes, duration: duration)
            nextIndex += 1
            return chunk
        }
    }

    /// True while an initialization segment waits for its first media segment.
    var hasPendingInit: Bool { pendingInit != nil }
}

/// Minimal ISO-BMFF top-level box walker, used by tests and by a debug check
/// that mirrors the server's `extract_mp4_init_segment` (backend/utils/webm_utils.py).
enum MP4Boxes {
    struct Box: Equatable {
        var type: String
        var offset: Int
        var size: Int
    }

    enum WalkError: Error, Equatable {
        case truncated(Int)
        case badSize(Int)
    }

    /// Top-level boxes of `data` in order.
    static func topLevel(_ data: Data) throws -> [Box] {
        var boxes: [Box] = []
        var offset = 0
        let bytes = [UInt8](data)
        while offset + 8 <= bytes.count {
            var size = Int(readUInt32(bytes, offset))
            let type = String(bytes: bytes[(offset + 4)..<(offset + 8)], encoding: .ascii) ?? "????"
            if size == 1 {
                guard offset + 16 <= bytes.count else { throw WalkError.truncated(offset) }
                size = Int(readUInt64(bytes, offset + 8))
                guard size >= 16 else { throw WalkError.badSize(offset) }
            } else if size == 0 {
                size = bytes.count - offset
            } else if size < 8 {
                throw WalkError.badSize(offset)
            }
            boxes.append(Box(type: type, offset: offset, size: size))
            offset += size
        }
        return boxes
    }

    /// The server's chunk-0 rule: a `moov` before the first `moof`/`mdat`, and at
    /// least one `moof`/`mdat`. Returns the init-segment length (bytes before the
    /// first `moof`/`mdat`) or throws the reason the server would give.
    static func serverInitSegmentLength(_ data: Data) throws -> Int {
        var sawMoov = false
        for box in try topLevel(data) {
            if box.type == "moof" || box.type == "mdat" {
                guard sawMoov else { throw InitParseError.missingMoov }
                return box.offset
            }
            if box.type == "moov" { sawMoov = true }
        }
        throw InitParseError.noFragment
    }

    enum InitParseError: Error, Equatable {
        case missingMoov
        case noFragment
    }

    /// The server's subsession test (`chunk_is_init_bearing`): bytes 4…8 are `ftyp`.
    static func startsWithFtyp(_ data: Data) -> Bool {
        guard data.count >= 8 else { return false }
        return data[data.startIndex + 4..<data.startIndex + 8] == Data("ftyp".utf8)
    }

    private static func readUInt32(_ b: [UInt8], _ o: Int) -> UInt32 {
        UInt32(b[o]) << 24 | UInt32(b[o + 1]) << 16 | UInt32(b[o + 2]) << 8 | UInt32(b[o + 3])
    }

    private static func readUInt64(_ b: [UInt8], _ o: Int) -> UInt64 {
        (0..<8).reduce(UInt64(0)) { ($0 << 8) | UInt64(b[o + $1]) }
    }
}
