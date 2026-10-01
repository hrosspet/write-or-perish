import Foundation
import os

/// What a speaker icon plays: a node, a profile, or a saved reference (web `SpeakerIcon`).
enum ListenTarget: Hashable, Sendable {
    case node(Int)
    case profile(Int)
    case item(Int)

    var id: Int {
        switch self {
        case .node(let id), .profile(let id), .item(let id): return id
        }
    }

    var source: ChunkQueuePlayer.Source {
        switch self {
        case .node(let id): return .node(id)
        case .profile(let id): return .profile(id)
        case .item(let id): return .item(id)
        }
    }

    /// The web's per-entity base (`/nodes/<id>`, `/external/items/<id>`, `/profile/<id>`).
    var base: String {
        switch self {
        case .node(let id): return "/api/nodes/\(id)"
        case .item(let id): return "/api/external/items/\(id)"
        case .profile(let id): return "/api/profile/\(id)"
        }
    }

    var ttsStream: String {
        switch self {
        case .node(let id): return APIPath.sseNodeTTS(id)
        case .profile(let id): return APIPath.sseProfileTTS(id)
        case .item(let id): return APIPath.sseItemTTS(id)
        }
    }

    /// Profiles have no `/tts-chapters`.
    var hasChapters: Bool {
        if case .profile = self { return false }
        return true
    }

    /// "Node 12" / "Reference 12" / "Profile 12" when the content has no heading.
    var fallbackTitle: String {
        switch self {
        case .node(let id): return "Node \(id)"
        case .item(let id): return "Reference \(id)"
        case .profile(let id): return "Profile \(id)"
        }
    }

    /// Web `extractMarkdownHeader`: the first line starting with `#`, without the hashes.
    static func title(from content: String?) -> String? {
        for line in (content ?? "").split(separator: "\n", omittingEmptySubsequences: false) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("#") {
                return trimmed.replacingOccurrences(of: "^#+\\s*", with: "", options: .regularExpression)
                    .trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }
}

extension AudioCenter {
    /// Speaker-icon tap (web `SpeakerIcon.handleClick`): the original recording
    /// first (chunked recordings as a queue), else the TTS file with its
    /// chapters; nothing yet and voice mode on → `POST /tts`, then play the
    /// chunks as they stream in. Plays in the global player (the mini-player).
    func listen(to target: ListenTarget, content: String?, onTtsGenerated: (() -> Void)? = nil) {
        guard let app = appState, loadingSource == nil else { return }
        let title = ListenTarget.title(from: content) ?? target.fallbackTitle
        let cacheKey = ListenCacheKey(target: target, content: content ?? "")
        if let cached = listenCache[cacheKey] {
            player.load(urls: cached.urls, durations: cached.durations, title: title, source: target.source,
                        chapters: cached.chapters)
            return
        }
        setLoading(target.source)
        listenTask?.cancel()
        listenTask = Task { [weak self] in
            guard let self else { return }
            defer { if self.loadingSource == target.source && !self.player.generatingTTS { self.setLoading(nil) } }
            let api = app.api
            var urls: AudioURLs?
            if let (data, http) = try? await api.data(for: APIRequest(.get, "\(target.base)/audio")), http.statusCode == 200 {
                urls = try? api.decode(AudioURLs.self, from: data)
            }
            guard !Task.isCancelled else { return }

            if case .node = target, urls?.hasAudioChunks == true || (urls?.originalURL == nil && urls?.ttsURL == nil),
               let chunks: AudioChunksResponse = try? await api.get("\(target.base)/audio-chunks"),
               !chunks.chunks.isEmpty {
                let entry = ListenCacheEntry(urls: chunks.chunks.map(\.url), durations: chunks.chunks.map(\.duration), chapters: [])
                self.listenCache[cacheKey] = entry
                self.player.load(urls: entry.urls, durations: entry.durations, title: title, source: target.source)
                return
            }
            if let path = urls?.originalURL ?? urls?.ttsURL {
                // Chapters only for TTS audio (a recording has no sections, #145).
                let chapters = urls?.originalURL == nil ? await self.fetchChapters(target, singleFile: true) : []
                let entry = ListenCacheEntry(urls: [path], durations: [nil], chapters: chapters)
                self.listenCache[cacheKey] = entry
                self.player.load(urls: [path], title: title, source: target.source, chapters: chapters)
                return
            }
            // Nothing exists: only voice-mode users may generate TTS.
            guard app.user?.voiceModeEnabled == true else { return }
            do {
                _ = try await api.data(for: .json(.post, "\(target.base)/tts", nil))
            } catch {
                return
            }
            await self.streamGeneratedTTS(target, title: title, cacheKey: cacheKey, onTtsGenerated: onTtsGenerated)
        }
    }

    private func streamGeneratedTTS(_ target: ListenTarget, title: String, cacheKey: ListenCacheKey,
                                    onTtsGenerated: (() -> Void)?) async {
        guard let app = appState else { return }
        var received = Set<Int>()
        var first = true
        var lastSection: Int?
        player.generatingTTS = true
        defer {
            if player.source == target.source || first { player.generatingTTS = false }
            setLoading(nil)
        }
        do {
            for try await message in app.sse.subscribe(path: target.ttsStream) {
                guard !Task.isCancelled else { return }
                guard case .event(let event) = message else { continue }
                switch TTSStreamEvent(event) {
                case .chunkReady(let chunk):
                    guard received.insert(chunk.chunkIndex).inserted else { continue }
                    if first {
                        first = false
                        setLoading(nil)
                        lastSection = chunk.sectionIndex
                        player.load(urls: [chunk.audioURL], durations: [chunk.duration], title: title, source: target.source)
                        player.generatingTTS = true
                        Task { self.player.setChapters(await self.fetchChapters(target, singleFile: false)) }
                    } else if player.source == target.source {
                        player.append(url: chunk.audioURL, duration: chunk.duration)
                        if let section = chunk.sectionIndex, section != lastSection {
                            lastSection = section
                            Task { self.player.setChapters(await self.fetchChapters(target, singleFile: false)) }
                        }
                    }
                case .allComplete(let complete):
                    if player.source == target.source {
                        player.generatingTTS = false
                        player.setChapters(await fetchChapters(target, singleFile: false))
                    }
                    if let url = complete.ttsURL {
                        listenCache[cacheKey] = ListenCacheEntry(urls: [url], durations: [nil],
                                                                 chapters: await fetchChapters(target, singleFile: true))
                    }
                    onTtsGenerated?()
                    return
                case .error:
                    return
                default:
                    continue
                }
            }
        } catch {
            log.info("listen-aloud stream ended with an error")
        }
    }

    /// `GET …/tts-chapters` (nodes and saved references; empty unless > 1 section).
    private func fetchChapters(_ target: ListenTarget, singleFile: Bool) async -> [QueueChapter] {
        guard target.hasChapters, let api = appState?.api,
              let answer: TTSChaptersResponse = try? await api.get("\(target.base)/tts-chapters") else { return [] }
        return answer.chapters.map {
            QueueChapter(title: $0.title, chunkIndex: singleFile ? nil : $0.chunkIndex, startTime: $0.startTime)
        }
    }

    /// Whether a speaker icon for `target` shows as playing (accent colour).
    func isPlaying(_ target: ListenTarget) -> Bool {
        player.source == target.source && player.isPlaying
    }

    func isLoading(_ target: ListenTarget) -> Bool {
        loadingSource == target.source
    }
}

struct ListenCacheKey: Hashable {
    var target: ListenTarget
    var content: String
}

struct ListenCacheEntry {
    var urls: [String]
    var durations: [Double?]
    var chapters: [QueueChapter]
}

/// Download a node's audio (web `DownloadAudioIcon`): the original recording,
/// else the merged recording as MP3 (`audio-download?format=mp3`), else the TTS.
/// Returns a temporary file named like the web's download, for the share sheet.
enum AudioDownloader {
    enum Failure: Error { case nothingToDownload }

    @MainActor
    static func download(nodeId: Int, api: APIClient) async throws -> URL {
        let (data, http) = try await api.data(for: APIRequest(.get, APIPath.nodeAudio(nodeId)))
        guard http.statusCode == 200 else { throw Failure.nothingToDownload }
        let urls = try api.decode(AudioURLs.self, from: data)
        if let original = urls.originalURL {
            let ext = fileExtension(original, fallback: "webm")
            return try await fetch(original, to: "node-\(nodeId)-recording.\(ext)", api: api)
        }
        if urls.hasAudioChunks,
           let file = try? await fetch("\(APIPath.nodeAudioDownload(nodeId))?format=mp3",
                                       to: "node-\(nodeId)-recording.mp3", api: api) {
            return file
        }
        if let tts = urls.ttsURL {
            return try await fetch(tts, to: "node-\(nodeId)-tts.mp3", api: api)
        }
        throw Failure.nothingToDownload
    }

    /// Web: `original_url.split('.').pop().replace(/\.enc$/, '')`.
    static func fileExtension(_ url: String, fallback: String) -> String {
        let path = url.split(separator: "?").first.map(String.init) ?? url
        let trimmed = path.hasSuffix(".enc") ? String(path.dropLast(4)) : path
        let ext = (trimmed as NSString).pathExtension
        return ext.isEmpty ? fallback : ext
    }

    @MainActor
    private static func fetch(_ path: String, to name: String, api: APIClient) async throws -> URL {
        var relative = path
        if path.hasPrefix("http"), let url = URL(string: path) {
            relative = url.path + (url.query.map { "?" + $0 } ?? "")
        }
        var r = APIRequest(.get, relative)
        r.accept = "*/*"
        r.timeout = 300
        let (tmp, _) = try await api.download(r)
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent(name)
        try FileManager.default.moveItem(at: tmp, to: dest)
        return dest
    }
}
