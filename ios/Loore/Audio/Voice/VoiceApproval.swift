import Foundation

/// A Voice conversation whose thread has write tool calls waiting for the user's
/// approval (`Core/Models/ToolApprovals.swift`). Peter, 2026-11-12, on the
/// approval dialog appearing on the Voice screen: "yes, I'd like that. A spoken
/// announcement should also be added after the model reply, informing the user
/// they need to confirm, or reject the tool called. Also, the lockscreen controls
/// shouldn't allow next recording (replay of the reply should be still possible,
/// but the announcement should still be there after the LLM reply)".
///
/// The announcement is part of the reply's audio (the server adds it to the
/// reply's TTS), so the app plays and replays it like the rest of the reply.
/// While a call waits, no new Voice turn starts in the thread: not from the
/// Voice screen, not from the Live Activity, and the server refuses one too
/// (`streaming/init` answers 409 `approval_pending`).
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
}
