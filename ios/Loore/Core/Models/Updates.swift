import Foundation

/// `GET /api/updates` (no trailing slash): the dev-update channel (map B §2.4.10, E §9).
struct UpdatesPayload: Decodable, Sendable {
    var changelog: [ChangelogSection]
    var notifications: [UpdateNotification]
    var polls: [PendingPoll]

    init(changelog: [ChangelogSection] = [], notifications: [UpdateNotification] = [], polls: [PendingPoll] = []) {
        self.changelog = changelog
        self.notifications = notifications
        self.polls = polls
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        changelog = c.tolerant(.changelog, default: [])
        notifications = c.tolerant(.notifications, default: [])
        polls = c.tolerant(.polls, default: [])
    }

    enum CodingKeys: String, CodingKey { case changelog, notifications, polls }

    var totalCount: Int { changelog.count + notifications.count + polls.count }
    var isEmpty: Bool { totalCount == 0 }
}

struct ChangelogSection: Decodable, Identifiable, Hashable, Sendable {
    /// Section slug from `backend/user_changelog.md`.
    var id: String
    var title: String
    /// Date-only string ("2026-09-28"); the web shows it verbatim.
    var date: String?
    /// Markdown; may contain in-app links such as `/account#model`.
    var body: String

    init(id: String, title: String, date: String?, body: String) {
        self.id = id
        self.title = title
        self.date = date
        self.body = body
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = c.tolerant(.title, default: "")
        date = c.tolerant(.date)
        body = c.tolerant(.body, default: "")
    }

    enum CodingKeys: String, CodingKey { case id, title, date, body }
}

struct UpdateNotification: Decodable, Identifiable, Hashable, Sendable {
    var id: Int
    /// Open set: "profile_ready", "briefing_ready", "fix_ready", "issue_declined", "x_disconnected", …
    var type: String
    var title: String
    var body: String?
    /// In-app path ("/profile") or absolute https URL.
    var link: String?
    var createdAt: Date?
    var meta: Meta?

    struct Meta: Decodable, Hashable, Sendable {
        var version: Int?
        var createdAt: Date?

        enum CodingKeys: String, CodingKey {
            case version
            case createdAt = "created_at"
        }

        init(version: Int?, createdAt: Date?) {
            self.version = version
            self.createdAt = createdAt
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            version = c.flexibleInt(.version)
            createdAt = c.tolerant(.createdAt)
        }
    }

    enum CodingKeys: String, CodingKey {
        case id, type, title, body, link, meta
        case createdAt = "created_at"
    }

    init(id: Int, type: String, title: String, body: String? = nil, link: String? = nil,
         createdAt: Date? = nil, meta: Meta? = nil) {
        self.id = id
        self.type = type
        self.title = title
        self.body = body
        self.link = link
        self.createdAt = createdAt
        self.meta = meta
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        type = c.tolerant(.type, default: "")
        title = c.tolerant(.title, default: "")
        body = c.tolerant(.body)
        link = c.tolerant(.link)
        createdAt = c.tolerant(.createdAt)
        meta = c.tolerant(.meta)
    }
}

struct PendingPoll: Decodable, Identifiable, Hashable, Sendable {
    var id: Int
    var question: String
    var createdAt: Date?
    var active: Bool?
    var draftTerms: DraftTerms?
    var response: PollResponse?

    struct DraftTerms: Decodable, Hashable, Sendable {
        /// Display name of the model that would draft the answer.
        var model: String?
        /// "derived" | "recent_window"
        var dataSource: String?

        enum CodingKeys: String, CodingKey {
            case model
            case dataSource = "data_source"
        }
    }

    enum CodingKeys: String, CodingKey {
        case id, question, active, response
        case createdAt = "created_at"
        case draftTerms = "draft_terms"
    }

    init(id: Int, question: String, createdAt: Date? = nil, active: Bool? = nil,
         draftTerms: DraftTerms? = nil, response: PollResponse? = nil) {
        self.id = id
        self.question = question
        self.createdAt = createdAt
        self.active = active
        self.draftTerms = draftTerms
        self.response = response
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        question = c.tolerant(.question, default: "")
        createdAt = c.tolerant(.createdAt)
        active = c.tolerant(.active)
        draftTerms = c.tolerant(.draftTerms)
        response = c.tolerant(.response)
    }
}

struct PollResponse: Decodable, Hashable, Sendable {
    /// "drafting" | "draft" | "draft_failed" | "sent" | "declined"
    var status: String
    var content: String
    /// Model id when the text is the AI draft verbatim.
    var generatedBy: String?
    var draftRequestedAt: Date?
    var sentAt: Date?

    enum CodingKeys: String, CodingKey {
        case status, content
        case generatedBy = "generated_by"
        case draftRequestedAt = "draft_requested_at"
        case sentAt = "sent_at"
    }

    init(status: String, content: String, generatedBy: String? = nil) {
        self.status = status
        self.content = content
        self.generatedBy = generatedBy
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = c.tolerant(.status, default: "")
        content = c.tolerant(.content, default: "")
        generatedBy = c.tolerant(.generatedBy)
        draftRequestedAt = c.tolerant(.draftRequestedAt)
        sentAt = c.tolerant(.sentAt)
    }
}

/// `{ "response": PollResponse }` answers of the poll endpoints.
struct PollResponseEnvelope: Decodable, Sendable {
    var response: PollResponse?
}
