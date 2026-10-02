import Foundation
import UIKit
import os

/// Persisted upload queue for recorded chunks (design doc §9.2, map C §2.3, §8.1).
///
/// - Each chunk's multipart body is written to Application Support first
///   (`completeUntilFirstUserAuthentication`, so it works while the phone is
///   locked) and deleted once the server has it.
/// - One worker per session uploads in index order, so chunk 0 (the init
///   segment) always lands first. Nothing overtakes it: while recording it is
///   retried every 16 s at most and never given up; once it is given up (after
///   Stop), later chunks wait behind it (a batch cannot be remuxed without it).
/// - Foreground `URLSession` with the web's retry schedule (4 retries, 2/4/8/16 s).
///   A chunk that still fails, and every pending chunk when the app goes to the
///   background, is also handed to a background `URLSession` (survives
///   suspension and termination; the server dedupes by index, the web's
///   `sendBeacon` copy).
/// - `settle` (Stop) makes one more foreground attempt for every chunk the
///   server does not have. A chunk still missing stays queued on disk (the
///   next launch retries it) and is reported, so the caller does not finalize
///   without it.
/// - `init_parse_failed` on chunk 0 is fatal (the session is dead, its audio is
///   deleted at once); other 4xx answers are given up without retry.
/// - A relaunch resumes whatever is left (`resumePending()`); sign-out deletes
///   the whole queue (`reset()`).
@MainActor
final class ChunkUploader: NSObject {
    static let shared = ChunkUploader()
    static let backgroundIdentifier = "org.loore.app.voice-uploads"
    static let retryDelays: [Double] = [2, 4, 8, 16]

    enum ChunkStatus: String, Codable { case pending, stored, failed, fatal }

    struct ChunkRecord: Codable, Equatable {
        var index: Int
        var status: ChunkStatus
        var contentType: String
        var attempts = 0
        var bytes = 0
    }

    struct Manifest: Codable {
        var sessionId: String
        /// Absolute `…/audio-chunk` URL (the environment at record time).
        var uploadURL: URL
        var chunks: [Int: ChunkRecord] = [:]
        /// Set when the recorder stopped: no more chunks will come.
        var closed = false
        /// Chunks the session already had on the server (a resumed recording).
        var firstIndex = 0
    }

    /// What `settle` reports before finalize.
    struct Outcome: Equatable {
        var produced: Int
        var stored: Int
        /// Chunks the server does not have (given up, or held back behind a
        /// missing chunk 0). Non-empty = do not finalize.
        var failed: [Int]
        var fatalMessage: String?
        /// Chunks stored before this recording resumed the session (web `existingChunkCount`).
        var prior = 0

        /// `total_chunks` for finalize: the session's chunk count, counting only
        /// the stored ones when some were given up (otherwise the server waits
        /// 10 minutes for them, C §10.4).
        var totalForFinalize: Int { prior + (failed.isEmpty ? produced : stored) }
    }

    /// Sends one prepared upload; injectable for tests.
    var transport: (URLRequest, URL) async throws -> (Data, HTTPURLResponse) = { request, file in
        let (data, response) = try await ChunkUploader.foregroundSession.upload(for: request, fromFile: file)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, http)
    }
    /// Seconds to wait before retry `n` (tests shorten it).
    var delay: (Int) -> Double = { ChunkUploader.retryDelays[min($0, ChunkUploader.retryDelays.count - 1)] }
    var cookieHeader: (URL) -> [String: String] = { url in
        HTTPCookie.requestHeaderFields(with: HTTPCookieStorage.shared.cookies(for: url) ?? [])
    }
    /// Called once when a session hits a fatal error.
    var onFatal: ((String, String) -> Void)?
    /// Called when a background relaunch finished its events.
    var backgroundCompletionHandler: (() -> Void)?

    private var manifests: [String: Manifest] = [:]
    private var workers: [String: Task<Void, Never>] = [:]
    private let root: URL
    private let fileManager = FileManager.default
    private let log = Logger(subsystem: "org.loore.app", category: "uploads")
    private lazy var backgroundSession: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.backgroundIdentifier)
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        Self.dropCookieStorage(config)
        return URLSession(configuration: config, delegate: BackgroundDelegate(owner: self), delegateQueue: .main)
    }()
    /// Uploads carry the cookie header set by `cookieHeader` (the app's in-memory jar).
    /// Neither session may keep cookies of its own: `HTTPCookieStorage.shared` writes
    /// them, including a refreshed `session` cookie, to a file on disk (review M1).
    static let foregroundSession: URLSession = {
        let config = URLSessionConfiguration.default
        dropCookieStorage(config)
        config.urlCache = nil
        return URLSession(configuration: config)
    }()
    private static func dropCookieStorage(_ config: URLSessionConfiguration) {
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
    }
    private let useBackgroundSession: Bool

    init(root: URL? = nil, useBackgroundSession: Bool = true) {
        let base = root ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("VoiceUploads", isDirectory: true)
        self.root = base
        self.useBackgroundSession = useBackgroundSession
        super.init()
        try? fileManager.createDirectory(at: base, withIntermediateDirectories: true,
                                         attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        if useBackgroundSession {
            NotificationCenter.default.addObserver(forName: UIApplication.didEnterBackgroundNotification,
                                                   object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.handOffPendingToBackground() }
            }
        }
    }

    // MARK: Public API

    /// Starts tracking a session (before its first chunk).
    func open(sessionId: String, uploadURL: URL, firstIndex: Int = 0) {
        if manifests[sessionId] == nil {
            var manifest = Manifest(sessionId: sessionId, uploadURL: uploadURL)
            manifest.firstIndex = firstIndex
            manifests[sessionId] = manifest
            persist(sessionId)
        }
    }

    /// Writes the chunk to disk and schedules its upload.
    func enqueue(sessionId: String, chunk: RecordedChunk) {
        guard var manifest = manifests[sessionId] else {
            log.error("enqueue for an unknown session")
            return
        }
        // A dead session (fatal answer): its audio is not kept.
        if manifest.chunks.values.contains(where: { $0.status == .fatal }) { return }
        var form = MultipartFormData()
        form.addFile("chunk", filename: "chunk_\(chunk.index).mp4", mimeType: "audio/mp4", data: chunk.data)
        form.addField("chunk_index", String(chunk.index))
        form.addField("mime_type", "audio/mp4")
        let body = form.finalized()
        do {
            try directory(for: sessionId).createIfNeeded(fileManager)
            try body.write(to: bodyFile(sessionId, chunk.index),
                           options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch {
            log.error("could not write chunk \(chunk.index) to disk")
        }
        manifest.chunks[chunk.index] = ChunkRecord(index: chunk.index, status: .pending,
                                                   contentType: form.contentType, bytes: chunk.data.count)
        manifests[sessionId] = manifest
        persist(sessionId)
        startWorker(sessionId)
    }

    /// No more chunks for this session.
    func close(sessionId: String) {
        manifests[sessionId]?.closed = true
        persist(sessionId)
    }

    /// Waits until every chunk of a closed session is stored, given up, or fatal,
    /// then gives every missing chunk one more foreground attempt (the thinking
    /// cue keeps the app running here). Missing chunks stay queued on disk.
    func settle(sessionId: String) async -> Outcome {
        close(sessionId: sessionId)
        while let worker = workers[sessionId] {
            await worker.value
            if workers[sessionId] == worker { workers[sessionId] = nil }
        }
        await finalPass(sessionId)
        let result = outcome(sessionId)
        requeueMissing(sessionId)
        return result
    }

    func outcome(_ sessionId: String) -> Outcome {
        let chunks = manifests[sessionId]?.chunks.values.map { $0 } ?? []
        let fatal = chunks.contains { $0.status == .fatal }
        return Outcome(produced: chunks.count,
                       stored: chunks.filter { $0.status == .stored }.count,
                       failed: chunks.filter { $0.status != .stored }.map(\.index).sorted(),
                       fatalMessage: fatal ? fatalMessages[sessionId] : nil,
                       prior: manifests[sessionId]?.firstIndex ?? 0)
    }

    /// Forgets a session and deletes its files (after finalize, or on discard).
    func forget(sessionId: String) {
        workers[sessionId]?.cancel()
        workers[sessionId] = nil
        manifests[sessionId] = nil
        fatalMessages[sessionId] = nil
        degraded.remove(sessionId)
        try? fileManager.removeItem(at: directory(for: sessionId))
    }

    /// After a relaunch: upload whatever a killed app left behind.
    func resumePending() {
        guard let dirs = try? fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { return }
        for dir in dirs {
            let file = dir.appendingPathComponent("manifest.json")
            guard let data = try? Data(contentsOf: file),
                  let manifest = try? JSONDecoder().decode(Manifest.self, from: data) else {
                // Unreadable (or from an older build): nothing we can resume.
                try? fileManager.removeItem(at: dir)
                continue
            }
            if manifests[manifest.sessionId] != nil { continue }
            // A dead session (fatal answer) is never resumed (M2).
            if manifest.chunks.values.contains(where: { $0.status == .fatal }) {
                try? fileManager.removeItem(at: dir)
                continue
            }
            var resumed = manifest
            // Given-up chunks whose body is still here get another chance (B1).
            for (index, record) in manifest.chunks where record.status == .failed
                && fileManager.fileExists(atPath: bodyFile(manifest.sessionId, index).path) {
                resumed.chunks[index]?.status = .pending
            }
            let open = resumed.chunks.values.contains { $0.status == .pending }
            if !open {
                try? fileManager.removeItem(at: dir)
                continue
            }
            // No recorder adds to a session left by an earlier launch.
            resumed.closed = true
            manifests[manifest.sessionId] = resumed
            persist(manifest.sessionId)
            log.info("resuming uploads for a session left by a previous launch")
            startWorker(manifest.sessionId)
        }
    }

    /// Sign-out: stop every upload (foreground and background, which carry this
    /// user's cookie) and delete the queue, so nothing of this user's audio stays
    /// on disk or is sent with the next user's cookies (M2).
    func reset() {
        workers.values.forEach { $0.cancel() }
        workers = [:]
        manifests = [:]
        fatalMessages = [:]
        degraded = []
        if useBackgroundSession {
            backgroundSession.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
        }
        for dir in (try? fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? [] {
            try? fileManager.removeItem(at: dir)
        }
    }

    // MARK: Worker

    private var fatalMessages: [String: String] = [:]
    /// Sessions where a chunk already used up its retries (the network is
    /// down): later chunks get one attempt each, then go to the background
    /// session, so Stop does not wait 30 s per chunk.
    private var degraded: Set<String> = []

    private func startWorker(_ sessionId: String) {
        guard workers[sessionId] == nil else { return }
        workers[sessionId] = Task { [weak self] in
            guard let self else { return }
            await self.drain(sessionId)
            // No suspension between drain's last check and this line (main actor),
            // so a chunk enqueued meanwhile starts a new worker.
            self.workers[sessionId] = nil
        }
    }

    private func drain(_ sessionId: String) async {
        while !Task.isCancelled {
            guard let manifest = manifests[sessionId],
                  let next = manifest.chunks.values.filter({ $0.status == .pending }).min(by: { $0.index < $1.index })
            else { return }
            if manifest.chunks.values.contains(where: { $0.status == .fatal }) { return }
            // Nothing overtakes a given-up init segment (B1).
            if manifest.chunks[manifest.firstIndex]?.status == .failed { return }
            await upload(sessionId, index: next.index)
        }
    }

    /// The session's first chunk (the init segment) is not on the server yet.
    private func initMissing(_ manifest: Manifest) -> Bool {
        guard let record = manifest.chunks[manifest.firstIndex] else { return false }
        return record.status != .stored
    }

    private enum AttemptResult { case stored, retry, giveUp, fatal(String) }

    private func upload(_ sessionId: String, index: Int) async {
        var attempt = 0
        let isInit = manifests[sessionId]?.firstIndex == index
        while !Task.isCancelled {
            // A background copy may have landed while this one waited.
            if manifests[sessionId]?.chunks[index]?.status == .stored { return }
            let result = await attemptUpload(sessionId, index: index)
            switch result {
            case .stored:
                stored(sessionId, index)
                return
            case .fatal(let message):
                failFatally(sessionId, index, message)
                return
            case .giveUp:
                mark(sessionId, index, .failed)
                return
            case .retry:
                if isInit && manifests[sessionId]?.closed == false {
                    // Chunk 0 is never given up while recording: later chunks wait
                    // behind it (B1). One background copy after the regular retries.
                    if attempt == Self.retryDelays.count { startBackgroundUpload(sessionId, index: index) }
                } else if attempt >= Self.retryDelays.count || degraded.contains(sessionId) {
                    log.error("chunk \(index) failed after \(attempt + 1) attempts; handing to the background session")
                    degraded.insert(sessionId)
                    mark(sessionId, index, .failed)
                    startBackgroundUpload(sessionId, index: index)
                    return
                }
                let wait = delay(attempt)
                attempt += 1
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            }
        }
    }

    private func stored(_ sessionId: String, _ index: Int) {
        degraded.remove(sessionId)
        mark(sessionId, index, .stored)
        try? fileManager.removeItem(at: bodyFile(sessionId, index))
    }

    private func failFatally(_ sessionId: String, _ index: Int, _ message: String) {
        mark(sessionId, index, .fatal)
        fatalMessages[sessionId] = message
        // Nothing of a dead session can be sent: delete its audio now (M2).
        for chunk in manifests[sessionId]?.chunks.keys.map({ $0 }) ?? [] {
            try? fileManager.removeItem(at: bodyFile(sessionId, chunk))
        }
        onFatal?(sessionId, message)
    }

    /// Stop: one more attempt for every chunk the server does not have, in
    /// index order; if the init segment still fails, nothing after it is sent.
    private func finalPass(_ sessionId: String) async {
        guard let manifest = manifests[sessionId],
              !manifest.chunks.values.contains(where: { $0.status == .fatal }) else { return }
        let missing = manifest.chunks.values.filter { $0.status != .stored }.map(\.index).sorted()
        for index in missing {
            guard !Task.isCancelled, manifests[sessionId]?.chunks[index]?.status != .stored else { continue }
            let isInit = index == manifest.firstIndex
            guard fileManager.fileExists(atPath: bodyFile(sessionId, index).path) else {
                if isInit { return }
                continue
            }
            switch await attemptUpload(sessionId, index: index) {
            case .stored:
                stored(sessionId, index)
            case .fatal(let message):
                failFatally(sessionId, index, message)
                return
            case .giveUp, .retry:
                mark(sessionId, index, .failed)
                if isInit { return }
            }
        }
    }

    /// Chunks still missing after `settle` stay queued: the next launch
    /// (`resumePending`), a background copy, or the next chunk of a resumed
    /// recording uploads them.
    private func requeueMissing(_ sessionId: String) {
        guard let manifest = manifests[sessionId],
              !manifest.chunks.values.contains(where: { $0.status == .fatal }) else { return }
        for record in manifest.chunks.values where record.status == .failed
            && fileManager.fileExists(atPath: bodyFile(sessionId, record.index).path) {
            manifests[sessionId]?.chunks[record.index]?.status = .pending
        }
        persist(sessionId)
    }

    private func attemptUpload(_ sessionId: String, index: Int) async -> AttemptResult {
        guard let manifest = manifests[sessionId], let record = manifest.chunks[index] else { return .giveUp }
        let file = bodyFile(sessionId, index)
        guard fileManager.fileExists(atPath: file.path) else { return .giveUp }
        manifests[sessionId]?.chunks[index]?.attempts += 1
        let request = makeRequest(manifest.uploadURL, contentType: record.contentType)
        do {
            let (data, http) = try await transport(request, file)
            return Self.classify(status: http.statusCode, body: data, index: index)
        } catch {
            log.info("chunk \(index) transport error")
            return .retry
        }
    }

    /// Maps an upload answer (map C §2.3).
    private static func classify(status: Int, body: Data, index: Int) -> AttemptResult {
        switch status {
        case 200..<300:
            return .stored
        case 400:
            let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            if json?["code"] as? String == "init_parse_failed" {
                let detail = json?["detail"] as? String ?? "unknown parse error"
                return .fatal("Your audio recording could not be processed. Please try recording again. (\(detail))")
            }
            return .giveUp
        case 401, 403, 404, 413, 422:
            return .giveUp
        default:
            return .retry
        }
    }

    private func makeRequest(_ url: URL, contentType: String) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 120
        request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(TimeZone.current.identifier, forHTTPHeaderField: "X-Timezone")
        for (name, value) in cookieHeader(url) { request.setValue(value, forHTTPHeaderField: name) }
        return request
    }

    private func mark(_ sessionId: String, _ index: Int, _ status: ChunkStatus) {
        manifests[sessionId]?.chunks[index]?.status = status
        persist(sessionId)
    }

    // MARK: Background session

    /// Recreates the background session after a relaunch so its events arrive.
    func reconnectBackgroundSession() {
        guard useBackgroundSession else { return }
        _ = backgroundSession
    }

    /// The app is going to the background: every chunk not yet stored gets a
    /// background copy that finishes even if the app is suspended.
    func handOffPendingToBackground() {
        for (sessionId, manifest) in manifests {
            let holdBack = initMissing(manifest)
            for record in manifest.chunks.values where record.status == .pending {
                // Nothing overtakes the init segment (B1).
                if holdBack && record.index != manifest.firstIndex { continue }
                startBackgroundUpload(sessionId, index: record.index)
            }
        }
    }

    private func startBackgroundUpload(_ sessionId: String, index: Int) {
        guard useBackgroundSession, let manifest = manifests[sessionId], let record = manifest.chunks[index] else { return }
        let file = bodyFile(sessionId, index)
        guard fileManager.fileExists(atPath: file.path) else { return }
        let task = backgroundSession.uploadTask(with: makeRequest(manifest.uploadURL, contentType: record.contentType),
                                                fromFile: file)
        task.taskDescription = "\(sessionId)|\(index)"
        task.resume()
    }

    fileprivate func backgroundTaskFinished(description: String?, status: Int?) {
        guard let parts = description?.split(separator: "|"), parts.count == 2,
              let index = Int(parts[1]) else { return }
        let sessionId = String(parts[0])
        guard let status, (200..<300).contains(status) else { return }
        if manifests[sessionId] == nil, let restored = loadManifest(sessionId) {
            manifests[sessionId] = restored
        }
        guard manifests[sessionId]?.chunks[index] != nil else { return }
        mark(sessionId, index, .stored)
        try? fileManager.removeItem(at: bodyFile(sessionId, index))
        // Chunks held back behind this one (the init segment) can go now.
        if manifests[sessionId]?.chunks.values.contains(where: { $0.status == .pending }) == true {
            startWorker(sessionId)
        }
    }

    // MARK: Files

    private func directory(for sessionId: String) -> URL {
        root.appendingPathComponent(sessionId, isDirectory: true)
    }

    private func bodyFile(_ sessionId: String, _ index: Int) -> URL {
        directory(for: sessionId).appendingPathComponent("chunk_\(index).body")
    }

    private func persist(_ sessionId: String) {
        guard let manifest = manifests[sessionId] else { return }
        do {
            try directory(for: sessionId).createIfNeeded(fileManager)
            let data = try JSONEncoder().encode(manifest)
            try data.write(to: directory(for: sessionId).appendingPathComponent("manifest.json"),
                           options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        } catch {
            log.error("could not persist the upload manifest")
        }
    }

    private func loadManifest(_ sessionId: String) -> Manifest? {
        let file = directory(for: sessionId).appendingPathComponent("manifest.json")
        return (try? Data(contentsOf: file)).flatMap { try? JSONDecoder().decode(Manifest.self, from: $0) }
    }
}

/// Background `URLSession` delegate: marks chunks stored and calls the system's
/// completion handler after a background relaunch.
private final class BackgroundDelegate: NSObject, URLSessionTaskDelegate {
    weak var owner: ChunkUploader?

    init(owner: ChunkUploader) {
        self.owner = owner
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let status = (task.response as? HTTPURLResponse)?.statusCode
        let description = task.taskDescription
        MainActor.assumeIsolated {
            owner?.backgroundTaskFinished(description: description, status: error == nil ? status : nil)
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        MainActor.assumeIsolated {
            owner?.backgroundCompletionHandler?()
            owner?.backgroundCompletionHandler = nil
        }
    }
}

private extension URL {
    func createIfNeeded(_ fileManager: FileManager) throws {
        if !fileManager.fileExists(atPath: path) {
            try fileManager.createDirectory(at: self, withIntermediateDirectories: true,
                                            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        }
    }
}
