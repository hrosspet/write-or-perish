import Foundation

// The documents workspace (map E §1): profile, todo, artifacts, prompts, and
// the version rows the history sheet lists.

/// A version id: an integer row id, or the prompts' synthetic `"default"` (v0).
enum VersionID: Hashable, Sendable, Decodable, CustomStringConvertible {
    case row(Int)
    case fileDefault

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let i = try? c.decode(Int.self) { self = .row(i); return }
        let s = try c.decode(String.self)
        if let i = Int(s) { self = .row(i) } else { self = .fileDefault }
    }

    var rowId: Int? {
        if case .row(let id) = self { return id }
        return nil
    }

    var description: String {
        switch self {
        case .row(let id): return String(id)
        case .fileDefault: return "default"
        }
    }
}

/// One row of a `…/versions` list (profile, todo, artifact, prompt), newest first.
struct VersionSummary: Decodable, Identifiable, Hashable, Sendable {
    var id: VersionID
    var versionNumber: Int
    var createdAt: Date?
    var generatedBy: String?
    var generationType: String?
    var sourceTokensUsed: Int?
    var tokensUsed: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case versionNumber = "version_number"
        case createdAt = "created_at"
        case generatedBy = "generated_by"
        case generationType = "generation_type"
        case sourceTokensUsed = "source_tokens_used"
        case tokensUsed = "tokens_used"
    }

    init(id: VersionID, versionNumber: Int, createdAt: Date? = nil, generatedBy: String? = nil,
         generationType: String? = nil, sourceTokensUsed: Int? = nil, tokensUsed: Int? = nil) {
        self.id = id
        self.versionNumber = versionNumber
        self.createdAt = createdAt
        self.generatedBy = generatedBy
        self.generationType = generationType
        self.sourceTokensUsed = sourceTokensUsed
        self.tokensUsed = tokensUsed
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(VersionID.self, forKey: .id)
        versionNumber = c.flexibleInt(.versionNumber) ?? 0
        createdAt = c.tolerant(.createdAt)
        generatedBy = c.tolerant(.generatedBy)
        generationType = c.tolerant(.generationType)
        sourceTokensUsed = c.flexibleInt(.sourceTokensUsed)
        tokensUsed = c.flexibleInt(.tokensUsed)
    }
}

/// `{versions: […]}`.
struct VersionList: Decodable, Sendable {
    var versions: [VersionSummary]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        versions = c.tolerant(AnyCodingKey("versions"), default: [])
    }
}

/// The content of one version, whichever envelope key the route uses
/// (`profile`, `todo`, `artifact`, `prompt`).
struct VersionContent: Decodable, Sendable {
    var content: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        for key in ["profile", "todo", "artifact", "prompt"] {
            if let inner = try? c.nestedContainer(keyedBy: AnyCodingKey.self, forKey: AnyCodingKey(key)) {
                content = inner.tolerant(AnyCodingKey("content"), default: "")
                return
            }
        }
        content = ""
    }
}

// MARK: - Profile

/// A profile version (`/api/profile/…`); the dashboard's `latest_profile` is `LatestProfile`.
struct ProfileVersionDetail: Decodable, Sendable {
    var id: Int
    var content: String

    enum CodingKeys: String, CodingKey { case profile }
    enum Inner: String, CodingKey { case id, content }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self).nestedContainer(keyedBy: Inner.self, forKey: .profile)
        id = try c.decode(Int.self, forKey: .id)
        content = c.tolerant(.content, default: "")
    }
}

/// `GET /api/export/profile-progress` (ProfileGenerationWatcher, map E §10).
struct ProfileProgress: Decodable, Equatable, Sendable {
    struct Latest: Decodable, Equatable, Sendable {
        var id: Int
        var generationType: String?
        var createdAt: Date?

        enum CodingKeys: String, CodingKey {
            case id
            case generationType = "generation_type"
            case createdAt = "created_at"
        }

        init(id: Int, generationType: String? = nil, createdAt: Date? = nil) {
            self.id = id
            self.generationType = generationType
            self.createdAt = createdAt
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = try c.decode(Int.self, forKey: .id)
            generationType = c.tolerant(.generationType)
            createdAt = c.tolerant(.createdAt)
        }
    }

    var running: Bool
    var source: String?
    var status: String
    var progress: Double
    var message: String
    var taskId: String?
    var error: String?
    var latestProfile: Latest?
    var batchStepFailed: Bool

    enum CodingKeys: String, CodingKey {
        case running, source, status, progress, message, error
        case taskId = "task_id"
        case latestProfile = "latest_profile"
        case batchStepFailed = "batch_step_failed"
    }

    init(running: Bool, source: String? = nil, status: String, progress: Double = 0, message: String = "",
         taskId: String? = nil, error: String? = nil, latestProfile: Latest? = nil, batchStepFailed: Bool = false) {
        self.running = running
        self.source = source
        self.status = status
        self.progress = progress
        self.message = message
        self.taskId = taskId
        self.error = error
        self.latestProfile = latestProfile
        self.batchStepFailed = batchStepFailed
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        running = c.tolerant(.running, default: false)
        source = c.tolerant(.source)
        status = c.tolerant(.status, default: "idle")
        progress = c.tolerant(.progress, default: 0.0)
        message = c.tolerant(.message, default: "")
        taskId = c.tolerant(.taskId)
        error = c.tolerant(.error)
        latestProfile = c.tolerant(.latestProfile)
        batchStepFailed = c.tolerant(.batchStepFailed, default: false)
    }
}

// MARK: - Todo

/// `{todo: …|null}` from `/api/todo/` (GET, PATCH, PUT, revert).
struct TodoEnvelope: Decodable, Sendable {
    var todo: TodoDoc?
}

struct TodoDoc: Decodable, Equatable, Sendable {
    var id: Int
    var content: String
    var generatedBy: String?
    var createdAt: Date?
    var versionNumber: Int?
    /// Names the stored text of this version; it changes with every write.
    /// Sent back as `base_revision` on PATCH and PUT, so a save made on an
    /// older list is refused (409 `todo_changed`) instead of dropping newer
    /// changes (#430, #476, #477).
    var revision: String?

    enum CodingKeys: String, CodingKey {
        case id, content, revision
        case generatedBy = "generated_by"
        case createdAt = "created_at"
        case versionNumber = "version_number"
    }

    init(id: Int, content: String, generatedBy: String? = nil, createdAt: Date? = nil, versionNumber: Int? = nil,
         revision: String? = nil) {
        self.id = id
        self.content = content
        self.generatedBy = generatedBy
        self.createdAt = createdAt
        self.versionNumber = versionNumber
        self.revision = revision
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        content = c.tolerant(.content, default: "")
        generatedBy = c.tolerant(.generatedBy)
        createdAt = c.tolerant(.createdAt)
        versionNumber = c.flexibleInt(.versionNumber)
        revision = c.tolerant(.revision)
    }
}

// MARK: - Artifacts

/// One artifact from `GET /api/artifacts/` (placeholders have no id and no date).
struct ArtifactDoc: Decodable, Identifiable, Equatable, Sendable {
    var kind: String
    var rowId: Int?
    var title: String
    var description: String?
    var content: String
    var generatedBy: String?
    var createdAt: Date?

    var id: String { kind }
    /// The kind has a saved version (the web checks `created_at`).
    var exists: Bool { createdAt != nil }

    enum CodingKeys: String, CodingKey {
        case kind, title, description, content
        case rowId = "id"
        case generatedBy = "generated_by"
        case createdAt = "created_at"
    }

    init(kind: String, rowId: Int? = nil, title: String, description: String? = nil, content: String = "",
         generatedBy: String? = nil, createdAt: Date? = nil) {
        self.kind = kind
        self.rowId = rowId
        self.title = title
        self.description = description
        self.content = content
        self.generatedBy = generatedBy
        self.createdAt = createdAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(String.self, forKey: .kind)
        rowId = c.tolerant(.rowId)
        title = c.tolerant(.title, default: "")
        description = c.tolerant(.description)
        content = c.tolerant(.content, default: "")
        generatedBy = c.tolerant(.generatedBy)
        createdAt = c.tolerant(.createdAt)
    }
}

struct ArtifactList: Decodable, Sendable {
    var artifacts: [ArtifactDoc]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        artifacts = c.tolerant(AnyCodingKey("artifacts"), default: [])
    }
}

// MARK: - Prompts

/// A row of `GET /api/prompts/`.
struct PromptSummary: Decodable, Identifiable, Equatable, Sendable {
    var promptKey: String
    var title: String
    var preview: String
    var versionNumber: Int
    var generatedBy: String?
    var createdAt: Date?
    var defaultUpdated: Bool

    var id: String { promptKey }

    enum CodingKeys: String, CodingKey {
        case title, preview
        case promptKey = "prompt_key"
        case versionNumber = "version_number"
        case generatedBy = "generated_by"
        case createdAt = "created_at"
        case defaultUpdated = "default_updated"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        promptKey = try c.decode(String.self, forKey: .promptKey)
        title = c.tolerant(.title, default: promptKey)
        preview = c.tolerant(.preview, default: "")
        versionNumber = c.flexibleInt(.versionNumber) ?? 0
        generatedBy = c.tolerant(.generatedBy)
        createdAt = c.tolerant(.createdAt)
        defaultUpdated = c.tolerant(.defaultUpdated, default: false)
    }
}

struct PromptList: Decodable, Sendable {
    var prompts: [PromptSummary]
}

/// `{prompt: …}` from `GET/PUT /api/prompts/<key>` and the revert/acknowledge routes.
struct PromptDoc: Decodable, Equatable, Sendable {
    var rowId: Int?
    var promptKey: String
    var title: String
    var content: String
    var generatedBy: String?
    var createdAt: Date?
    var versionNumber: Int
    var defaultUpdated: Bool

    enum CodingKeys: String, CodingKey {
        case title, content
        case rowId = "id"
        case promptKey = "prompt_key"
        case generatedBy = "generated_by"
        case createdAt = "created_at"
        case versionNumber = "version_number"
        case defaultUpdated = "default_updated"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rowId = c.tolerant(.rowId)
        promptKey = c.tolerant(.promptKey, default: "")
        title = c.tolerant(.title, default: "")
        content = c.tolerant(.content, default: "")
        generatedBy = c.tolerant(.generatedBy)
        createdAt = c.tolerant(.createdAt)
        versionNumber = c.flexibleInt(.versionNumber) ?? 0
        defaultUpdated = c.tolerant(.defaultUpdated, default: false)
    }
}

struct PromptEnvelope: Decodable, Sendable {
    var prompt: PromptDoc
}
