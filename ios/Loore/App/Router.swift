import Foundation
import Observation
import UIKit

/// A page shown in a sheet: a public web page (Safari view) or an authenticated
/// web view carrying the app's cookies (Admin, Connect X).
struct WebPresentation: Identifiable, Hashable {
    enum Kind: Hashable { case safari, authenticated }

    var id = UUID()
    var url: URL
    var kind: Kind
    var title: String?
}

/// A document of the Artifacts workspace (map E §1).
enum WorkspaceDocument: Hashable, Sendable {
    case profile
    case todo
    case artifact(String)
    /// The new-artifact form (`/artifacts?create=1`).
    case create
}

/// Tab selection and per-tab navigation stacks (design doc §5).
@MainActor
@Observable
final class Router {
    var selectedTab: AppTab = .reflect
    /// Each tab's pushed routes (its `NavigationStack` path).
    var paths: [AppTab: [AppRoute]] = [:]
    var presentedWeb: WebPresentation?
    /// The document the Artifacts tab's workspace shows (web `/profile`, `/todo`,
    /// `/artifacts/:kind`); bubbles and routes switch it in place.
    var workspace: WorkspaceDocument = .profile

    func path(for tab: AppTab) -> [AppRoute] {
        paths[tab] ?? []
    }

    func setPath(_ path: [AppRoute], for tab: AppTab) {
        paths[tab] = path
    }

    func popToRoot(_ tab: AppTab? = nil) {
        paths[tab ?? selectedTab] = []
    }

    /// Pops the top screen of the current tab (the web's `navigate(-1)`).
    func pop() {
        guard var path = paths[selectedTab], !path.isEmpty else { return }
        path.removeLast()
        paths[selectedTab] = path
    }

    /// The route under the top of the current tab's stack.
    var previousRoute: AppRoute? {
        let path = paths[selectedTab] ?? []
        return path.count >= 2 ? path[path.count - 2] : nil
    }

    /// Replaces the top of the current tab's stack (`navigate(…, {replace: true})`).
    /// On a tab root, pushes instead.
    func replaceTop(with route: AppRoute) {
        var path = paths[selectedTab] ?? []
        if path.isEmpty { path.append(route) } else { path[path.count - 1] = route }
        paths[selectedTab] = path
    }

    /// Swaps the last occurrence of `old` in the current tab's stack for `new`
    /// (drops a consumed `?awaitLlm=` so a back step does not repeat the hand-off).
    func replaceLast(_ old: AppRoute, with new: AppRoute) {
        guard var path = paths[selectedTab], let index = path.lastIndex(of: old) else { return }
        path[index] = new
        paths[selectedTab] = path
    }

    enum ExternalHandling: Equatable { case safari, system, ignore }

    /// How an outside link opens. `SFSafariViewController` raises an exception
    /// for anything but http(s) (review M16: a reference URL without a scheme
    /// crashed the app), so other links never reach it: `mailto:` goes to the
    /// system, everything else (no scheme, other apps' schemes) is ignored.
    static func externalHandling(_ url: URL) -> ExternalHandling {
        switch url.scheme?.lowercased() {
        case "http", "https": return url.host?.isEmpty == false ? .safari : .ignore
        case "mailto": return .system
        default: return .ignore
        }
    }

    /// Opens an outside link in Safari or Mail under the same allowlist, for a
    /// sheet the in-app Safari view cannot cover (the Updates sheet). Other
    /// schemes are ignored (#442).
    static func openOutsideApp(_ url: URL) {
        openOutsideApp(url) { UIApplication.shared.open($0) }
    }

    static func openOutsideApp(_ url: URL, opener: (URL) -> Void) {
        switch externalHandling(url) {
        case .safari, .system: opener(url)
        case .ignore: break
        }
    }

    func reset() {
        selectedTab = .reflect
        paths = [:]
        presentedWeb = nil
        workspace = .profile
    }

    /// Opens `route`: switches to its tab and pushes it, or presents it as a web page.
    /// `commonsAvailable` hides the Commons tab when the user lacks `share_v1_enabled`.
    func open(_ route: AppRoute, environment: AppEnvironment, commonsAvailable: Bool) {
        switch route {
        case .webPage(let path):
            presentedWeb = WebPresentation(url: environment.frontendURL(path: path), kind: .safari)
            return
        case .external(let url):
            switch Self.externalHandling(url) {
            case .safari: presentedWeb = WebPresentation(url: url, kind: .safari)
            case .system: UIApplication.shared.open(url)
            case .ignore: break
            }
            return
        case .admin:
            selectedTab = .more
            presentedWeb = WebPresentation(url: environment.frontendURL(path: "/admin"),
                                           kind: .authenticated, title: "Admin")
            return
        case .waitlist:
            return
        default:
            break
        }

        let from = selectedTab
        var tab = route.preferredTab ?? selectedTab
        if tab == .commons && !commonsAvailable { tab = .reflect }
        selectedTab = tab
        if route.carriesItsThreadToReflect && tab != from {
            // The other tab keeps its stack; Reflect's older screens are replaced.
            paths[tab] = (paths[from] ?? []) + [route]
        } else if let document = route.workspaceDocument, tab == .artifacts {
            paths[tab] = []
            workspace = document
        } else if route.isTabRoot && route.preferredTab == tab {
            paths[tab] = []
        } else {
            // Includes `.commons` without the flag: pushed on Reflect, where it
            // shows the web's "Not available." screen.
            paths[tab, default: []].append(route)
        }
    }
}
