import Foundation

/// Which markdown images load on their own (#441, web `utils/markdownImages.js`).
///
/// Markdown in Loore does not always come from the viewer: LLM replies can be
/// steered by outside content the model read (Read's tweets, bookmarked pages,
/// imports), and public nodes come from other users. An image that loads as the
/// text renders tells its host that the node was opened, from which IP, and its
/// URL can carry text from the reply. So only Loore's own media loads by itself;
/// any other image is a placeholder naming its host, and loads only after the
/// user taps it.
enum MarkdownImageSource: Equatable {
    /// Loore's own media (`/media/…` on a Loore host): loads as the view appears.
    case own(URL)
    /// Any other web image: a placeholder naming `host`; loads after a tap.
    case remote(URL, host: String)
    /// No usable http(s) URL: the alt text only.
    case none

    static func classify(_ source: String, environment: AppEnvironment) -> MarkdownImageSource {
        let trimmed = source.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .none }
        let resolved = trimmed.hasPrefix("/") && !trimmed.hasPrefix("//")
            ? URL(string: trimmed, relativeTo: environment.backendOrigin)?.absoluteURL
            : URL(string: trimmed)
        guard let url = resolved, let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host?.lowercased(), !host.isEmpty else { return .none }
        if environment.ownHosts.contains(host), isOwnMediaPath(url) { return .own(url) }
        return .remote(url, host: host + (url.port.map { ":\($0)" } ?? ""))
    }

    /// `/media/` and plain path characters only: no percent-escapes (an encoded
    /// "../" decodes into another route) and no "." or ".." segments.
    private static func isOwnMediaPath(_ url: URL) -> Bool {
        let path = url.path(percentEncoded: true)
        guard JSRegex.firstMatch(path, #"^/media/[A-Za-z0-9_\-./]+$"#) != nil else { return false }
        return !path.split(separator: "/").contains { $0 == "." || $0 == ".." }
    }
}

/// What a tapped markdown link does (#442). Every link goes through the app's
/// router and its allowlist (`Router.externalHandling`): node links open the
/// thread, Loore paths their screen, http(s) links the in-app Safari view and
/// `mailto:` Mail. Anything else (other apps' schemes, `tel:`, `javascript:`,
/// links without a scheme) is never handed to the system and renders as plain text.
enum MarkdownLinkTarget: Equatable {
    case thread(Int)
    case route(AppRoute)
    case ignore

    @MainActor
    static func resolve(_ link: String, environment: AppEnvironment) -> MarkdownLinkTarget {
        if let id = NodeLinks.nodeId(link, currentOrigin: environment.frontendOrigin) { return .thread(id) }
        guard let route = AppRoute.parse(link, environment: environment) else { return .ignore }
        if case .external(let url) = route, Router.externalHandling(url) == .ignore { return .ignore }
        return .route(route)
    }
}
