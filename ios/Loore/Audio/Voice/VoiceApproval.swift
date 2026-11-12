import Foundation

/// A Voice conversation whose thread has write tool calls waiting for the user's
/// approval (`Core/Models/ToolApprovals.swift`). Peter, 2026-11-12, on the
/// approval dialog appearing on the Voice screen: "yes, I'd like that. A spoken
/// announcement should also be added after the model reply, informing the user
/// they need to confirm, or reject the tool called. Also, the lockscreen controls
/// shouldn't allow next recording (replay of the reply should be still possible,
/// but the announcement should still be there after the LLM reply)".
///
/// The announcement is its own clip, appended to the end of the reply's audio
/// queue only while one of the reply's changes waits (Peter, 2026-11-12: "the
/// announcement should be TTSed as a separate chunk and appended to the queue.
/// So that when it's not pending, it's not voiced out anymore"). The reply's
/// own audio is unchanged. While a call waits, no new Voice turn starts in the
/// thread: not from the Voice screen, not from the Live Activity, and the
/// server refuses one too (`streaming/init` answers 409 `approval_pending`).
enum VoiceApproval {
    /// The server's refusal of a Voice turn while a change waits (409).
    static let code = "approval_pending"

    /// Whether *error* is that refusal.
    static func isRefusal(_ error: Error) -> Bool {
        (error as? APIError)?.code == code
    }

    /// The line under the disabled record control, before " · Review" (as the
    /// thread screen's waiting line).
    static func waitingLine(count: Int) -> String {
        count == 1 ? "A change to your data waits for your approval"
            : "\(count) changes to your data wait for your approval"
    }

    /// Said when Record is pressed on the lock screen while a change waits
    /// (a local notification, as for the other lock-screen refusals).
    static func recordRefusal(count: Int) -> String {
        waitingLine(count: count)
            + ". Open Loore to approve or reject \(count == 1 ? "it" : "them") before you record again."
    }

    /// INTRODUCED HEURISTIC: the longest the end of a reply's audio queue waits
    /// for the announcement's answer. It is asked as soon as the reply is
    /// written, so it is usually in long before; a slow or failed answer ends
    /// the queue without it rather than leaving the user in silence.
    static let announcementWait: Double = 5
}

/// `GET /api/voice/approval-announcement?node=<reply id>`: how many of the
/// reply's write calls wait now, and the clip that says so (nil when none wait,
/// or the clip could not be made: then nothing extra plays).
struct ApprovalAnnouncement: Decodable, Equatable, Sendable {
    var count: Int
    var url: String?
    /// Seconds, when the server knows them.
    var duration: Double?

    init(count: Int, url: String? = nil, duration: Double? = nil) {
        self.count = count
        self.url = url
        self.duration = duration
    }

    enum CodingKeys: String, CodingKey { case count, url, duration }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        count = c.tolerant(.count, default: 0)
        url = c.tolerant(.url)
        duration = c.tolerant(.duration)
    }

    /// The clip to play: only while a change waits.
    var clip: String? {
        guard count > 0, let url, !url.isEmpty else { return nil }
        return url
    }
}
