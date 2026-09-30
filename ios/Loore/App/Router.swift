import Foundation
import Observation

/// A page shown in a sheet: a public web page (Safari view) or an authenticated
/// web view carrying the app's cookies (Admin, Connect X).
struct WebPresentation: Identifiable, Hashable {
    enum Kind: Hashable { case safari, authenticated }

    var id = UUID()
    var url: URL
    var kind: Kind
    var title: String?
}

/// Tab selection and per-tab navigation stacks (design doc §5).
@MainActor
@Observable
final class Router {
    var selectedTab: AppTab = .reflect
    /// Each tab's pushed routes (its `NavigationStack` path).
    var paths: [AppTab: [AppRoute]] = [:]
    var presentedWeb: WebPresentation?

    func path(for tab: AppTab) -> [AppRoute] {
        paths[tab] ?? []
    }

    func setPath(_ path: [AppRoute], for tab: AppTab) {
        paths[tab] = path
    }

    func popToRoot(_ tab: AppTab? = nil) {
        paths[tab ?? selectedTab] = []
    }

    func reset() {
        selectedTab = .reflect
        paths = [:]
        presentedWeb = nil
    }

    /// Opens `route`: switches to its tab and pushes it, or presents it as a web page.
    /// `commonsAvailable` hides the Commons tab when the user lacks `share_v1_enabled`.
    func open(_ route: AppRoute, environment: AppEnvironment, commonsAvailable: Bool) {
        switch route {
        case .webPage(let path):
            presentedWeb = WebPresentation(url: environment.frontendURL(path: path), kind: .safari)
            return
        case .external(let url):
            presentedWeb = WebPresentation(url: url, kind: .safari)
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

        var tab = route.preferredTab ?? selectedTab
        if tab == .commons && !commonsAvailable { tab = .reflect }
        selectedTab = tab
        if route.isTabRoot && route.preferredTab == tab {
            paths[tab] = []
        } else {
            // Includes `.commons` without the flag: pushed on Reflect, where it
            // shows the web's "Not available." screen.
            paths[tab, default: []].append(route)
        }
    }
}
