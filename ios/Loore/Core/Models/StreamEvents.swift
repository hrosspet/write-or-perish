import Foundation

// Typed payloads of the SSE streams (map B §2.3.6–2.3.7). Decode them from an
// `SSEEvent` with `event.decode(_:)`; the event name picks the type.

/// `GET /api/sse/nodes/<id>/llm-stream`.
enum LLMStreamEvent: Sendable, Equatable {
    /// The whole text so far: replace what is shown.
    case snapshot(String)
    /// Text appended since the last event.
    case delta(String)
    /// Generation ended. Read the final content from the node, not from deltas.
    case done(status: TaskStatus?, continuationNodeId: Int?, error: String?)
    case error(String)
    case heartbeat
    case close
    case other(String)

    init(_ event: SSEEvent) {
        struct Text: Decodable { var text: String? }
        struct Done: Decodable {
            var status: TaskStatus?
            var continuation_node_id: Int?
            var error: String?
        }
        struct Err: Decodable { var error: String?; var message: String? }
        switch event.event {
        case "snapshot": self = .snapshot((try? event.decode(Text.self))?.text ?? "")
        case "delta": self = .delta((try? event.decode(Text.self))?.text ?? "")
        case "done":
            let d = try? event.decode(Done.self)
            self = .done(status: d?.status, continuationNodeId: d?.continuation_node_id, error: d?.error)
        case "error":
            let e = try? event.decode(Err.self)
            self = .error(e?.error ?? e?.message ?? "Stream error")
        case "heartbeat": self = .heartbeat
        case "close": self = .close
        default: self = .other(event.event)
        }
    }
}

/// `GET /api/sse/{nodes|profiles|items}/<id>/tts-stream`.
enum TTSStreamEvent: Sendable, Equatable {
    struct Chunk: Decodable, Sendable, Equatable {
        var chunkIndex: Int
        /// Relative `/media/...` path (cache-busting query included).
        var audioURL: String
        var duration: Double?
        var sectionIndex: Int?
        var sectionTitle: String?

        enum CodingKeys: String, CodingKey {
            case duration
            case chunkIndex = "chunk_index"
            case audioURL = "audio_url"
            case sectionIndex = "section_index"
            case sectionTitle = "section_title"
        }
    }

    struct Complete: Decodable, Sendable, Equatable {
        var ttsURL: String?
        var continuationNodeId: Int?
        var preview: String?

        enum CodingKeys: String, CodingKey {
            case preview
            case ttsURL = "tts_url"
            case continuationNodeId = "continuation_node_id"
        }
    }

    case chunkReady(Chunk)
    case allComplete(Complete)
    case error(String)
    case heartbeat
    case close
    case other(String)

    init(_ event: SSEEvent) {
        struct Err: Decodable { var error: String?; var message: String? }
        switch event.event {
        case "chunk_ready":
            if let chunk = try? event.decode(Chunk.self) {
                self = .chunkReady(chunk)
            } else {
                self = .other(event.event)
            }
        case "all_complete":
            self = .allComplete((try? event.decode(Complete.self)) ?? Complete())
        case "error":
            let e = try? event.decode(Err.self)
            self = .error(e?.message ?? e?.error ?? "TTS generation failed")
        case "heartbeat": self = .heartbeat
        case "close": self = .close
        default: self = .other(event.event)
        }
    }
}

/// `GET /api/sse/drafts/<session_id>/transcription-stream`.
enum TranscriptionStreamEvent: Sendable, Equatable {
    struct AllComplete: Decodable, Sendable, Equatable {
        var content: String?
        var draftId: Int?
        var llmNodeId: Int?
        var warning: String?

        enum CodingKeys: String, CodingKey {
            case content, warning
            case draftId = "draft_id"
            case llmNodeId = "llm_node_id"
        }
    }

    case chunkComplete(chunkIndex: Int)
    case chunkError(chunkIndex: Int, error: String?)
    /// The whole assembled transcript: replace the text with it.
    case contentUpdate(content: String, completedChunks: Int)
    case allComplete(AllComplete)
    case error(String)
    case heartbeat
    case close
    case other(String)

    init(_ event: SSEEvent) {
        struct ChunkPayload: Decodable { var chunk_index: Int?; var error: String? }
        struct ContentPayload: Decodable { var content: String?; var completed_chunks: Int? }
        struct Err: Decodable { var error: String?; var message: String? }
        switch event.event {
        case "chunk_complete":
            self = .chunkComplete(chunkIndex: (try? event.decode(ChunkPayload.self))?.chunk_index ?? -1)
        case "chunk_error":
            let p = try? event.decode(ChunkPayload.self)
            self = .chunkError(chunkIndex: p?.chunk_index ?? -1, error: p?.error)
        case "content_update":
            let p = try? event.decode(ContentPayload.self)
            self = .contentUpdate(content: p?.content ?? "", completedChunks: p?.completed_chunks ?? 0)
        case "all_complete":
            self = .allComplete((try? event.decode(AllComplete.self)) ?? AllComplete())
        case "error":
            let e = try? event.decode(Err.self)
            self = .error(e?.error ?? e?.message ?? "Transcription failed")
        case "heartbeat": self = .heartbeat
        case "close": self = .close
        default: self = .other(event.event)
        }
    }
}
