import Foundation
import Observation
import os

/// The four archive importers (web `ImportData`, map E §7.1).
enum ImportKind: String, CaseIterable, Identifiable, Sendable {
    case claude, chatgpt, markdown, twitter

    var id: String { rawValue }

    var buttonTitle: String {
        switch self {
        case .claude: return "Import Claude"
        case .chatgpt: return "Import ChatGPT"
        case .markdown: return "Import Markdown (e.g. Obsidian)"
        case .twitter: return "Import Tweets"
        }
    }

    var analyzePath: String {
        switch self {
        case .claude: return APIPath.importClaudeAnalyze
        case .chatgpt: return APIPath.importChatGPTAnalyze
        case .markdown: return APIPath.importMarkdownAnalyze
        case .twitter: return APIPath.importTwitterAnalyze
        }
    }

    var confirmPath: String {
        switch self {
        case .claude: return APIPath.importClaudeConfirm
        case .chatgpt: return APIPath.importChatGPTConfirm
        case .markdown: return APIPath.importMarkdownConfirm
        case .twitter: return APIPath.importTwitterConfirm
        }
    }

    /// The web's fallback analyze error per importer.
    var analyzeError: String {
        switch self {
        case .claude: return "Error analyzing Claude export. Please try again."
        case .chatgpt: return "Error analyzing ChatGPT export. Please try again."
        case .markdown: return "Error analyzing import file. Please try again."
        case .twitter: return "Error analyzing Twitter export. Please try again."
        }
    }

    var confirmError: String {
        switch self {
        case .claude: return "Error importing Claude data. Please try again."
        case .chatgpt: return "Error importing ChatGPT data. Please try again."
        case .markdown: return "Error importing data. Please try again."
        case .twitter: return "Error importing Twitter data. Please try again."
        }
    }
}

/// What an analyze call found (kept to re-post on confirm, as the web does).
struct ImportAnalysis {
    let kind: ImportKind
    /// The analyze answer as JSON objects (`files` / `conversations` are posted back).
    let raw: [String: Any]

    private func int(_ key: String) -> Int { (raw[key] as? NSNumber)?.intValue ?? 0 }

    var totalFiles: Int { int("total_files") }
    var totalSize: Int { int("total_size") }
    var totalTokens: Int { int("total_tokens") }
    var totalConversations: Int { int("total_conversations") }
    var totalMessages: Int { int("total_messages") }
    var importToken: String? { raw["import_token"] as? String }
    var totalTweets: Int { int("total_tweets") }
    var originalCount: Int { int("original_count") }
    var replyCount: Int { int("reply_count") }
    var skippedRetweets: Int { int("skipped_retweets") }
    var originalTokens: Int { int("original_tokens") }
}

/// The "Import Finished" numbers (`created`, `skipped`, `restored`, `updated`, `empty`).
struct ImportResult: Equatable {
    var created = 0
    var skipped = 0
    var restored = 0
    var updated = 0
    var empty = 0
    var profileUpdateTaskId: String?

    init(_ object: [String: Any]) {
        func int(_ key: String) -> Int { (object[key] as? NSNumber)?.intValue ?? 0 }
        created = int("created")
        skipped = int("skipped")
        restored = int("restored")
        updated = int("updated")
        empty = int("empty")
        profileUpdateTaskId = object["profile_update_task_id"] as? String
    }

    init(created: Int = 0, skipped: Int = 0, restored: Int = 0, updated: Int = 0, empty: Int = 0) {
        self.created = created
        self.skipped = skipped
        self.restored = restored
        self.updated = updated
        self.empty = empty
    }

    /// Imported always; Restored, Updated, Skipped and "No text" when > 0.
    var stats: [(label: String, value: Int, highlight: Bool)] {
        [("Imported", created, created > 0), ("Restored", restored, restored > 0), ("Updated", updated, updated > 0),
         ("Skipped", skipped, false), ("No text", empty, false)]
            .filter { $0.0 == "Imported" || $0.1 > 0 }
    }

    var notes: [String] {
        if created == 0 && restored == 0 && updated == 0 {
            return ["Everything in this archive was already imported — nothing new was added."]
        }
        var out: [String] = []
        if updated > 0 {
            out.append("Updated items were already imported; their privacy and AI-usage settings now match this import.")
        }
        if skipped > 0 { out.append("Skipped items were already imported and left untouched.") }
        if empty > 0 {
            out.append("Posts with no text — usually media-only tweets — were not imported. There was nothing to write.")
        }
        return out
    }
}

/// The import flow: pick → (extract) → analyze → confirm dialog → confirm
/// (→ Twitter task polling) → 409 restore-or-skip → "Import Finished".
@MainActor
@Observable
final class ImportModel {
    enum Stage: Equatable { case extracting, analyzing, importing }

    static let stageLabels: [Stage: String] = [.extracting: "Extracting…", .analyzing: "Analyzing…", .importing: "Importing…"]
    /// A confirm that outlives nginx's 60 s window (design doc §10).
    static let timeoutMessage = "The import took longer than the connection allows. It may still finish on the server: check your Log in a few minutes before importing again (a repeat skips what already arrived)."

    private let app: AppState
    private let log = Logger(subsystem: "org.loore.app", category: "import")

    private(set) var stage: Stage?
    var error: String?
    /// Set to open the file picker for this importer.
    var picking: ImportKind?
    var analysis: ImportAnalysis?
    var importType = "separate_nodes"
    var dateOrdering = "modified"
    var includeReplies = false
    var privacy: PrivacyLevel = .private
    var aiUsage: AIUsage = .off
    private(set) var progress: (done: Int, total: Int)?
    /// `409 deleted_content_matches`: the count to show; the retry repeats the confirm.
    var deletedMatches: Int?
    var result: ImportResult?

    init(app: AppState) {
        self.app = app
    }

    var busy: Bool { stage != nil }

    // MARK: Pick and analyze

    func picked(_ url: URL, kind: ImportKind) async {
        error = nil
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let directory: URL
        do {
            directory = try ImportFiles.workDirectory()
        } catch {
            self.error = kind.analyzeError
            return
        }
        defer { try? FileManager.default.removeItem(at: directory) }

        var source = url
        var field = "zip_file"
        var filename = url.lastPathComponent
        var mime = "application/zip"
        if kind == .claude || kind == .chatgpt {
            stage = .extracting
            do {
                source = try await Task.detached(priority: .userInitiated) {
                    try ImportFiles.extractConversations(from: url, into: directory)
                }.value
            } catch let failure as ImportFiles.Failure {
                fail(ImportFiles.message(failure, kind: kind))
                return
            } catch {
                fail(ImportFiles.message(.unreadableZip, kind: kind))
                return
            }
            field = "conversations_file"
            filename = "conversations.json"
            mime = "application/json"
        }
        if ImportFiles.fileSize(source) > ImportFiles.uploadLimit {
            fail(ImportFiles.message(.tooLarge, kind: kind))
            return
        }
        stage = .analyzing
        do {
            let (fieldValue, fileName, mimeType, src) = (field, filename, mime, source)
            let body = try await Task.detached(priority: .userInitiated) {
                try ImportFiles.multipartBody(field: fieldValue, filename: fileName, mimeType: mimeType, source: src,
                                              into: directory)
            }.value
            var request = APIRequest(.post, kind.analyzePath)
            request.contentType = body.contentType
            request.timeout = 300
            let (data, _) = try await app.api.upload(request, fromFile: body.url)
            guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw APIError.decoding("import analyze")
            }
            analysis = ImportAnalysis(kind: kind, raw: object)
            importType = "separate_nodes"
            stage = nil
        } catch let failure as APIError {
            fail(analyzeMessage(failure, kind: kind))
        } catch {
            fail(kind.analyzeError)
        }
    }

    /// ChatGPT's richer analyze errors; the others use the server text or a fallback.
    func analyzeMessage(_ error: APIError, kind: ImportKind) -> String {
        let body = error.body
        if kind == .chatgpt {
            if let message = body?.error ?? body?.details {
                if let details = body?.details, let main = body?.error { return "\(main): \(details)" }
                return message
            }
            if let status = error.status {
                if status == 413 { return "conversations.json is too large to upload. Please contact support." }
                return "Error analyzing ChatGPT export (HTTP \(status)). Please try again."
            }
            return "Error analyzing ChatGPT export. The request did not reach the server — check your connection."
        }
        return error.serverMessage ?? kind.analyzeError
    }

    // MARK: Confirm

    func cancel() {
        guard !busy else { return }
        analysis = nil
    }

    /// The confirm body the web sends for this importer.
    func confirmBody(_ analysis: ImportAnalysis, onDeleted: String?) -> [String: Any] {
        var body: [String: Any] = ["privacy_level": privacy.rawString, "ai_usage": aiUsage.rawString]
        switch analysis.kind {
        case .claude, .chatgpt:
            body["conversations"] = analysis.raw["conversations"] ?? []
        case .markdown:
            body["files"] = analysis.raw["files"] ?? []
            body["import_type"] = importType
            body["date_ordering"] = dateOrdering
        case .twitter:
            body["import_token"] = analysis.importToken ?? ""
            body["import_type"] = importType
            body["include_replies"] = includeReplies
        }
        if let onDeleted { body["on_deleted"] = onDeleted }
        return body
    }

    func confirm(onDeleted: String? = nil) async {
        guard let analysis, !busy else { return }
        stage = .importing
        progress = nil
        error = nil
        let directory = try? ImportFiles.workDirectory()
        defer { if let directory { try? FileManager.default.removeItem(at: directory) } }
        do {
            let json = try JSONSerialization.data(withJSONObject: confirmBody(analysis, onDeleted: onDeleted))
            var request = APIRequest(.post, analysis.kind.confirmPath)
            request.contentType = "application/json"
            request.timeout = 90
            let data: Data
            if let directory {
                let file = directory.appendingPathComponent("confirm.json")
                try json.write(to: file)
                data = try await app.api.upload(request, fromFile: file).0
            } else {
                request.body = json
                data = try await app.api.data(for: request).0
            }
            let object = (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
            if analysis.kind == .twitter, let taskId = object["task_id"] as? String {
                progress = (0, (object["total"] as? NSNumber)?.intValue ?? 0)
                await pollTwitter(taskId)
            } else {
                finish(ImportResult(object))
            }
        } catch let failure as APIError {
            if failure.status == 409, failure.body?.error == "deleted_content_matches" {
                deletedMatches = failure.body?.extra["deleted_matches"]?.intValue ?? 0
                stage = nil
                return
            }
            if failure.status == 504 || failure.status == 502 || (failure.isOffline && isTimeout(failure)) {
                fail(Self.timeoutMessage)
            } else {
                fail(failure.serverMessage ?? analysis.kind.confirmError)
            }
        } catch {
            fail(analysis.kind.confirmError)
        }
    }

    private func isTimeout(_ error: APIError) -> Bool {
        if case .transport(let code, _) = error { return code == NSURLErrorTimedOut }
        return false
    }

    /// `GET /api/import/status/<task>` every 1.5 s.
    private func pollTwitter(_ taskId: String) async {
        while !Task.isCancelled {
            do {
                let status: [String: JSONValue] = try await app.api.get(APIPath.importStatus(taskId), poll: true)
                switch status["status"]?.stringValue {
                case "completed":
                    let resultObject = (try? JSONSerialization.jsonObject(
                        with: JSONEncoder().encode(status["result"] ?? .object([:])))) as? [String: Any] ?? [:]
                    finish(ImportResult(resultObject))
                    return
                case "failed":
                    fail(status["error"]?.stringValue ?? ImportKind.twitter.confirmError)
                    return
                case "running":
                    progress = (status["done"]?.intValue ?? 0, status["total"]?.intValue ?? progress?.total ?? 0)
                default:
                    break
                }
            } catch {
                fail("Lost track of the import — check your Log to see whether it finished.")
                return
            }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }
    }

    /// "Restore deleted content" / "Keep it deleted": retry with `on_deleted`.
    func resolveDeleted(_ choice: String?) async {
        deletedMatches = nil
        guard let choice else { return }
        await confirm(onDeleted: choice)
    }

    private func finish(_ outcome: ImportResult) {
        analysis = nil
        stage = nil
        progress = nil
        error = nil
        result = outcome
        log.info("import finished: created \(outcome.created), skipped \(outcome.skipped)")
    }

    private func fail(_ message: String) {
        error = message
        stage = nil
        progress = nil
    }

    /// OK on "Import Finished": the web reloads the page; the app refetches the
    /// user (which starts the profile watcher when a build was handed off).
    func acknowledgeResult() async {
        let started = result?.profileUpdateTaskId != nil
        result = nil
        app.signals.post(.logChanged)
        await app.loadUser()
        if started { app.profileWatcher.start() }
    }
}
