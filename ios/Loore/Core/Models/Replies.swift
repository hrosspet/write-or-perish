import Foundation

// Async-task payloads: LLM replies, TTS, transcription (map B §2.2.4–2.2.5, §6).

/// `POST /api/nodes/<id>/llm` answer (202). `nodeId` is the NEW placeholder reply node.
struct LLMRequestResponse: Decodable, Sendable {
    var message: String?
    var taskId: String?
    var status: TaskStatus?
    var nodeId: Int

    enum CodingKeys: String, CodingKey {
        case message, status
        case taskId = "task_id"
        case nodeId = "node_id"
    }
}

/// `GET /api/nodes/<id>/llm-status`. The source of truth while a reply is generated.
struct LLMStatus: Decodable, Sendable {
    var nodeId: Int
    var status: TaskStatus?
    var progress: Int
    /// User-facing failure text (e.g. a provider's rate-limit message).
    var error: String?
    var taskInfo: JSONValue?
    /// The answer continues on another node (within-turn chain, #158): follow it.
    var continuationNodeId: Int?
    var ttsTaskStatus: TaskStatus?
    /// TTS is spoken while the reply is written (`tts_task_id == "voice-stream"`).
    var ttsStreaming: Bool
    /// Present when status is completed or cancelled.
    var content: String?
    var toolCallsMeta: [ToolCallMeta]?
    /// "batch" while a batch read waits at the provider.
    var stage: String?
    var batchSubmittedAt: Date?
    var feedPicksCount: Int?
    /// Always present; each distinct string is toasted once (8 s) on completion.
    var warnings: [String]
    var node: StatusNode?

    struct StatusNode: Decodable, Sendable {
        var id: Int
        var content: String?
        var nodeType: NodeType?
        var llmModel: String?
        var createdAt: Date?

        enum CodingKeys: String, CodingKey {
            case id, content
            case nodeType = "node_type"
            case llmModel = "llm_model"
            case createdAt = "created_at"
        }
    }

    enum CodingKeys: String, CodingKey {
        case status, progress, error, content, stage, warnings, node
        case nodeId = "node_id"
        case taskInfo = "task_info"
        case continuationNodeId = "continuation_node_id"
        case ttsTaskStatus = "tts_task_status"
        case ttsStreaming = "tts_streaming"
        case toolCallsMeta = "tool_calls_meta"
        case batchSubmittedAt = "batch_submitted_at"
        case feedPicksCount = "feed_picks_count"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodeId = c.tolerant(.nodeId, default: 0)
        status = c.tolerant(.status)
        progress = c.tolerant(.progress, default: 0)
        error = c.tolerant(.error)
        taskInfo = c.tolerant(.taskInfo)
        continuationNodeId = c.tolerant(.continuationNodeId)
        ttsTaskStatus = c.tolerant(.ttsTaskStatus)
        ttsStreaming = c.tolerant(.ttsStreaming, default: false)
        content = c.tolerant(.content)
        toolCallsMeta = c.tolerant(.toolCallsMeta)
        stage = c.tolerant(.stage)
        batchSubmittedAt = c.tolerant(.batchSubmittedAt)
        feedPicksCount = c.tolerant(.feedPicksCount)
        warnings = c.tolerant(.warnings, default: [])
        node = c.tolerant(.node)
    }
}

extension LLMStatus: PollableStatus {
    var pollStatus: TaskStatus? { status }
}

/// `POST /api/read/from-node/<id>` answer (202): a glean (#435). The gleaning's
/// pending node, or (the admin's auto-generate-off path) only the prompt node.
struct GleanStartResponse: Decodable, Equatable, Sendable {
    var llmNodeId: Int?
    var promptNodeId: Int?
    var taskId: String?

    enum CodingKeys: String, CodingKey {
        case llmNodeId = "llm_node_id"
        case promptNodeId = "prompt_node_id"
        case taskId = "task_id"
    }

    init(llmNodeId: Int?, promptNodeId: Int? = nil, taskId: String? = nil) {
        self.llmNodeId = llmNodeId
        self.promptNodeId = promptNodeId
        self.taskId = taskId
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        llmNodeId = c.flexibleInt(.llmNodeId)
        promptNodeId = c.flexibleInt(.promptNodeId)
        taskId = c.tolerant(.taskId)
    }

    /// The node to open: the gleaning, else the prompt node.
    var openId: Int? { llmNodeId ?? promptNodeId }
}

/// A glean (#435) as the clients send it: `POST /api/read/from-node/<id>` with
/// `{}` (the server's default: a read model of the user's own provider) or
/// `{"model": "<id>"}` from the picker beside the Glean button.
enum GleanRequest {
    static func body(model: String?) -> JSONValue {
        guard let model, !model.isEmpty else { return .object([:]) }
        return .object(["model": .string(model)])
    }
}

/// `POST /api/nodes/<id>/tts` answers: 200 with `ttsURL` (play it) or 202 (subscribe to the stream).
struct TTSRequestResponse: Decodable, Sendable {
    var message: String?
    var ttsURL: String?
    var taskId: String?
    var status: TaskStatus?
    var nodeId: Int?

    enum CodingKeys: String, CodingKey {
        case message, status
        case ttsURL = "tts_url"
        case taskId = "task_id"
        case nodeId = "node_id"
    }
}

/// `GET /api/nodes/<id>/tts-status`.
struct TTSStatus: Decodable, Sendable {
    var nodeId: Int?
    var status: TaskStatus?
    var progress: Int
    var taskInfo: JSONValue?
    var node: Node?

    struct Node: Decodable, Sendable {
        var id: Int
        var audioTTSURL: String?

        enum CodingKeys: String, CodingKey {
            case id
            case audioTTSURL = "audio_tts_url"
        }
    }

    enum CodingKeys: String, CodingKey {
        case status, progress, node
        case nodeId = "node_id"
        case taskInfo = "task_info"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodeId = c.tolerant(.nodeId)
        status = c.tolerant(.status)
        progress = c.tolerant(.progress, default: 0)
        taskInfo = c.tolerant(.taskInfo)
        node = c.tolerant(.node)
    }
}

extension TTSStatus: PollableStatus {
    var pollStatus: TaskStatus? { status }
}

/// `GET /api/nodes/<id>/audio` (200). A 202 answer means TTS is generating.
struct AudioURLs: Decodable, Sendable {
    var originalURL: String?
    var ttsURL: String?
    var hasAudioChunks: Bool
    /// Present on the 202 "generating" answer.
    var status: String?
    var progress: Int?

    enum CodingKeys: String, CodingKey {
        case status, progress
        case originalURL = "original_url"
        case ttsURL = "tts_url"
        case hasAudioChunks = "has_audio_chunks"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        originalURL = c.tolerant(.originalURL)
        ttsURL = c.tolerant(.ttsURL)
        hasAudioChunks = c.tolerant(.hasAudioChunks, default: false)
        status = c.tolerant(.status)
        progress = c.tolerant(.progress)
    }
}

/// `GET /api/nodes/<id>/audio-chunks`.
struct AudioChunksResponse: Decodable, Sendable {
    struct Chunk: Decodable, Hashable, Sendable {
        var url: String
        /// Seconds (ffprobe; 300.0 fallback on the server).
        var duration: Double
    }

    var chunks: [Chunk]
}

/// `GET /api/nodes/<id>/tts-chapters`.
struct TTSChaptersResponse: Decodable, Sendable {
    struct Chapter: Decodable, Hashable, Sendable {
        var sectionIndex: Int
        var title: String
        var chunkIndex: Int
        /// Cumulative seconds.
        var startTime: Double

        enum CodingKeys: String, CodingKey {
            case title
            case sectionIndex = "section_index"
            case chunkIndex = "chunk_index"
            case startTime = "start_time"
        }
    }

    var chapters: [Chapter]
}

/// `GET /api/nodes/<id>/transcription-status`.
struct TranscriptionStatus: Decodable, Sendable {
    var nodeId: Int?
    var status: TaskStatus?
    var progress: Int
    var error: String?
    var content: String?

    enum CodingKeys: String, CodingKey {
        case status, progress, error, content
        case nodeId = "node_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodeId = c.tolerant(.nodeId)
        status = c.tolerant(.status)
        progress = c.tolerant(.progress, default: 0)
        error = c.tolerant(.error)
        content = c.tolerant(.content)
    }
}

extension TranscriptionStatus: PollableStatus {
    var pollStatus: TaskStatus? { status }
}
