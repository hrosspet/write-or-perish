import Foundation

// Circles: entries shared with people their author chose (`/api/circles`,
// `audience` on `GET /api/nodes/<id>`). People are named by their Loore
// username only; a circle's name and id reach nobody but its owner.

/// One of the user's own circles: a named list of people.
struct PeopleCircle: Decodable, Identifiable, Hashable, Sendable {
    var id: Int
    var name: String
    var members: [CircleMember]
    /// Entries now shared with the circle (the add dialog states it).
    var sharedCount: Int

    enum CodingKeys: String, CodingKey {
        case id, name, members
        case sharedCount = "shared_count"
    }

    init(id: Int, name: String, members: [CircleMember] = [], sharedCount: Int = 0) {
        self.id = id
        self.name = name
        self.members = members
        self.sharedCount = sharedCount
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        name = c.tolerant(.name, default: "")
        members = c.tolerant(.members, default: [])
        sharedCount = c.tolerant(.sharedCount, default: 0)
    }
}

struct CircleMember: Decodable, Hashable, Sendable {
    var username: String
    /// Added for new entries only (the default): reads what is shared with the circle from then on.
    var onlyNew: Bool

    enum CodingKeys: String, CodingKey {
        case username
        case onlyNew = "only_new"
    }

    init(username: String, onlyNew: Bool = false) {
        self.username = username
        self.onlyNew = onlyNew
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        username = c.tolerant(.username, default: "")
        onlyNew = c.tolerant(.onlyNew, default: false)
    }
}

/// `GET /api/circles`.
struct CirclesResponse: Decodable, Sendable {
    var circles: [PeopleCircle]

    enum CodingKeys: String, CodingKey { case circles }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        circles = c.tolerant(.circles, default: [])
    }
}

/// `{circle}`: the answer to creating or renaming a circle, adding or removing a person.
struct CircleEnvelope: Decodable, Sendable {
    var circle: PeopleCircle
}

/// `POST /api/circles/<id>/members/preview`: the name exists, and how many
/// entries are already shared with the circle.
struct CircleMemberPreview: Decodable, Hashable, Sendable {
    var username: String
    var circle: String
    var alreadyMember: Bool
    var sharedCount: Int

    enum CodingKeys: String, CodingKey {
        case username, circle
        case alreadyMember = "already_member"
        case sharedCount = "shared_count"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        username = c.tolerant(.username, default: "")
        circle = c.tolerant(.circle, default: "")
        alreadyMember = c.tolerant(.alreadyMember, default: false)
        sharedCount = c.tolerant(.sharedCount, default: 0)
    }
}

/// `POST /api/circles/audience-preview`: who would read an entry shared with
/// these circles and names.
struct AudiencePreview: Decodable, Sendable {
    var readers: [String]

    enum CodingKeys: String, CodingKey { case readers }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        readers = c.tolerant(.readers, default: [])
    }
}

/// `audience` on a shared node, for its author and its readers: the people
/// who read the entry and its replies.
struct NodeAudience: Decodable, Hashable, Sendable {
    /// The shared entry the thread's audience belongs to.
    var entryId: Int
    var author: String
    var isAuthor: Bool
    var readers: [String]
    /// The node is the shared entry itself (not a reply in its thread).
    var isEntry: Bool
    /// The entry's own AI usage: with `none` nobody can generate an AI reply in its thread.
    var entryAIUsage: AIUsage
    /// What the entry is shared with: for its author, on the entry itself.
    var circleIds: [Int]?
    var usernames: [String]?
    /// The viewer's own artifacts an AI reply generated here would read, by
    /// their plain names ("profile", "memory", "to-do list"). Empty when the
    /// thread has no prompt that reads artifacts of the viewer's.
    var aiReads: [String]

    enum CodingKeys: String, CodingKey {
        case author, readers, usernames
        case aiReads = "ai_reads"
        case entryId = "entry_id"
        case isAuthor = "is_author"
        case isEntry = "is_entry"
        case entryAIUsage = "entry_ai_usage"
        case circleIds = "circle_ids"
    }

    init(entryId: Int, author: String, isAuthor: Bool, readers: [String], isEntry: Bool = true,
         entryAIUsage: AIUsage = .chat, circleIds: [Int]? = nil, usernames: [String]? = nil,
         aiReads: [String] = []) {
        self.entryId = entryId
        self.author = author
        self.isAuthor = isAuthor
        self.readers = readers
        self.isEntry = isEntry
        self.entryAIUsage = entryAIUsage
        self.circleIds = circleIds
        self.usernames = usernames
        self.aiReads = aiReads
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        entryId = try c.decode(Int.self, forKey: .entryId)
        author = c.tolerant(.author, default: "")
        isAuthor = c.tolerant(.isAuthor, default: false)
        readers = c.tolerant(.readers, default: [])
        isEntry = c.tolerant(.isEntry, default: false)
        entryAIUsage = c.tolerant(.entryAIUsage, default: .off)
        circleIds = c.tolerant(.circleIds)
        usernames = c.tolerant(.usernames)
        aiReads = c.tolerant(.aiReads, default: [])
    }
}

/// `GET /api/nodes/<id>/audience`: for the edit form.
struct NodeAudienceAnswer: Decodable, Sendable {
    var audience: NodeAudience?
    /// The node sits under a shared node: these people would read it as "circles".
    var threadAudience: NodeAudience?
    /// For the node's author: the discussion above the node, from the start
    /// of the thread. `count` is the number of the author's entries on that
    /// path; `shared` says the path is shared with the node's audience now.
    /// nil when the node has nothing above it.
    var threadPath: ThreadPath?

    struct ThreadPath: Decodable, Hashable, Sendable {
        var count: Int
        var shared: Bool

        enum CodingKeys: String, CodingKey { case count, shared }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            count = c.tolerant(.count, default: 0)
            shared = c.tolerant(.shared, default: false)
        }
    }

    enum CodingKeys: String, CodingKey {
        case audience
        case threadAudience = "thread_audience"
        case threadPath = "thread_path"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        audience = c.tolerant(.audience)
        threadAudience = c.tolerant(.threadAudience)
        threadPath = c.tolerant(.threadPath)
    }
}

/// `GET /api/circles/shared-with-me`: entries shared with the user, newest
/// first, grouped by person. Nothing in it counts or tracks reading.
struct SharedWithMe: Decodable, Sendable {
    struct Person: Decodable, Identifiable, Hashable, Sendable {
        var username: String
        var entries: [Entry]
        var id: String { username }

        enum CodingKeys: String, CodingKey { case username, entries }

        init(username: String, entries: [Entry]) {
            self.username = username
            self.entries = entries
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            username = c.tolerant(.username, default: "")
            entries = c.tolerant(.entries, default: [])
        }
    }

    struct Entry: Decodable, Identifiable, Hashable, Sendable {
        var id: Int
        var title: String
        var preview: String
        var createdAt: Date?
        var lastReplyBy: String?
        var lastReplyByMe: Bool

        enum CodingKeys: String, CodingKey {
            case id, title, preview
            case createdAt = "created_at"
            case lastReplyBy = "last_reply_by"
            case lastReplyByMe = "last_reply_by_me"
        }

        init(id: Int, title: String, preview: String = "", createdAt: Date? = nil, lastReplyBy: String? = nil,
             lastReplyByMe: Bool = false) {
            self.id = id
            self.title = title
            self.preview = preview
            self.createdAt = createdAt
            self.lastReplyBy = lastReplyBy
            self.lastReplyByMe = lastReplyByMe
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(Int.self, forKey: .id)
            title = c.tolerant(.title, default: "")
            preview = c.tolerant(.preview, default: "")
            createdAt = c.tolerant(.createdAt)
            lastReplyBy = c.tolerant(.lastReplyBy)
            lastReplyByMe = c.tolerant(.lastReplyByMe, default: false)
        }
    }

    var people: [Person]
    /// The people whose entries the user chose not to see.
    var hidden: [String]
    /// `?before=` for the next, older page; nil on the last one.
    var nextBefore: Int?

    enum CodingKeys: String, CodingKey {
        case people, hidden
        case nextBefore = "next_before"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        people = c.tolerant(.people, default: [])
        hidden = c.tolerant(.hidden, default: [])
        nextBefore = c.tolerant(.nextBefore)
    }

    /// Older entries join their person's group, or open a new one (web `load(before)`).
    static func merge(_ current: [Person], older: [Person]) -> [Person] {
        var merged = current
        for person in older {
            if let index = merged.firstIndex(where: { $0.username == person.username }) {
                merged[index].entries.append(contentsOf: person.entries)
            } else {
                merged.append(person)
            }
        }
        return merged
    }
}

/// `PUT /api/nodes/<id>/audience`.
struct NodeAudienceSetAnswer: Decodable, Sendable {
    var id: Int
    var audience: NodeAudience?

    enum CodingKeys: String, CodingKey { case id, audience }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        audience = c.tolerant(.audience)
    }
}
