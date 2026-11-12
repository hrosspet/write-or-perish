import Foundation

// AI replies in shared threads (Peter, 2026-11-12): "all AI replies in a public
// thread are private by default, autogenerate is off in public threads and
// circles and can't be turned on, and only the user can change an AI reply to
// that circle/public (after the reply being generated and the user supposedly
// having read it and decided it to share it)". And: a public node never quotes
// a node that isn't public. The server's rules: backend/utils/llm_nodes.py
// (`shared_with_others`, `auto_generate_refusal`, `reply_in_progress`) and
// backend/utils/publish_quotes.py; the web's: frontend/src/utils/sharedThread.js.

extension PrivacyLevel {
    /// People other than the owner can read a node with this privacy: anything
    /// but private (public, or shared with circles). A reply asked for under such
    /// a node, or a new entry with it, is in a shared thread, where Loore never
    /// replies by itself. The app's one test of "a shared thread": a sharing that
    /// does not show in a node's own privacy has to be added here (and on the server).
    var isSharedWithOthers: Bool {
        switch self {
        case .private: return false
        case .unknown(let raw): return !raw.isEmpty && raw != "private"
        case .circles, .public: return true
        }
    }
}

extension Optional where Wrapped == PrivacyLevel {
    /// No privacy known: not shared (the server's `level or "private"`).
    var isSharedWithOthers: Bool { self?.isSharedWithOthers ?? false }
}

enum SharedThread {
    /// 409 on the automatic reply request under a shared node; `llm_error_code`
    /// on the routes that keep the entry and skip the reply.
    static let autoGenerateOffCode = "auto_generate_off"
    /// 409 on a privacy change of an AI reply still being written.
    static let replyInProgressCode = "reply_in_progress"
    /// A locked auto-generate toggle's hint and caption (web `AUTO_GENERATE_OFF_HINT`).
    static let autoGenerateOffHint = "Auto-generate is off in public and shared threads."
    /// The Write New Entry toggle's line while it is locked (web NodeForm).
    static let autoGenerateLockedFormText = autoGenerateOffHint
        + " You can tap LLM Response on the created node; the reply stays private until you change it."

    /// The owner's sign on a private AI reply under a node others can read.
    static let privateReplyNote = "Only you see this reply"

    /// An AI reply only its owner can read, answering a node other people can
    /// read (public or circles): its owner sees `privateReplyNote` on it.
    /// Nobody else sees the reply, and a user's own entry never gets the note.
    static func showsPrivateReplyNote(isAIReply: Bool, owned: Bool, deleted: Bool = false,
                                      privacy: PrivacyLevel?, parentPrivacy: PrivacyLevel?) -> Bool {
        isAIReply && owned && !deleted && privacy == .private && parentPrivacy.isSharedWithOthers
    }
}

extension APIError {
    /// The server refused an automatic reply under a node other people can read;
    /// nothing was created. Not an error to show: LLM Response stays available.
    var isAutoGenerateOff: Bool { status == 409 && code == SharedThread.autoGenerateOffCode }

    /// An AI reply's privacy cannot change while it is being written.
    var isReplyInProgress: Bool { status == 409 && code == SharedThread.replyInProgressCode }
}

/// 409 `quotes_not_public`: something about to be public (an entry, a reply, an
/// edit, a share) quotes an entry that isn't. Nothing was saved; the answer
/// names each quoted entry so the user can make it public first.
struct QuotesNotPublic: Equatable, Sendable {
    struct Quoted: Equatable, Identifiable, Sendable {
        var id: Int
        /// The user may open the entry: its preview and link are there.
        var canOpen: Bool
        var preview: String?
        var permalink: String?
        var privacyLevel: PrivacyLevel?

        init(id: Int, canOpen: Bool, preview: String? = nil, permalink: String? = nil,
             privacyLevel: PrivacyLevel? = nil) {
            self.id = id
            self.canOpen = canOpen
            self.preview = preview
            self.permalink = permalink
            self.privacyLevel = privacyLevel
        }

        init?(json: JSONValue) {
            guard let id = json["id"]?.intValue else { return nil }
            self.init(id: id, canOpen: json["can_open"]?.boolValue ?? false,
                      preview: json["preview"]?.stringValue, permalink: json["permalink"]?.stringValue,
                      privacyLevel: json["privacy_level"]?.stringValue.map(PrivacyLevel.init(rawString:)))
        }

        /// The dialog row's text (web `QuotesNotPublicDialog`).
        var rowText: String {
            guard canOpen else { return "An entry you can't open (#\(id)). Remove its quote to publish." }
            if let preview, !preview.isEmpty { return preview }
            return "Entry #\(id)"
        }
    }

    static let code = "quotes_not_public"
    static let fallbackMessage = "This can't be made public yet: it quotes an entry that isn't public. "
        + "Make the quoted entry public first, then try again."

    var message: String
    var quoted: [Quoted]

    init(message: String, quoted: [Quoted]) {
        self.message = message
        self.quoted = quoted
    }

    /// The refusal in `error`, or nil for any other error.
    init?(_ error: Error) {
        guard let apiError = error as? APIError, apiError.status == 409, apiError.code == Self.code else { return nil }
        let body = apiError.body
        let text = body?.error ?? ""
        message = text.isEmpty ? Self.fallbackMessage : text
        quoted = (body?.extra["quoted"]?.arrayValue ?? []).compactMap(Quoted.init(json:))
    }
}
