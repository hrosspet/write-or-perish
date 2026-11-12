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

    /// A person's name as it is printed: "@ben". A name that already has
    /// its "@" keeps the one.
    static func at(_ name: String) -> String {
        var bare = Substring(name)
        while bare.hasPrefix("@") { bare.removeFirst() }
        return bare.isEmpty ? "" : "@\(bare)"
    }

    /// ["a", "b", "c"] → "@a, @b and @c".
    static func joinNames(_ names: [String]) -> String {
        let list = names.map(at).filter { !$0.isEmpty }
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
        let author = at(audience.author)
        let others = max(audience.readers.count - 1, 0)
        let whom = others == 0 ? "you" : "you and \(countWord(others)) other \(others == 1 ? "person" : "people")"
        let only = others == 0 ? "Only you and \(author) can read it" : "Only they can read it"
        return "\(author) shared this with \(whom). \(only), and nobody but \(author) can make it public."
    }

    /// The lines of the readers list under the circles icon: the author first
    /// ("…, the author"), the other readers, the viewer as "You".
    static func readerLines(_ audience: NodeAudience, me: String?) -> [String] {
        var lines = [audience.author == me ? "You, the author" : "\(at(audience.author)), the author"]
        lines += audience.readers.filter { $0 != me }.map(at)
        if let me, audience.readers.contains(me) { lines.append("You") }
        return lines
    }

    static let readersListTitle = "Can read this entry and its replies"

    /// "@anna's".
    static func possessive(_ name: String) -> String { "\(at(name))'s" }

    // MARK: AI replies in a shared thread

    /// Why the AI-reply control is off in a shared thread, in a sentence, or
    /// nil when a reply can be generated. `nodeAIUsage` is the AI usage of
    /// the node the reply would answer.
    static func aiReplyOffReason(_ audience: NodeAudience, nodeAIUsage: AIUsage?, myDefaultAIUsage: AIUsage?) -> String? {
        let entryNone = "This entry's AI usage is None, so no AI can read it."
        if audience.entryAIUsage == .off {
            return audience.isAuthor ? entryNone
                : "\(at(audience.author)) set this entry's AI usage to None, so no AI can read it."
        }
        if nodeAIUsage == .off { return entryNone }
        if myDefaultAIUsage == .off { return "Your own AI usage is set to None in Account settings." }
        return nil
    }

    /// For a reader, over the reply form of someone else's shared thread, under
    /// a node of their own (where LLM Response is).
    static let foreignSharedNote = "Before your reply is sent, Loore names the people who will see it. An AI reply is generated here only when you press LLM Response, and only you see it until you share it."
    /// The same under someone else's node: no LLM Response there (the public
    /// rule, Peter 2026-11-12), so it says how to ask.
    static let foreignSharedNoteOthers = "Before your reply is sent, Loore names the people who will see it. To ask your AI, write your reply first and press LLM Response under it. Only you see the AI reply until you share it."

    static let aiReplyDialogTitle = "Generate an AI reply?"
    static let aiReplyDialogConfirm = "Generate"

    /// ["profile", "memory", "to-do list"] → "profile, memory and to-do list" (no "@").
    static func joinPlain(_ items: [String]) -> String {
        let list = items.filter { !$0.isEmpty }
        guard list.count > 1 else { return list.joined() }
        return list.dropLast().joined(separator: ", ") + " and " + list[list.count - 1]
    }

    /// What the requester's AI reads of their own data for a reply in a
    /// shared thread when a prompt someone else wrote asks for it
    /// (`ai_reads`), in a sentence; nil when it reads none. The reply is the
    /// requester's alone (every AI reply is created private, Peter 2026-11-12).
    static func aiReadsPrivateSentence(_ aiReads: [String]) -> String? {
        let list = joinPlain(aiReads)
        guard !list.isEmpty else { return nil }
        return "Your AI reads your \(list) for this reply, as the prompt shared in this thread asks."
    }

    /// The dialog before an AI reply in a thread with an `ai_reads_key`.
    static func aiReplyPrivateDialogBody(aiReads: [String]) -> String {
        let first = "Only you will see it."
        return aiReadsPrivateSentence(aiReads).map { "\(first) \($0)" } ?? first
    }

    /// The same when the thread's prompt is an older version of Loore's own
    /// default (`ai_reads_older_default`; Peter, 2026-11-12: "show the dialog
    /// for older defaults, but warn the user that they are old").
    static func aiReplyOlderDefaultDialogBody(aiReads: [String]) -> String {
        let first = "This thread uses an older version of Loore's own prompt. Only you will see the reply."
        let list = joinPlain(aiReads)
        return list.isEmpty ? first : "\(first) Your AI reads your \(list) for it."
    }

    // MARK: Sharing the discussion above an entry

    static let sharePathLabel = "Share also the discussion leading up to this node?"
    static let sharePathNote = "Everything from the start of this thread down to this entry is then shared with the same people, including the thread's prompt and the AI replies on the way. People's AI replies use the prompt with their own artifacts, never yours. Without it, only this entry is shared."

    // MARK: The add dialog

    static let addMemberAlsoEarlier = "Also show earlier entries"

    static func addMemberTitle(name: String, circle: String) -> String { "Add \(at(name)) to \(circle)?" }

    static func addMemberConfirm(name: String) -> String { "Add \(at(name))" }

    /// The default adds the person for entries shared from now on. The
    /// entries already shared with the circle open to them only with the
    /// other button.
    static func addMemberBody(name: String, circle: String, sharedCount: Int) -> String {
        let who = at(name)
        let first = "\(who) will read every entry you share with \(circle) from now on."
        guard sharedCount > 0 else { return "\(first) You have not shared anything with it yet." }
        let what = sharedCount == 1 ? "The entry" : "The \(sharedCount) entries"
        let stay = sharedCount == 1 ? "stays" : "stay"
        return "\(first) \(what) you have already shared with it \(stay) closed to \(who) unless you choose \"\(addMemberAlsoEarlier)\"."
    }

    static func addMemberNote(name: String) -> String {
        let who = at(name)
        return "\(who) gets a note in Loore the first time there is something to read. You can remove \(who) at any time; the entries are closed to \(who) from that moment."
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
        return "\(when). \(entry.lastReplyByMe ? "You" : at(by)) replied."
    }

    /// The "Remove <name>" dialog. A person reads an entry through any of
    /// its circles or by being named for it, so the dialog says what
    /// removing them from this one circle does, and what it does not.
    static func removeMemberBody(name: String, circleName: String) -> String {
        let who = at(name)
        return "From this moment \(who) no longer reads your entries through \(circleName). An entry you also shared with \(who) another way (another circle \(who) is in, or by name) stays open to \(who). \(who) is not told."
    }
}
