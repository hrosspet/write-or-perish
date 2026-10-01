import Foundation

// Share and Commons (map E §8; `backend/routes/share.py`, `commons.py`).

/// A share draft / published / revoked piece (`GET /api/share`).
struct ShareItem: Decodable, Identifiable, Equatable, Sendable {
    var id: Int
    var content: String
    var shareType: String
    var status: String
    var sourceNodeId: Int?
    var publicNodeId: Int?
    var permalink: String?
    var createdAt: Date?
    var updatedAt: Date?
    var publishedAt: Date?
    var revokedAt: Date?

    enum CodingKeys: String, CodingKey {
        case id, content, status, permalink
        case shareType = "share_type"
        case sourceNodeId = "source_node_id"
        case publicNodeId = "public_node_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case publishedAt = "published_at"
        case revokedAt = "revoked_at"
    }

    init(id: Int, content: String, shareType: String = "other", status: String = "draft",
         publicNodeId: Int? = nil, permalink: String? = nil, createdAt: Date? = nil,
         publishedAt: Date? = nil, revokedAt: Date? = nil) {
        self.id = id
        self.content = content
        self.shareType = shareType
        self.status = status
        self.publicNodeId = publicNodeId
        self.permalink = permalink
        self.createdAt = createdAt
        self.publishedAt = publishedAt
        self.revokedAt = revokedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        content = c.tolerant(.content, default: "")
        shareType = c.tolerant(.shareType, default: "other")
        status = c.tolerant(.status, default: "draft")
        sourceNodeId = c.tolerant(.sourceNodeId)
        publicNodeId = c.tolerant(.publicNodeId)
        permalink = c.tolerant(.permalink)
        createdAt = c.tolerant(.createdAt)
        updatedAt = c.tolerant(.updatedAt)
        publishedAt = c.tolerant(.publishedAt)
        revokedAt = c.tolerant(.revokedAt)
    }
}

struct ShareList: Decodable, Sendable {
    var shares: [ShareItem]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        shares = c.tolerant(AnyCodingKey("shares"), default: [])
    }
}

/// One public root in `GET /api/commons/feed?page=N`.
struct CommonsItem: Decodable, Identifiable, Equatable, Sendable {
    var id: Int
    var username: String
    var permalink: String?
    var content: String
    var createdAt: Date?
    var replyCount: Int

    enum CodingKeys: String, CodingKey {
        case id, username, permalink, content
        case createdAt = "created_at"
        case replyCount = "reply_count"
    }

    init(id: Int, username: String, permalink: String? = nil, content: String, createdAt: Date? = nil,
         replyCount: Int = 0) {
        self.id = id
        self.username = username
        self.permalink = permalink
        self.content = content
        self.createdAt = createdAt
        self.replyCount = replyCount
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        username = c.tolerant(.username, default: "")
        permalink = c.tolerant(.permalink)
        content = c.tolerant(.content, default: "")
        createdAt = c.tolerant(.createdAt)
        replyCount = c.flexibleInt(.replyCount) ?? 0
    }
}

struct CommonsPage: Decodable, Sendable {
    var items: [CommonsItem]
    var hasMore: Bool
    var page: Int

    enum CodingKeys: String, CodingKey {
        case items, page
        case hasMore = "has_more"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = c.tolerant(.items, default: [])
        hasMore = c.tolerant(.hasMore, default: false)
        page = c.tolerant(.page, default: 1)
    }
}
