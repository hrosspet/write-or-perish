import Foundation

/// Voice mode is closed where no reply may follow, and the Voice screen says why
/// instead of offering the record button:
/// - AI usage is `none` (server code `ai_usage_none`, web `VoicePage`): a fresh
///   conversation when the account's Default AI usage is None, or a thread whose
///   entries are not AI-readable.
/// - The thread is too long for another AI reply (`thread_too_long`, #648): the
///   screen offers a new thread. A turn refused for it is said aloud.
/// The server refuses `streaming/init` and `voice/from-node` the same way.
struct VoiceAIBlock: Equatable, Sendable {
    enum Scope: String, Equatable, Sendable {
        /// The account's Default AI usage (a fresh voice conversation).
        case account
        /// The thread Voice would continue.
        case thread
    }

    enum Reason: Equatable, Sendable {
        /// AI usage keeps the account or the thread away from AI.
        case aiUsage
        /// The thread is too long for another AI reply (#648).
        case threadTooLong
    }

    var scope: Scope
    var reason: Reason = .aiUsage
    /// The server's text for the refusal (thread too long), when it sent one.
    var message: String?

    init(scope: Scope, reason: Reason = .aiUsage, message: String? = nil) {
        self.scope = scope
        self.reason = reason
        self.message = message
    }

    /// The thread is too long for another reply; `message` is the server's text.
    static func threadTooLong(message: String? = nil) -> VoiceAIBlock {
        let text = message?.trimmingCharacters(in: .whitespacesAndNewlines)
        return VoiceAIBlock(scope: .thread, reason: .threadTooLong,
                            message: (text?.isEmpty ?? true) ? nil : text)
    }

    static let code = "ai_usage_none"
    static let title = "Voice mode needs AI"
    static let accountBody = "Voice mode needs AI to listen and reply. Your Default AI usage is set to None, "
        + "so Loore keeps your entries away from AI. You can change it in Account settings."
    static let threadBody = "Voice mode needs AI to listen and reply. AI usage in this thread is set to None, "
        + "so Loore keeps it away from AI. You can change it when you edit the thread's entries, and the "
        + "default for new entries in Account settings."
    /// The Account page row with Default AI usage (`/account#ai-usage`).
    static let accountAnchor = "ai-usage"

    var isThreadTooLong: Bool { reason == .threadTooLong }

    /// The screen's heading.
    var heading: String { isThreadTooLong ? ThreadLength.blockTitle : Self.title }

    var body: String {
        if isThreadTooLong { return message ?? ThreadLength.limitText }
        return scope == .account ? Self.accountBody : Self.threadBody
    }

    /// The block a refused request stands for: an error whose `code` is
    /// `ai_usage_none` (the server answers 403; `scope` comes from the body,
    /// `fallback` when it has none) or `thread_too_long` (409, #648).
    static func from(_ error: Error, fallback: Scope) -> VoiceAIBlock? {
        guard let api = error as? APIError else { return nil }
        if api.code == ThreadLength.code {
            return .threadTooLong(message: api.body?.error)
        }
        guard api.code == code else { return nil }
        let scope = api.body?.extra["scope"]?.stringValue.flatMap(Scope.init(rawValue:)) ?? fallback
        return VoiceAIBlock(scope: scope)
    }

    /// What the Voice screen shows before the record button.
    /// - A fresh conversation (`threadId` nil): blocked when the account's Default
    ///   AI usage does not let AI read (unknown account: not blocked).
    /// - A thread: the server's `GET /api/voice/availability` answer. No answer (an
    ///   older server, offline) is not a block: `streaming/init` refuses anyway and
    ///   its error opens the same message.
    static func decide(threadId: Int?, accountAIUsage: AIUsage?, availability: VoiceAvailability?) -> VoiceAIBlock? {
        guard threadId != nil else {
            guard let accountAIUsage, !accountAIUsage.allowsAI else { return nil }
            return VoiceAIBlock(scope: .account)
        }
        return availability?.block
    }

    /// "Back to the thread": pop when the screen under Voice is a thread (the
    /// thread's Voice Mode button opened it); otherwise the thread replaces Voice.
    static func backPops(previous: AppRoute?) -> Bool {
        if case .thread = previous { return true }
        return false
    }
}

/// `GET /api/voice/availability[?parent=<id>]`.
struct VoiceAvailability: Decodable, Equatable, Sendable {
    var allowed: Bool
    var code: String?
    var scope: String?
    var error: String?
    /// The thread is close to its limit (#648): "notice" (allowed). The limit
    /// itself comes as `allowed: false`, `code: thread_too_long`.
    var threadLength: ThreadLength?

    /// The block this answer stands for: `ai_usage_none` or `thread_too_long`
    /// closes Voice mode; an unknown reason does not.
    var block: VoiceAIBlock? {
        guard !allowed else { return nil }
        if code == ThreadLength.code { return .threadTooLong(message: error) }
        guard code == VoiceAIBlock.code else { return nil }
        return VoiceAIBlock(scope: scope.flatMap(VoiceAIBlock.Scope.init(rawValue:)) ?? .thread)
    }

    /// The thread is getting long: the screen shows the notice under the record button.
    var showsNotice: Bool { allowed && threadLength == .notice }

    enum CodingKeys: String, CodingKey {
        case allowed, code, scope, error
        case threadLength = "thread_length"
    }

    init(allowed: Bool, code: String? = nil, scope: String? = nil, error: String? = nil,
         threadLength: ThreadLength? = nil) {
        self.allowed = allowed
        self.code = code
        self.scope = scope
        self.error = error
        self.threadLength = threadLength
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        allowed = c.tolerant(.allowed, default: true)
        code = c.tolerant(.code)
        scope = c.tolerant(.scope)
        error = c.tolerant(.error)
        threadLength = ThreadLength(rawString: c.tolerant(.threadLength))
    }
}
