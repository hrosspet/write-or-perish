import SwiftUI

/// A screen whose native version arrives in a later milestone. It says so
/// calmly and offers the web page in an authenticated web view meanwhile.
/// Delete a route's case from `RouteDestination` when its native screen lands.
struct PlaceholderScreen: View {
    let route: AppRoute
    @Environment(AppState.self) private var app

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                PageHeader(title: route.screenTitle)
                if case .commons = route, !app.capabilities.showsCommons {
                    Text("Not available.")
                        .font(LooreFont.body)
                        .foregroundStyle(LooreColor.textMuted)
                } else {
                    Text("This part of Loore isn't in the app yet. It works on the web in the meantime.")
                        .font(LooreFont.body)
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(5)
                        .fixedSize(horizontal: false, vertical: true)
                    if let path = route.webPath {
                        Button("Open on the web") {
                            app.router.presentedWeb = WebPresentation(
                                url: app.environment.frontendURL(path: path),
                                kind: .authenticated,
                                title: route.screenTitle)
                        }
                        .buttonStyle(.loorePrimary)
                        .accessibilityIdentifier("placeholder.openWeb")
                    }
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .looreReadableWidth()
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .accessibilityIdentifier("placeholder.\(route.screenTitle)")
    }
}

extension AppRoute {
    /// Title the screen shows (the web's page titles).
    var screenTitle: String {
        switch self {
        case .home: return "Reflect"
        case .voice: return "Voice"
        case .textMode: return "Text"
        case .log: return "Log"
        case .thread: return "Thread"
        case .profile: return "Profile"
        case .todo: return "Todo"
        case .artifacts(let kind): return kind.map(Self.artifactTitle) ?? "Artifacts"
        case .newArtifact: return "New artifact"
        case .references: return "References"
        case .reference: return "Reference"
        case .prompts: return "Prompts"
        case .prompt: return "Prompt"
        case .account: return "Account"
        case .importData: return "Import Data"
        case .share: return "Share"
        case .commons: return "Commons"
        case .admin: return "Admin"
        case .welcome: return "Welcome"
        case .confirmEmail: return "Confirm email"
        case .confirmAccountDeletion: return "Delete my account"
        case .waitlist: return "Loore"
        case .webPage: return "Loore"
        case .external(let url): return url.host ?? "Link"
        }
    }

    private static func artifactTitle(_ kind: String) -> String {
        switch kind {
        case "memory": return "Memory"
        case "scratchpad": return "Scratchpad"
        case "predictions": return "Predictions"
        case "ai_preferences": return "AI Interaction Preferences"
        case "intentions": return "Intentions"
        default: return kind
        }
    }

    /// The web path for this route (for the "Open on the web" fallback and share links).
    var webPath: String? {
        switch self {
        case .home: return "/"
        case .voice(let parent, let resume):
            var items: [String] = []
            if let resume { items.append("resume=\(resume)") }
            if let parent { items.append("parent=\(parent)") }
            return "/voice" + (items.isEmpty ? "" : "?" + items.joined(separator: "&"))
        case .textMode: return "/textmode"
        case .log: return "/log"
        case .thread(let id, _): return "/node/\(id)"
        case .profile: return "/profile"
        case .todo: return "/todo"
        case .artifacts(let kind): return kind.map { "/artifacts/\($0)" } ?? "/artifacts"
        case .newArtifact: return "/artifacts?create=1"
        case .references: return "/references"
        case .reference(let id): return "/references/\(id)"
        case .prompts: return "/prompts"
        case .prompt(let key): return "/prompts/\(key)"
        case .account(let anchor): return "/account" + (anchor.map { "#\($0)" } ?? "")
        case .importData(let anchor): return "/import" + (anchor.map { "#\($0)" } ?? "")
        case .share: return "/share"
        case .commons: return "/commons"
        case .admin: return "/admin"
        case .welcome: return "/welcome"
        case .confirmEmail(let token): return "/confirm-email" + (token.map { "?token=\($0)" } ?? "")
        case .confirmAccountDeletion(let token):
            return "/confirm-account-deletion" + (token.map { "?token=\($0)" } ?? "")
        case .waitlist: return "/alpha-thank-you"
        case .webPage(let path): return path
        case .external: return nil
        }
    }
}
