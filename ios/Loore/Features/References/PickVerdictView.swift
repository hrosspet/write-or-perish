import SwiftUI

/// `POST /api/nodes/<id>/feed-picks/<item>/verdict`: the pick's marks as stored.
/// `feedback` is the tweet verdict that counts for this pick (#352).
struct PickVerdictAnswer: Decodable, Equatable, Sendable {
    var introVerdict: String?
    var feedback: String?
    var feedbackShared: Bool
    var readAt: Date?

    enum CodingKeys: String, CodingKey {
        case feedback
        case introVerdict = "intro_verdict"
        case feedbackShared = "feedback_shared"
        case readAt = "read_at"
    }

    init(introVerdict: String? = nil, feedback: String? = nil, feedbackShared: Bool = false, readAt: Date? = nil) {
        self.introVerdict = introVerdict
        self.feedback = feedback
        self.feedbackShared = feedbackShared
        self.readAt = readAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        introVerdict = c.tolerant(.introVerdict)
        feedback = c.tolerant(.feedback)
        feedbackShared = c.tolerant(.feedbackShared, default: false)
        readAt = c.tolerant(.readAt)
    }
}

/// The verdicts on one pick of a gleaning (web `PickVerdict`, #502, option B of
/// the 2026-10-14 mockup, Peter). A pick is Loore's intro (the paragraph above
/// the card) and the tweet in the card, each good, unrated or bad.
///
/// At rest one ⊕ ⊖ pair under the card ("Good pick" / "Bad pick"); a tap gives
/// both parts that verdict and opens the pair into "The intro ⊕ ⊖  The tweet
/// ⊕ ⊖". A lit glyph clears its part, the other one changes it; with both
/// cleared the pair closes again. A pick with any verdict opens on load.
/// VoiceOver's focus follows as the web's keyboard focus does: to the intro's
/// glyph when the pair opens, to the pick's when it closes.
///
/// One request per tap, the pick-level one carrying both parts. The tweet's
/// part is the reference's own verdict (it marks the tweet read); the intro's is
/// the pick's, its value only. The glyphs show what the server answered.
enum PickVerdict {
    enum Subject: String, Sendable { case pick, intro, tweet }

    struct Marks: Equatable, Sendable {
        var intro: String?
        var tweet: String?
        var opened: Bool { intro != nil || tweet != nil }

        func value(_ subject: Subject) -> String? {
            switch subject {
            case .pick: return nil
            case .intro: return intro
            case .tweet: return tweet
            }
        }
    }

    /// The names the glyphs carry (Peter, 2026-10-14: "the intro", not "the point").
    static func label(_ subject: Subject, _ value: String) -> String {
        let good = value == "good"
        switch subject {
        case .pick: return good ? "Good pick" : "Bad pick"
        case .intro: return good ? "Good intro" : "Bad intro"
        case .tweet: return good ? "Good tweet" : "Bad tweet"
        }
    }

    /// What a tap sends: the pick's tap sets both parts, a part's tap only its
    /// own, and a lit glyph clears it.
    static func body(_ subject: Subject, _ value: String, marks: Marks) -> [String: JSONValue] {
        switch subject {
        case .pick:
            return ["intro": .string(value), "tweet": .string(value)]
        case .intro, .tweet:
            let next: JSONValue = marks.value(subject) == value ? .null : .string(value)
            return [subject.rawValue: next]
        }
    }

    /// Where focus goes once the server answered, when the pair opened or closed.
    static func focusAfter(_ value: String, was: Marks, now: Marks) -> String? {
        if !was.opened && now.opened { return "intro-\(value)" }
        if was.opened && !now.opened { return "pick-\(value)" }
        return nil
    }

    static func send(nodeId: Int, itemId: Int, body: [String: JSONValue], api: APIClient) async throws -> PickVerdictAnswer {
        try await api.post(APIPath.feedPickVerdict(nodeId, item: itemId), json: .object(body))
    }
}

struct PickVerdictView: View {
    let nodeId: Int
    let itemId: Int
    let intro: String?
    let tweet: String?
    var tweetShared = false
    /// The verdict the model knew when it picked, named beside an unrated tweet.
    var ratedBefore: (verdict: String, at: Date?)?
    /// (answer, the parts sent)
    var onChange: ((PickVerdictAnswer, Set<String>) -> Void)?

    @Environment(AppState.self) private var app
    @State private var marks = PickVerdict.Marks()
    @State private var sharedShown = false
    @State private var saving = false
    @AccessibilityFocusState private var focus: String?

    var body: some View {
        FlowLayout(spacing: 18, lineSpacing: 0) {
            if marks.opened {
                part("The intro", .intro)
                part("The tweet", .tweet)
            } else {
                pair(.pick)
            }
            if let ratedBefore, marks.tweet == nil {
                Text("You rated this tweet \(ratedBefore.verdict) on \(LooreDateFormat.date(ratedBefore.at, relative: false))")
                    .font(LooreFont.sansOblique(12.8, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .frame(minHeight: 40)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
        .padding(.top, 2)
        .onAppear { marks = .init(intro: intro, tweet: tweet); sharedShown = tweetShared }
        .onChange(of: intro) { _, new in marks.intro = new }
        .onChange(of: tweet) { _, new in marks.tweet = new; sharedShown = tweetShared }
        .accessibilityIdentifier("pick.verdict")
    }

    private func part(_ title: String, _ subject: PickVerdict.Subject) -> some View {
        HStack(spacing: 2) {
            Text(title)
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textMuted)
                .accessibilityHidden(true)
            pair(subject)
        }
    }

    private func pair(_ subject: PickVerdict.Subject) -> some View {
        HStack(spacing: 0) {
            glyph(subject, "good", symbol: "plus.circle")
            glyph(subject, "bad", symbol: "minus.circle")
        }
    }

    private func glyph(_ subject: PickVerdict.Subject, _ value: String, symbol: String) -> some View {
        let selected = marks.value(subject) == value
        var label = PickVerdict.label(subject, value)
        if subject == .tweet && selected && sharedShown { label += " (your rating from another reply)" }
        return Button { choose(subject, value) } label: {
            Image(systemName: selected ? symbol + ".fill" : symbol)
                .font(.system(size: 16, weight: .light))
                .foregroundStyle(selected ? LooreColor.accent : LooreColor.textMuted)
                .frame(width: 40, height: 40)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(label)
        .accessibilityLabel(label)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityFocused($focus, equals: "\(subject.rawValue)-\(value)")
        .accessibilityIdentifier("pick.\(subject.rawValue).\(value)")
    }

    private func choose(_ subject: PickVerdict.Subject, _ value: String) {
        guard !saving else { return }
        let body = PickVerdict.body(subject, value, marks: marks)
        let was = marks
        saving = true
        Task {
            defer { saving = false }
            do {
                let answer = try await PickVerdict.send(nodeId: nodeId, itemId: itemId, body: body, api: app.api)
                let now = PickVerdict.Marks(intro: answer.introVerdict, tweet: answer.feedback)
                marks = now
                sharedShown = answer.feedbackShared
                if let next = PickVerdict.focusAfter(value, was: was, now: now) { focus = next }
                onChange?(answer, Set(body.keys))
            } catch {
                app.toasts.show("Could not save your verdict.", duration: 4)
            }
        }
    }
}
