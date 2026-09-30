import Foundation

/// The backend the app talks to (design doc §2 "Environments").
/// Release builds always use Production; Debug builds default to Local and can
/// switch (Account → long-press the version label, or `-LooreEnvironment`).
enum AppEnvironment: String, CaseIterable, Identifiable, Sendable {
    case local
    case staging
    case production

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .local: return "Local"
        case .staging: return "Staging"
        case .production: return "Production"
        }
    }

    /// Origin that serves `/api`, `/auth`, `/media` and `/api/sse`.
    var backendOrigin: URL {
        switch self {
        case .production: return URL(string: "https://loore.org")!
        case .staging: return URL(string: "https://staging.loore.org")!
        case .local: return Self.localBackendOverride ?? URL(string: "http://localhost:5010")!
        }
    }

    /// Origin of the web frontend (`FRONTEND_URL` on the backend): where every
    /// auth redirect lands. Same as the backend in production and staging.
    var frontendOrigin: URL {
        switch self {
        case .production, .staging: return backendOrigin
        case .local: return Self.localFrontendOverride ?? URL(string: "http://localhost:3001")!
        }
    }

    /// The Docker dev stack of `make dev`. The simulator reaches the Mac's localhost;
    /// a phone needs the Mac's `.local` name or LAN address (see ios/README.md).
    static var localBackendOverride: URL? {
        #if DEBUG
        return UserDefaults.standard.string(forKey: DefaultsKey.localBackendURL).flatMap(URL.init(string:))
        #else
        return nil
        #endif
    }

    static var localFrontendOverride: URL? {
        #if DEBUG
        return UserDefaults.standard.string(forKey: DefaultsKey.localFrontendURL).flatMap(URL.init(string:))
        #else
        return nil
        #endif
    }

    var host: String { backendOrigin.host ?? "" }
    var usesHTTPS: Bool { backendOrigin.scheme == "https" }

    /// Hosts whose links are "ours" (open in-app). Mirrors `utils/nodeLinks.js`:
    /// loore.org, www.loore.org, staging.loore.org, plus the current backend and frontend.
    var ownHosts: Set<String> {
        var hosts: Set<String> = ["loore.org", "www.loore.org", "staging.loore.org"]
        if let h = backendOrigin.host { hosts.insert(h) }
        if let h = frontendOrigin.host { hosts.insert(h) }
        return hosts
    }

    func url(path: String) -> URL {
        URL(string: path, relativeTo: backendOrigin)!.absoluteURL
    }

    func frontendURL(path: String) -> URL {
        URL(string: path, relativeTo: frontendOrigin)!.absoluteURL
    }

    /// The environment for this launch: Release is always Production. In Debug,
    /// the `-LooreEnvironment` launch argument wins, then the switcher's stored choice, then Local.
    static func resolveCurrent(launch: LaunchOptions = .current) -> AppEnvironment {
        #if DEBUG
        if let fromArgs = launch.environment { return fromArgs }
        if let stored = UserDefaults.standard.string(forKey: DefaultsKey.environment),
           let env = AppEnvironment(rawValue: stored) {
            return env
        }
        return .local
        #else
        return .production
        #endif
    }

    /// Debug switcher persistence.
    static func storeSelection(_ env: AppEnvironment) {
        #if DEBUG
        UserDefaults.standard.set(env.rawValue, forKey: DefaultsKey.environment)
        #endif
    }
}

/// UserDefaults keys used across the app. Web localStorage names are kept where
/// the web has the same per-device preference (map A §4.5).
enum DefaultsKey {
    static let environment = "loore.debug.environment"
    static let localBackendURL = "loore.debug.localBackendURL"
    static let localFrontendURL = "loore.debug.localFrontendURL"
    /// "light" | "dark"; absent = follow the system (web `loore_theme`).
    static let theme = "loore_theme"
    /// Fallback mirror of `user.craft_mode` (web `loore_craft_mode`).
    static let craftMode = "loore_craft_mode"
    /// Auto-generate replies (web `loore_auto_generate`, default true). Used from M2.
    static let autoGenerate = "loore_auto_generate"
    static let agenticReply = "loore_agentic_reply"
    static let lastPrivacyLevel = "loore_last_privacy_level"
    static let lastAIUsage = "loore_last_ai_usage"
    static let publicReplyAck = "loore_public_reply_ack"
}
