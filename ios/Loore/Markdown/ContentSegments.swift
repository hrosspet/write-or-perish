import Foundation

/// The custom syntax that is handled before markdown (web `QuotedContent`,
/// map D §3.1): `{quote:N}`, `{quote_ext:N}` and the `{user_*}` artifact
/// placeholders split the content, and each text piece between them is its
/// own markdown document (so a list interrupted by a quote becomes two lists).
enum ContentSegment: Equatable, Sendable {
    case text(String)
    case quote(Int)
    case externalQuote(Int)
    case artifact(ArtifactPlaceholder)
}

/// `{user_<kind>}` placeholders in system-prompt nodes.
enum ArtifactPlaceholder: String, CaseIterable, Sendable {
    case profile, todo, recentRaw = "recent_raw", recent, aiPreferences = "ai_preferences",
         memory, scratchpad, intentions

    /// The web's `InlineArtifactSection` labels.
    var label: String {
        switch self {
        case .profile: return "User Profile"
        case .todo: return "User TODO"
        case .recent: return "Recent Context Summary"
        case .aiPreferences: return "AI Preferences"
        case .recentRaw: return "Recent Context Raw"
        case .memory: return "Memory"
        case .scratchpad: return "Scratchpad"
        case .intentions: return "Intentions"
        }
    }
}

enum ContentSegmenter {
    /// `COMBINED_PATTERN` (recent_raw before recent, quote_ext before quote).
    static let combinedPattern =
        #"(\{quote_ext:\d+\}|\{quote:\d+\}|\{user_(?:profile|todo|recent_raw|recent|ai_preferences|memory|scratchpad|intentions)\})"#
    /// Quote markers in a node's content (`NodeDetail`).
    static let quoteMarkerPattern = #"\{quote(?:_ext)?:\d+\}"#

    /// Replaces `{share_guidance}` / `{external_content_guidance}` (and one
    /// following newline) with the texts the model received, or removes them.
    static func applyGuidance(_ content: String, shareGuidance: String?, externalGuidance: String?) -> String {
        var s = content
        let share = shareGuidance ?? ""
        s = JSRegex.replaceAll(s, #"\{share_guidance\}\n?"#,
                               NSRegularExpression.escapedTemplate(for: share.isEmpty ? "" : share + "\n"))
        let external = externalGuidance ?? ""
        s = JSRegex.replaceAll(s, #"\{external_content_guidance\}\n?"#,
                               NSRegularExpression.escapedTemplate(for: external.isEmpty ? "" : external + "\n"))
        return s
    }

    /// Splits content on the combined marker pattern, dropping empty pieces.
    static func segments(_ content: String) -> [ContentSegment] {
        var out: [ContentSegment] = []
        for part in JSRegex.splitKeepingCaptures(content, combinedPattern) where !part.isEmpty {
            if let m = JSRegex.firstMatch(part, #"^\{quote_ext:(\d+)\}$"#), let id = m[1].flatMap({ Int($0) }) {
                out.append(.externalQuote(id))
            } else if let m = JSRegex.firstMatch(part, #"^\{quote:(\d+)\}$"#), let id = m[1].flatMap({ Int($0) }) {
                out.append(.quote(id))
            } else if let m = JSRegex.firstMatch(part, #"^\{user_(profile|todo|recent_raw|recent|ai_preferences|memory|scratchpad|intentions)\}$"#),
                      let kind = m[1].flatMap(ArtifactPlaceholder.init(rawValue:)) {
                out.append(.artifact(kind))
            } else {
                out.append(.text(part))
            }
        }
        return out
    }

    /// The quote markers a text holds, joined (`NodeDetail`'s refetch key).
    static func quoteMarkerKey(_ content: String?) -> String {
        JSRegex.allMatches(content ?? "", quoteMarkerPattern).joined(separator: ",")
    }

    static func hasQuoteMarkers(_ content: String?) -> Bool {
        JSRegex.test(content ?? "", quoteMarkerPattern)
    }

    /// A reply's text while it is written (#367): quote markers removed and
    /// `:::share` fence lines dropped until the reply is complete.
    static func partialReplyText(_ text: String?) -> String {
        let noQuotes = JSRegex.replaceAll(text ?? "", #"\{quote(?:_ext)?:\d+\}"#, "")
        return noQuotes.jsLines
            .filter { !JSRegex.test($0, #"^:::(?:share(?:[ \t]+[A-Za-z]+)?)?[ \t]*\r?$"#, options: [.caseInsensitive]) }
            .joined(separator: "\n")
    }
}
