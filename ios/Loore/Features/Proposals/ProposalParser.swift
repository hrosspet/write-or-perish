import Foundation

/// Port of the pure functions in `frontend/src/components/ProposalInline.js`
/// (map D §6.2). Its jest tests are ported one-to-one in `ProposalParserTests`.
enum ProposalParser {
    struct Sections: Equatable {
        var completed: String?
        var newTasks: String?
        var priority: String?
        var note: String?
        var issueTitle: String?
        var issueDescription: String?
        var issueCategory: String?
        var feedback: String?
        var feedbackCategory: String?
        var share: String?
        var shareType: String?
    }

    struct ShareBlock: Equatable {
        var content: String
        var type: String
    }

    struct PriorityItem: Equatable {
        var text: String
        var hint: String
    }

    static let shareFenceOpen = #"^:::share(?:[ \t]+([A-Za-z]+))?[ \t]*\r?$"#
    static let shareFenceClose = #"^:::[ \t]*\r?$"#

    /// ProposalInline's own `stripInlineMarkdown`: bold markers only.
    static func stripBold(_ text: String) -> String {
        JSRegex.replaceAll(JSRegex.replaceAll(text, #"\*\*(.+?)\*\*"#, "$1"), #"__(.+?)__"#, "$1")
    }

    static func stripProposalTag(_ text: String) -> String {
        JSRegex.replaceAll(text, #"\s*\[\w+-proposal:[^\]]*\]"#, "")
    }

    private static func isOpenFence(_ line: String) -> Bool {
        JSRegex.test(line, shareFenceOpen, options: [.caseInsensitive])
    }

    private static func isCloseFence(_ line: String) -> Bool {
        JSRegex.test(line, shareFenceClose)
    }

    static func hasShareBlocks(_ text: String?) -> Bool {
        guard let text, !text.isEmpty else { return false }
        return text.jsLines.contains(where: isOpenFence)
    }

    /// One block per `:::share` fence (an unclosed fence runs to the end);
    /// without fences, the legacy `### Share` / `### Share type` headings.
    static func parseShareBlocks(_ text: String?) -> [ShareBlock] {
        guard let text, !text.isEmpty else { return [] }
        var shares: [ShareBlock] = []
        var current: (type: String, lines: [String])?
        func flush() {
            guard let block = current else { return }
            let content = block.lines.joined(separator: "\n").jsTrimmed
            if !content.isEmpty { shares.append(ShareBlock(content: content, type: block.type)) }
            current = nil
        }
        for line in text.jsLines {
            if current != nil {
                if isCloseFence(line) { flush() } else { current?.lines.append(line) }
                continue
            }
            if let m = JSRegex.firstMatch(line, shareFenceOpen, options: [.caseInsensitive]) {
                current = ((m[1] ?? "").lowercased(), [])
            }
        }
        flush()
        if !shares.isEmpty { return shares }
        let legacy = parseOrientResponse(text)
        if let share = legacy.share, !share.isEmpty {
            return [ShareBlock(content: share, type: legacy.shareType ?? "")]
        }
        return []
    }

    static func parseTodoItems(_ text: String) -> [String] {
        text.jsLines
            .map { line in
                let noBullet = JSRegex.replaceFirst(line, #"^[-*]\s*"#, "")
                return JSRegex.replaceFirst(noBullet, #"^\[[ xX]\]\s*"#, "").jsTrimmed
            }
            .map(stripBold)
            .filter { !$0.isEmpty }
    }

    static func parsePriorityItems(_ text: String) -> [PriorityItem] {
        text.jsLines
            .filter { !$0.jsTrimmed.isEmpty }
            .map { line -> PriorityItem in
                var cleaned = JSRegex.replaceFirst(line, #"^\d+[.)]\s*"#, "")
                cleaned = JSRegex.replaceFirst(cleaned, #"^[-*]\s*"#, "")
                cleaned = JSRegex.replaceFirst(cleaned, #"^\[[ xX]\]\s*"#, "").jsTrimmed
                if let m = JSRegex.firstMatch(cleaned, #"^(.+?)\s*[—–]\s*(.+)$"#), let a = m[1], let b = m[2] {
                    return PriorityItem(text: stripBold(a.jsTrimmed), hint: b.jsTrimmed)
                }
                if let m = JSRegex.firstMatch(cleaned, #"^(.+?)\s*\(([^)]+)\)\s*$"#), let a = m[1], let b = m[2] {
                    return PriorityItem(text: stripBold(a.jsTrimmed), hint: b.jsTrimmed)
                }
                return PriorityItem(text: stripBold(cleaned), hint: "")
            }
            .filter { !$0.text.isEmpty }
    }

    /// Removes fenced `:::share` blocks (their `###` headings are the share's own).
    static func stripShareBlocks(_ text: String) -> String {
        var out: [String] = []
        var inFence = false
        for line in text.jsLines {
            if inFence {
                if isCloseFence(line) { inFence = false }
                continue
            }
            if isOpenFence(line) { inFence = true; continue }
            out.append(line)
        }
        return out.joined(separator: "\n")
    }

    static func parseOrientResponse(_ text: String) -> Sections {
        var sections = Sections()
        let parts = JSRegex.split(stripShareBlocks(text), #"^###\s+"#, options: [.anchorsMatchLines])
        // parts[0] is the lead-in before the first heading: never a section.
        for part in parts.dropFirst() {
            if part.jsTrimmed.isEmpty { continue }
            let firstNewline = part.jsIndexOf("\n")
            if firstNewline < 0 { continue }
            let heading = part.jsSubstring(0, firstNewline).jsTrimmed.lowercased()
            let body = part.jsSubstring(firstNewline + 1).jsTrimmed
            func firstLine(_ s: String) -> String { (s.jsLines.first ?? "").jsTrimmed.lowercased() }
            if heading.contains("completed") { sections.completed = body }
            else if heading.contains("new task") { sections.newTasks = body }
            else if heading.contains("priority") { sections.priority = body }
            else if heading.contains("note") { sections.note = stripProposalTag(body) }
            else if heading.contains("issue title") || heading == "title" { sections.issueTitle = stripProposalTag(body).jsTrimmed }
            else if heading == "description" { sections.issueDescription = stripProposalTag(body).jsTrimmed }
            else if heading == "category" { sections.issueCategory = firstLine(stripProposalTag(body)) }
            else if heading == "feedback" { sections.feedback = stripProposalTag(body).jsTrimmed }
            else if heading == "feedback category" { sections.feedbackCategory = firstLine(stripProposalTag(body)) }
            else if heading == "share" { sections.share = stripProposalTag(body).jsTrimmed }
            else if heading == "share type" { sections.shareType = firstLine(stripProposalTag(body)) }
        }
        return sections
    }

    static func hasProposalSections(_ text: String?) -> Bool {
        guard let text, !text.isEmpty else { return false }
        if hasShareBlocks(text) { return true }
        let headings = JSRegex.allMatches(stripShareBlocks(text), #"^###\s+(.+)"#, options: [.anchorsMatchLines])
            .map { JSRegex.replaceFirst($0, #"^###\s+"#, "").lowercased() }
        let taskKeywords = ["completed", "new task", "new tasks", "priority"]
        let hasTodo = headings.contains { h in taskKeywords.contains { h.contains($0) } }
        let hasIssue = headings.contains { $0.contains("issue title") || $0 == "title" }
            && headings.contains { $0.contains("description") }
        let hasFeedback = headings.contains { $0 == "feedback" }
        let hasShare = headings.contains { $0 == "share" }
        return hasTodo || hasIssue || hasFeedback || hasShare
    }

    private static let sectionOrder = ["completed", "new task"]
    private static let sectionTitles = ["completed": "Completed", "new task": "New Tasks"]

    /// Moves a todo item between `###` sections (the card's tick / untick),
    /// creating the target section next to the source when it is missing (#377).
    static func moveProposalItem(_ content: String, itemText: String, from fromSection: String,
                                 to toSection: String, prepend: Bool = false) -> String {
        var lines = content.jsLines
        let sectionRegex = #"^###\s+(.+)"#
        struct Section { var heading: String; var start: Int; var end: Int }
        var sections: [Section] = []
        for (i, line) in lines.enumerated() {
            if let m = JSRegex.firstMatch(line, sectionRegex), let h = m[1] {
                if !sections.isEmpty { sections[sections.count - 1].end = i }
                sections.append(Section(heading: h.jsTrimmed.lowercased(), start: i, end: lines.count))
            }
        }
        if !sections.isEmpty { sections[sections.count - 1].end = lines.count }

        guard let from = sections.first(where: { $0.heading.contains(fromSection) }) else { return content }
        let to = sections.first(where: { $0.heading.contains(toSection) })

        var matchIdx = -1
        var rawLine = ""
        if from.start + 1 < from.end {
            for i in (from.start + 1)..<from.end {
                var s = JSRegex.replaceFirst(lines[i], #"^[-*]\s*"#, "")
                s = JSRegex.replaceFirst(s, #"^\[[ xX]\]\s*"#, "")
                s = JSRegex.replaceFirst(s, #"^\d+[.)]\s*"#, "").jsTrimmed
                if stripBold(s) == itemText {
                    matchIdx = i
                    rawLine = lines[i]
                    break
                }
            }
        }
        guard matchIdx >= 0 else { return content }
        lines.remove(at: matchIdx)

        if to != nil {
            var toInsert = -1
            for (i, line) in lines.enumerated() {
                if let m = JSRegex.firstMatch(line, sectionRegex), let h = m[1], h.jsTrimmed.lowercased().contains(toSection) {
                    toInsert = i + 1
                    if !prepend {
                        while toInsert < lines.count && !JSRegex.test(lines[toInsert], sectionRegex)
                                && !lines[toInsert].jsTrimmed.isEmpty {
                            toInsert += 1
                        }
                    }
                    break
                }
            }
            if toInsert >= 0 { lines.insert(rawLine, at: toInsert) }
        } else {
            let heading = "### \(sectionTitles[toSection] ?? toSection)"
            let rank = { (key: String) in sectionOrder.firstIndex(of: key) ?? -1 }
            if rank(toSection) >= 0 && rank(toSection) < rank(fromSection) {
                // Target comes first: new section right above the source heading.
                lines.insert(contentsOf: [heading, rawLine, ""], at: from.start)
            } else {
                // Target comes after: below the source section's last non-blank line
                // (the item was removed, so the section's end moved up by one).
                var last = from.start
                if from.start + 1 < from.end - 1 {
                    for i in (from.start + 1)..<(from.end - 1) where !lines[i].jsTrimmed.isEmpty { last = i }
                }
                lines.insert(contentsOf: ["", heading, rawLine], at: last + 1)
            }
        }
        return lines.joined(separator: "\n")
    }

    /// The prose around a proposal: the lead-in (`before`, above the card) and
    /// trailing commentary (`after`, below it); the structured sections and the
    /// share fences are left out. `shareOnly` (user-authored nodes) removes
    /// only the fences.
    static func splitProposalText(_ text: String?, shareOnly: Bool = false) -> (before: String, after: String) {
        guard let text, !text.isEmpty else { return ("", "") }
        var before: [String] = []
        var after: [String] = []
        var inProposal = false
        var valueSection = false
        var valueConsumed = false
        var seenProposal = false
        var proposalHeadings = ["completed", "new task", "new tasks", "priority", "priority order",
                                "issue title", "title", "description", "category", "feedback"]
        let hasTaskSection = !shareOnly
            && JSRegex.test(stripShareBlocks(text), #"^###\s+.*(completed|new task|priority)"#,
                            options: [.anchorsMatchLines, .caseInsensitive])
        if hasTaskSection { proposalHeadings.append("note") }
        let exactProposalHeadings = ["share", "share type"]
        func isValueHeading(_ h: String) -> Bool { h == "category" || h == "feedback category" || h == "share type" }
        func keep(_ line: String) { if seenProposal { after.append(line) } else { before.append(line) } }
        var inShareFence = false
        for line in text.jsLines {
            if inShareFence {
                if isCloseFence(line) { inShareFence = false }
                continue
            }
            if isOpenFence(line) {
                inShareFence = true
                inProposal = false
                valueSection = false
                seenProposal = true
                continue
            }
            let headingMatch = shareOnly ? nil : JSRegex.firstMatch(line, #"^###\s+(.+)"#)
            if let headingMatch, let raw = headingMatch[1] {
                let h = raw.jsTrimmed.lowercased()
                if proposalHeadings.contains(where: { h.contains($0) || h == $0 }) || exactProposalHeadings.contains(h) {
                    inProposal = true
                    valueSection = isValueHeading(h)
                    valueConsumed = false
                    seenProposal = true
                    continue
                } else {
                    inProposal = false
                    valueSection = false
                }
            }
            if inProposal && valueSection {
                if line.jsTrimmed.isEmpty { continue }
                if !valueConsumed {
                    valueConsumed = true
                    continue
                }
                inProposal = false
                valueSection = false
                keep(line)
                continue
            }
            if !inProposal { keep(line) }
        }
        return (before.joined(separator: "\n").jsTrimmed, after.joined(separator: "\n").jsTrimmed)
    }

    static func stripProposalSections(_ text: String?) -> String {
        guard let text, !text.isEmpty else { return text ?? "" }
        let split = splitProposalText(text)
        return [split.before, split.after].filter { !$0.isEmpty }.joined(separator: "\n\n")
    }
}
