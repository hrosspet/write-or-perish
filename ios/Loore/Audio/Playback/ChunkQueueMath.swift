import Foundation

/// One chapter of the queue (web `AudioContext` chapters: `{title, start_time, chunk_index}`).
struct QueueChapter: Equatable, Sendable {
    var title: String
    /// Chunk the chapter starts at (nil for a merged single file).
    var chunkIndex: Int?
    /// Fallback start in seconds (single-file chapters from `/tts-chapters`).
    var startTime: Double
}

/// The queue's time maths (web `seekToCumulativeTime`, `chapterStartTime`),
/// kept pure so it is unit-tested.
struct ChunkQueueMath: Equatable {
    var durations: [Double]

    var total: Double { durations.reduce(0, +) }

    /// Cumulative start of chunk `index`.
    func start(of index: Int) -> Double {
        durations.prefix(max(0, min(index, durations.count))).reduce(0, +)
    }

    /// Cumulative position of `offset` seconds into chunk `index`.
    func cumulative(index: Int, offset: Double) -> Double {
        start(of: index) + max(0, offset)
    }

    /// Where cumulative time `t` falls: the chunk and the offset inside it.
    /// Times past the end land at the end of the last chunk (`atEnd` = true
    /// within 0.1 s of the end, like the web, which then only shows the end).
    func locate(_ t: Double) -> (index: Int, offset: Double, atEnd: Bool)? {
        guard !durations.isEmpty else { return nil }
        let clamped = max(0, t)
        let end = total
        if clamped >= end - 0.1 {
            let last = durations.count - 1
            return (last, durations[last], true)
        }
        var acc = 0.0
        for (i, d) in durations.enumerated() {
            if clamped < acc + d { return (i, clamped - acc, false) }
            acc += d
        }
        let last = durations.count - 1
        return (last, durations[last], true)
    }

    /// Start of a chapter: from its chunk's live cumulative start when it has
    /// one, else its stored start time (web `chapterStartTime`).
    func chapterStart(_ chapter: QueueChapter) -> Double {
        if let index = chapter.chunkIndex, index < durations.count {
            return start(of: index)
        }
        return chapter.startTime
    }

    /// Index of the chapter playing at `t` (the last one starting at or before it).
    func chapterIndex(at t: Double, chapters: [QueueChapter]) -> Int? {
        guard !chapters.isEmpty else { return nil }
        var found: Int?
        for (i, c) in chapters.enumerated() where chapterStart(c) <= t + 0.01 {
            found = i
        }
        return found ?? 0
    }
}

/// Voice-mode chapter titles (web `chapterTitleFromContent`): the node text's
/// first words without markdown furniture, cut at a word boundary.
enum ChapterTitle {
    static let maxLength = 44
    static let placeholder = "…"

    static func from(content: String?) -> String? {
        let stripped = (content ?? "").replacingOccurrences(of: "[#*_`>\\[\\]]", with: "", options: .regularExpression)
        let clean = stripped.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        guard !clean.isEmpty else { return nil }
        if clean.count <= maxLength { return clean }
        let cut = String(clean.prefix(maxLength))
        if let space = cut.lastIndex(of: " "), cut.distance(from: cut.startIndex, to: space) > 20 {
            return String(cut[..<space]) + "…"
        }
        return cut + "…"
    }

    /// Roman numerals for chain chapters on the Voice screen.
    static let roman = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII"]

    static func numeral(_ index: Int) -> String {
        index < roman.count ? roman[index] : String(index + 1)
    }
}

/// `m:ss` (web `formatTime`); `00:00` style for the recording timer (web `formatDuration`).
enum AudioTimeFormat {
    static func clock(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "0:00" }
        let s = Int(seconds)
        return "\(s / 60):" + String(format: "%02d", s % 60)
    }

    static func recording(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "00:00" }
        let s = Int(seconds)
        return String(format: "%02d:%02d", s / 60, s % 60)
    }
}
