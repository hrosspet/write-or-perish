import Foundation

/// A refused Voice turn, said aloud (#651; the thread's length first, #648):
/// someone talking hands-free, or with the phone locked, does not see the
/// message on the screen. One fixed sentence per refusal, by the code the server
/// answers with. It is said in the replies' voice from a cached clip (`GET
/// /api/voice/refusal/<code>.mp3`, made once per voice, as the approval
/// announcement is), never as part of a reply's audio: the conversation plays it
/// when the refusal comes and stops it when a new recording starts. Where the
/// clip can't be had (offline, no clip, too slow) the device's own speech says
/// the same sentence. The sentences are the server's (`backend/utils/
/// voice_refusals.py`) and the web's: plain words, no numbers, no prices, and
/// nothing that points to buying.
enum SpokenRefusal: String, CaseIterable, Equatable, Sendable {
    /// The monthly usage limit (402 `monthly_spend_limit_reached`).
    case usageLimit = "monthly_spend_limit_reached"
    /// AI usage None for the account or the thread (403 `ai_usage_none`).
    case aiUsage = "ai_usage_none"
    /// A change the AI proposed waits for the user's approval (409 `approval_pending`).
    case approvalWaiting = "approval_pending"
    /// The thread is too long for another reply (409 `thread_too_long`).
    case threadTooLong = "thread_too_long"
    /// Payments on (#438): the account's access ended while the turn was recorded.
    /// The recording is kept and transcribed, only the reply is skipped. One per
    /// reason; the sentence is the text the entry and the screen show (it names
    /// the state, never where or how to pay).
    case subscriptionEnded = "subscription_required_ended"
    case paymentFailed = "subscription_required_past_due"
    case freeMonthEnded = "subscription_required_free_month"
    case noSubscription = "subscription_required_none"

    /// The refusal a server code names (a recording's `warning_code`), or nil.
    init?(code: String?) {
        guard let code, let refusal = SpokenRefusal(rawValue: code) else { return nil }
        self = refusal
    }

    /// The access-ended refusal whose text is *message* (a 402 for a kept turn
    /// carries it as its message), or nil.
    init?(keptRecordingMessage message: String?) {
        guard let message,
              let refusal = Self.allCases.first(where: { $0.isAccessEnded && $0.text == message })
        else { return nil }
        self = refusal
    }

    /// A recording kept after the account's access ended (#438).
    var isAccessEnded: Bool {
        switch self {
        case .subscriptionEnded, .paymentFailed, .freeMonthEnded, .noSubscription: return true
        default: return false
        }
    }

    /// What the clip says, and what the device says when there is no clip.
    var text: String {
        switch self {
        case .usageLimit:
            return "You've reached your monthly usage limit, so Loore can't reply until it resets."
        case .aiUsage:
            return "AI usage is set to None, so Loore can't reply here."
        case .approvalWaiting:
            return "A change to your data is waiting for your approval. Approve or reject it on the screen to continue."
        case .threadTooLong:
            return ThreadLength.spokenText
        case .subscriptionEnded:
            return "Your subscription has ended, so Loore doesn't reply. Your recording is saved."
        case .paymentFailed:
            return "Your last payment didn't go through, so Loore doesn't reply. Your recording is saved."
        case .freeMonthEnded:
            return "Your free month has ended, so Loore doesn't reply. Your recording is saved."
        case .noSubscription:
            return "Loore doesn't reply on an account without a subscription. Your recording is saved."
        }
    }

    var clipPath: String { APIPath.voiceRefusalClip(rawValue) }

    /// INTRODUCED HEURISTIC: the longest a refusal waits for its clip before the
    /// device says the sentence instead. A clip already made arrives within a
    /// second; the first request for a voice makes it (one speech call, about two
    /// seconds). A refusal said much later is no longer an answer to the press.
    static let clipWait: Double = 4
}

/// Says a refusal: its clip, asked for once per app run, played on its own;
/// otherwise the device's speech. The parts are closures (live: `AudioCenter`),
/// so the order is unit-tested.
@MainActor
final class RefusalVoice {
    /// The clip's bytes, or nil (offline, refused, not made, too slow).
    var fetch: (SpokenRefusal) async -> Data? = { _ in nil }
    /// Plays the bytes and returns once they have played (true), or false when
    /// they could not be played or were stopped.
    var playClip: (Data) async -> Bool = { _ in false }
    var stopClip: () -> Void = {}
    /// The device's speech; returns once said or stopped.
    var sayOnDevice: (String) async -> Void = { _ in }
    var stopDevice: () -> Void = {}

    private var clips: [SpokenRefusal: Data] = [:]
    /// Bumped by every sentence and by `stop()`: a sentence that was replaced or
    /// stopped while its clip was fetched says nothing.
    private var token = 0

    /// Returns once the sentence has been said, or stopped.
    func say(_ refusal: SpokenRefusal) async {
        token += 1
        let mine = token
        stopClip()
        stopDevice()
        var data = clips[refusal]
        if data == nil {
            data = await fetch(refusal)
            if let data { clips[refusal] = data }
        }
        guard mine == token else { return }
        if let data, await playClip(data) { return }
        guard mine == token else { return }
        await sayOnDevice(refusal.text)
    }

    /// Stops the sentence at once (a new recording starts, or Voice closes).
    func stop() {
        token += 1
        stopClip()
        stopDevice()
    }
}
