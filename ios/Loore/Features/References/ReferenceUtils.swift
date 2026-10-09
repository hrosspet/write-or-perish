import Foundation

/// Saved-reference helpers: a port of `utils/references.js`. The functions
/// take plain fields of a serialized reference (`_serialize_item` in
/// `backend/routes/external.py`) so any model can call them.
enum ReferenceUtils {
    /// A YouTube watch page: video id and start time in seconds.
    struct YouTubeVideo: Equatable, Sendable {
        var id: String
        var start: Int
    }

    /// `asCardNode(item)`: the node-shaped fields the Log card reads.
    struct CardFields: Equatable, Sendable {
        var id: Int
        var preview: String
        /// The reference title in the card's thread-name slot ("" when none).
        var threadName: String
        var createdAt: Date?
        var childCount: Int
    }

    /// `SOURCE_LABEL`.
    static let sourceLabels: [String: String] = [
        "web_clip": "Page",
        "twitter_bookmark": "Tweet",
        "community_archive": "Archive tweet",
        "read_pick": "Archive tweet",
    ]

    /// `sourceLabel(item)`: "Video" for a YouTube clip, else `sourceLabels`,
    /// else the raw source.
    static func sourceLabel(source: String, url: String?) -> String {
        if youtubeVideo(source: source, url: url) != nil { return "Video" }
        if let label = sourceLabels[source], !label.isEmpty { return label }
        return source
    }

    /// `authorLabel(item)`: "@handle" for tweets, after the display name when
    /// the archive has one (#435); the byline as-is for a web clip; nil when
    /// there is no handle.
    static func authorLabel(source: String, handle: String?, name: String? = nil) -> String? {
        guard let handle, !handle.isEmpty else { return nil }
        if source == "web_clip" { return handle }
        if let name, !name.isEmpty { return "\(name) @\(handle)" }
        return "@\(handle)"
    }

    /// `asCardNode(item)`: the title takes the thread-name slot; the date is
    /// `posted_at`, else `fetched_at`; child count is always 0.
    static func asCardNode(id: Int, preview: String?, title: String?, postedAt: Date?, fetchedAt: Date?) -> CardFields {
        CardFields(id: id, preview: preview ?? "", threadName: title ?? "", createdAt: postedAt ?? fetchedAt,
                   childCount: 0)
    }

    /// `bodyWithoutTitle(item)`: drop a leading markdown heading line that
    /// repeats the title (trimmed, case-insensitive).
    static func bodyWithoutTitle(title: String?, content: String?) -> String {
        let content = content ?? ""
        guard let title, !title.isEmpty else { return content }
        // /^\s*#{1,6}[ \t]+([^\n]+?)[ \t]*(?:\n|$)/ with JS `\s` and `$`.
        guard let m = JSRegex.firstMatch(content, "^\(JSRegex.space)*#{1,6}[ \\t]+([^\\n]+?)[ \\t]*(?:\\n|\\z)"),
              let whole = m[0], let heading = m[1] else { return content }
        if heading.jsTrimmed.lowercased(with: nil).utf16.elementsEqual(title.jsTrimmed.lowercased(with: nil).utf16) {
            return content.jsSubstring(whole.jsLength)
        }
        return content
    }

    /// `tweetId(item)`: the external id of a tweet-like source, else nil.
    static func tweetId(source: String, externalId: String?) -> String? {
        guard ["twitter_bookmark", "community_archive", "read_pick"].contains(source) else { return nil }
        guard let externalId, !externalId.isEmpty else { return nil }
        return externalId
    }

    /// `YOUTUBE_HOSTS`.
    static let youtubeHosts: Set<String> = [
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtube-nocookie.com",
    ]

    /// `youtubeVideo(item)`: a web clip of a YouTube video (watch, youtu.be,
    /// embed, shorts, live, v paths) with its `t=`/`start=` time; nil for
    /// everything else, including channels and playlists.
    static func youtubeVideo(source: String, url: String?) -> YouTubeVideo? {
        guard source == "web_clip", let url, !url.isEmpty, let u = ParsedURL(url) else { return nil }
        guard youtubeHosts.contains(u.hostname) else { return nil }
        let id: String?
        if u.hostname == "youtu.be" {
            id = String(u.pathname.dropFirst()).components(separatedBy: "/").first
        } else if u.pathname == "/watch" {
            id = u.searchParam("v")
        } else {
            id = embedPathId(u.pathname)
        }
        guard let id, isYouTubeId(id) else { return nil }
        let t = u.searchParam("t").flatMap { $0.isEmpty ? nil : $0 } ?? u.searchParam("start")
        return YouTubeVideo(id: id, start: startSeconds(t))
    }

    /// `startSeconds(t)`: "1h2m3s", "90s", "90" → seconds; anything else → 0.
    /// A value too large for `Int` is 0.
    static func startSeconds(_ t: String?) -> Int {
        guard let t, !t.isEmpty else { return 0 }
        let units = Array(t.utf16)
        func isDigit(_ u: UInt16) -> Bool { u >= 0x30 && u <= 0x39 }
        func number(_ digits: ArraySlice<UInt16>) -> Double {
            Double(String(decoding: digits, as: UTF16.self)) ?? 0
        }
        func toInt(_ d: Double) -> Int { d < 9.2e18 ? Int(d) : 0 }
        if units.allSatisfy(isDigit) { return toInt(number(units[...])) }
        // /^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/
        var pos = 0
        var total = 0.0
        for (unit, factor) in [(UInt16(0x68), 3600.0), (0x6D, 60.0), (0x73, 1.0)] {
            var end = pos
            while end < units.count, isDigit(units[end]) { end += 1 }
            if end > pos, end < units.count, units[end] == unit {
                total += number(units[pos..<end]) * factor
                pos = end + 1
            }
        }
        return pos == units.count ? toInt(total) : 0
    }

    /// `^\/(?:embed|shorts|live|v)\/([^/]+)` on the pathname.
    private static func embedPathId(_ path: String) -> String? {
        let parts = path.components(separatedBy: "/")
        guard parts.count >= 3, parts[0].isEmpty, ["embed", "shorts", "live", "v"].contains(parts[1]),
              !parts[2].isEmpty else { return nil }
        return parts[2]
    }

    /// `YOUTUBE_ID_RE`: `^[A-Za-z0-9_-]{11}$`.
    private static func isYouTubeId(_ id: String) -> Bool {
        let units = Array(id.utf16)
        return units.count == 11 && units.allSatisfy { u in
            (u >= 0x41 && u <= 0x5A) || (u >= 0x61 && u <= 0x7A) || (u >= 0x30 && u <= 0x39) || u == 0x5F || u == 0x2D
        }
    }
}

/// The parts of a WHATWG `new URL(...)` that `youtubeVideo` reads: lowercased
/// hostname, pathname and query. Nil where the web's constructor throws for
/// a URL that could otherwise name a YouTube host (no scheme, bad port).
/// Simplified: no IDNA mapping and no dot-segment resolution.
private struct ParsedURL {
    let hostname: String
    let pathname: String
    let query: String

    init?(_ input: String) {
        // Strip leading/trailing C0 controls and spaces, drop tabs and newlines.
        var scalars = Array(input.unicodeScalars)
        while let first = scalars.first, first.value <= 0x20 { scalars.removeFirst() }
        while let last = scalars.last, last.value <= 0x20 { scalars.removeLast() }
        scalars.removeAll { $0 == "\t" || $0 == "\n" || $0 == "\r" }
        let s = String(String.UnicodeScalarView(scalars))

        // Scheme: ALPHA *(ALPHA / DIGIT / "+" / "-" / ".") ":"
        guard let colon = s.firstIndex(of: ":") else { return nil }
        let scheme = s[..<colon]
        guard let head = scheme.unicodeScalars.first, head.isASCIIAlpha,
              scheme.unicodeScalars.allSatisfy({ $0.isASCIIAlpha || ("0"..."9").contains($0) || "+-.".unicodeScalars.contains($0) })
        else { return nil }
        let special = ["http", "https", "ws", "wss", "ftp", "file"].contains(scheme.lowercased())
        var rest = String(s[s.index(after: colon)...])

        // Fragment and query.
        if let hash = rest.firstIndex(of: "#") { rest = String(rest[..<hash]) }
        var query = ""
        if let q = rest.firstIndex(of: "?") {
            query = String(rest[rest.index(after: q)...])
            rest = String(rest[..<q])
        }
        if special { rest = rest.replacingOccurrences(of: "\\", with: "/") }

        // Authority: special schemes skip any run of slashes; others need "//".
        var authority = ""
        var path = rest
        if special || rest.hasPrefix("//") {
            let afterSlashes = rest.drop(while: { $0 == "/" })
            if let slash = afterSlashes.firstIndex(of: "/") {
                authority = String(afterSlashes[..<slash])
                path = String(afterSlashes[slash...])
            } else {
                authority = String(afterSlashes)
                path = special ? "/" : ""
            }
        }
        if let at = authority.lastIndex(of: "@") { authority = String(authority[authority.index(after: at)...]) }
        var host = authority
        if !authority.hasPrefix("["), let portColon = authority.lastIndex(of: ":") {
            host = String(authority[..<portColon])
            let port = authority[authority.index(after: portColon)...]
            guard port.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
            if !port.isEmpty, (Int(port) ?? Int.max) > 65535 { return nil }
        }
        hostname = (host.removingPercentEncoding ?? host).lowercased()
        pathname = path
        self.query = query
    }

    /// `URLSearchParams.get(name)`: first value, `+` as space, percent-decoded.
    func searchParam(_ name: String) -> String? {
        for pair in query.split(separator: "&", omittingEmptySubsequences: true) {
            let key: Substring
            let value: Substring
            if let eq = pair.firstIndex(of: "=") {
                key = pair[..<eq]
                value = pair[pair.index(after: eq)...]
            } else {
                key = pair
                value = ""
            }
            if Self.formDecode(key) == name { return Self.formDecode(value) }
        }
        return nil
    }

    private static func formDecode(_ s: Substring) -> String {
        let bytes = Array(s.utf8)
        var out: [UInt8] = []
        out.reserveCapacity(bytes.count)
        var i = 0
        func hex(_ b: UInt8) -> UInt8? {
            switch b {
            case 0x30...0x39: return b - 0x30
            case 0x41...0x46: return b - 0x41 + 10
            case 0x61...0x66: return b - 0x61 + 10
            default: return nil
            }
        }
        while i < bytes.count {
            let b = bytes[i]
            if b == 0x2B {
                out.append(0x20)
            } else if b == 0x25, i + 2 < bytes.count, let hi = hex(bytes[i + 1]), let lo = hex(bytes[i + 2]) {
                out.append(hi << 4 | lo)
                i += 2
            } else {
                out.append(b)
            }
            i += 1
        }
        return String(decoding: out, as: UTF8.self)
    }
}

private extension Unicode.Scalar {
    var isASCIIAlpha: Bool { ("a"..."z").contains(self) || ("A"..."Z").contains(self) }
}
