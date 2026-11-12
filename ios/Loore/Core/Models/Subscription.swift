import Foundation

/// `subscription` on the signed-in user (#438), sent only while the server
/// has payments on (`payments_enabled`). The app shows it in Settings and
/// nothing else: there is no way to buy here, and no link or mention of
/// where to buy (Peter, 2026-11-12). A subscription bought on the web works
/// in the app.
struct SubscriptionState: Decodable, Equatable, Sendable {
    /// "standard", "supporter", "discounted"; nil without a subscription.
    var level: String?
    /// "none", "trialing", "active", "past_due", "ended".
    var status: String
    /// What the account may do: "set_by_hand" or "active" (everything),
    /// "lapsed" (its writing, reading, export and Settings; no AI replies),
    /// "none" (never had a subscription: not let in). Nil from an older server.
    var state: String?
    var currentPeriodEnd: Date?
    var trialEnd: Date?
    var cancelAtPeriodEnd: Bool
    /// Alpha or pro, set by hand: the paid features without a subscription.
    var planSetByHand: Bool
    /// Whether Loore replies on this account now.
    var aiReplies: Bool
    /// An alpha tester with the free month after the opening (#438), which
    /// still runs (`freeMonthActive`) until `freeMonthUntil`.
    var alphaTester: Bool
    var freeMonthActive: Bool
    var freeMonthUntil: Date?

    enum CodingKeys: String, CodingKey {
        case level, status, state
        case currentPeriodEnd = "current_period_end"
        case trialEnd = "trial_end"
        case cancelAtPeriodEnd = "cancel_at_period_end"
        case planSetByHand = "plan_set_by_hand"
        case aiReplies = "ai_replies"
        case alphaTester = "alpha_tester"
        case freeMonthActive = "free_month_active"
        case freeMonthUntil = "free_month_until"
    }

    init(level: String? = nil, status: String = "none", state: String? = nil, currentPeriodEnd: Date? = nil,
         trialEnd: Date? = nil, cancelAtPeriodEnd: Bool = false, planSetByHand: Bool = false,
         aiReplies: Bool = true, alphaTester: Bool = false, freeMonthActive: Bool = false,
         freeMonthUntil: Date? = nil) {
        self.level = level
        self.status = status
        self.state = state
        self.currentPeriodEnd = currentPeriodEnd
        self.trialEnd = trialEnd
        self.cancelAtPeriodEnd = cancelAtPeriodEnd
        self.planSetByHand = planSetByHand
        self.aiReplies = aiReplies
        self.alphaTester = alphaTester
        self.freeMonthActive = freeMonthActive
        self.freeMonthUntil = freeMonthUntil
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        level = c.tolerant(.level)
        status = c.tolerant(.status, default: "none")
        state = c.tolerant(.state)
        currentPeriodEnd = c.tolerant(.currentPeriodEnd)
        trialEnd = c.tolerant(.trialEnd)
        cancelAtPeriodEnd = c.tolerant(.cancelAtPeriodEnd, default: false)
        planSetByHand = c.tolerant(.planSetByHand, default: false)
        aiReplies = c.tolerant(.aiReplies, default: true)
        alphaTester = c.tolerant(.alphaTester, default: false)
        freeMonthActive = c.tolerant(.freeMonthActive, default: false)
        freeMonthUntil = c.tolerant(.freeMonthUntil)
    }

    var levelName: String? {
        switch level {
        case "standard": return "Standard"
        case "supporter": return "Supporter"
        case "discounted": return "Reduced"
        default: return nil
        }
    }

    /// The Settings row: level, state and date, as the web says them, with
    /// nothing about buying (no price, no "subscribe", no "manage").
    func statusText(plan: String) -> String {
        // A live subscription is shown also on a plan set by hand: it still
        // renews until it is cancelled on the web.
        let live = ["trialing", "active", "past_due"].contains(status)
        if planSetByHand && !live {
            return "Included with your plan (\(plan.capitalized))."
        }
        // An alpha tester without a live subscription: the free month, and
        // what stays after it.
        if alphaTester && !live {
            if freeMonthActive { return "Free month until \(freeMonthUntil.map(Self.day) ?? "—")." }
            return "Free month ended. Your writing, reading and export stay; AI replies, Voice mode and profile updates are paused."
        }
        let name = levelName ?? "Subscription"
        let end = currentPeriodEnd.map(Self.day)
        let trial = (trialEnd ?? currentPeriodEnd).map(Self.day)
        switch status {
        case "trialing":
            // Subscribed during the free month: the trial is its rest.
            if alphaTester {
                if cancelAtPeriodEnd { return "\(name), cancelled. Free month until \(trial ?? "—")." }
                return "\(name). Free month until \(trial ?? "—")."
            }
            if cancelAtPeriodEnd { return "\(name) trial, cancelled. It ends on \(trial ?? "—")." }
            return "\(name) trial until \(trial ?? "—")."
        case "active":
            if cancelAtPeriodEnd { return "\(name), cancelled. It ends on \(end ?? "—")." }
            return "\(name). Renews on \(end ?? "—")."
        case "past_due":
            return "\(name). The last payment didn't go through, so AI replies, Voice mode and profile updates are paused."
        case "ended":
            return "Ended. Your writing, reading and export stay; AI replies, Voice mode and profile updates are paused."
        default:
            return "No subscription."
        }
    }

    static func day(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "MMMM d, yyyy"
        return f.string(from: date)
    }
}
