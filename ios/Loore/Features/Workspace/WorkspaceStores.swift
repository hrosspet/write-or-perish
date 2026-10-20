import Foundation
import Observation
import os

/// The artifact list behind the ArtifactsNav bubbles (web `ArtifactsNav`'s
/// module cache): shown at once on every visit, refreshed in the background
/// and after `artifactsChanged` (`loore_artifacts_changed`).
@MainActor
@Observable
final class ArtifactsStore {
    private(set) var artifacts: [ArtifactDoc] = []
    /// True once a fetch succeeded (the Artifacts page waits for it).
    private(set) var loaded = false
    private var inFlight: Task<Void, Never>?

    /// `GET /api/artifacts/`. Errors keep the cached list (Profile and Todo still show).
    func refresh(api: APIClient) async {
        if let inFlight { await inFlight.value; return }
        let task = Task {
            do {
                let list: ArtifactList = try await api.get(APIPath.artifacts)
                artifacts = list.artifacts
                loaded = true
            } catch {
                Logger(subsystem: "org.loore.app", category: "artifacts").info("artifact list load failed")
            }
        }
        inFlight = task
        await task.value
        inFlight = nil
    }

    func artifact(_ kind: String) -> ArtifactDoc? {
        artifacts.first { $0.kind == kind }
    }

    func reset() {
        artifacts = []
        loaded = false
    }
}

/// App-wide watcher for profile generation (web `ProfileGenerationWatcher`,
/// map E §10). Polls `/api/export/profile-progress` while a build runs (60 s
/// for the batch pipeline, 5 s for the synchronous task, no duration cap,
/// stops after 10 consecutive errors), publishes progress for the Profile page
/// and toasts the outcome.
@MainActor
@Observable
final class ProfileGenerationWatcher {
    /// What the Profile page renders (`loore_profile_progress` detail).
    struct Progress: Equatable {
        var running: Bool
        var status: String
        var progress: Double
        var message: String
        var source: String?
        var latestProfileId: Int?
    }

    enum Outcome: String, Equatable { case completed, failed, stalled }

    static let batchInterval: TimeInterval = 60
    static let syncInterval: TimeInterval = 5
    static let maxConsecutiveErrors = 10

    private(set) var active = false
    /// The latest broadcast (`loore_profile_progress`); nil until the first one.
    private(set) var progress: Progress?
    /// Bumped on every `loore_profile_done`.
    private(set) var doneCount = 0

    private var source: String?
    private var syncTaskId: String?
    /// nil = never saw it running (`undefined` on the web); .some(nil) = running with no profile yet.
    private var startVersion: Int??
    private var batchPendingHint = false
    private var loop: Task<Void, Never>?

    /// Side effects, wired by `AppState`.
    var showToast: (String, TimeInterval) -> Void = { _, _ in }
    /// Clears `profile_generation_task_id` / `profile_batch_pending` in the cached user.
    var clearUserFlags: () -> Void = {}
    var fetch: (String?) async throws -> ProfileProgress = { _ in throw CancellationError() }

    /// The user payload says a build is running (web: `user` effect).
    func userLoaded(_ user: CurrentUser) {
        guard user.profileGenerationTaskId != nil || user.profileBatchPending else { return }
        batchPendingHint = user.profileBatchPending
        start()
    }

    /// `loore_profile_started` (fired natively after an import returns a profile task).
    func start() {
        guard !active else { return }
        active = true
        loop?.cancel()
        loop = Task { [weak self] in await self?.run() }
    }

    func stop() {
        loop?.cancel()
        loop = nil
        active = false
        source = nil
        syncTaskId = nil
        startVersion = nil
        progress = nil
    }

    private var interval: TimeInterval {
        (source ?? (batchPendingHint ? "batch" : "sync")) == "batch" ? Self.batchInterval : Self.syncInterval
    }

    private func run() async {
        var errors = 0
        var lastStatus: String?
        while active && !Task.isCancelled {
            do {
                let data = try await fetch(syncTaskId)
                guard !Task.isCancelled, active else { return }
                errors = 0
                lastStatus = data.status
                handle(data)
                guard active else { return }
            } catch {
                if Task.isCancelled || (error as? APIError)?.isCancelled == true { return }
                errors += 1
                if errors >= Self.maxConsecutiveErrors {
                    if lastStatus != "failed" { gaveUp() }
                    return
                }
            }
            await Poller.sleep(interval, wake: .foreground)
        }
    }

    /// One poll answer (the web's data effect). Returns the terminal outcome, if any.
    @discardableResult
    func handle(_ data: ProfileProgress) -> Outcome? {
        guard active else { return nil }
        if data.running {
            source = data.source
            if data.source == "sync", let task = data.taskId, task != syncTaskId { syncTaskId = task }
            if startVersion == nil { startVersion = .some(data.latestProfile?.id) }
            progress = Progress(running: true, status: data.status, progress: data.progress, message: data.message,
                                source: data.source, latestProfileId: data.latestProfile?.id)
            return nil
        }
        let sawRunning = startVersion != nil
        let newVersion = sawRunning && data.latestProfile?.id != startVersion!
        var outcome: Outcome?
        if data.status == "failed" {
            outcome = .failed
        } else if data.status == "stalled" || (data.status == "idle" && sawRunning && data.batchStepFailed) {
            outcome = .stalled
        } else if data.status == "completed" || (data.status == "idle" && newVersion) {
            outcome = .completed
        } else if data.status == "idle" && sawRunning {
            outcome = .stalled
        }
        reset()
        if let outcome {
            progress = Progress(running: false, status: outcome.rawValue, progress: 0, message: "")
        }
        doneCount += 1
        switch outcome {
        case .completed: showToast("Your profile has been updated ✓", 6)
        case .failed: showToast("Profile generation failed", 3)
        case .stalled: showToast("Profile generation stopped before finishing — it will be retried in the background", 3)
        case nil: break
        }
        return outcome
    }

    /// Polling gave up on errors: stop quietly (no toast), still signal done.
    func gaveUp() {
        guard active else { return }
        reset()
        doneCount += 1
    }

    private func reset() {
        active = false
        source = nil
        syncTaskId = nil
        startVersion = nil
        batchPendingHint = false
        clearUserFlags()
    }
}
