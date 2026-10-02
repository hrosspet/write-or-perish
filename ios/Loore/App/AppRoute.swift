import Foundation

/// The app's tabs (design doc §5): the web NavBar as a bottom tab bar.
enum AppTab: String, CaseIterable, Hashable, Sendable {
    case reflect, artifacts, log, commons, more

    var title: String {
        switch self {
        case .reflect: return "Reflect"
        case .artifacts: return "Artifacts"
        case .log: return "Log"
        case .commons: return "Commons"
        case .more: return "More"
        }
    }
}

/// Every in-app destination, parsed from the web's paths (map A §1) so links
/// in markdown, notifications and changelog entries open native screens.
enum AppRoute: Hashable, Sendable {
    case home
    case voice(parentId: Int?, resumeLLMId: Int?)
    case textMode
    case log
    case thread(id: Int, awaitLLM: Int?)
    case profile
    case todo
    /// nil = the web's default kind (`memory`).
    case artifacts(kind: String?)
    /// `/artifacts?create=1`: the new-artifact form.
    case newArtifact
    case references
    case reference(id: Int)
    case prompts
    case prompt(key: String)
    /// `anchor` is the web hash: email, x, model, references, ai-usage, craft.
    case account(anchor: String?)
    case importData(anchor: String?)
    case share
    case commons
    case admin
    case welcome
    case confirmEmail(token: String?)
    case waitlist
    /// Marketing and public pages shown as web views (`/@user`, `/vision`, …).
    case webPage(path: String)
    /// A link to another site.
    case external(URL)

    /// Parses a link: absolute `http(s)` URLs on Loore's own hosts and relative
    /// paths become in-app routes; other URLs become `.external`.
    /// Mirrors `utils/nodeLinks.js` (loore.org, www, staging, same origin, relative).
    static func parse(_ link: String, environment: AppEnvironment) -> AppRoute? {
        let trimmed = link.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if trimmed.hasPrefix("/") && !trimmed.hasPrefix("//") {
            guard let components = URLComponents(string: trimmed) else { return nil }
            return route(for: components)
        }
        guard let url = URL(string: trimmed), let scheme = url.scheme?.lowercased() else { return nil }
        if scheme == "http" || scheme == "https" {
            if let host = url.host?.lowercased(), environment.ownHosts.contains(host),
               let components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
                return route(for: components)
            }
            return .external(url)
        }
        return .external(url)
    }

    /// `(username, slug)` of a public permalink path `/@username/slug` (web `PermalinkRoute`).
    static func permalink(in path: String) -> (username: String, slug: String)? {
        let parts = path.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        guard parts.count == 2, parts[0].hasPrefix("@"), parts[0].count > 1, !parts[1].isEmpty else { return nil }
        return (String(parts[0].dropFirst()), parts[1])
    }

    /// The node id of a thread link, if `link` is one (`/node/<id>` on an own host).
    static func nodeId(inLink link: String, environment: AppEnvironment) -> Int? {
        if case .thread(let id, _) = parse(link, environment: environment) { return id }
        return nil
    }

    private static func route(for components: URLComponents) -> AppRoute {
        let parts = components.path.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        let query = Dictionary(
            (components.queryItems ?? []).compactMap { item in item.value.map { (item.name, $0) } },
            uniquingKeysWith: { first, _ in first }
        )
        let anchor = components.fragment.flatMap { $0.isEmpty ? nil : $0 }
        func int(_ key: String) -> Int? { query[key].flatMap(Int.init) }

        guard let first = parts.first else { return .home }
        if first.hasPrefix("@") {
            return first.count > 1 ? .webPage(path: components.path) : .home
        }
        switch (first, parts.count) {
        case ("node", 2):
            if let id = Int(parts[1]) { return .thread(id: id, awaitLLM: int("awaitLlm")) }
            return .home
        case ("voice", 1): return .voice(parentId: int("parent"), resumeLLMId: int("resume"))
        case ("textmode", 1): return .textMode
        case ("log", 1), ("feed", 1): return .log
        case ("profile", 1), ("dashboard", 1): return .profile
        case ("dashboard", 2): return .webPage(path: "/@\(parts[1])")
        case ("todo", 1): return .todo
        case ("artifacts", 1): return query["create"] == "1" ? .newArtifact : .artifacts(kind: nil)
        case ("artifacts", 2): return .artifacts(kind: parts[1])
        case ("ai-preferences", 1): return .artifacts(kind: "ai_preferences")
        case ("references", 1): return .references
        case ("references", 2):
            if let id = Int(parts[1]) { return .reference(id: id) }
            return .references
        case ("prompts", 1): return .prompts
        case ("prompts", 2): return .prompt(key: parts[1])
        case ("account", 1): return .account(anchor: anchor)
        case ("import", 1): return .importData(anchor: anchor)
        case ("share", 1): return .share
        case ("commons", 1): return .commons
        case ("admin", 1): return .admin
        case ("welcome", 1): return .welcome
        case ("confirm-email", 1): return .confirmEmail(token: query["token"])
        case ("alpha-thank-you", 1): return .waitlist
        case ("landing", 1), ("vision", 1), ("why-loore", 1), ("how-to", 1):
            return .webPage(path: "/" + first)
        case ("login", 1): return .home
        default:
            return .home // the web's catch-all redirects to "/"
        }
    }

    /// The tab a route belongs to when opened from outside a stack.
    /// `nil` = keep the current tab (threads are pushed where they were opened).
    /// Voice on a thread (`parent` / `resume`) and Text Mode are pushed where they
    /// were opened, above the thread, as on the web where Back returns to it: in
    /// Reflect they landed on top of that tab's older screens and conversation.
    var preferredTab: AppTab? {
        switch self {
        case .voice(let parentId, let resumeLLMId):
            return parentId == nil && resumeLLMId == nil ? .reflect : nil
        case .textMode: return nil
        case .home, .welcome, .share: return .reflect
        case .profile, .todo, .artifacts, .newArtifact: return .artifacts
        case .log: return .log
        case .commons: return .commons
        case .account, .importData, .references, .reference, .prompts, .prompt, .admin, .confirmEmail:
            return .more
        case .thread: return nil
        case .waitlist, .webPage, .external: return nil
        }
    }

    /// Routes that are a tab's root screen (opening them pops that tab to root).
    /// The documents workspace (Profile, Todo, artifacts) is the Artifacts tab's
    /// root; the router switches its document in place (web `ArtifactsNav`).
    var isTabRoot: Bool {
        switch self {
        case .home, .profile, .todo, .artifacts, .newArtifact, .log, .commons: return true
        default: return false
        }
    }

    /// The workspace document a Profile/Todo/artifact route selects.
    var workspaceDocument: WorkspaceDocument? {
        switch self {
        case .profile: return .profile
        case .todo: return .todo
        case .artifacts(let kind): return .artifact(kind ?? "memory")
        case .newArtifact: return .create
        default: return nil
        }
    }

    /// Routes shown modally as web pages rather than pushed.
    var isWebPresentation: Bool {
        switch self {
        case .webPage, .external: return true
        default: return false
        }
    }
}
