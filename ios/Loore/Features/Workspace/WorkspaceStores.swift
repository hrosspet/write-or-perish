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
    /// The user's deleted kinds that were not made again (#603).
    private(set) var deletedKinds: [String] = []
    /// True once a fetch succeeded (the Artifacts page waits for it).
    private(set) var loaded = false
    private var inFlight: Task<Void, Never>?
    /// Bumped by every delete, so a list fetched before it is not applied.
    private var deletes = 0

    static let deleteFailedMessage = "Could not delete the artifact."

    /// `GET /api/artifacts/`. Errors keep the cached list (Profile and Todo still show).
    func refresh(api: APIClient) async {
        if let inFlight { await inFlight.value; return }
        let task = Task {
            // A delete that lands while the list is on its way may still be in
            // it: fetch once more rather than bring the bubble back.
            for _ in 0..<2 {
                let deletesBefore = deletes
                do {
                    let list: ArtifactList = try await api.get(APIPath.artifacts)
                    if deletes != deletesBefore { continue }
                    artifacts = list.artifacts
                    deletedKinds = list.deletedKinds
                    loaded = true
                } catch {
                    Logger(subsystem: "org.loore.app", category: "artifacts").info("artifact list load failed")
                }
                return
            }
        }
        inFlight = task
        await task.value
        inFlight = nil
    }

    func artifact(_ kind: String) -> ArtifactDoc? {
        artifacts.first { $0.kind == kind }
    }

    /// The user deleted `kind` and has not made it again: a link to it says so.
    func isDeleted(_ kind: String) -> Bool {
        deletedKinds.contains(kind) && artifact(kind) == nil
    }

    enum DeleteResult: Equatable {
        case deleted
        /// Nothing was deleted; the text is for a toast.
        case failed(String)
    }

    /// `DELETE /api/artifacts/<kind>`. A 404 saying the kind was already deleted
    /// (from another device) counts as done. The list is left as it is: the
    /// caller removes the bubble with `markDeleted`, in the same update as the
    /// next selection.
    func delete(_ kind: String, api: APIClient) async -> DeleteResult {
        do {
            let _: EmptyResponse = try await api.delete(APIPath.artifact(kind))
            return .deleted
        } catch let error as APIError {
            if error.isNotFound, error.body?.extra["deleted"]?.boolValue == true { return .deleted }
            return .failed(error.userMessage(fallback: Self.deleteFailedMessage))
        } catch {
            return .failed(Self.deleteFailedMessage)
        }
    }

    /// Drops `kind` from the list and remembers it as deleted.
    func markDeleted(_ kind: String) {
        deletes += 1
        artifacts.removeAll { $0.kind == kind }
        if !deletedKinds.contains(kind) { deletedKinds.append(kind) }
    }

    func reset() {
        artifacts = []
        deletedKinds = []
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
