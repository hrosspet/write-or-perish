import Foundation

/// Circles: wording shared by the picker, the entry page and the dialogs
/// (port of `frontend/src/utils/circles.js`; the same sentences, word for word).
enum CirclesWording {
    private static let numberWords = [
        "no", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "eleven", "twelve",
    ]

    /// "two" … "twelve", then digits.
    static func countWord(_ n: Int) -> String {
        numberWords.indices.contains(n) ? numberWords[n] : String(n)
    }

    /// ["a", "b", "c"] → "a, b and c".
    static func joinNames(_ names: [String]) -> String {
        let list = names.filter { !$0.isEmpty }
        guard list.count > 1 else { return list.joined() }
        return list.dropLast().joined(separator: ", ") + " and " + list[list.count - 1]
    }

    /// Names typed into "for this entry only": commas or spaces, @ optional.
    static func splitNames(_ text: String) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for raw in text.replacingOccurrences(of: ",", with: " ").split(whereSeparator: \.isWhitespace) {
            var name = String(raw)
            while name.hasPrefix("@") { name.removeFirst() }
            guard !name.isEmpty, seen.insert(name.lowercased()).inserted else { continue }
            out.append(name)
        }
        return out
    }

    /// The typed text ends a name: a separator was typed last, or it is empty.
    static func endsAName(_ text: String) -> Bool {
        guard let last = text.last else { return true }
        return last == "," || last.isWhitespace
    }

    /// The sentence under the picker, before saving.
    static func readersSentence(_ readers: [String], what: String = "entry") -> String {
        guard !readers.isEmpty else { return "" }
        return "\(joinNames(readers)) can read this \(what) and reply to it. Nobody else can."
    }

    /// The save button: "Share with Close friends" for one circle and no extra
    /// names, else "Share with 5 people".
    static func shareButtonLabel(circles: [PeopleCircle], circleIds: [Int], names: [String], readers: [String]) -> String {
        let picked = circles.filter { circleIds.contains($0.id) }
        if picked.count == 1 && names.isEmpty { return "Share with \(picked[0].name)" }
        let n = readers.count
        if n == 0 { return "Share" }
        return "Share with \(n) \(n == 1 ? "person" : "people")"
    }

    /// Everyone who sees a reply in a shared thread, except the one writing:
    /// the author and the readers.
    static func replyAudienceNames(_ audience: NodeAudience?, me: String?) -> [String] {
        guard let audience else { return [] }
        return ([audience.author] + audience.readers).filter { !$0.isEmpty && $0 != me }
    }

    /// The banner over a shared entry, for a reader.
    static func sharedBannerText(_ audience: NodeAudience) -> String {
        let author = audience.author
        let others = max(audience.readers.count - 1, 0)
        let whom = others == 0 ? "you" : "you and \(countWord(others)) other \(others == 1 ? "person" : "people")"
        let only = others == 0 ? "Only you and \(author) can read it" : "Only they can read it"
        return "\(author) shared this with \(whom). \(only), and nobody but \(author) can make it public."
    }

    /// The lines of the readers list under the circles icon: the author first
    /// ("…, the author"), the other readers, the viewer as "You".
    static func readerLines(_ audience: NodeAudience, me: String?) -> [String] {
        var lines = [audience.author == me ? "You, the author" : "\(audience.author), the author"]
        lines += audience.readers.filter { $0 != me }
        if let me, audience.readers.contains(me) { lines.append("You") }
        return lines
    }

    static let readersListTitle = "Can read this entry and its replies"

    static func possessive(_ name: String) -> String { "\(name)'s" }

    /// Why "Ask my AI" is off, in a sentence, or nil when it can be asked.
    static func askAIOffReason(_ audience: NodeAudience, myDefaultAIUsage: AIUsage?) -> String? {
        if audience.entryAIUsage == .off {
            return "\(audience.author) set this entry's AI usage to None, so no AI can read it."
        }
        if myDefaultAIUsage == .off { return "Your own AI usage is set to None in Account settings." }
        return nil
    }

    // MARK: The add dialog

    static func addMemberBody(name: String, circle: String, sharedCount: Int) -> String {
        if sharedCount == 0 {
            return "\(name) will be able to read every entry you share with \(circle) from now on. You have not shared anything with it yet."
        }
        let what = sharedCount == 1 ? "entry" : "\(sharedCount) entries"
        return "\(name) will be able to read the \(what) you have already shared with \(circle), and every entry you share with it from now on."
    }

    static func addMemberNote(name: String) -> String {
        "\(name) gets a note in Loore the first time there is something to read. You can remove \(name) at any time; the entries are closed to \(name) from that moment."
    }

    static func deleteCircleBody(name: String, sharedCount: Int) -> String {
        guard sharedCount > 0 else { return "The list of people is deleted. Nothing was shared with this circle." }
        let one = sharedCount == 1
        return "The \(one ? "entry" : "\(sharedCount) entries") you shared with \(name) \(one ? "is" : "are") closed to its people from this moment, unless you also shared \(one ? "it" : "them") another way. The entries stay yours."
    }

    // MARK: Shared with you

    /// "Today, 9:40" / "28 October" / "28 October 2025" (web `sharedDate`).
    static func sharedDate(_ date: Date?, now: Date = Date(), timeZone: TimeZone = .current) -> String {
        guard let date else { return "" }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        if cal.isDate(date, inSameDayAs: now) {
            let c = cal.dateComponents([.hour, .minute], from: date)
            return "Today, \(c.hour ?? 0):" + String(format: "%02d", c.minute ?? 0)
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_GB")
        formatter.timeZone = timeZone
        formatter.dateFormat = cal.component(.year, from: date) == cal.component(.year, from: now) ? "d MMMM" : "d MMMM yyyy"
        return formatter.string(from: date)
    }

    /// The line under a shared entry: its date, and who replied last.
    static func entryMeta(_ entry: SharedWithMe.Entry, now: Date = Date(), timeZone: TimeZone = .current) -> String {
        let when = sharedDate(entry.createdAt, now: now, timeZone: timeZone)
        guard let by = entry.lastReplyBy, !by.isEmpty else { return when }
        return "\(when). \(entry.lastReplyByMe ? "You" : by) replied."
    }
}
