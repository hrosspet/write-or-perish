import SwiftUI

/// The tab shell (design doc §5): Reflect, Artifacts, Log, Commons (flagged), More.
/// No badges or counts on tabs (docs/LOORE-ESSENCE.md: nothing counts the user).
struct MainTabView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        @Bindable var router = app.router
        TabView(selection: $router.selectedTab) {
            TabStack(tab: .reflect) { HomeView() }
                .tabItem { Label { Text(AppTab.reflect.title) } icon: { Image(uiImage: TabIcons.reflect) } }
                .tag(AppTab.reflect)
                .accessibilityIdentifier("tab.reflect")

            TabStack(tab: .artifacts) { PlaceholderScreen(route: .profile) }
                .tabItem { Label(AppTab.artifacts.title, systemImage: "doc.text").environment(\.symbolVariants, .none) }
                .tag(AppTab.artifacts)

            TabStack(tab: .log) { PlaceholderScreen(route: .log) }
                .tabItem { Label(AppTab.log.title, systemImage: "book.closed").environment(\.symbolVariants, .none) }
                .tag(AppTab.log)

            if app.capabilities.showsCommons {
                TabStack(tab: .commons) { PlaceholderScreen(route: .commons) }
                    .tabItem { Label(AppTab.commons.title, systemImage: "person.2").environment(\.symbolVariants, .none) }
                    .tag(AppTab.commons)
            }

            TabStack(tab: .more) { MoreView() }
                .tabItem { Label(AppTab.more.title, systemImage: "ellipsis").environment(\.symbolVariants, .none) }
                .tag(AppTab.more)
        }
        .onChange(of: app.capabilities.showsCommons) { _, shows in
            if !shows && app.router.selectedTab == .commons { app.router.selectedTab = .reflect }
        }
    }
}

/// One tab's `NavigationStack`, bound to the router's path for that tab.
struct TabStack<Root: View>: View {
    let tab: AppTab
    @ViewBuilder var root: () -> Root
    @Environment(AppState.self) private var app

    var body: some View {
        let path = Binding(
            get: { app.router.path(for: tab) },
            set: { app.router.setPath($0, for: tab) }
        )
        NavigationStack(path: path) {
            root()
                .navigationDestination(for: AppRoute.self) { route in
                    RouteDestination(route: route)
                }
        }
    }
}

/// Maps a route to its screen. Later milestones replace the placeholders here
/// (M2: thread, text mode, log; M3: voice; M4: the feature pages).
struct RouteDestination: View {
    let route: AppRoute

    var body: some View {
        switch route {
        case .home:
            HomeView()
        case .account:
            AccountView()
        default:
            PlaceholderScreen(route: route)
        }
    }
}

/// Template tab icons drawn from the web's own marks.
enum TabIcons {
    /// The Loore ECG mark as a template image for the Reflect tab.
    @MainActor static let reflect: UIImage = {
        let renderer = ImageRenderer(content: LooreLogo(size: 26, color: .black))
        renderer.scale = UIScreen.main.scale
        return (renderer.uiImage ?? UIImage(systemName: "waveform")!).withRenderingMode(.alwaysTemplate)
    }()
}
