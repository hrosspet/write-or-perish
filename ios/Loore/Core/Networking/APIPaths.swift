import Foundation

/// Canonical request paths (map B §0 "Trailing slashes").
///
/// Routes Flask registers as `"/"` under a prefix are canonical WITH the slash;
/// the slashless form costs a 308 round trip. `/api/updates` and `/api/share`
/// are the reverse: registered as `""`, a trailing slash is a 404.
/// Add new endpoints here so the slash rule lives in one place.
enum APIPath {
    // MARK: Auth (non-/api: never follow redirects)
    static let magicLinkSend = "/auth/magic-link/send"
    static let magicLinkVerify = "/auth/magic-link/verify"
    static let logout = "/auth/logout"
    static let xLogin = "/auth/login"
    static let xConnect = "/auth/x/connect"

    // MARK: Account
    static let dashboard = "/api/dashboard/"
    static let user = "/api/dashboard/user"
    static let timezone = "/api/dashboard/timezone"
    static let email = "/api/dashboard/email"
    static let emailConfirm = "/api/dashboard/email/confirm"
    static let emailPending = "/api/dashboard/email/pending"
    static let disconnectX = "/api/dashboard/x"
    static let termsAccept = "/api/terms/accept"
    static let health = "/api/health"

    // MARK: Updates channel
    static let updates = "/api/updates"
    static func changelog(_ id: String, action: String) -> String {
        "/api/updates/changelog/\(escape(id))/\(action)"
    }
    static func notification(_ id: Int, action: String) -> String { "/api/updates/notifications/\(id)/\(action)" }
    static func poll(_ id: Int) -> String { "/api/updates/polls/\(id)" }
    static func pollDraft(_ id: Int) -> String { "/api/updates/polls/\(id)/draft" }
    static func pollResponse(_ id: Int) -> String { "/api/updates/polls/\(id)/response" }
    static func pollSend(_ id: Int) -> String { "/api/updates/polls/\(id)/send" }
    static func pollDecline(_ id: Int) -> String { "/api/updates/polls/\(id)/decline" }

    // MARK: Nodes
    static let nodes = "/api/nodes/"
    static let nodeModels = "/api/nodes/models"
    static let defaultModel = "/api/nodes/default-model"
    static let nodeTitles = "/api/nodes/titles"
    static func node(_ id: Int) -> String { "/api/nodes/\(id)" }
    static func nodeLLM(_ id: Int) -> String { "/api/nodes/\(id)/llm" }
    static func llmStatus(_ id: Int) -> String { "/api/nodes/\(id)/llm-status" }
    static func deleteImpact(_ id: Int) -> String { "/api/nodes/\(id)/delete-impact" }
    static func threadName(_ id: Int) -> String { "/api/nodes/\(id)/thread-name" }
    static func resolveQuotes(_ id: Int) -> String { "/api/nodes/\(id)/resolve-quotes" }
    static func suggestedModel(_ id: Int) -> String { "/api/nodes/\(id)/suggested-model" }
    static func pin(_ id: Int) -> String { "/api/nodes/\(id)/pin" }
    static func nodeAudio(_ id: Int) -> String { "/api/nodes/\(id)/audio" }
    static func nodeAudioChunks(_ id: Int) -> String { "/api/nodes/\(id)/audio-chunks" }
    static func nodeAudioDownload(_ id: Int) -> String { "/api/nodes/\(id)/audio-download" }
    static func nodeTTS(_ id: Int) -> String { "/api/nodes/\(id)/tts" }
    static func nodeTTSStatus(_ id: Int) -> String { "/api/nodes/\(id)/tts-status" }
    static func nodeTTSChapters(_ id: Int) -> String { "/api/nodes/\(id)/tts-chapters" }
    static func transcriptionStatus(_ id: Int) -> String { "/api/nodes/\(id)/transcription-status" }
    static func feedPicks(_ id: Int) -> String { "/api/nodes/\(id)/feed-picks" }
    static let uploadInit = "/api/nodes/upload/init"
    static let uploadChunk = "/api/nodes/upload/chunk"
    static let uploadFinalize = "/api/nodes/upload/finalize"
    static let uploadCleanup = "/api/nodes/upload/cleanup"

    // MARK: Saved references quoted in replies (`{quote_ext:N}`)
    static func externalItemRead(_ id: Int) -> String { "/api/external/items/\(id)/read" }
    static func externalItemFeedback(_ id: Int) -> String { "/api/external/items/\(id)/feedback" }

    // MARK: Proposal accepts
    static let todoApplyDraft = "/api/todo/apply-draft"
    static let githubCreateIssue = "/api/github/create-issue"
    static let feedbackSubmit = "/api/feedback/submit"
    static let shareSaveProposal = "/api/share/save-proposal"

    // MARK: Read (admin feature)
    static let readStart = "/api/read/start"
    static func readFromNode(_ id: Int) -> String { "/api/read/from-node/\(id)" }
    static func readRerun(_ id: Int) -> String { "/api/read/\(id)/rerun" }

    // MARK: Log and search
    static let log = "/api/log"
    static let search = "/api/search"
    static let semanticSearch = "/api/search/semantic"

    // MARK: Drafts, recording, voice, text mode
    static let drafts = "/api/drafts/"
    static let interruptedDrafts = "/api/drafts/interrupted"
    static let streamingInit = "/api/drafts/streaming/init"
    static func streamingChunk(_ sid: String) -> String { "/api/drafts/streaming/\(escape(sid))/audio-chunk" }
    static func streamingStatus(_ sid: String) -> String { "/api/drafts/streaming/\(escape(sid))/status" }
    static func streamingFinalize(_ sid: String) -> String { "/api/drafts/streaming/\(escape(sid))/finalize" }
    static func streamingTranscribeRemaining(_ sid: String) -> String {
        "/api/drafts/streaming/\(escape(sid))/transcribe-remaining"
    }
    static func streamingSaveAsNode(_ sid: String) -> String { "/api/drafts/streaming/\(escape(sid))/save-as-node" }
    static func streamingDiscard(_ sid: String) -> String { "/api/drafts/streaming/\(escape(sid))/discard" }
    static let voice = "/api/voice/"
    static func voiceFromNode(_ id: Int) -> String { "/api/voice/from-node/\(id)" }
    static let voiceTiming = "/api/voice/timing"
    static let voiceTimingClock = "/api/voice/timing/clock"
    static let textmodeStart = "/api/textmode/start"
    static func textmodeFromNode(_ id: Int) -> String { "/api/textmode/from-node/\(id)" }

    // MARK: Workspace (M4)
    static let profile = "/api/profile/"
    static let profileProgress = "/api/export/profile-progress"
    static let todo = "/api/todo/"
    static let artifacts = "/api/artifacts/"
    static let prompts = "/api/prompts/"
    static let share = "/api/share"
    static let exportThreads = "/api/export/threads"

    // MARK: Server-sent events
    static func sseLLMStream(_ nodeId: Int) -> String { "/api/sse/nodes/\(nodeId)/llm-stream" }
    static func sseNodeTTS(_ nodeId: Int) -> String { "/api/sse/nodes/\(nodeId)/tts-stream" }
    static func sseProfileTTS(_ profileId: Int) -> String { "/api/sse/profiles/\(profileId)/tts-stream" }
    static func sseItemTTS(_ itemId: Int) -> String { "/api/sse/items/\(itemId)/tts-stream" }
    static func sseDraftTranscription(_ sid: String) -> String {
        "/api/sse/drafts/\(escape(sid))/transcription-stream"
    }

    /// Blueprint roots registered as `"/"`: canonical with a trailing slash.
    static let slashRoots: Set<String> = [
        "/api/dashboard", "/api/nodes", "/api/drafts", "/api/todo", "/api/artifacts",
        "/api/profile", "/api/prompts", "/api/voice",
    ]

    /// Registered as `""`: a trailing slash is a 404.
    static let noSlashRoots: Set<String> = ["/api/updates", "/api/share"]

    /// Returns the canonical form of `path` (query string untouched).
    static func canonical(_ path: String) -> String {
        let parts = path.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        var bare = String(parts[0])
        let query = parts.count > 1 ? "?" + parts[1] : ""
        if slashRoots.contains(bare) {
            bare += "/"
        } else if bare.hasSuffix("/"), noSlashRoots.contains(String(bare.dropLast())) {
            bare.removeLast()
        }
        return bare + query
    }

    private static func escape(_ segment: String) -> String {
        segment.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/")))
            ?? segment
    }
}
