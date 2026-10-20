import Foundation

// Saved references (map E §4, §7.2): `backend/routes/external.py` `_serialize_item`.

/// One saved reference. The detail and feed-pick answers add `content`.
struct ExternalItem: Decodable, Identifiable, Equatable, Sendable {
    var id: Int
    var source: String
    var externalId: String?
    var authorHandle: String?
    /// The tweet author's display name when the archive has one (#435).
    var authorName: String?
    var title: String?
    var preview: String
    var url: String?
    var postedAt: Date?
    var fetchedAt: Date?
    var readAt: Date?
    var feedback: String?
    var feedbackAt: Date?
    var editedAt: Date?
    var hasTTS: Bool
    var surfacedCount: Int
    var lastSurfacedAt: Date?
    var content: String?
    /// Feed picks: the verdict came from another reply (#352).
    var feedbackShared: Bool

    enum CodingKeys: String, CodingKey {
        case id, source, title, preview, url, feedback, content
        case externalId = "external_id"
        case authorHandle = "author_handle"
        case authorName = "author_name"
        case postedAt = "posted_at"
        case fetchedAt = "fetched_at"
        case readAt = "read_at"
        case feedbackAt = "feedback_at"
        case editedAt = "edited_at"
        case hasTTS = "has_tts"
        case surfacedCount = "surfaced_count"
        case lastSurfacedAt = "last_surfaced_at"
        case feedbackShared = "feedback_shared"
    }

    init(id: Int, source: String, externalId: String? = nil, authorHandle: String? = nil, title: String? = nil,
         preview: String = "", url: String? = nil, postedAt: Date? = nil, fetchedAt: Date? = nil,
         readAt: Date? = nil, feedback: String? = nil, hasTTS: Bool = false, surfacedCount: Int = 0,
         lastSurfacedAt: Date? = nil, content: String? = nil, feedbackShared: Bool = false) {
        self.id = id
        self.source = source
        self.externalId = externalId
        self.authorHandle = authorHandle
        self.title = title
        self.preview = preview
        self.url = url
        self.postedAt = postedAt
        self.fetchedAt = fetchedAt
        self.readAt = readAt
        self.feedback = feedback
        self.hasTTS = hasTTS
        self.surfacedCount = surfacedCount
        self.lastSurfacedAt = lastSurfacedAt
        self.content = content
        self.feedbackShared = feedbackShared
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        source = c.tolerant(.source, default: "")
        externalId = c.tolerant(.externalId) ?? c.flexibleInt(.externalId).map(String.init)
        authorHandle = c.tolerant(.authorHandle)
        authorName = c.tolerant(.authorName)
        title = c.tolerant(.title)
        preview = c.tolerant(.preview, default: "")
        url = c.tolerant(.url)
        postedAt = c.tolerant(.postedAt)
        fetchedAt = c.tolerant(.fetchedAt)
        readAt = c.tolerant(.readAt)
        feedback = c.tolerant(.feedback)
        feedbackAt = c.tolerant(.feedbackAt)
        editedAt = c.tolerant(.editedAt)
        hasTTS = c.tolerant(.hasTTS, default: false)
        surfacedCount = c.flexibleInt(.surfacedCount) ?? 0
        lastSurfacedAt = c.tolerant(.lastSurfacedAt)
        content = c.tolerant(.content)
        feedbackShared = c.tolerant(.feedbackShared, default: false)
    }
}

/// `GET /api/external/items?sort=saved&page=&per_page=`.
struct ExternalItemsPage: Decodable, Sendable {
    var items: [ExternalItem]
    var total: Int
    var hasMore: Bool
    /// Saved items per source (`community_archive`, `twitter_bookmark`, `web_clip`).
    var counts: [String: Int]

    enum CodingKeys: String, CodingKey {
        case items, total, counts
        case hasMore = "has_more"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = c.tolerant(.items, default: [])
        total = c.tolerant(.total, default: 0)
        hasMore = c.tolerant(.hasMore, default: false)
        counts = c.tolerant(.counts, default: [:])
    }
}

/// `{id, read_at}` from the read mark routes.
struct ReadMarkAnswer: Decodable, Sendable {
    var readAt: Date?

    enum CodingKeys: String, CodingKey { case readAt = "read_at" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        readAt = c.tolerant(.readAt)
    }
}

/// `DELETE /api/external/items/<id>`.
struct DeleteReferenceAnswer: Decodable, Sendable {
    var deleted: Bool?
    var keptAsPick: Bool?

    enum CodingKeys: String, CodingKey {
        case deleted
        case keptAsPick = "kept_as_pick"
    }
}

/// `GET /api/nodes/<id>/feed-picks` (legacy Read replies, map E §4.5).
struct FeedPicksAnswer: Decodable, Sendable {
    struct Pick: Decodable, Identifiable, Sendable {
        var rank: Int
        var relevance: Int?
        var recommended: Bool
        var pickedBy: String?
        var why: String?
        var item: ExternalItem

        var id: Int { item.id }

        enum CodingKeys: String, CodingKey {
            case rank, relevance, recommended, why, item
            case pickedBy = "picked_by"
        }

        init(rank: Int, relevance: Int?, recommended: Bool, pickedBy: String?, why: String?, item: ExternalItem) {
            self.rank = rank
            self.relevance = relevance
            self.recommended = recommended
            self.pickedBy = pickedBy
            self.why = why
            self.item = item
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            rank = c.flexibleInt(.rank) ?? 0
            relevance = c.flexibleInt(.relevance)
            recommended = c.tolerant(.recommended, default: false)
            pickedBy = c.tolerant(.pickedBy)
            why = c.tolerant(.why)
            item = try c.decode(ExternalItem.self, forKey: .item)
        }
    }

    var picks: [Pick]

    init(picks: [Pick]) { self.picks = picks }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        picks = c.tolerant(AnyCodingKey("picks"), default: [])
    }
}

/// `POST /api/nodes/<id>/feed-picks/read` → `{node_id, read_at: {item_id: iso}}`.
struct FeedPicksReadAnswer: Decodable, Sendable {
    var readAt: [Int: Date]

    init(readAt: [Int: Date]) { self.readAt = readAt }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let map: IntKeyedMap<Date> = c.tolerant(AnyCodingKey("read_at"), default: IntKeyedMap())
        var out: [Int: Date] = [:]
        for (id, value) in map.values { if let value { out[id] = value } }
        readAt = out
    }
}

// MARK: - Import page: external sources and tokens

/// `GET /api/external/twitter/status`.
struct TwitterSyncStatus: Decodable, Equatable, Sendable {
    var configured: Bool
    var connected: Bool
    var revoked: Bool
    var handle: String?
    var lastSyncedAt: Date?
    var lastSyncCreated: Int?

    enum CodingKeys: String, CodingKey {
        case configured, connected, revoked, handle
        case lastSyncedAt = "last_synced_at"
        case lastSyncCreated = "last_sync_created"
    }

    init(configured: Bool = false, connected: Bool = false, revoked: Bool = false, handle: String? = nil,
         lastSyncedAt: Date? = nil, lastSyncCreated: Int? = nil) {
        self.configured = configured
        self.connected = connected
        self.revoked = revoked
        self.handle = handle
        self.lastSyncedAt = lastSyncedAt
        self.lastSyncCreated = lastSyncCreated
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        configured = c.tolerant(.configured, default: false)
        connected = c.tolerant(.connected, default: false)
        revoked = c.tolerant(.revoked, default: false)
        handle = c.tolerant(.handle)
        lastSyncedAt = c.tolerant(.lastSyncedAt)
        lastSyncCreated = c.flexibleInt(.lastSyncCreated)
    }
}

/// A personal API token row (Chrome clipper); `token` only in the create answer.
struct APITokenRow: Decodable, Identifiable, Equatable, Sendable {
    var id: Int
    var name: String
    var prefix: String
    var scope: String?
    var createdAt: Date?
    var lastUsedAt: Date?
    var token: String?

    enum CodingKeys: String, CodingKey {
        case id, name, prefix, scope, token
        case createdAt = "created_at"
        case lastUsedAt = "last_used_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        name = c.tolerant(.name, default: "")
        prefix = c.tolerant(.prefix, default: "")
        scope = c.tolerant(.scope)
        createdAt = c.tolerant(.createdAt)
        lastUsedAt = c.tolerant(.lastUsedAt)
        token = c.tolerant(.token)
    }
}

struct APITokenList: Decodable, Sendable {
    var tokens: [APITokenRow]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        tokens = c.tolerant(AnyCodingKey("tokens"), default: [])
    }
}
