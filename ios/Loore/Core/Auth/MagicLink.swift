import Foundation

/// Parsing for the pasted sign-in link and the verify endpoint's redirect (map B §3.2, §4.1 A).
enum MagicLink {
    /// Error codes the backend puts on `/login?error=`, with the web's wording (`LoginPage.js`).
    static let errorMessages: [String: String] = [
        "invalid_or_expired": "This sign-in link is invalid or has expired. Please request a new one.",
        "link_already_used": "This sign-in link has already been used. Please request a new one.",
        "confirm_needs_account": "No Loore account signs in that way yet. Sign in to the account you asked from, not with the address you are confirming.",
        "x_try_again": "Sign in with X did not finish in this browser. Please try again.",
    ]

    static let genericFailure = "That link didn't sign you in. Please request a new one."

    static func message(for code: String?) -> String {
        guard let code, let text = errorMessages[code] else { return genericFailure }
        return text
    }

    /// Characters of an itsdangerous URL-safe token (base64url parts joined by dots).
    private static let tokenCharacters = CharacterSet(charactersIn:
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")

    /// Extracts the token from whatever was pasted: the full verify URL (with or
    /// without surrounding text), or the bare token.
    static func extractToken(from pasted: String) -> String? {
        let text = pasted.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        if let url = firstURL(in: text),
           let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
           let token = items.first(where: { $0.name == "token" })?.value,
           isPlausibleToken(token) {
            return token
        }
        if let range = text.range(of: "token=") {
            let tail = text[range.upperBound...]
            let token = String(tail.prefix { !$0.isWhitespace && $0 != "&" && $0 != "\"" && $0 != "'" && $0 != ">" })
            let decoded = token.removingPercentEncoding ?? token
            if isPlausibleToken(decoded) { return decoded }
        }
        if isPlausibleToken(text) { return text }
        return nil
    }

    /// The host of a pasted URL, used to warn when a link belongs to another environment.
    static func linkHost(in pasted: String) -> String? {
        firstURL(in: pasted.trimmingCharacters(in: .whitespacesAndNewlines))?.host
    }

    static func isPlausibleToken(_ s: String) -> Bool {
        s.count >= 16 && s.unicodeScalars.allSatisfy { tokenCharacters.contains($0) }
    }

    private static func firstURL(in text: String) -> URL? {
        let pattern = #"https?://[^\s"'<>]+"#
        guard let range = text.range(of: pattern, options: .regularExpression) else { return nil }
        var candidate = String(text[range])
        // Trailing punctuation copied with the link from prose.
        while let last = candidate.last, ".,;)]".contains(last) { candidate.removeLast() }
        candidate = candidate.replacingOccurrences(of: "&amp;", with: "&")
        return URL(string: candidate)
    }

    enum VerifyOutcome: Equatable {
        case success
        case failure(code: String?)
    }

    /// Interprets `GET /auth/magic-link/verify` fetched with redirects disabled:
    /// a redirect to `/login?error=<code>` is a failure, any other redirect a success.
    static func outcome(status: Int, location: String?) -> VerifyOutcome {
        guard (300..<400).contains(status), let location, !location.isEmpty else {
            return .failure(code: nil)
        }
        let url = URL(string: location)
        let path = url?.path ?? location
        if path == "/login" || path.hasSuffix("/login") {
            let code = url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?
                .queryItems?.first(where: { $0.name == "error" })?.value
            return .failure(code: code)
        }
        return .success
    }

    /// Where a successful verify sends the browser (the link's `next_url`, e.g.
    /// `/welcome` from the Activate & Welcome email or `/confirm-email?token=…`),
    /// as an in-app path; nil for the backend's default target (`/dashboard`) and `/`,
    /// after which the app stays on Reflect.
    static func landingPath(location: String?) -> String? {
        guard let location, let components = URLComponents(string: location) else { return nil }
        let path = components.path
        guard !path.isEmpty, !["/", "/dashboard", "/login"].contains(path) else { return nil }
        var result = path
        if let query = components.percentEncodedQuery, !query.isEmpty { result += "?" + query }
        if let fragment = components.fragment, !fragment.isEmpty { result += "#" + fragment }
        return result
    }
}
