import Foundation

// Drafts, recording sessions, voice and text-mode starts (map B §2.3).

/// `GET/POST /api/drafts/` (typed-text autosave). A draft from an unfinished
/// recording also carries `sessionId` + `hasStoredChunks` (run recovery).
struct Draft: Decodable, Identifiable, Sendable {
    var id: Int
    var content: String
    var nodeId: Int?
    var parentId: Int?
    var createdAt: Date?
    var updatedAt: Date?
    var parentDeleted: Bool
    var warning: String?
    var sessionId: String?
    var hasStoredChunks: Bool

    enum CodingKeys: String, CodingKey {
        case id, content, warning
        case nodeId = "node_id"
        case parentId = "parent_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case parentDeleted = "parent_deleted"
        case sessionId = "session_id"
        case hasStoredChunks = "has_stored_chunks"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        content = c.tolerant(.content, default: "")
        nodeId = c.tolerant(.nodeId)
        parentId = c.tolerant(.parentId)
        createdAt = c.tolerant(.createdAt)
        updatedAt = c.tolerant(.updatedAt)
        parentDeleted = c.tolerant(.parentDeleted, default: false)
        warning = c.tolerant(.warning)
        sessionId = c.tolerant(.sessionId)
        hasStoredChunks = c.tolerant(.hasStoredChunks, default: false)
    }
}

/// An element of `GET /api/drafts/interrupted` (newest first).
/// Resume at `max(chunk_index)+1` from `/status`, not at `chunkCount` (map B R3).
struct InterruptedDraft: Decodable, Identifiable, Sendable {
    var id: Int
    var sessionId: String
    var parentId: Int?
    var label: String?
    var content: String
    var chunkCount: Int
    var hasStoredChunks: Bool
    /// "audio/webm" | "audio/mp4" | nil
    var streamingMimeType: String?
    var createdAt: Date?
    var updatedAt: Date?
    var parentDeleted: Bool
    var warning: String?

    enum CodingKeys: String, CodingKey {
        case id, label, content, warning
        case sessionId = "session_id"
        case parentId = "parent_id"
        case chunkCount = "chunk_count"
        case hasStoredChunks = "has_stored_chunks"
        case streamingMimeType = "streaming_mime_type"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case parentDeleted = "parent_deleted"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        sessionId = try c.decode(String.self, forKey: .sessionId)
        parentId = c.tolerant(.parentId)
        label = c.tolerant(.label)
        content = c.tolerant(.content, default: "")
        chunkCount = c.tolerant(.chunkCount, default: 0)
        hasStoredChunks = c.tolerant(.hasStoredChunks, default: false)
        streamingMimeType = c.tolerant(.streamingMimeType)
        createdAt = c.tolerant(.createdAt)
        updatedAt = c.tolerant(.updatedAt)
        parentDeleted = c.tolerant(.parentDeleted, default: false)
        warning = c.tolerant(.warning)
    }
}

/// `POST /api/drafts/streaming/init` (201).
struct StreamingInitResponse: Decodable, Sendable {
    var sessionId: String
    var draftId: Int?
    /// "/api/sse/drafts/<session_id>/transcription-stream"
    var sseURL: String?

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case draftId = "draft_id"
        case sseURL = "sse_url"
    }
}

/// `POST /api/drafts/streaming/<sid>/audio-chunk`: 202 stored, or 200 "Chunk already uploaded".
struct ChunkUploadResponse: Decodable, Sendable {
    var chunkIndex: Int?
    var status: String?
    var message: String?
    var taskId: String?
    var batchQueued: Bool

    enum CodingKeys: String, CodingKey {
        case status, message
        case chunkIndex = "chunk_index"
        case taskId = "task_id"
        case batchQueued = "batch_queued"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        chunkIndex = c.flexibleInt(.chunkIndex)
        status = c.tolerant(.status)
        message = c.tolerant(.message)
        taskId = c.tolerant(.taskId)
        batchQueued = c.tolerant(.batchQueued, default: false)
    }
}

/// `GET /api/drafts/streaming/<sid>/status`.
struct StreamingSessionStatus: Decodable, Sendable {
    struct Chunk: Decodable, Hashable, Sendable {
        var chunkIndex: Int
        /// "stored" | "processing" | "pending" | "completed" | "failed"
        var status: String
        var text: String?
        var error: String?

        enum CodingKeys: String, CodingKey {
            case status, text, error
            case chunkIndex = "chunk_index"
        }
    }

    var sessionId: String?
    var draftId: Int?
    var streamingStatus: StreamingStatus?
    var streamingMimeType: String?
    var totalChunks: Int?
    var completedChunks: Int
    var failedChunks: Int
    var chunks: [Chunk]
    var content: String
    /// The Voice chain created the reply.
    var llmNodeId: Int?
    /// The reply was skipped (spend cap, bad placeholder, …).
    var warning: String?

    enum CodingKeys: String, CodingKey {
        case chunks, content, warning
        case sessionId = "session_id"
        case draftId = "draft_id"
        case streamingStatus = "streaming_status"
        case streamingMimeType = "streaming_mime_type"
        case totalChunks = "total_chunks"
        case completedChunks = "completed_chunks"
        case failedChunks = "failed_chunks"
        case llmNodeId = "llm_node_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = c.tolerant(.sessionId)
        draftId = c.tolerant(.draftId)
        streamingStatus = c.tolerant(.streamingStatus)
        streamingMimeType = c.tolerant(.streamingMimeType)
        totalChunks = c.tolerant(.totalChunks)
        completedChunks = c.tolerant(.completedChunks, default: 0)
        failedChunks = c.tolerant(.failedChunks, default: 0)
        chunks = c.tolerant(.chunks, default: [])
        content = c.tolerant(.content, default: "")
        llmNodeId = c.tolerant(.llmNodeId)
        warning = c.tolerant(.warning)
    }

    /// Next chunk index to use when resuming (map B R3).
    var nextChunkIndex: Int { (chunks.map(\.chunkIndex).max() ?? -1) + 1 }
}

/// `POST /api/drafts/streaming/<sid>/finalize` (202).
struct FinalizeResponse: Decodable, Sendable {
    var message: String?
    var taskId: String?
    var draftId: Int?
    var totalChunks: Int?

    enum CodingKeys: String, CodingKey {
        case message
        case taskId = "task_id"
        case draftId = "draft_id"
        case totalChunks = "total_chunks"
    }
}

/// `POST /api/drafts/streaming/<sid>/save-as-node` (201). `spendCapped` is a 201,
/// not a 402: show the cap banner yourself.
struct SaveAsNodeResponse: Decodable, Sendable {
    var id: Int
    var userNodeId: Int?
    var tipId: Int?
    var content: String?
    var parentId: Int?
    var privacyLevel: PrivacyLevel?
    var aiUsage: AIUsage?
    var createdAt: Date?
    var conversationId: Int?
    var llmNodeId: Int?
    var taskId: String?
    var spendCapped: Bool
    var llmError: String?

    enum CodingKeys: String, CodingKey {
        case id, content
        case userNodeId = "user_node_id"
        case tipId = "tip_id"
        case parentId = "parent_id"
        case privacyLevel = "privacy_level"
        case aiUsage = "ai_usage"
        case createdAt = "created_at"
        case conversationId = "conversation_id"
        case llmNodeId = "llm_node_id"
        case taskId = "task_id"
        case spendCapped = "spend_capped"
        case llmError = "llm_error"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        userNodeId = c.tolerant(.userNodeId)
        tipId = c.tolerant(.tipId)
        content = c.tolerant(.content)
        parentId = c.tolerant(.parentId)
        privacyLevel = c.tolerant(.privacyLevel)
        aiUsage = c.tolerant(.aiUsage)
        createdAt = c.tolerant(.createdAt)
        conversationId = c.tolerant(.conversationId)
        llmNodeId = c.tolerant(.llmNodeId)
        taskId = c.tolerant(.taskId)
        spendCapped = c.tolerant(.spendCapped, default: false)
        llmError = c.tolerant(.llmError)
    }
}

/// `POST /api/textmode/start` and `/api/textmode/from-node/<id>` (202).
struct TextmodeStartResponse: Decodable, Sendable {
    /// System node carrying the textmode prompt (thread root). Absent on from-node.
    var conversationId: Int?
    /// from-node only: textmode prompt inserted under the node, or nil inside an agentic thread.
    var promptNodeId: Int?
    var userNodeId: Int
    var llmNodeId: Int?
    var taskId: String?
    var spendCapped: Bool
    var llmError: String?

    enum CodingKeys: String, CodingKey {
        case conversationId = "conversation_id"
        case promptNodeId = "prompt_node_id"
        case userNodeId = "user_node_id"
        case llmNodeId = "llm_node_id"
        case taskId = "task_id"
        case spendCapped = "spend_capped"
        case llmError = "llm_error"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        conversationId = c.tolerant(.conversationId)
        promptNodeId = c.tolerant(.promptNodeId)
        userNodeId = try c.decode(Int.self, forKey: .userNodeId)
        llmNodeId = c.tolerant(.llmNodeId)
        taskId = c.tolerant(.taskId)
        spendCapped = c.tolerant(.spendCapped, default: false)
        llmError = c.tolerant(.llmError)
    }
}

/// `POST /api/voice/` (fallback turn start, 202).
struct VoiceSessionResponse: Decodable, Sendable {
    var parentId: Int?
    var userNodeId: Int?
    var llmNodeId: Int
    var taskId: String?

    enum CodingKeys: String, CodingKey {
        case parentId = "parent_id"
        case userNodeId = "user_node_id"
        case llmNodeId = "llm_node_id"
        case taskId = "task_id"
    }
}

/// `POST /api/voice/from-node/<id>`: always `mode == "processing"`.
struct VoiceFromNodeResponse: Decodable, Sendable {
    var mode: String?
    var llmNodeId: Int
    var parentId: Int?
    /// A new reply is being generated (else: play the existing one).
    var fresh: Bool

    enum CodingKeys: String, CodingKey {
        case mode, fresh
        case llmNodeId = "llm_node_id"
        case parentId = "parent_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mode = c.tolerant(.mode)
        llmNodeId = try c.decode(Int.self, forKey: .llmNodeId)
        parentId = c.tolerant(.parentId)
        fresh = c.tolerant(.fresh, default: false)
    }
}
