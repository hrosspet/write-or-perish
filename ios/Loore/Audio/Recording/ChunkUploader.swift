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
///   deleted at once); so is `session_taken_over` (#388: another device
///   continued the session, which no longer takes this phone's chunks), and
///   `session_not_found` for a session recorded under the signed-in account
///   (it was discarded or ended elsewhere; `sessionNotFound`). Other 4xx
///   answers are given up without retry.
/// - A chunk past what one recording may hold (`chunk_index_out_of_range`,
///   `recording_too_large`, `chunk_too_large`; backend/utils/recording_limits)
///   ends the session's uploads there: it and every later chunk are `beyond`,
///   never sent again (not at Stop, not after a relaunch), and their files are
///   deleted. The chunks before stay, so the caller finalizes with them
///   (`onLimit`, `Outcome.totalForFinalize`). A chunk that came after the
///   finalize (`chunk_not_announced`) is not the bound: it alone is `beyond`,
///   and nothing is said.
/// - A relaunch resumes whatever is left (`resumePending()`).
/// - Each session belongs to the account it was recorded under, and uploads
///   only while that account is signed in (#763). A sign-out the person did
///   not ask for (the server refused the sign-in) stops the uploads and keeps
///   the queue (`hold()`); it goes on when that account signs in again. The
///   person's own Logout deletes their account's audio (`reset(account:)`).
/// - A deleted account's audio stays while the account can still be
///   restored and uploads if it is; it leaves the phone when the 30 days end
///   (`accountDeleted`, `removeEnded`; #777).
/// - A continued recording is numbered past every piece of it this phone
///   still holds, which go first; no queued file is written over (#754).
@MainActor
final class ChunkUploader: NSObject {
    static let shared = ChunkUploader()
    static let backgroundIdentifier = "org.loore.app.voice-uploads"
    static let retryDelays: [Double] = [2, 4, 8, 16]
    /// The server's hide window for a recording session (`LIVE_SESSION_WINDOW_SEC`
    /// in backend/utils/streaming_session.py): for this long after its last
    /// chunk no other client is offered the session.
    static let serverLiveWindow: TimeInterval = 45

    /// `beyond`: refused for good (past what one recording may hold, or after
    /// the recording's finalize); never sent again.
    enum ChunkStatus: String, Codable { case pending, stored, failed, fatal, beyond }

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
        /// The first chunk of this phone's latest recording into the session
        /// (its init segment). Chunks before it were on the server already (a
        /// resumed recording), or are still in this manifest (`prior`).
        var firstIndex = 0
        /// When this phone began its last upload the server stored (nil in
        /// manifests from earlier builds). No later than the server's own stamp
        /// for that chunk, so an age measured from it is never less than the
        /// server's (`mayRelease`).
        var lastUploadAt: Date?
        /// The first chunk refused as past what one recording may hold (nil
        /// until then, and in manifests from earlier builds): no chunk from it
        /// on is sent.
        var limitFrom: Int?
        /// The account the session was recorded under (#763): it uploads only
        /// while that account is signed in. nil in manifests from earlier
        /// builds: the first account that signs in takes them, as those
        /// builds did.
        var account: Int?
        /// The session's next index on the server when this phone last opened
        /// it (the claim's `next_chunk_index`, 0 for a new session): the
        /// server held every chunk below it. nil in manifests from earlier
        /// builds, where it was `firstIndex`.
        var serverHeld: Int?
        /// Taken by the first account that signed in from an earlier build,
        /// which recorded no account: whose recording it is, is not known, so
        /// a "not found" answer deletes nothing of it (`sessionNotFound`).
        var adopted: Bool?
        /// When the account it was recorded under can no longer be restored
        /// (#777: its deletion's date; a past moment once the deletion has
        /// started): from then on the session leaves the phone
        /// (`removeEnded`). Cleared when that account signs in again (a
        /// restore). nil while the account is not known to be deleted, and in
        /// manifests from earlier builds.
        var accountDeletedOn: Date?

        var isDead: Bool { chunks.values.contains { $0.status == .fatal } }

        /// The session's chunks on the server that this manifest has no record
        /// of: every index below `serverHeld` it does not hold. Counted when
        /// asked, so a chunk that reaches the queue after a continuation's
        /// `open` (a left recording's last piece, #753) is counted once,
        /// whichever came first.
        var priorCount: Int {
            let held = serverHeld ?? firstIndex
            return held - chunks.keys.filter { $0 < held }.count
        }
    }

    /// What `settle` reports before finalize.
    struct Outcome: Equatable {
        var produced: Int
        var stored: Int
        /// Chunks the server does not have (given up, or held back behind a
        /// missing chunk 0). Non-empty = do not finalize. Chunks refused past
        /// the recording's limit are not among them (`beyond`).
        var failed: [Int]
        var fatalMessage: String?
        /// Chunks stored before this recording resumed the session (web `existingChunkCount`).
        var prior = 0
        /// Chunks refused as past what one recording may hold: never sent, and
        /// not counted at finalize.
        var beyond = 0

        /// `total_chunks` for finalize: the session's chunk count, counting only
        /// the stored ones when some were given up (otherwise the server waits
        /// 10 minutes for them, C §10.4) or refused past the recording's limit
        /// (the server takes no count above it).
        var totalForFinalize: Int { prior + (failed.isEmpty ? produced - beyond : stored) }
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
    /// The clock (tests move it).
    var now: () -> Date = Date.init
    /// Called once when a session hits a fatal error.
    var onFatal: ((String, String) -> Void)?
    /// Called once per session when the server refuses its audio for the
    /// usage limit or the account's open recordings (402, 429; a recording
    /// that held no audio for too long, backend/utils/recording_limits),
    /// with the server's words. The audio stays on the phone and is sent
    /// again later (a relaunch, the next chunk of a continued recording).
    var onRefused: ((String, String) -> Void)?
    private var refusalsSaid: Set<String> = []
    /// Called once per session when the server refuses a chunk as past what one
    /// recording may hold, with its words (session id, message). The chunks
    /// before it are on the server; the caller stops and finalizes.
    var onLimit: ((String, String) -> Void)?
    private var limitsSaid: Set<String> = []
    /// Called when a background relaunch finished its events.
    var backgroundCompletionHandler: (() -> Void)?
    /// The account signed in now (#763): only sessions recorded under it
    /// upload. nil while nobody is (and in tests, whose sessions have none).
    private(set) var account: Int?
    /// The app signed out without the person asking (`hold`): nothing
    /// uploads until an account signs in (`resumePending`).
    private(set) var isHeld = false
    /// Sessions in memory at `hold`: the next sign-in reads them again from
    /// disk, as a relaunch does.
    private var heldSessions: Set<String> = []

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

    /// Where the shared uploader keeps audio until it is sent.
    static var defaultRoot: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("VoiceUploads", isDirectory: true)
    }

    init(root: URL? = nil, useBackgroundSession: Bool = true) {
        let base = root ?? Self.defaultRoot
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

    /// Starts tracking a session (before its first chunk), for the account
    /// signed in now.
    /// - Returns: the index the recording's first chunk takes: `firstIndex`
    ///   (the server's next index), or one past the highest chunk this phone
    ///   still holds of the session, whichever is higher (#754). Those chunks
    ///   (a Stop that could not send them) stay and are sent first; the new
    ///   ones never take their numbers or files.
    @discardableResult
    func open(sessionId: String, uploadURL: URL, firstIndex: Int = 0) -> Int {
        // On disk only: read it, so its queued chunks are neither lost nor
        // written over.
        if manifests[sessionId] == nil, let saved = loadManifest(sessionId) {
            manifests[sessionId] = saved
        }
        heldSessions.remove(sessionId)
        // A session that died here (a fatal answer) and is recorded into again:
        // this phone claimed it back from the device that continued it (#388).
        // Its audio was deleted at the fatal answer, and its fatal state would
        // drop every new chunk, so it starts over.
        if manifests[sessionId]?.isDead == true {
            forget(sessionId: sessionId)
        }
        guard var manifest = manifests[sessionId] else {
            var manifest = Manifest(sessionId: sessionId, uploadURL: uploadURL)
            manifest.firstIndex = firstIndex
            manifest.serverHeld = firstIndex
            manifest.account = account
            manifests[sessionId] = manifest
            persist(sessionId)
            return firstIndex
        }
        // Kept, also while it holds no chunk yet: a chunk of the recording
        // before may still reach the queue (a left recording's last piece,
        // #753), and `priorCount` counts it once whenever it comes.
        let first = max(firstIndex, (manifest.chunks.keys.max() ?? -1) + 1)
        manifest.serverHeld = max(manifest.serverHeld ?? manifest.firstIndex, firstIndex)
        manifest.firstIndex = first
        manifest.uploadURL = uploadURL
        // The server let this account claim or start it: its recording, and
        // the account is not deleted.
        manifest.account = account
        manifest.adopted = nil
        manifest.accountDeletedOn = nil
        manifest.closed = false
        // Given-up chunks whose body is still here get another chance, as at a relaunch.
        for (index, record) in manifest.chunks where record.status == .failed
            && fileManager.fileExists(atPath: bodyFile(sessionId, index).path) {
            manifest.chunks[index]?.status = .pending
        }
        manifests[sessionId] = manifest
        persist(sessionId)
        if !manifest.chunks.isEmpty {
            log.info("continuing a session with chunks still on this phone; new chunks from \(first)")
        }
        startWorker(sessionId)
        return first
    }

    /// Writes the chunk to disk and schedules its upload.
    func enqueue(sessionId: String, chunk: RecordedChunk) {
        guard var manifest = manifests[sessionId] else {
            log.error("enqueue for an unknown session")
            return
        }
        // A dead session (fatal answer): its audio is not kept.
        if manifest.isDead { return }
        // A number this phone already used in the session is never written
        // again (#754): its audio is queued here, or the server has it.
        if manifest.chunks[chunk.index] != nil {
            log.fault("chunk \(chunk.index) is already queued for this session; not written over")
            return
        }
        // Past the recording's limit: the server takes no more, so nothing is
        // written or sent (dictation keeps its own copy for "Save audio").
        if let limit = manifest.limitFrom, chunk.index >= limit {
            manifest.chunks[chunk.index] = ChunkRecord(index: chunk.index, status: .beyond,
                                                       contentType: "", bytes: chunk.data.count)
            manifests[sessionId] = manifest
            persist(sessionId)
            return
        }
        var form = MultipartFormData()
        form.addFile("chunk", filename: "chunk_\(chunk.index).mp4", mimeType: "audio/mp4", data: chunk.data)
        form.addField("chunk_index", String(chunk.index))
        form.addField("mime_type", "audio/mp4")
        form.addField("device_key", RecordingDevice.key)
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
                       failed: chunks.filter { $0.status != .stored && $0.status != .beyond }
                           .map(\.index).sorted(),
                       fatalMessage: fatal ? fatalMessages[sessionId] : nil,
                       prior: manifests[sessionId]?.priorCount ?? 0,
                       beyond: chunks.filter { $0.status == .beyond }.count)
    }

    /// Forgets a session and deletes its files (after finalize, or on discard).
    func forget(sessionId: String) {
        workers[sessionId]?.cancel()
        workers[sessionId] = nil
        dropFromMemory(sessionId)
        try? fileManager.removeItem(at: directory(for: sessionId))
    }

    /// Forgets a session's state in memory; its files stay.
    private func dropFromMemory(_ sessionId: String) {
        manifests[sessionId] = nil
        fatalMessages[sessionId] = nil
        degraded.remove(sessionId)
        limitsSaid.remove(sessionId)
        heldSessions.remove(sessionId)
    }

    /// An account signed in (at launch, or again after a sign-out): upload
    /// whatever a killed app, or a sign-out the person did not ask for (`hold`),
    /// left of this account's recordings. Sessions recorded under another
    /// account stay on the phone, unsent, until that account signs in (#763).
    /// - Parameters:
    ///   - account: the account signed in (nil only in tests).
    ///   - releaseMargin: the release request's own time in flight
    ///     (see `mayRelease`).
    /// - Returns: sessions to release on the server: a recorder was still writing
    ///   them when the app ended, nothing is left to upload, and the server still
    ///   hides them from every other client (`mayRelease`). Released, they are
    ///   offered for recovery at once instead of after `serverLiveWindow` (#423
    ///   walk test). A session with chunks still to upload is not returned:
    ///   recovered before they land, its continuation would reuse their indexes.
    @discardableResult
    func resumePending(account: Int? = nil, releaseMargin: TimeInterval = 0) -> [String] {
        self.account = account
        isHeld = false
        // Held at a sign-out: read again from disk below, as after a relaunch.
        heldSessions.forEach(dropFromMemory)
        // Deleted accounts whose 30 days have ended meanwhile (#777).
        removeEnded()
        // The account signed in, so it is not deleted (a restore): its audio
        // stays until it is sent, as after any sign-in.
        for (sessionId, manifest) in manifests where manifest.account == account && manifest.accountDeletedOn != nil {
            manifests[sessionId]?.accountDeletedOn = nil
            persist(sessionId)
        }
        guard let dirs = try? fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { return [] }
        var abandoned: [String] = []
        for dir in dirs {
            let file = dir.appendingPathComponent("manifest.json")
            guard let data = try? Data(contentsOf: file),
                  let manifest = try? JSONDecoder().decode(Manifest.self, from: data) else {
                // Unreadable (or from an older build): nothing we can resume.
                try? fileManager.removeItem(at: dir)
                continue
            }
            if manifests[manifest.sessionId] != nil { continue }
            // A dead session (fatal answer) is never resumed (M2). It holds no audio.
            if manifest.isDead {
                try? fileManager.removeItem(at: dir)
                continue
            }
            // Another account's recording: it stays for that account, and is
            // never sent with this one's sign-in (#763).
            if let owner = manifest.account, owner != account { continue }
            var resumed = manifest
            // From an earlier build: this account takes it, as that build did.
            if manifest.account == nil && account != nil { resumed.adopted = true }
            resumed.account = account
            resumed.accountDeletedOn = nil
            // Given-up chunks whose body is still here get another chance (B1).
            for (index, record) in manifest.chunks where record.status == .failed
                && fileManager.fileExists(atPath: bodyFile(manifest.sessionId, index).path) {
                resumed.chunks[index]?.status = .pending
            }
            let open = resumed.chunks.values.contains { $0.status == .pending }
            if !open {
                if !manifest.closed && Self.mayRelease(lastUploadAt: manifest.lastUploadAt, now: now(),
                                                       margin: releaseMargin) {
                    abandoned.append(manifest.sessionId)
                }
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
        return abandoned
    }

    /// Whether a relaunch may release a session it was recording: only while the
    /// server still hides it from every other client, so no other device can
    /// have continued it. Released later, a session the web has continued since
    /// would lose its liveness: the phone would offer Discard (which deletes the
    /// web's audio) and Continue (whose chunk numbers clash with the web's), and
    /// a status poll could complete it under the web (review of #424). After the
    /// window the server offers the session anyway, so there is nothing to
    /// release. No record (an older build, or no chunk stored): no release.
    /// - Parameter margin: subtracted from the window for the release request's
    ///   time in flight.
    static func mayRelease(lastUploadAt: Date?, now: Date, margin: TimeInterval) -> Bool {
        guard let lastUploadAt else { return false }
        let age = now.timeIntervalSince(lastUploadAt)
        return age >= 0 && age < serverLiveWindow - margin
    }

    /// The app signed out without the person asking: the server no longer
    /// takes this sign-in (a 401), or the person left the restore question
    /// (#763). Every upload stops (each carries that sign-in's cookie), and
    /// the queue stays on the phone with the account each session was
    /// recorded under. A chunk the recorder still hands over is kept the same
    /// way. When that account signs in again, its sessions upload
    /// (`resumePending`); under another account they are never sent. When
    /// the account is deleted, `accountDeleted` follows and sets when its
    /// sessions leave the phone (#777).
    func hold() {
        isHeld = true
        account = nil
        stopEveryUpload()
        heldSessions.formUnion(manifests.keys)
    }

    /// The person signed out (Logout, M2): stop every upload (foreground and
    /// background, which carry this sign-in's cookie) and delete the audio
    /// recorded under `account`, so none of it stays on disk
    /// (`holdsUnsentAudio` lets the screen say so first, #763). Deleting the
    /// account does not come here: its audio is held (`accountDeleted`). Audio
    /// another account recorded on this phone stays for that account; it is
    /// never sent with another account's cookies.
    /// - Parameter owner: the account signing out; nil (it could not be
    ///   loaded) deletes every session's audio.
    func reset(account owner: Int?) {
        stopEveryUpload()
        manifests = [:]
        fatalMessages = [:]
        refusalsSaid = []
        limitsSaid = []
        degraded = []
        heldSessions = []
        account = nil
        isHeld = false
        for dir in (try? fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? [] {
            if let owner, let recordedBy = savedManifest(in: dir)?.account, recordedBy != owner { continue }
            try? fileManager.removeItem(at: dir)
        }
    }

    /// Whether this phone holds audio recorded under `owner` (under any
    /// account when nil) that has not reached the server: what `reset`
    /// deletes. A chunk's file is deleted once the server has it.
    func holdsUnsentAudio(account owner: Int?) -> Bool {
        for dir in (try? fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? [] {
            let manifest = manifests[dir.lastPathComponent] ?? savedManifest(in: dir)
            if let owner, let recordedBy = manifest?.account, recordedBy != owner { continue }
            let files = (try? fileManager.contentsOfDirectory(atPath: dir.path)) ?? []
            if files.contains(where: { $0.hasSuffix(".body") }) { return true }
        }
        return false
    }

    /// The phone learned that `deleted.account` is deleted (#777): the
    /// person deleted it here, the server refused its sign-in and said so,
    /// or a sign-in reached its restore question. Its sessions stay on the
    /// phone while the account can still be restored: a restore and
    /// sign-in sends them (`resumePending`). They leave the phone when that
    /// ends (`DeletedAccount.audioRemovedAt`: on the deletion's date, or at
    /// once when it has started or the account is gone; `removeEnded`).
    /// With no date known they are kept, as after any sign-out. An answer
    /// about the account signed in now changes nothing: it arrived after a
    /// sign-in, which shows the account is back.
    func accountDeleted(_ deleted: DeletedAccount) {
        guard let removeAt = deleted.audioRemovedAt, deleted.account != account else { return }
        for dir in sessionDirectories() {
            let sessionId = dir.lastPathComponent
            if manifests[sessionId]?.account == deleted.account {
                manifests[sessionId]?.accountDeletedOn = removeAt
                persist(sessionId)
            } else if manifests[sessionId] == nil, var saved = savedManifest(in: dir), saved.account == deleted.account {
                saved.accountDeletedOn = removeAt
                write(saved)
            }
        }
        removeEnded()
    }

    /// Removes the sessions of deleted accounts whose date has passed
    /// (#777): the account can no longer be restored, so their audio can
    /// never be sent. Runs at launch, when the app comes back to the
    /// foreground, at every sign-in and when the phone learns of a
    /// deletion. Never the signed-in account's.
    func removeEnded() {
        let current = now()
        for dir in sessionDirectories() {
            let sessionId = dir.lastPathComponent
            guard let manifest = manifests[sessionId] ?? savedManifest(in: dir),
                  let end = manifest.accountDeletedOn, end <= current,
                  manifest.account != account else { continue }
            forget(sessionId: sessionId)
            log.info("removed a deleted account's recording that had not reached the server")
        }
    }

    private func sessionDirectories() -> [URL] {
        (try? fileManager.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
    }

    private func stopEveryUpload() {
        workers.values.forEach { $0.cancel() }
        workers = [:]
        if useBackgroundSession {
            backgroundSession.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
        }
    }

    /// Only the signed-in account's sessions upload, and nothing while held (#763).
    private func mayUpload(_ sessionId: String) -> Bool {
        guard !isHeld, let manifest = manifests[sessionId] else { return false }
        return manifest.account == account
    }

    // MARK: Worker

    private var fatalMessages: [String: String] = [:]
    /// Sessions where a chunk already used up its retries (the network is
    /// down): later chunks get one attempt each, then go to the background
    /// session, so Stop does not wait 30 s per chunk.
    private var degraded: Set<String> = []

    private func startWorker(_ sessionId: String) {
        guard workers[sessionId] == nil, mayUpload(sessionId) else { return }
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

    private enum AttemptResult {
        case stored, retry, giveUp, fatal(String), refused(String), limit(String), dropped
        /// 404 `session_not_found`: the signed-in account has no such recording.
        case notFound
    }

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
            case .refused(let message):
                mark(sessionId, index, .failed)
                sayRefusal(sessionId, message)
                return
            case .limit(let message):
                reachedLimit(sessionId, from: index, message)
                return
            case .dropped:
                drop(sessionId, index)
                return
            case .notFound:
                sessionNotFound(sessionId, index)
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

    private func sayRefusal(_ sessionId: String, _ message: String) {
        guard !refusalsSaid.contains(sessionId) else { return }
        refusalsSaid.insert(sessionId)
        onRefused?(sessionId, message)
    }

    /// The server refused chunk `index` as past what one recording may hold: it
    /// and every later chunk not stored are `beyond` (their files deleted, never
    /// sent again), and the caller hears it once.
    private func reachedLimit(_ sessionId: String, from index: Int, _ message: String) {
        guard let manifest = manifests[sessionId] else { return }
        for record in manifest.chunks.values where record.index >= index && record.status != .stored {
            manifests[sessionId]?.chunks[record.index]?.status = .beyond
            try? fileManager.removeItem(at: bodyFile(sessionId, record.index))
        }
        manifests[sessionId]?.limitFrom = min(manifest.limitFrom ?? index, index)
        persist(sessionId)
        log.info("chunk \(index) is past the recording's limit; no later chunk is sent")
        guard !limitsSaid.contains(sessionId) else { return }
        limitsSaid.insert(sessionId)
        onLimit?(sessionId, message)
    }

    /// The recording was finished without this chunk (`chunk_not_announced`):
    /// not sent again, and nothing is said.
    private func drop(_ sessionId: String, _ index: Int) {
        mark(sessionId, index, .beyond)
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

    /// The server answered that the signed-in account has no such recording
    /// (404 `session_not_found`): it was discarded or ended elsewhere (the
    /// web's #320), or cleaned up. Nothing of it can ever be stored, so the
    /// session is dead, its pieces are deleted from the phone and no longer
    /// count as unsent audio, and a recorder still writing it stops (as at a
    /// takeover). Only for a session recorded under the account signed in
    /// now, while it still is: under another account the request is never
    /// sent (`mayUpload`), and a queue an earlier build left (`adopted`) may
    /// be another account's, so its pieces stay.
    /// - Returns: whether the session is dead now.
    @discardableResult
    private func sessionNotFound(_ sessionId: String, _ index: Int) -> Bool {
        guard mayUpload(sessionId), manifests[sessionId]?.adopted != true else {
            mark(sessionId, index, .failed)
            return false
        }
        log.info("the server has no such recording; its pieces on this phone are deleted")
        failFatally(sessionId, index, Self.sessionNotFoundMessage)
        return true
    }

    /// For a recorder still writing a session the server no longer has (web
    /// `SESSION_ENDED_MESSAGE`).
    static let sessionNotFoundMessage = "This recording was ended in another tab or on another device, so recording stopped here. Any audio this phone had not sent yet is not part of it."

    /// Stop: one more attempt for every chunk the server does not have, in
    /// index order; if the init segment still fails, nothing after it is sent.
    private func finalPass(_ sessionId: String) async {
        guard let manifest = manifests[sessionId],
              !manifest.chunks.values.contains(where: { $0.status == .fatal }) else { return }
        let missing = manifest.chunks.values.filter { $0.status != .stored && $0.status != .beyond }
            .map(\.index).sorted()
        for index in missing {
            let status = manifests[sessionId]?.chunks[index]?.status
            guard !Task.isCancelled, status != .stored, status != .beyond else { continue }
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
            case .limit(let message):
                reachedLimit(sessionId, from: index, message)
            case .dropped:
                drop(sessionId, index)
            case .notFound:
                if sessionNotFound(sessionId, index) || isInit { return }
            case .giveUp, .retry, .refused:
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
        guard let manifest = manifests[sessionId], let record = manifest.chunks[index],
              mayUpload(sessionId) else { return .giveUp }
        let file = bodyFile(sessionId, index)
        guard fileManager.fileExists(atPath: file.path) else { return .giveUp }
        manifests[sessionId]?.chunks[index]?.attempts += 1
        let request = makeRequest(manifest.uploadURL, contentType: record.contentType)
        let began = now()
        do {
            let (data, http) = try await transport(request, file)
            let result = Self.classify(status: http.statusCode, body: data, index: index)
            // Persisted by the caller's `stored`.
            if case .stored = result { manifests[sessionId]?.lastUploadAt = began }
            return result
        } catch {
            log.info("chunk \(index) transport error")
            return .retry
        }
    }

    /// The codes for a chunk past what one recording may hold
    /// (backend/routes/drafts.py; web `utils/recordingLimits`): about four
    /// hours, its bytes, or one chunk's size. Keyed on the code, never the words.
    static let limitCodes: Set<String> = ["chunk_index_out_of_range", "recording_too_large", "chunk_too_large"]
    /// A chunk past the count a finalizing recording announced: the recording
    /// was finished without it. Not the bound: dropped, nothing said.
    static let notAnnouncedCode = "chunk_not_announced"
    /// The signed-in account has no such recording (backend `upload_streaming_chunk`).
    static let sessionNotFoundCode = "session_not_found"
    /// The server's words at the four-hour bound (backend `LONGEST_RECORDING_TEXT`),
    /// for an answer that carries none (web `LONGEST_RECORDING_MESSAGE`).
    static let longestRecordingMessage = "This recording has reached the longest Loore takes, about four hours, and has stopped. Everything before this is saved."
    /// For the other bounds' answers without words (web `RECORDING_LIMIT_MESSAGE`).
    static let limitMessage = "This recording is larger than Loore accepts. Everything before this part is saved."

    /// Maps an upload answer (map C §2.3).
    private static func classify(status: Int, body: Data, index: Int) -> AttemptResult {
        if status == 400 || status == 413 {
            // Past what one recording may hold. Every chunk before it is on the
            // server; at the four hours and the bytes no later chunk is taken
            // either, and at one chunk's size the recording stops all the same.
            let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            let code = json?["code"] as? String
            if let code, limitCodes.contains(code) {
                let words = (json?["error"] as? String).flatMap { $0.contains(" ") ? $0 : nil }
                return .limit(words ?? (code == "chunk_index_out_of_range" ? longestRecordingMessage : limitMessage))
            }
            if code == notAnnouncedCode { return .dropped }
        }
        if status == 404 {
            // Keyed on the code: a 404 without it (a proxy, a route moved) is
            // not the recording being gone.
            let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            if json?["code"] as? String == sessionNotFoundCode { return .notFound }
        }
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
        case 409:
            // #388: another device continued the session; retrying cannot help.
            let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            if json?["code"] as? String == RecordingDevice.takenOverCode {
                return .fatal(RecordingDevice.takenOverMessage)
            }
            return .retry
        case 401, 403, 404, 413, 422:
            return .giveUp
        case 402, 429:
            // A recording that held no audio for too long, refused as a new
            // start would be: the usage limit, or the account's open
            // recordings (backend/utils/recording_limits). Retrying now
            // cannot help; the chunk stays on the phone.
            let json = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            let words = (json?["message"] as? String) ?? (json?["error"] as? String)
            return .refused(words.flatMap { $0.contains(" ") ? $0 : nil }
                            ?? "Loore can't take this recording right now. It is kept on this phone.")
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
        for (sessionId, manifest) in manifests where mayUpload(sessionId) {
            let holdBack = initMissing(manifest)
            for record in manifest.chunks.values where record.status == .pending {
                // Nothing overtakes the init segment (B1).
                if holdBack && record.index != manifest.firstIndex { continue }
                startBackgroundUpload(sessionId, index: record.index)
            }
        }
    }

    private func startBackgroundUpload(_ sessionId: String, index: Int) {
        guard useBackgroundSession, mayUpload(sessionId), let manifest = manifests[sessionId],
              let record = manifest.chunks[index] else { return }
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
        let restored = manifests[sessionId] == nil
        if restored, let saved = loadManifest(sessionId) {
            manifests[sessionId] = saved
        }
        guard manifests[sessionId]?.chunks[index] != nil else { return }
        mark(sessionId, index, .stored)
        try? fileManager.removeItem(at: bodyFile(sessionId, index))
        // Chunks held back behind this one (the init segment) can go now.
        if manifests[sessionId]?.chunks.values.contains(where: { $0.status == .pending }) == true {
            startWorker(sessionId)
        }
        // Not this account's (or nobody signed in yet): the next sign-in of
        // its account reads it from disk (#763).
        if restored && !mayUpload(sessionId) { manifests[sessionId] = nil }
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
        write(manifest)
    }

    private func write(_ manifest: Manifest) {
        let sessionId = manifest.sessionId
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
        savedManifest(in: directory(for: sessionId))
    }

    private func savedManifest(in dir: URL) -> Manifest? {
        let file = dir.appendingPathComponent("manifest.json")
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

/// This phone's recording key (#388), one per install. It goes with every
/// recording request: `device_key` in init and claim (which make it the
/// session's writer), in each chunk's form and in finalize. A session another
/// device continued refuses it with `session_taken_over`, so two devices never
/// record into one session. Per install, not per launch: chunks a killed app
/// left behind upload after the relaunch with the same key.
enum RecordingDevice {
    static let takenOverCode = "session_taken_over"
    static let takenOverMessage = "This recording was continued in another tab or on another device, so recording stopped here. Any audio this phone had not sent yet is not part of it."
    private static let defaultsKey = "loore_recording_device_key"

    static let key: String = {
        let defaults = UserDefaults.standard
        if let saved = defaults.string(forKey: RecordingDevice.defaultsKey), !saved.isEmpty {
            return saved
        }
        let made = UUID().uuidString
        defaults.set(made, forKey: RecordingDevice.defaultsKey)
        return made
    }()
}

private extension URL {
    func createIfNeeded(_ fileManager: FileManager) throws {
        if !fileManager.fileExists(atPath: path) {
            try fileManager.createDirectory(at: self, withIntermediateDirectories: true,
                                            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        }
    }
}
