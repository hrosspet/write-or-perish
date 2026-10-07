import Foundation

/// A string enum that never fails to decode: values the app does not know
/// (the backend adds values without versioning, map B §5) become `.unknown(raw)`.
///
/// Conforming enums implement `init(rawString:)` and `rawString`; Codable,
/// `description` and string-literal support come from the extension.
protocol OpenEnum: Codable, Hashable, Sendable, CustomStringConvertible, ExpressibleByStringLiteral {
    init(rawString: String)
    var rawString: String { get }
}

extension OpenEnum {
    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(rawString: try container.decode(String.self))
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawString)
    }

    var description: String { rawString }

    init(stringLiteral value: String) { self.init(rawString: value) }
}

/// `privacy_level` (circles is unimplemented: private for everyone but the owner).
enum PrivacyLevel: OpenEnum {
    case `private`, circles, `public`, unknown(String)

    init(rawString: String) {
        switch rawString {
        case "private": self = .private
        case "circles": self = .circles
        case "public": self = .public
        default: self = .unknown(rawString)
        }
    }

    var rawString: String {
        switch self {
        case .private: return "private"
        case .circles: return "circles"
        case .public: return "public"
        case .unknown(let s): return s
        }
    }
}

/// `ai_usage`: `chat` and `train` let the AI read the content.
/// The wire value `"none"` is the case `.off` (a case named `none` would
/// collide with `Optional.none` when comparing an `AIUsage?`).
enum AIUsage: OpenEnum {
    case off, chat, train, unknown(String)

    init(rawString: String) {
        switch rawString {
        case "none": self = .off
        case "chat": self = .chat
        case "train": self = .train
        default: self = .unknown(rawString)
        }
    }

    var rawString: String {
        switch self {
        case .off: return "none"
        case .chat: return "chat"
        case .train: return "train"
        case .unknown(let s): return s
        }
    }

    /// Whether the AI may read content with this setting (web `utils/aiUsage.js`).
    var allowsAI: Bool { self == .chat || self == .train }
}

/// `node_type`. The backend says to treat an unknown type as `user`.
enum NodeType: OpenEnum {
    case user, llm, link, unknown(String)

    init(rawString: String) {
        switch rawString {
        case "user": self = .user
        case "llm": self = .llm
        case "link": self = .link
        default: self = .unknown(rawString)
        }
    }

    var rawString: String {
        switch self {
        case .user: return "user"
        case .llm: return "llm"
        case .link: return "link"
        case .unknown(let s): return s
        }
    }
}

/// Status of an async task (LLM, TTS, transcription). LLM also has `cancelled`.
enum TaskStatus: OpenEnum {
    case pending, processing, completed, failed, cancelled, unknown(String)

    init(rawString: String) {
        switch rawString {
        case "pending": self = .pending
        case "processing": self = .processing
        case "completed": self = .completed
        case "failed": self = .failed
        case "cancelled": self = .cancelled
        default: self = .unknown(rawString)
        }
    }

    var rawString: String {
        switch self {
        case .pending: return "pending"
        case .processing: return "processing"
        case .completed: return "completed"
        case .failed: return "failed"
        case .cancelled: return "cancelled"
        case .unknown(let s): return s
        }
    }

    /// The poller's terminal set (`useAsyncTaskPolling`): completed, failed, cancelled.
    var isTerminal: Bool { self == .completed || self == .failed || self == .cancelled }
    var isInFlight: Bool { self == .pending || self == .processing }
}

/// `plan`: voice mode = alpha/pro or admin.
enum Plan: OpenEnum {
    case free, alpha, pro, unknown(String)

    init(rawString: String) {
        switch rawString {
        case "free": self = .free
        case "alpha": self = .alpha
        case "pro": self = .pro
        default: self = .unknown(rawString)
        }
    }

    var rawString: String {
        switch self {
        case .free: return "free"
        case .alpha: return "alpha"
        case .pro: return "pro"
        case .unknown(let s): return s
        }
    }
}

/// Draft `streaming_status` of a recording session.
enum StreamingStatus: OpenEnum {
    case recording, finalizing, completed, failed, unknown(String)

    init(rawString: String) {
        switch rawString {
        case "recording": self = .recording
        case "finalizing": self = .finalizing
        case "completed": self = .completed
        case "failed": self = .failed
        default: self = .unknown(rawString)
        }
    }

    var rawString: String {
        switch self {
        case .recording: return "recording"
        case .finalizing: return "finalizing"
        case .completed: return "completed"
        case .failed: return "failed"
        case .unknown(let s): return s
        }
    }
}

/// `prefill_consent` answer on the waitlist consent card.
enum PrefillConsent: OpenEnum {
    case yes, no, unknown(String)

    init(rawString: String) {
        switch rawString {
        case "yes": self = .yes
        case "no": self = .no
        default: self = .unknown(rawString)
        }
    }

    var rawString: String {
        switch self {
        case .yes: return "yes"
        case .no: return "no"
        case .unknown(let s): return s
        }
    }
}
