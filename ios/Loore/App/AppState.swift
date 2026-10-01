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
        /// Saved references were added, edited or deleted: the References list refetches.
        case referencesChanged
    }

    private(set) var todoChanged = 0
    private(set) var artifactsChanged = 0
    private(set) var profileGenerationStarted = 0
    private(set) var lastCreatedNodeId: Int?
    private(set) var nodeCreated = 0
    private(set) var logChanged = 0
    private(set) var referencesChanged = 0

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
        case .referencesChanged:
            referencesChanged += 1
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
    /// Audio session, the shared queue player, the voice conversation (M3).
    let audio = AudioCenter()
    /// Node-link titles for markdown bodies (session cache, M2).
    let nodeTitles = NodeTitleStore()
    /// The ArtifactsNav bubble list (M4).
    let artifacts = ArtifactsStore()
    /// App-wide profile-generation poller (M4, web `ProfileGenerationWatcher`).
    let profileWatcher = ProfileGenerationWatcher()

    private(set) var phase: Phase = .launching
    private(set) var user: CurrentUser?
    /// Session flag: block cost actions up front (web `utils/spendCap` `isSpendBlocked`).
    private(set) var spendCapped = false
    /// The "LIMIT REACHED" banner text while it shows.
    var spendCapBannerMessage: String?
    /// Unread dev updates to show in the Updates sheet (fetched once per launch).
    var pendingUpdates: UpdatesPayload?

    private var updatesFetched = false
    #if DEBUG
    private var launchRouteHandled = false
    #endif
    /// Where the pasted sign-in link pointed (web `next_url`), opened once the user can see the app.
    private var pendingLandingPath: String?
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
        #if DEBUG
        theme = ThemeManager(defaults: defaults, forced: launch.theme)
        #else
        theme = ThemeManager(defaults: defaults, forced: nil)
        #endif
        installEventHandler()
        APIClient.clearSharedCookieStorage()
        // The voice uploader sets the `Cookie` header itself; it reads the app's
        // in-memory jar (the default would read `HTTPCookieStorage.shared`).
        ChunkUploader.shared.cookieHeader = { [weak self] url in
            HTTPCookie.requestHeaderFields(with: self?.api.cookieStorage.cookies(for: url) ?? [])
        }
        audio.attach(self)
        profileWatcher.showToast = { [weak self] text, duration in self?.toasts.show(text, duration: duration) }
        profileWatcher.clearUserFlags = { [weak self] in
            self?.user?.profileGenerationTaskId = nil
            self?.user?.profileBatchPending = false
        }
        profileWatcher.fetch = { [weak self] taskId in
            guard let api = self?.api else { throw CancellationError() }
            let query = taskId.map { [URLQueryItem(name: "task_id", value: $0)] } ?? []
            return try await api.get(APIPath.profileProgress, query: query, poll: true)
        }
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
        #if DEBUG
        if launch.resetState {
            auth.vault.clear()
            for key in [DefaultsKey.theme, DefaultsKey.craftMode] { defaults.removeObject(forKey: key) }
            theme.forget()
            for cookie in api.cookieStorage.cookies ?? [] { api.cookieStorage.deleteCookie(cookie) }
        }
        #endif
        auth.startObservingCookies()
        #if DEBUG
        if let cookie = launch.sessionCookie {
            auth.injectSessionCookie(cookie)
        } else {
            auth.restoreStoredCookies()
        }
        #else
        auth.restoreStoredCookies()
        #endif
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
        audio.didSignIn()
        if newUser.approved { profileWatcher.userLoaded(newUser) }
    }

    /// Replaces the user after a `PUT /api/dashboard/user` (the web's `setUser(res.data.user)`).
    func replaceUser(_ newUser: CurrentUser) {
        user = newUser
        if let craft = newUser.craftMode { defaults.set(craft, forKey: DefaultsKey.craftMode) }
    }

    func applyEmailState(_ state: EmailState) {
        user?.apply(state)
    }

    /// After `DELETE /api/dashboard/x` (the web's `setUser({twitter_login:false, twitter_handle:null})`).
    func markXDisconnected() {
        user?.twitterLogin = false
        user?.twitterHandle = nil
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
        guard let user, user.approved, user.termsUpToDate, !updatesFetched else { return }
        #if DEBUG
        if launch.skipUpdates { return }
        #endif
        updatesFetched = true
        Task {
            guard let payload: UpdatesPayload = try? await api.get(APIPath.updates) else { return }
            if !payload.isEmpty { self.pendingUpdates = payload }
        }
    }

    private func openLaunchRouteIfReady() {
        guard let user, user.approved, user.termsUpToDate else { return }
        #if DEBUG
        if !launchRouteHandled, let path = launch.route {
            launchRouteHandled = true
            if let route = AppRoute.parse(path, environment: environment) { open(route) }
        }
        #endif
        if let path = pendingLandingPath {
            pendingLandingPath = nil
            if let route = AppRoute.parse(path, environment: environment) { open(route) }
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
        if case .webPage(let path) = route, phase == .signedIn, isApproved,
           let permalink = AppRoute.permalink(in: path) {
            openPermalink(username: permalink.username, slug: permalink.slug, path: path)
            return
        }
        router.open(route, environment: environment, commonsAvailable: capabilities.showsCommons)
    }

    /// `/@username/slug` (web `PermalinkRoute`): a signed-in member gets the thread
    /// view of the resolved node, as on the web; anything that does not resolve
    /// (not public, flag off, offline) opens the public page instead.
    private func openPermalink(username: String, slug: String, path: String) {
        Task {
            let target: PermalinkTarget? = try? await api.get(APIPath.commonsPermalink(username: username, slug: slug))
            let route: AppRoute = target?.nodeId.map { .thread(id: $0, awaitLLM: nil) } ?? .webPage(path: path)
            router.open(route, environment: environment, commonsAvailable: capabilities.showsCommons)
        }
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

    /// After a successful magic-link verify or X web login. `landing` is the
    /// link's target page (`/welcome`, `/confirm-email?token=…`), as the web lands there.
    func signInCompleted(landing: String? = nil) async {
        pendingLandingPath = landing
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
        audio.signedOut()
        user = nil
        phase = .signedOut
        spendCapped = false
        spendCapBannerMessage = nil
        pendingUpdates = nil
        updatesFetched = false
        pendingLandingPath = nil
        router.reset()
        toasts.clear()
        nodeTitles.reset()
        artifacts.reset()
        profileWatcher.stop()
    }

    #if DEBUG
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
    #endif

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
