import Foundation

/// The signed-in user: `user` in `GET /api/dashboard/` and `PUT /api/dashboard/user`
/// (map B §2.4.1 "CurrentUser"). Every field is decoded tolerantly; absent
/// booleans default to the safe side (not approved, terms not current, no flags).
struct CurrentUser: Decodable, Equatable, Sendable {
    var id: Int
    var username: String
    var description: String?
    var acceptedTermsAt: Date?
    var termsUpToDate: Bool
    var approved: Bool
    var email: String?
    var isAdmin: Bool
    var plan: Plan
    var voiceModeEnabled: Bool
    var craftMode: Bool?
    var preferredModel: String?
    var profileGenerationTaskId: String?
    var profileBatchPending: Bool
    var defaultPrivacyLevel: PrivacyLevel
    var defaultAIUsage: AIUsage
    var twitterLogin: Bool
    var twitterHandle: String?
    var pendingEmail: String?
    var pendingEmailExpired: Bool
    var prefillConsent: PrefillConsent?
    var prefilledHandle: String?
    var timezone: String
    var spendBlocked: Bool
    var shareV1Enabled: Bool
    var shareV1Available: Bool
    var publicSharingEnabled: Bool
    var externalContentAvailable: Bool
    var externalContentEnabled: Bool
    /// Deleting the account (#269); nil when the server has no account deletion.
    var accountDeletion: AccountDeletionInfo?
    /// "Delete all my writing" (#268), which an account deletion replaces.
    var dataDeletion: DataDeletionStatus?

    enum CodingKeys: String, CodingKey {
        case id, username, description, email, plan, timezone
        case acceptedTermsAt = "accepted_terms_at"
        case termsUpToDate = "terms_up_to_date"
        case approved
        case isAdmin = "is_admin"
        case voiceModeEnabled = "voice_mode_enabled"
        case craftMode = "craft_mode"
        case preferredModel = "preferred_model"
        case profileGenerationTaskId = "profile_generation_task_id"
        case profileBatchPending = "profile_batch_pending"
        case defaultPrivacyLevel = "default_privacy_level"
        case defaultAIUsage = "default_ai_usage"
        case twitterLogin = "twitter_login"
        case twitterHandle = "twitter_handle"
        case pendingEmail = "pending_email"
        case pendingEmailExpired = "pending_email_expired"
        case prefillConsent = "prefill_consent"
        case prefilledHandle = "prefilled_handle"
        case spendBlocked = "spend_blocked"
        case shareV1Enabled = "share_v1_enabled"
        case shareV1Available = "share_v1_available"
        case publicSharingEnabled = "public_sharing_enabled"
        case externalContentAvailable = "external_content_available"
        case externalContentEnabled = "external_content_enabled"
        case accountDeletion = "account_deletion"
        case dataDeletion = "data_deletion"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        username = c.tolerant(.username, default: "")
        description = c.tolerant(.description)
        acceptedTermsAt = c.tolerant(.acceptedTermsAt)
        termsUpToDate = c.tolerant(.termsUpToDate, default: false)
        approved = c.tolerant(.approved, default: false)
        email = c.tolerant(.email)
        isAdmin = c.tolerant(.isAdmin, default: false)
        plan = c.tolerant(.plan, default: .free)
        voiceModeEnabled = c.tolerant(.voiceModeEnabled, default: false)
        craftMode = c.tolerant(.craftMode)
        preferredModel = c.tolerant(.preferredModel)
        profileGenerationTaskId = c.tolerant(.profileGenerationTaskId)
        profileBatchPending = c.tolerant(.profileBatchPending, default: false)
        defaultPrivacyLevel = c.tolerant(.defaultPrivacyLevel, default: .private)
        defaultAIUsage = c.tolerant(.defaultAIUsage, default: .chat)
        twitterLogin = c.tolerant(.twitterLogin, default: false)
        twitterHandle = c.tolerant(.twitterHandle)
        pendingEmail = c.tolerant(.pendingEmail)
        pendingEmailExpired = c.tolerant(.pendingEmailExpired, default: false)
        prefillConsent = c.tolerant(.prefillConsent)
        prefilledHandle = c.tolerant(.prefilledHandle)
        timezone = c.tolerant(.timezone, default: "UTC")
        spendBlocked = c.tolerant(.spendBlocked, default: false)
        shareV1Enabled = c.tolerant(.shareV1Enabled, default: false)
        shareV1Available = c.tolerant(.shareV1Available, default: false)
        publicSharingEnabled = c.tolerant(.publicSharingEnabled, default: false)
        externalContentAvailable = c.tolerant(.externalContentAvailable, default: false)
        externalContentEnabled = c.tolerant(.externalContentEnabled, default: false)
        accountDeletion = c.tolerant(.accountDeletion)
        dataDeletion = c.tolerant(.dataDeletion)
    }

    /// Applies an email-state answer from any `/api/dashboard/email*` call
    /// (web `utils/emailState.js`).
    mutating func apply(_ state: EmailState) {
        email = state.email
        pendingEmail = state.pendingEmail
        pendingEmailExpired = state.pendingEmailExpired
    }
}

/// The ~10 user fields that decide what a user sees (design doc §3
/// "Capabilities"; map A §4.3). Views read these, never the raw user.
struct UserCapabilities: Equatable, Sendable {
    var approved = false
    var termsUpToDate = false
    var isAdmin = false
    var plan: Plan = .free
    /// Server-computed: admin or plan alpha/pro. Gates TTS generation and the voice UI.
    var voiceModeEnabled = false
    var craftMode = false
    /// Share, Commons, "My public page", public-reply UI (env flag AND user opt-in).
    var shareEnabled = false
    /// Env flag only: whether Account shows the public-sharing opt-in.
    var shareAvailable = false
    var externalContentEnabled = false
    var externalContentAvailable = false
    var twitterLogin = false
    var spendBlocked = false

    init() {}

    /// `craftModeFallback` is the per-device mirror the web keeps in
    /// `localStorage.loore_craft_mode`, used only when the user object lacks the field.
    init(user: CurrentUser, craftModeFallback: Bool = false) {
        approved = user.approved
        termsUpToDate = user.termsUpToDate
        isAdmin = user.isAdmin
        plan = user.plan
        voiceModeEnabled = user.voiceModeEnabled
        craftMode = user.craftMode ?? craftModeFallback
        shareEnabled = user.shareV1Enabled
        shareAvailable = user.shareV1Available
        externalContentEnabled = user.externalContentEnabled
        externalContentAvailable = user.externalContentAvailable
        twitterLogin = user.twitterLogin
        spendBlocked = user.spendBlocked
    }

    /// Commons tab and Share card (map A §2.1: approved and `share_v1_enabled`).
    var showsCommons: Bool { approved && shareEnabled }
}

/// `GET /api/dashboard/` envelope: the signed-in user and the newest profile version.
/// The server sends no thread cards here (#481); the Log lists the threads.
struct DashboardResponse: Decodable, Sendable {
    var user: CurrentUser
    var latestProfile: LatestProfile?

    enum CodingKeys: String, CodingKey {
        case user
        case latestProfile = "latest_profile"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        user = try c.decode(CurrentUser.self, forKey: .user)
        latestProfile = c.tolerant(.latestProfile)
    }
}

/// `latest_profile` on the dashboard (the newest profile version).
struct LatestProfile: Decodable, Identifiable, Sendable {
    var id: Int
    var content: String
    var generatedBy: String?
    var tokensUsed: Int?
    var createdAt: Date?
    var sourceTokensUsed: Int?
    var sourceOriginStats: JSONValue?
    var sourceDataCutoff: Date?
    var generationType: String?
    var hasTTS: Bool
    /// The version's AI usage when the server sends it (nil otherwise): a
    /// version that is not AI-readable gets no new speech.
    var aiUsage: AIUsage?

    enum CodingKeys: String, CodingKey {
        case id, content
        case aiUsage = "ai_usage"
        case generatedBy = "generated_by"
        case tokensUsed = "tokens_used"
        case createdAt = "created_at"
        case sourceTokensUsed = "source_tokens_used"
        case sourceOriginStats = "source_origin_stats"
        case sourceDataCutoff = "source_data_cutoff"
        case generationType = "generation_type"
        case hasTTS = "has_tts"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        content = c.tolerant(.content, default: "")
        generatedBy = c.tolerant(.generatedBy)
        tokensUsed = c.tolerant(.tokensUsed)
        createdAt = c.tolerant(.createdAt)
        sourceTokensUsed = c.tolerant(.sourceTokensUsed)
        sourceOriginStats = c.tolerant(.sourceOriginStats)
        sourceDataCutoff = c.tolerant(.sourceDataCutoff)
        generationType = c.tolerant(.generationType)
        hasTTS = c.tolerant(.hasTTS, default: false)
        aiUsage = c.tolerant(.aiUsage)
    }
}

/// `PUT /api/dashboard/user` answer.
struct UpdateUserResponse: Decodable, Sendable {
    var message: String?
    var user: CurrentUser
}

/// Every `/api/dashboard/email*` endpoint answers with this state (#260).
struct EmailState: Decodable, Equatable, Sendable {
    var message: String?
    var email: String?
    var pendingEmail: String?
    var pendingEmailExpired: Bool

    enum CodingKeys: String, CodingKey {
        case message, email
        case pendingEmail = "pending_email"
        case pendingEmailExpired = "pending_email_expired"
    }

    init(message: String? = nil, email: String?, pendingEmail: String?, pendingEmailExpired: Bool) {
        self.message = message
        self.email = email
        self.pendingEmail = pendingEmail
        self.pendingEmailExpired = pendingEmailExpired
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        message = c.tolerant(.message)
        email = c.tolerant(.email)
        pendingEmail = c.tolerant(.pendingEmail)
        pendingEmailExpired = c.tolerant(.pendingEmailExpired, default: false)
    }
}

/// `POST /api/terms/accept` answer.
struct TermsAcceptResponse: Decodable, Sendable {
    var message: String?
    var acceptedTermsAt: Date?
    var acceptedTermsVersion: String?

    enum CodingKeys: String, CodingKey {
        case message
        case acceptedTermsAt = "accepted_terms_at"
        case acceptedTermsVersion = "accepted_terms_version"
    }
}

/// `PATCH /api/dashboard/timezone` answer.
struct TimezoneResponse: Decodable, Sendable {
    var timezone: String
}

/// Generic `{ "message": … }` answer.
struct MessageResponse: Decodable, Sendable {
    var message: String?
}
