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

            TabStack(tab: .artifacts) { WorkspaceView() }
                .tabItem { Label(AppTab.artifacts.title, systemImage: "doc.text").environment(\.symbolVariants, .none) }
                .tag(AppTab.artifacts)

            TabStack(tab: .log) { LogView() }
                .tabItem { Label(AppTab.log.title, systemImage: "book.closed").environment(\.symbolVariants, .none) }
                .tag(AppTab.log)

            if app.capabilities.showsCommons {
                TabStack(tab: .commons) { CommonsView() }
                    .tabItem { Label(AppTab.commons.title, systemImage: "person.2").environment(\.symbolVariants, .none) }
                    .tag(AppTab.commons)
            }

            TabStack(tab: .more) { MoreView() }
                .tabItem { Label(AppTab.more.title, systemImage: "ellipsis").environment(\.symbolVariants, .none) }
                .tag(AppTab.more)
        }
        // A node is loading from any screen (a thread, Text mode, the Log, Write
        // New Entry): the screen stays, with the spinner, until the node's page opens.
        .overlay { NodeOpeningSpinner() }
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
        .safeAreaInset(edge: .bottom, spacing: 0) { MiniPlayerView() }
    }
}

/// Maps a route to its screen. `PlaceholderScreen` remains only for routes
/// that never push (admin, waitlist, web pages open as sheets).
struct RouteDestination: View {
    let route: AppRoute

    var body: some View {
        switch route {
        case .home:
            HomeView()
        case .account(let anchor):
            AccountView(anchor: anchor)
        case .voice(let parentId, let resumeLLMId):
            VoiceView(parentId: parentId, resumeLLMId: resumeLLMId)
        case .thread(let id, let awaitLLM):
            // A new page per node: a route replaced by another node's (a failed reply,
            // a delete) would otherwise keep the old page and its model. Keyed by the
            // id alone, so dropping a consumed `awaitLLM` keeps the page.
            ThreadView(nodeId: id, awaitLLM: awaitLLM).id(id)
        case .log:
            LogView()
        case .textMode:
            TextModeView()
        case .profile, .todo, .artifacts, .newArtifact:
            WorkspaceView(pinned: route.workspaceDocument)
        case .references:
            ReferencesView()
        case .reference(let id):
            ReferenceDetailView(itemId: id)
        case .prompts:
            PromptsView()
        case .prompt(let key):
            PromptDetailView(promptKey: key)
        case .importData(let anchor):
            ImportView(anchor: anchor)
        case .share:
            ShareView()
        case .commons:
            CommonsView()
        case .welcome:
            WelcomeView()
        case .confirmEmail(let token):
            ConfirmEmailView(token: token)
        case .confirmAccountDeletion(let token):
            ConfirmAccountDeletionView(token: token)
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
