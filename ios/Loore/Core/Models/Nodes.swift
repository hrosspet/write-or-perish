import Foundation

// Node wire shapes (map B §2.2.0). The backend has no shared serializer, so
// each shape is its own struct instead of one struct with everything optional.

/// Author of the focal node: `user { id, username }`. For an AI reply this is
/// the model's pseudo-user (username == model id).
struct NodeAuthor: Decodable, Hashable, Sendable {
    var id: Int
    var username: String
}

/// `_system_prompt_fields`, present on the focal node, ancestors and children.
struct SystemPromptFields: Decodable, Hashable, Sendable {
    var isSystemPrompt: Bool
    var promptTitle: String?
    var promptKey: String?
    var userPromptId: Int?
    var promptVersionNumber: Int?
    var contextArtifacts: ContextArtifacts?

    enum CodingKeys: String, CodingKey {
        case isSystemPrompt = "is_system_prompt"
        case promptTitle = "prompt_title"
        case promptKey = "prompt_key"
        case userPromptId = "user_prompt_id"
        case promptVersionNumber = "prompt_version_number"
        case contextArtifacts = "context_artifacts"
    }

    init(isSystemPrompt: Bool = false) {
        self.isSystemPrompt = isSystemPrompt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        isSystemPrompt = c.tolerant(.isSystemPrompt, default: false)
        promptTitle = c.tolerant(.promptTitle)
        promptKey = c.tolerant(.promptKey)
        userPromptId = c.tolerant(.userPromptId)
        promptVersionNumber = c.tolerant(.promptVersionNumber)
        contextArtifacts = c.tolerant(.contextArtifacts)
    }
}

/// The texts that were substituted into a system prompt's placeholders, so the
/// client can show the prompt as the model saw it (web `QuotedContent`).
struct ContextArtifacts: Decodable, Hashable, Sendable {
    struct PromptRef: Decodable, Hashable, Sendable {
        var id: Int?
        var title: String?
        var versionNumber: Int?
        var promptKey: String?

        enum CodingKeys: String, CodingKey {
            case id, title
            case versionNumber = "version_number"
            case promptKey = "prompt_key"
        }
    }

    struct VersionedText: Decodable, Hashable, Sendable {
        var id: Int?
        var versionNumber: Int?
        var content: String

        enum CodingKeys: String, CodingKey {
            case id, content
            case versionNumber = "version_number"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = c.tolerant(.id)
            versionNumber = c.tolerant(.versionNumber)
            content = c.tolerant(.content, default: "")
        }
    }

    struct RecentRaw: Decodable, Hashable, Sendable {
        var coversStart: String?
        var coversEnd: String?
        var sourceTokens: Int?

        enum CodingKeys: String, CodingKey {
            case coversStart = "covers_start"
            case coversEnd = "covers_end"
            case sourceTokens = "source_tokens"
        }
    }

    var prompt: PromptRef?
    var profile: VersionedText?
    var todo: VersionedText?
    var recent: VersionedText?
    /// Inline artifacts keyed by kind: memory, scratchpad, ai_preferences, intentions (and any new kind).
    var artifacts: [String: VersionedText]
    var shareGuidance: String?
    var externalContentGuidance: String?
    var recentRaw: RecentRaw?

    private static let fixedKeys: Set<String> = [
        "prompt", "profile", "todo", "recent", "share_guidance", "external_content_guidance", "recent_raw",
    ]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        prompt = c.tolerant(AnyCodingKey("prompt"))
        profile = c.tolerant(AnyCodingKey("profile"))
        todo = c.tolerant(AnyCodingKey("todo"))
        recent = c.tolerant(AnyCodingKey("recent"))
        recentRaw = c.tolerant(AnyCodingKey("recent_raw"))
        struct ContentOnly: Decodable { var content: String? }
        shareGuidance = (c.tolerant(AnyCodingKey("share_guidance")) as ContentOnly?)?.content
        externalContentGuidance = (c.tolerant(AnyCodingKey("external_content_guidance")) as ContentOnly?)?.content
        var kinds: [String: VersionedText] = [:]
        for key in c.allKeys where !Self.fixedKeys.contains(key.stringValue) {
            if let value: VersionedText = c.tolerant(key) {
                kinds[key.stringValue] = value
            }
        }
        artifacts = kinds
    }
}

/// One entry of `tool_calls_meta` (loosely typed on the server).
/// Names starting with `_` are internal markers the UI hides (`_batch`, `_read`, `_feed_sample`, `_mode`).
struct ToolCallMeta: Decodable, Hashable, Sendable {
    var name: String
    var raw: [String: JSONValue]

    init(name: String, raw: [String: JSONValue] = [:]) {
        self.name = name
        self.raw = raw
    }

    init(from decoder: Decoder) throws {
        let object = try [String: JSONValue](from: decoder)
        raw = object
        name = object["name"]?.stringValue ?? ""
    }

    var isInternal: Bool { name.hasPrefix("_") }
    var status: String? { raw["status"]?.stringValue }
    /// "started" | "completed" | "failed" for proposal apply flows.
    var applyStatus: String? { raw["apply_status"]?.stringValue }
    var applyError: String? { raw["apply_error"]?.stringValue }
    subscript(key: String) -> JSONValue? { raw[key] }
}

/// `read_window` on Community Archive read replies (admin PoC).
struct ReadWindow: Decodable, Hashable, Sendable {
    var exportId: Int?
    var days: Int?
    var scope: String?
    var windowStart: Date?
    var windowEnd: Date?
    var tweets: Int?
    var accounts: Int?
    var excluded: Int?

    enum CodingKeys: String, CodingKey {
        case days, scope, tweets, accounts, excluded
        case exportId = "export_id"
        case windowStart = "window_start"
        case windowEnd = "window_end"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        exportId = c.flexibleInt(.exportId)
        days = c.flexibleInt(.days)
        scope = c.tolerant(.scope)
        windowStart = c.tolerant(.windowStart)
        windowEnd = c.tolerant(.windowEnd)
        tweets = c.flexibleInt(.tweets)
        accounts = c.flexibleInt(.accounts)
        excluded = c.flexibleInt(.excluded)
    }
}

/// Shape A: the focal node of `GET /api/nodes/<id>` (with `ancestors` and the whole
/// `children` subtree), and `node` in `PUT /api/nodes/<id>` (tree keys absent → empty).
struct NodeDetail: Decodable, Identifiable, Sendable {
    var id: Int
    var content: String
    var nodeType: NodeType
    var createdAt: Date?
    var updatedAt: Date?
    /// "/@<owner>/<slug>" while the node is public and has a slug.
    var permalink: String?
    var user: NodeAuthor?
    var parentUserId: Int?
    var privacyLevel: PrivacyLevel
    var aiUsage: AIUsage
    var pinnedAt: Date?
    var llmModel: String?
    /// nil = written in Loore; "twitter" | "chatgpt" | "claude" | "markdown" for imports.
    var origin: String?
    var llmTaskStatus: TaskStatus?
    var hasOriginalAudio: Bool
    var hasTTS: Bool
    /// Partial reply text while `llmTaskStatus` is pending/processing (#367).
    var streamingContent: String?
    var toolCallsMeta: [ToolCallMeta]?
    var feedPicksCount: Int?
    var systemPrompt: SystemPromptFields
    var readReply: Bool
    var readWindow: ReadWindow?

    // Tree keys (GET only)
    var childCount: Int
    /// Root first, direct parent last. Privacy-blocked ancestors are omitted.
    var ancestors: [AncestorNode]
    /// Sorted by `descendant_count` descending by the server.
    var children: [TreeNode]
    var inReadThread: Bool
    var readReplyAbove: Bool
    /// The `ai_usage` a new reply under this node should default to (#362).
    var replyAIUsage: AIUsage?

    enum CodingKeys: String, CodingKey {
        case id, content, permalink, user, origin, ancestors, children
        case nodeType = "node_type"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case parentUserId = "parent_user_id"
        case privacyLevel = "privacy_level"
        case aiUsage = "ai_usage"
        case pinnedAt = "pinned_at"
        case llmModel = "llm_model"
        case llmTaskStatus = "llm_task_status"
        case hasOriginalAudio = "has_original_audio"
        case hasTTS = "has_tts"
        case streamingContent = "streaming_content"
        case toolCallsMeta = "tool_calls_meta"
        case feedPicksCount = "feed_picks_count"
        case readReply = "read_reply"
        case readWindow = "read_window"
        case childCount = "child_count"
        case inReadThread = "in_read_thread"
        case readReplyAbove = "read_reply_above"
        case replyAIUsage = "reply_ai_usage"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        content = c.tolerant(.content, default: "")
        nodeType = c.tolerant(.nodeType, default: .user)
        createdAt = c.tolerant(.createdAt)
        updatedAt = c.tolerant(.updatedAt)
        permalink = c.tolerant(.permalink)
        user = c.tolerant(.user)
        parentUserId = c.tolerant(.parentUserId)
        privacyLevel = c.tolerant(.privacyLevel, default: .private)
        aiUsage = c.tolerant(.aiUsage, default: .off)
        pinnedAt = c.tolerant(.pinnedAt)
        llmModel = c.tolerant(.llmModel)
        origin = c.tolerant(.origin)
        llmTaskStatus = c.tolerant(.llmTaskStatus)
        hasOriginalAudio = c.tolerant(.hasOriginalAudio, default: false)
        hasTTS = c.tolerant(.hasTTS, default: false)
        streamingContent = c.tolerant(.streamingContent)
        toolCallsMeta = c.tolerant(.toolCallsMeta)
        feedPicksCount = c.tolerant(.feedPicksCount)
        systemPrompt = (try? SystemPromptFields(from: decoder)) ?? SystemPromptFields()
        readReply = c.tolerant(.readReply, default: false)
        readWindow = c.tolerant(.readWindow)
        childCount = c.tolerant(.childCount, default: 0)
        ancestors = c.tolerant(.ancestors, default: [])
        children = c.tolerant(.children, default: [])
        inReadThread = c.tolerant(.inReadThread, default: false)
        readReplyAbove = c.tolerant(.readReplyAbove, default: false)
        replyAIUsage = c.tolerant(.replyAIUsage)
    }

    var isLLM: Bool { nodeType == .llm }
    var authorUsername: String { user?.username ?? "" }
}

/// Shape B: an element of `ancestors`. Alive ancestors carry content; soft-deleted
/// ones the viewer could see come as tombstones (`deleted == true`, no content).
struct AncestorNode: Decodable, Identifiable, Hashable, Sendable {
    var id: Int
    var deleted: Bool
    var deletedAt: Date?
    var username: String?
    var llmModel: String?
    var content: String?
    var preview: String?
    var nodeType: NodeType
    /// ALL child rows, including deleted and private ones.
    var childCount: Int
    var createdAt: Date?
    var userId: Int?
    var parentUserId: Int?
    var aiUsage: AIUsage?
    var privacyLevel: PrivacyLevel?
    var systemPrompt: SystemPromptFields

    enum CodingKeys: String, CodingKey {
        case id, deleted, username, content, preview
        case deletedAt = "deleted_at"
        case llmModel = "llm_model"
        case nodeType = "node_type"
        case childCount = "child_count"
        case createdAt = "created_at"
        case userId = "user_id"
        case parentUserId = "parent_user_id"
        case aiUsage = "ai_usage"
        case privacyLevel = "privacy_level"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        deleted = c.tolerant(.deleted, default: false)
        deletedAt = c.tolerant(.deletedAt)
        username = c.tolerant(.username)
        llmModel = c.tolerant(.llmModel)
        content = c.tolerant(.content)
        preview = c.tolerant(.preview)
        nodeType = c.tolerant(.nodeType, default: .user)
        childCount = c.tolerant(.childCount, default: 0)
        createdAt = c.tolerant(.createdAt)
        userId = c.tolerant(.userId)
        parentUserId = c.tolerant(.parentUserId)
        aiUsage = c.tolerant(.aiUsage)
        privacyLevel = c.tolerant(.privacyLevel)
        systemPrompt = (try? SystemPromptFields(from: decoder)) ?? SystemPromptFields()
    }
}

/// Shape C: an element of `children`, recursive. Three variants share the struct:
/// a full node, a tombstone (`deleted`, kept because it has visible descendants),
/// and an inaccessible stub (`inaccessible`, only `id`).
struct TreeNode: Decodable, Identifiable, Hashable, Sendable {
    var id: Int
    var deleted: Bool
    var inaccessible: Bool
    var deletedAt: Date?
    var content: String?
    var nodeType: NodeType
    /// Visible children.
    var childCount: Int
    var createdAt: Date?
    var updatedAt: Date?
    var username: String?
    var llmModel: String?
    var origin: String?
    var descendantCount: Int
    var userId: Int?
    var parentUserId: Int?
    var children: [TreeNode]
    var hasTTS: Bool
    var privacyLevel: PrivacyLevel?
    var aiUsage: AIUsage?
    var systemPrompt: SystemPromptFields

    enum CodingKeys: String, CodingKey {
        case id, deleted, inaccessible, content, username, origin, children
        case deletedAt = "deleted_at"
        case nodeType = "node_type"
        case childCount = "child_count"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case llmModel = "llm_model"
        case descendantCount = "descendant_count"
        case userId = "user_id"
        case parentUserId = "parent_user_id"
        case hasTTS = "has_tts"
        case privacyLevel = "privacy_level"
        case aiUsage = "ai_usage"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        deleted = c.tolerant(.deleted, default: false)
        inaccessible = c.tolerant(.inaccessible, default: false)
        deletedAt = c.tolerant(.deletedAt)
        content = c.tolerant(.content)
        nodeType = c.tolerant(.nodeType, default: .user)
        childCount = c.tolerant(.childCount, default: 0)
        createdAt = c.tolerant(.createdAt)
        updatedAt = c.tolerant(.updatedAt)
        username = c.tolerant(.username)
        llmModel = c.tolerant(.llmModel)
        origin = c.tolerant(.origin)
        descendantCount = c.tolerant(.descendantCount, default: 0)
        userId = c.tolerant(.userId)
        parentUserId = c.tolerant(.parentUserId)
        children = c.tolerant(.children, default: [])
        hasTTS = c.tolerant(.hasTTS, default: false)
        privacyLevel = c.tolerant(.privacyLevel)
        aiUsage = c.tolerant(.aiUsage)
        systemPrompt = (try? SystemPromptFields(from: decoder)) ?? SystemPromptFields()
    }
}

/// `PUT /api/nodes/<id>` answer.
struct NodeUpdateResponse: Decodable, Sendable {
    var message: String?
    var node: NodeDetail
    var descendantsUpdated: Int

    enum CodingKeys: String, CodingKey {
        case message, node
        case descendantsUpdated = "descendants_updated"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        message = c.tolerant(.message)
        node = try c.decode(NodeDetail.self, forKey: .node)
        descendantsUpdated = c.tolerant(.descendantsUpdated, default: 0)
    }
}

/// `POST /api/nodes/` (JSON mode) answer. `tipId` is the node to open when a long
/// entry was split into a chain (`splitInto` > 1).
struct NodeCreateResponse: Decodable, Sendable {
    var id: Int
    var content: String?
    var nodeType: NodeType?
    var parentId: Int?
    var createdAt: Date?
    var username: String?
    var privacyLevel: PrivacyLevel?
    var aiUsage: AIUsage?
    var permalink: String?
    var splitInto: Int?
    var tipId: Int?

    enum CodingKeys: String, CodingKey {
        case id, content, username, permalink
        case nodeType = "node_type"
        case parentId = "parent_id"
        case createdAt = "created_at"
        case privacyLevel = "privacy_level"
        case aiUsage = "ai_usage"
        case splitInto = "split_into"
        case tipId = "tip_id"
    }
}

/// `DELETE /api/nodes/<id>` answer.
struct NodeDeleteResponse: Decodable, Sendable {
    var scheduled: Int?
    var graceDays: Int?
    var orphanedPromptDeleted: Int?
    var deletedPinnedIds: [Int]

    enum CodingKeys: String, CodingKey {
        case scheduled
        case graceDays = "grace_days"
        case orphanedPromptDeleted = "orphaned_prompt_deleted"
        case deletedPinnedIds = "deleted_pinned_ids"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        scheduled = c.tolerant(.scheduled)
        graceDays = c.tolerant(.graceDays)
        orphanedPromptDeleted = c.tolerant(.orphanedPromptDeleted)
        deletedPinnedIds = c.tolerant(.deletedPinnedIds, default: [])
    }
}

/// `GET /api/nodes/<id>/delete-impact` answer.
struct DeleteImpactResponse: Decodable, Sendable {
    var orphanedSystemPromptId: Int?

    enum CodingKeys: String, CodingKey {
        case orphanedSystemPromptId = "orphaned_system_prompt_id"
    }
}

/// `PUT /api/nodes/<root>/thread-name` answer.
struct ThreadNameResponse: Decodable, Sendable {
    var threadName: String?

    enum CodingKeys: String, CodingKey { case threadName = "thread_name" }
}

/// `POST /api/nodes/<id>/pin` answer.
struct PinResponse: Decodable, Sendable {
    var message: String?
    var pinnedAt: Date?

    enum CodingKeys: String, CodingKey {
        case message
        case pinnedAt = "pinned_at"
    }
}

/// `GET /api/nodes/titles?ids=` answer: `null` = missing or not visible.
struct NodeTitlesResponse: Decodable, Sendable {
    struct Title: Decodable, Hashable, Sendable {
        var id: Int
        var title: String?
        var deleted: Bool

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(Int.self, forKey: .id)
            title = c.tolerant(.title)
            deleted = c.tolerant(.deleted, default: false)
        }

        enum CodingKeys: String, CodingKey { case id, title, deleted }
    }

    var titles: IntKeyedMap<Title>

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        titles = c.tolerant(.titles, default: IntKeyedMap())
    }

    enum CodingKeys: String, CodingKey { case titles }
}

/// `GET /api/nodes/<id>/resolve-quotes` answer.
struct ResolvedQuotes: Decodable, Sendable {
    struct QuotedNode: Decodable, Hashable, Sendable {
        var id: Int
        var deleted: Bool
        var content: String?
        var username: String?
        var userId: Int?
        var createdAt: Date?
        var nodeType: NodeType?
        var aiUsage: AIUsage?

        enum CodingKeys: String, CodingKey {
            case id, deleted, content, username
            case userId = "user_id"
            case createdAt = "created_at"
            case nodeType = "node_type"
            case aiUsage = "ai_usage"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(Int.self, forKey: .id)
            deleted = c.tolerant(.deleted, default: false)
            content = c.tolerant(.content)
            username = c.tolerant(.username)
            userId = c.tolerant(.userId)
            createdAt = c.tolerant(.createdAt)
            nodeType = c.tolerant(.nodeType)
            aiUsage = c.tolerant(.aiUsage)
        }
    }

    struct QuotedExternal: Decodable, Hashable, Sendable {
        var id: Int
        var content: String
        var source: String?
        var authorHandle: String?
        var title: String?
        var url: String?
        var postedAt: Date?
        var userId: Int?
        var readAt: Date?
        var feedback: String?
        var feedbackShared: Bool?
        var recommendationId: Int?
        var ratedBefore: JSONValue?

        enum CodingKeys: String, CodingKey {
            case id, content, source, title, url, feedback
            case authorHandle = "author_handle"
            case postedAt = "posted_at"
            case userId = "user_id"
            case readAt = "read_at"
            case feedbackShared = "feedback_shared"
            case recommendationId = "recommendation_id"
            case ratedBefore = "rated_before"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(Int.self, forKey: .id)
            content = c.tolerant(.content, default: "")
            source = c.tolerant(.source)
            authorHandle = c.tolerant(.authorHandle)
            title = c.tolerant(.title)
            url = c.tolerant(.url)
            postedAt = c.tolerant(.postedAt)
            userId = c.tolerant(.userId)
            readAt = c.tolerant(.readAt)
            feedback = c.tolerant(.feedback)
            feedbackShared = c.tolerant(.feedbackShared)
            recommendationId = c.tolerant(.recommendationId)
            ratedBefore = c.tolerant(.ratedBefore)
        }
    }

    var quotes: IntKeyedMap<QuotedNode>
    var externalQuotes: IntKeyedMap<QuotedExternal>
    var hasQuotes: Bool

    enum CodingKeys: String, CodingKey {
        case quotes
        case externalQuotes = "external_quotes"
        case hasQuotes = "has_quotes"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        quotes = c.tolerant(.quotes, default: IntKeyedMap())
        externalQuotes = c.tolerant(.externalQuotes, default: IntKeyedMap())
        hasQuotes = c.tolerant(.hasQuotes, default: false)
    }
}

/// `GET /api/log` answer (map B §2.4.7). Pass `nextCursor` back unchanged (URL-encoded).
struct LogPage: Decodable, Sendable {
    var nodes: [LogCard]
    var hasMore: Bool
    var nextCursor: String?
    var page: Int?

    enum CodingKeys: String, CodingKey {
        case nodes, page
        case hasMore = "has_more"
        case nextCursor = "next_cursor"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodes = c.tolerant(.nodes, default: [])
        hasMore = c.tolerant(.hasMore, default: false)
        nextCursor = c.tolerant(.nextCursor)
        page = c.tolerant(.page)
    }
}

/// A Log card. `id` is the DISPLAY node (open it); rename/delete target `threadRootId`.
struct LogCard: Decodable, Identifiable, Hashable, Sendable {
    var id: Int
    var threadRootId: Int
    var newestNodeId: Int?
    var threadName: String?
    var canRename: Bool
    var preview: String
    var nodeType: NodeType
    var childCount: Int
    var createdAt: Date?
    var pinnedAt: Date?
    var username: String
    var humanOwnerUsername: String?
    var llmModel: String?
    var origin: String?
    var hasOriginalAudio: Bool
    var promptKey: String?

    enum CodingKeys: String, CodingKey {
        case id, preview, username, origin
        case threadRootId = "thread_root_id"
        case newestNodeId = "newest_node_id"
        case threadName = "thread_name"
        case canRename = "can_rename"
        case nodeType = "node_type"
        case childCount = "child_count"
        case createdAt = "created_at"
        case pinnedAt = "pinned_at"
        case humanOwnerUsername = "human_owner_username"
        case llmModel = "llm_model"
        case hasOriginalAudio = "has_original_audio"
        case promptKey = "prompt_key"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        threadRootId = c.tolerant(.threadRootId, default: id)
        newestNodeId = c.tolerant(.newestNodeId)
        threadName = c.tolerant(.threadName)
        canRename = c.tolerant(.canRename, default: true)
        preview = c.tolerant(.preview, default: "")
        nodeType = c.tolerant(.nodeType, default: .user)
        childCount = c.tolerant(.childCount, default: 0)
        createdAt = c.tolerant(.createdAt)
        pinnedAt = c.tolerant(.pinnedAt)
        username = c.tolerant(.username, default: "")
        humanOwnerUsername = c.tolerant(.humanOwnerUsername)
        llmModel = c.tolerant(.llmModel)
        origin = c.tolerant(.origin)
        hasOriginalAudio = c.tolerant(.hasOriginalAudio, default: false)
        promptKey = c.tolerant(.promptKey)
    }
}

/// `GET /api/nodes/models`.
struct ModelsResponse: Decodable, Sendable {
    var models: [ModelInfo]
}

struct ModelInfo: Decodable, Identifiable, Hashable, Sendable {
    var id: String
    var name: String
    /// "openai" | "anthropic"
    var provider: String
    var featured: Bool
    /// May be used for a Read.
    var read: Bool
    /// May be used for anything other than a Read (a reply, the account default).
    /// False for a read-only model; a server without the field means true.
    var chat: Bool

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = c.tolerant(.name, default: id)
        provider = c.tolerant(.provider, default: "")
        featured = c.tolerant(.featured, default: false)
        read = c.tolerant(.read, default: false)
        chat = c.tolerant(.chat, default: true)
    }

    enum CodingKeys: String, CodingKey { case id, name, provider, featured, read, chat }
}

/// `GET /api/nodes/default-model` and `/api/nodes/<id>/suggested-model`.
struct SuggestedModel: Decodable, Sendable {
    var suggestedModel: String?
    /// "predecessor" | "user_preference" | "default"
    var source: String?

    enum CodingKeys: String, CodingKey {
        case source
        case suggestedModel = "suggested_model"
    }
}
