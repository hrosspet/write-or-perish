import AVFoundation
import Observation
import os

/// The app's one audio queue (design doc §9.3, map C §5.5, §8.4 option A), shared
/// by voice mode and the global mini-player — the native `AudioContext`.
///
/// - `AVQueuePlayer` with one item per chunk; a parallel durations array gives
///   cumulative time, chapters and seeking across chunks (`ChunkQueueMath`).
/// - Server durations are used when given and corrected from the item once it
///   has loaded (> 0.5 s off), like the web's `loadedmetadata`/`ended` fixes.
/// - Dedupe: a URL already in the queue is never appended twice.
/// - Every media request carries the session cookies (`AVURLAssetHTTPCookiesKey`);
///   `.mp4` originals are served as octet-stream, so their MIME type is overridden.
/// - Rate 1 / 1.25 / 1.5 / 2 with the time-domain pitch algorithm.
/// - A chunk that fails (unplayable format, 403/404, network) is reported once
///   per queue (`onPlaybackError`, the web's playback toast) and skipped like an
///   ended one, so the queue still drains or finishes.
@MainActor
@Observable
final class ChunkQueuePlayer {
    struct Entry: Equatable {
        var url: URL
        /// Seconds; 0 while unknown.
        var duration: Double
        /// The item could not be played (skipped; counts as 0 s).
        var failed = false
    }

    /// What the queue is playing, so a speaker icon can show its own state.
    enum Source: Equatable {
        case voice
        case node(Int)
        case profile(Int)
        case item(Int)
    }

    static let rates: [Float] = [1, 1.25, 1.5, 2]

    private(set) var entries: [Entry] = []
    private(set) var currentIndex = 0
    private(set) var cumulativeTime: Double = 0
    private(set) var chapters: [QueueChapter] = []
    private(set) var title = ""
    private(set) var source: Source?
    /// The player holds a queue (the mini-player shows while true).
    private(set) var isLoaded = false
    /// The user wants audio to play (web `isPlaying`).
    private(set) var isPlaying = false
    /// The queue ran out while TTS is still generating (web `waitingForChunks`).
    private(set) var waitingForChunks = false
    /// More chunks will be appended (web `generatingTTS`; the pulsing dot).
    var generatingTTS = false {
        didSet {
            if oldValue && !generatingTTS && waitingForChunks {
                waitingForChunks = false
                finish()
            }
        }
    }
    private(set) var rate: Float = 1

    /// The queue drained while generating (voice: start the thinking cue).
    @ObservationIgnored var onDrained: (() -> Void)?
    /// A chunk started after a drain (voice: stop the cue).
    @ObservationIgnored var onResumedAfterDrain: (() -> Void)?
    /// The last chunk ended and nothing more is coming.
    @ObservationIgnored var onFinished: (() -> Void)?
    /// Audio actually started (each transition to playing; voice: stop the cue).
    @ObservationIgnored var onStartedPlaying: (() -> Void)?
    /// State changed (Now Playing refresh).
    @ObservationIgnored var onStateChange: (() -> Void)?
    /// After `play()` (true) or `pause()` (false): voice silences or resumes its cue.
    @ObservationIgnored var onTransport: ((Bool) -> Void)?
    /// A chunk could not be played: the user-facing message (once per queue).
    @ObservationIgnored var onPlaybackError: ((String) -> Void)?
    /// Before `play()` starts audio: the owner activates the audio session.
    @ObservationIgnored var willPlay: (() -> Void)?
    @ObservationIgnored var cookiesProvider: () -> [HTTPCookie] = { [] }
    @ObservationIgnored var urlResolver: (String) -> URL? = { URL(string: $0) }

    @ObservationIgnored private let player = AVQueuePlayer()
    @ObservationIgnored private var itemIndex: [ObjectIdentifier: Int] = [:]
    @ObservationIgnored private var assets: [URL: AVURLAsset] = [:]
    @ObservationIgnored private var timeObserver: Any?
    @ObservationIgnored private var endObserver: NSObjectProtocol?
    @ObservationIgnored private var statusObservation: NSKeyValueObservation?
    @ObservationIgnored private var failureObserver: NSObjectProtocol?
    @ObservationIgnored private var itemObservations: [ObjectIdentifier: NSKeyValueObservation] = [:]
    @ObservationIgnored private var failureReported = false
    @ObservationIgnored private var onFirstPlaying: (() -> Void)?
    @ObservationIgnored private let log = Logger(subsystem: "org.loore.app", category: "player")

    var math: ChunkQueueMath { ChunkQueueMath(durations: entries.map(\.duration)) }
    var totalDuration: Double { math.total }
    var hasAudio: Bool { !entries.isEmpty }
    var atEnd: Bool { totalDuration > 0 && cumulativeTime >= totalDuration - 0.5 }

    init() {
        player.actionAtItemEnd = .advance
        player.automaticallyWaitsToMinimizeStalling = true
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(value: 1, timescale: 10), queue: .main) { [weak self] time in
            MainActor.assumeIsolated { self?.tick(time) }
        }
        endObserver = NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification,
                                                             object: nil, queue: .main) { [weak self] note in
            guard let item = note.object as? AVPlayerItem else { return }
            let id = ObjectIdentifier(item)
            MainActor.assumeIsolated { self?.itemEnded(id, item: item) }
        }
        failureObserver = NotificationCenter.default.addObserver(forName: AVPlayerItem.failedToPlayToEndTimeNotification,
                                                                 object: nil, queue: .main) { [weak self] note in
            guard let item = note.object as? AVPlayerItem else { return }
            let id = ObjectIdentifier(item)
            let error = note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
            MainActor.assumeIsolated { self?.itemFailed(id, item: item, error: error) }
        }
        statusObservation = player.observe(\.timeControlStatus, options: [.new, .old]) { [weak self] player, change in
            let playing = player.timeControlStatus == .playing
            let was = change.oldValue == .playing
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, playing, !was else { return }
                    self.onStartedPlaying?()
                    if let first = self.onFirstPlaying {
                        self.onFirstPlaying = nil
                        first()
                    }
                }
            }
        }
    }

    // MARK: Loading (web loadAudioQueue / loadAudio)

    /// Replaces the queue and plays from the start.
    /// - Parameter durations: server durations (nil or 0 = unknown, loaded from the file).
    func load(urls: [String], durations: [Double?] = [], title: String, source: Source?,
              chapters: [QueueChapter] = [], autoplay: Bool = true, onPlaying: (() -> Void)? = nil) {
        teardownItems()
        entries = []
        for (i, raw) in urls.enumerated() {
            guard let url = urlResolver(raw) else { continue }
            entries.append(Entry(url: url, duration: (i < durations.count ? durations[i] : nil) ?? 0))
        }
        self.title = title
        self.source = source
        self.chapters = chapters
        currentIndex = 0
        cumulativeTime = 0
        waitingForChunks = false
        failureReported = false
        isLoaded = !entries.isEmpty
        onFirstPlaying = onPlaying
        entries.indices.forEach(probeDurationIfNeeded)
        rebuild(from: 0, offset: 0)
        if autoplay { play() } else { onStateChange?() }
    }

    /// Appends a chunk (web `appendChunkToQueue`); plays it at once if the queue had drained.
    /// Returns false for a duplicate URL.
    @discardableResult
    func append(url raw: String, duration: Double?, chapterTitle: String? = nil) -> Bool {
        guard let url = urlResolver(raw) else { return false }
        if entries.contains(where: { $0.url == url }) { return false }
        entries.append(Entry(url: url, duration: duration ?? 0))
        let index = entries.count - 1
        if let chapterTitle {
            chapters.append(QueueChapter(title: chapterTitle, chunkIndex: index, startTime: math.start(of: index)))
        }
        probeDurationIfNeeded(index)
        if waitingForChunks {
            waitingForChunks = false
            currentIndex = index
            rebuild(from: index, offset: 0)
            if isPlaying { startPlayer() }
            onResumedAfterDrain?()
        } else {
            let item = makeItem(index)
            player.insert(item, after: nil)
        }
        onStateChange?()
        return true
    }

    func renameChapter(atChunk index: Int, to title: String) {
        guard let i = chapters.firstIndex(where: { $0.chunkIndex == index }) else { return }
        chapters[i].title = title
    }

    func setChapters(_ chapters: [QueueChapter]) {
        self.chapters = chapters
    }

    // MARK: Transport

    func play() {
        guard hasAudio else { return }
        willPlay?()
        if atEnd && player.items().isEmpty && !generatingTTS {
            seek(to: 0)
        } else if player.items().isEmpty && !waitingForChunks {
            rebuild(from: currentIndex, offset: max(0, cumulativeTime - math.start(of: currentIndex)))
        }
        isPlaying = true
        if !waitingForChunks { startPlayer() }
        onStateChange?()
        onTransport?(true)
    }

    func pause() {
        isPlaying = false
        player.pause()
        onStateChange?()
        onTransport?(false)
    }

    func togglePlayPause() {
        isPlaying ? pause() : play()
    }

    /// Web `stop()`: release the media, position 0; the player stays visible and replayable (#161).
    func stop() {
        isPlaying = false
        player.pause()
        teardownItems()
        currentIndex = 0
        cumulativeTime = 0
        waitingForChunks = false
        onStateChange?()
    }

    /// Web `closePlayer()`: full teardown; the mini-player hides.
    func close() {
        stop()
        entries = []
        chapters = []
        title = ""
        source = nil
        isLoaded = false
        generatingTTS = false
        onFirstPlaying = nil
        onStateChange?()
    }

    func seek(to t: Double) {
        guard let spot = math.locate(t) else { return }
        if spot.atEnd && !generatingTTS {
            // Web: seeking to within 0.1 s of the end just shows the end.
            player.pause()
            teardownItems()
            currentIndex = spot.index
            cumulativeTime = totalDuration
            onStateChange?()
            return
        }
        currentIndex = spot.index
        cumulativeTime = math.cumulative(index: spot.index, offset: spot.offset)
        waitingForChunks = false
        rebuild(from: spot.index, offset: spot.offset)
        if isPlaying { startPlayer() }
        onStateChange?()
    }

    func skip(by seconds: Double) {
        seek(to: max(0, min(totalDuration, cumulativeTime + seconds)))
    }

    /// 1 → 1.25 → 1.5 → 2 → 1 (web GlobalAudioPlayer).
    func cycleRate() {
        let i = Self.rates.firstIndex(of: rate) ?? 0
        setRate(Self.rates[(i + 1) % Self.rates.count])
    }

    func setRate(_ newRate: Float) {
        rate = newRate
        player.defaultRate = newRate
        if player.rate > 0 { player.rate = newRate }
        onStateChange?()
    }

    var currentChapterIndex: Int? { math.chapterIndex(at: cumulativeTime, chapters: chapters) }

    // MARK: Internals

    private func startPlayer() {
        player.defaultRate = rate
        player.playImmediately(atRate: rate)
    }

    private func tick(_ time: CMTime) {
        guard let item = player.currentItem, let index = itemIndex[ObjectIdentifier(item)] else { return }
        currentIndex = index
        let offset = CMTimeGetSeconds(item.currentTime())
        if offset.isFinite {
            cumulativeTime = math.cumulative(index: index, offset: offset)
        }
        let d = CMTimeGetSeconds(item.duration)
        if d.isFinite, d > 0, index < entries.count,
           entries[index].duration == 0 || abs(entries[index].duration - d) > 1 {
            entries[index].duration = d
        }
    }

    /// An item failed to load or to play on: tell the user once, then go on as
    /// if it had ended (web `audio.onerror` toast; M14).
    private func itemFailed(_ id: ObjectIdentifier, item: AVPlayerItem, error: Error?) {
        guard let index = itemIndex[id], index < entries.count else { return }
        log.error("chunk \(index) could not be played")
        entries[index].failed = true
        entries[index].duration = 0
        if !failureReported {
            failureReported = true
            onPlaybackError?(Self.playbackErrorMessage(url: entries[index].url, error: error))
        }
        player.remove(item)
        itemEnded(id, item: item)
    }

    /// The web's `playbackErrorMessage`, for what AVFoundation reports.
    nonisolated static func playbackErrorMessage(url: URL?, error: Error?) -> String {
        if let url, ListenFormats.isWebM(url.absoluteString) {
            return ListenFormats.webMMessage
        }
        let ns = error as NSError?
        let underlying = ns?.userInfo[NSUnderlyingErrorKey] as? NSError
        if ns?.domain == NSURLErrorDomain || underlying?.domain == NSURLErrorDomain {
            return "Network error loading audio. Try again."
        }
        return "Audio playback failed."
    }

    private func itemEnded(_ id: ObjectIdentifier, item: AVPlayerItem) {
        guard let index = itemIndex[id] else { return }
        itemIndex[id] = nil
        itemObservations.removeValue(forKey: id)?.invalidate()
        let actual = CMTimeGetSeconds(item.currentTime())
        if actual.isFinite, actual > 0, index < entries.count, abs(entries[index].duration - actual) > 0.5 {
            entries[index].duration = actual
        }
        let next = index + 1
        if next < entries.count {
            currentIndex = next
            cumulativeTime = math.start(of: next)
            if player.items().isEmpty {
                rebuild(from: next, offset: 0)
                if isPlaying { startPlayer() }
            }
        } else if generatingTTS {
            currentIndex = index
            cumulativeTime = totalDuration
            waitingForChunks = true
            onDrained?()
        } else {
            currentIndex = index
            finish()
        }
        onStateChange?()
    }

    private func finish() {
        isPlaying = false
        cumulativeTime = totalDuration
        player.pause()
        onStateChange?()
        onFinished?()
    }

    private func rebuild(from index: Int, offset: Double) {
        teardownItems()
        guard index < entries.count else { return }
        for i in index..<entries.count {
            player.insert(makeItem(i), after: nil)
        }
        if offset > 0.05 {
            player.currentItem?.seek(to: CMTime(seconds: offset, preferredTimescale: 600),
                                     toleranceBefore: .zero, toleranceAfter: .zero, completionHandler: nil)
        }
    }

    private func teardownItems() {
        player.removeAllItems()
        itemIndex = [:]
        itemObservations.values.forEach { $0.invalidate() }
        itemObservations = [:]
    }

    private func makeItem(_ index: Int) -> AVPlayerItem {
        let asset = asset(for: entries[index].url)
        let item = AVPlayerItem(asset: asset)
        item.audioTimePitchAlgorithm = .timeDomain
        let id = ObjectIdentifier(item)
        itemIndex[id] = index
        itemObservations[id] = item.observe(\.status, options: [.new]) { [weak self] item, _ in
            guard item.status == .failed else { return }
            let error = item.error
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.itemFailed(id, item: item, error: error) }
            }
        }
        return item
    }

    private func asset(for url: URL) -> AVURLAsset {
        if let cached = assets[url] { return cached }
        var options: [String: Any] = [AVURLAssetHTTPCookiesKey: cookiesProvider()]
        if url.path.lowercased().hasSuffix(".mp4") {
            options[AVURLAssetOverrideMIMETypeKey] = "audio/mp4"
        }
        let asset = AVURLAsset(url: url, options: options)
        assets[url] = asset
        if assets.count > 64 { assets.removeAll() }
        return asset
    }

    /// Unknown durations are read from the file (web `preloadChunkDurations`,
    /// 300 s fallback if it cannot be read).
    private func probeDurationIfNeeded(_ index: Int) {
        guard index < entries.count, entries[index].duration <= 0, !entries[index].failed else { return }
        let url = entries[index].url
        let asset = asset(for: url)
        Task { [weak self] in
            let seconds = (try? await asset.load(.duration)).map(CMTimeGetSeconds) ?? 300
            guard let self else { return }
            if let i = self.entries.firstIndex(where: { $0.url == url }), self.entries[i].duration <= 0,
               !self.entries[i].failed {
                self.entries[i].duration = seconds.isFinite && seconds > 0 ? seconds : 300
                self.onStateChange?()
            }
        }
    }
}
