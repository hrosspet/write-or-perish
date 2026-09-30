import SwiftUI
import Observation
import os

/// Cross-screen signals the web sends as `window` events (map A §4.4, E §0.2).
/// Screens observe the counters with `.onChange(of:)`; producers call `post`.
@MainActor
@Observable
final class AppSignals {
    enum Signal {
        /// The todo list changed (proposal applied, quick-add, edit): re-fetch before saving.
        case todoChanged
        /// An artifact was saved or reverted (`loore_artifacts_changed`).
        case artifactsChanged
        /// A profile build started (`loore_profile_started`).
        case profileGenerationStarted
        /// A node was created (new entry, reply): lists may refresh.
        case nodeCreated(Int)
        /// Nodes were deleted or a thread renamed: the Log refetches.
        case logChanged
    }

    private(set) var todoChanged = 0
    private(set) var artifactsChanged = 0
    private(set) var profileGenerationStarted = 0
    private(set) var lastCreatedNodeId: Int?
    private(set) var nodeCreated = 0
    private(set) var logChanged = 0

    func post(_ signal: Signal) {
        switch signal {
        case .todoChanged: todoChanged += 1
        case .artifactsChanged: artifactsChanged += 1
        case .profileGenerationStarted: profileGenerationStarted += 1
        case .nodeCreated(let id):
            lastCreatedNodeId = id
            nodeCreated += 1
        case .logChanged:
            logChanged += 1
        }
    }
}

/// The app's one shared state (design doc §3 "App state"): environment and
/// clients, the signed-in user, auth phase, theme, craft mode, spend cap,
/// toasts, routing and cross-screen signals. Feature screens own their view models.
@MainActor
@Observable
final class AppState {
    enum Phase: Equatable {
        /// Restoring cookies and loading the user.
        case launching
        case signedOut
        case signedIn
        /// Cookies exist but the backend could not be reached (or answered 5xx).
        case unreachable(String)
    }

    private(set) var environment: AppEnvironment
    private(set) var api: APIClient
    private(set) var auth: AuthService
    private(set) var sse: SSEClient

    let router = Router()
    let theme: ThemeManager
    let toasts = ToastCenter()
    let signals = AppSignals()
    let launch: LaunchOptions
    /// Node-link titles for markdown bodies (session cache, M2).
    let nodeTitles = NodeTitleStore()

    private(set) var phase: Phase = .launching
    private(set) var user: CurrentUser?
    /// Session flag: block cost actions up front (web `utils/spendCap` `isSpendBlocked`).
    private(set) var spendCapped = false
    /// The "LIMIT REACHED" banner text while it shows.
    var spendCapBannerMessage: String?
    /// Unread dev updates to show in the Updates sheet (fetched once per launch).
    var pendingUpdates: UpdatesPayload?

    private var updatesFetched = false
    private var launchRouteHandled = false
    private let secureStore: SecureStore
    private let defaults: UserDefaults
    private let log = Logger(subsystem: "org.loore.app", category: "app")

    init(launch: LaunchOptions = .current,
         secureStore: SecureStore = KeychainStore(),
         defaults: UserDefaults = .standard) {
        self.launch = launch
        self.secureStore = secureStore
        self.defaults = defaults
        let env = AppEnvironment.resolveCurrent(launch: launch)
        environment = env
        let api = APIClient(environment: env)
        self.api = api
        auth = AuthService(api: api, vault: CookieVault(store: secureStore, environment: env))
        sse = SSEClient(api: api)
        theme = ThemeManager(defaults: defaults, forced: launch.theme)
        installEventHandler()
        nodeTitles.fetch = { [weak self] ids in
            guard let api = await self?.api else { throw CancellationError() }
            let query = [URLQueryItem(name: "ids", value: ids.map(String.init).joined(separator: ","))]
            let answer: NodeTitlesResponse = try await api.get(APIPath.nodeTitles, query: query)
            return answer.titles.values
        }
    }

    // MARK: Derived state

    var capabilities: UserCapabilities {
        guard let user else { return UserCapabilities() }
        return UserCapabilities(user: user, craftModeFallback: defaults.bool(forKey: DefaultsKey.craftMode))
    }

    var isApproved: Bool { user?.approved == true }
    var needsTerms: Bool { user.map { !$0.termsUpToDate } ?? false }

    // MARK: Launch

    /// Restores the sign-in and loads the user. Called once from the root view.
    func start() async {
        if launch.resetState {
            auth.vault.clear()
            for key in [DefaultsKey.theme, DefaultsKey.craftMode] { defaults.removeObject(forKey: key) }
            theme.forget()
            for cookie in api.cookieStorage.cookies ?? [] { api.cookieStorage.deleteCookie(cookie) }
        }
        auth.startObservingCookies()
        if let cookie = launch.sessionCookie {
            auth.injectSessionCookie(cookie)
        } else {
            auth.restoreStoredCookies()
        }
        guard auth.hasAuthCookies else {
            phase = .signedOut
            return
        }
        await loadUser()
    }

    /// `GET /api/dashboard/`, the app's "who am I" call (web `UserContext`).
    func loadUser() async {
        do {
            let dashboard: DashboardResponse = try await api.get(APIPath.dashboard)
            didLoad(dashboard.user)
        } catch APIError.unauthorized {
            await signOutLocally()
        } catch let error as APIError {
            log.error("dashboard load failed: status \(error.status ?? -1)")
            if phase != .signedIn {
                phase = .unreachable(error.isOffline
                    ? "Couldn't reach Loore. Check your connection."
                    : "Loore didn't answer as expected. Please try again in a moment.")
            }
        } catch {
            if phase != .signedIn { phase = .unreachable("Couldn't reach Loore.") }
        }
    }

    /// Applies a fresh user object and runs the once-per-launch steps (design doc §4.5).
    func didLoad(_ newUser: CurrentUser) {
        user = newUser
        phase = .signedIn
        if let craft = newUser.craftMode {
            defaults.set(craft, forKey: DefaultsKey.craftMode)
        }
        if newUser.spendBlocked { spendCapped = true }
        syncTimezoneIfNeeded()
        fetchUpdatesIfNeeded()
        openLaunchRouteIfReady()
    }

    /// Replaces the user after a `PUT /api/dashboard/user` (the web's `setUser(res.data.user)`).
    func replaceUser(_ newUser: CurrentUser) {
        user = newUser
        if let craft = newUser.craftMode { defaults.set(craft, forKey: DefaultsKey.craftMode) }
    }

    func applyEmailState(_ state: EmailState) {
        user?.apply(state)
    }

    /// `PATCH /api/dashboard/timezone` when the device zone differs (approved users only:
    /// the route is not exempt from the approval gate).
    private func syncTimezoneIfNeeded() {
        guard let user, user.approved else { return }
        let deviceZone = TimeZone.current.identifier
        guard deviceZone != user.timezone else { return }
        Task {
            do {
                let answer: TimezoneResponse = try await api.patch(APIPath.timezone, json: ["timezone": .string(deviceZone)])
                self.user?.timezone = answer.timezone
            } catch {
                log.info("timezone sync failed (non-fatal)")
            }
        }
    }

    /// Once per launch, when approved and the terms are current: `GET /api/updates`.
    func fetchUpdatesIfNeeded() {
        guard let user, user.approved, user.termsUpToDate, !updatesFetched, !launch.skipUpdates else { return }
        updatesFetched = true
        Task {
            guard let payload: UpdatesPayload = try? await api.get(APIPath.updates) else { return }
            if !payload.isEmpty { self.pendingUpdates = payload }
        }
    }

    private func openLaunchRouteIfReady() {
        guard !launchRouteHandled, let path = launch.route, let user, user.approved, user.termsUpToDate else { return }
        launchRouteHandled = true
        if let route = AppRoute.parse(path, environment: environment) {
            open(route)
        }
    }

    // MARK: Terms and account

    /// `POST /api/terms/accept`. On success the user's terms are current; an
    /// unapproved user then sees the waitlist screen.
    func acceptTerms() async throws {
        let _: TermsAcceptResponse = try await api.post(APIPath.termsAccept)
        user?.termsUpToDate = true
        fetchUpdatesIfNeeded()
        openLaunchRouteIfReady()
    }

    /// `PUT /api/dashboard/user` with only the changed fields; replaces the user.
    @discardableResult
    func updateUser(_ fields: [String: JSONValue]) async throws -> CurrentUser {
        let answer: UpdateUserResponse = try await api.put(APIPath.user, json: .object(fields))
        replaceUser(answer.user)
        return answer.user
    }

    /// Craft mode (web NavBar `setCraftModeValue`): flips at once, mirrors to the
    /// device, then persists to the server. A failed save keeps the local value.
    func setCraftMode(_ on: Bool) async {
        defaults.set(on, forKey: DefaultsKey.craftMode)
        user?.craftMode = on
        _ = try? await updateUser(["craft_mode": .bool(on)])
    }

    // MARK: Navigation

    func open(_ route: AppRoute) {
        router.open(route, environment: environment, commonsAvailable: capabilities.showsCommons)
    }

    /// Handles a tapped link (markdown bodies, notifications). Returns false for
    /// links the caller should leave to the system.
    @discardableResult
    func openLink(_ link: String) -> Bool {
        guard let route = AppRoute.parse(link, environment: environment) else { return false }
        open(route)
        return true
    }

    // MARK: Sign-in and sign-out

    /// After a successful magic-link verify or X web login.
    func signInCompleted() async {
        phase = .launching
        updatesFetched = false
        await loadUser()
    }

    /// Logout: `GET /auth/logout`, then forget everything local.
    func signOut() async {
        await auth.signOut()
        resetSessionState()
    }

    /// Forget everything local without calling the server (after a 401).
    func signOutLocally() async {
        await auth.clearLocalSession()
        resetSessionState()
    }

    private func resetSessionState() {
        user = nil
        phase = .signedOut
        spendCapped = false
        spendCapBannerMessage = nil
        pendingUpdates = nil
        updatesFetched = false
        router.reset()
        toasts.clear()
        nodeTitles.reset()
    }

    /// Debug environment switcher: signs out of the current backend first
    /// (design doc §2: "Switching environment signs out").
    func switchEnvironment(to env: AppEnvironment) async {
        guard env != environment else { return }
        await signOut()
        AppEnvironment.storeSelection(env)
        environment = env
        let newAPI = APIClient(environment: env)
        api = newAPI
        auth = AuthService(api: newAPI, vault: CookieVault(store: secureStore, environment: env))
        sse = SSEClient(api: newAPI)
        installEventHandler()
        auth.startObservingCookies()
        phase = .signedOut
    }

    #if DEBUG
    /// Unit tests: route every call through a stubbed client and sign in `user`.
    func useForTesting(api: APIClient, user: CurrentUser?) {
        self.api = api
        sse = SSEClient(api: api)
        installEventHandler()
        self.user = user
        phase = user == nil ? .signedOut : .signedIn
    }
    #endif

    // MARK: API events

    private func installEventHandler() {
        api.setEventHandler { [weak self] event in
            Task { @MainActor in self?.handle(event) }
        }
    }

    func handle(_ event: APIEvent) {
        switch event {
        case .unauthorized:
            guard phase == .signedIn else { return }
            Task { await signOutLocally() }
        case .spendCapped(let message):
            spendCapped = true
            spendCapBannerMessage = message
        case .notApproved:
            // Approval changed under us: re-check, which routes to the waitlist.
            Task { await loadUser() }
        }
    }

    /// Marks the session as capped and shows the banner (web `notifySpendBlocked`),
    /// for cost actions refused before they start.
    func notifySpendBlocked() {
        spendCapped = true
        if spendCapBannerMessage == nil { spendCapBannerMessage = APIError.defaultSpendCapMessage }
    }
}
