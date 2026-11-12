import Foundation

// Long threads (#648; Peter, 2026-11-12): every AI reply reads the whole thread
// above it, so a very long thread, or one holding a very large document, makes
// each reply large. From 90 % of a limit on the size of the next reply's prompt
// the reply form says the thread is getting long; at the limit no AI reply is
// made in the thread (writing still works) and the form offers a new thread.
// No price, token counts or percentages are shown. The server measures
// (backend/utils/thread_size.py); the web's copy is the same.

/// `thread_length` on `GET /api/nodes/<id>` (the node's owner only): how close
/// the next AI reply under the node is to the limit. Absent or null: not close.
enum ThreadLength: String, Equatable, Sendable {
    /// From 90 % of the limit: a calm notice above the reply form.
    case notice
    /// At the limit: no AI reply here; the form offers a new thread.
    case limit

    /// The refusal's error code: the 409 on a reply request, `llm_error_code` on
    /// the routes that keep the entry and skip the reply, `warning_code` on a
    /// Voice recording whose reply was skipped, `error_code` on a reply the
    /// server refused.
    static let code = "thread_too_long"

    static let noticeText = "This thread is getting long. Each AI reply reads all of it, so AI replies here "
        + "will stop soon. You can start a new thread to keep going."
    /// The server's text; shown when an answer carries none.
    static let limitText = "This thread is too long for another AI reply. Each reply reads the whole thread, "
        + "including what it quotes, and this one has grown too large. You can still write here. "
        + "To keep talking with AI, start a new thread."
    /// Said aloud in Voice mode (on-device speech, no request) when a turn is refused.
    static let spokenText = "This thread is too long for another reply. Start a new thread to keep talking."
    /// The Voice screen's heading in place of the record button.
    static let blockTitle = "This thread is too long"
    static let newThreadLabel = "Start a new thread"

    /// The decoded field: unknown values (a later server) are no state.
    init?(rawString: String?) {
        guard let rawString, let value = ThreadLength(rawValue: rawString) else { return nil }
        self = value
    }

    /// Whether `code` is this refusal's.
    static func isRefusal(_ code: String?) -> Bool { code == Self.code }

    /// A server text (`thread_length_message`, a refusal's `error`) trimmed, or
    /// nil when there is none to show.
    static func message(_ raw: String?) -> String? {
        let text = raw?.trimmingCharacters(in: .whitespacesAndNewlines)
        return (text?.isEmpty ?? true) ? nil : text
    }
}

extension APIError {
    /// A reply request refused because the thread is too long for another AI
    /// reply (409 `thread_too_long`): nothing was created. Not an error to toast:
    /// the reply form switches to its limit state.
    var isThreadTooLong: Bool { status == 409 && code == ThreadLength.code }
}
