import XCTest
@testable import Loore

/// The stored sign-in at the first launch after an install, after an update,
/// and after a restore from a backup.
final class InstallCheckTests: XCTestCase {
    private var defaults: UserDefaults!
    private var store: InMemorySecureStore!
    private var folder: URL!

    override func setUp() {
        defaults = UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")
        store = InMemorySecureStore()
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("install-\(UUID().uuidString)")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: folder)
    }

    private func signIn(_ environment: AppEnvironment = .production) {
        let cookie = StoredCookie(name: "session", value: "s1", domain: environment.host)
            .makeCookie(sessionOnly: true)!
        CookieVault(store: store, environment: environment).capture(from: [cookie])
    }

    private func hasSignIn(_ environment: AppEnvironment = .production) -> Bool {
        CookieVault(store: store, environment: environment).hasRecord
    }

    // MARK: Telling the cases apart

    func testAnEmptyContainerShowsNoEarlierRun() {
        XCTAssertFalse(InstallCheck.hasEarlierData(defaults: defaults, uploadsFolder: folder))
    }

    func testTheUploadFolderShowsAnEarlierRun() throws {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        XCTAssertTrue(InstallCheck.hasEarlierData(defaults: defaults, uploadsFolder: folder))
    }

    func testAnyOfTheAppsOwnSettingsShowsAnEarlierRun() {
        for key in InstallCheck.earlierKeys {
            let fresh = UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!
            fresh.set("x", forKey: key)
            XCTAssertTrue(InstallCheck.hasEarlierData(defaults: fresh, uploadsFolder: folder), key)
        }
    }

    // MARK: What each case does

    func testFirstLaunchAfterAnInstallForgetsTheStoredSignIn() {
        for environment in AppEnvironment.allCases { signIn(environment) }
        XCTAssertTrue(InstallCheck.run(defaults: defaults, store: store, hasEarlierData: false))
        for environment in AppEnvironment.allCases { XCTAssertFalse(hasSignIn(environment)) }
        XCTAssertTrue(defaults.bool(forKey: InstallCheck.markerKey))
    }

    /// The build before this check wrote no marker: its data stands for it.
    func testAnUpdateFromABuildWithoutTheMarkerKeepsTheSignIn() {
        signIn()
        XCTAssertFalse(InstallCheck.run(defaults: defaults, store: store, hasEarlierData: true))
        XCTAssertTrue(hasSignIn())
        XCTAssertTrue(defaults.bool(forKey: InstallCheck.markerKey))
    }

    func testLaterLaunchesAndUpdatesKeepTheSignIn() {
        XCTAssertTrue(InstallCheck.run(defaults: defaults, store: store, hasEarlierData: false))
        signIn()
        // The marker decides from now on, whatever else the container holds.
        XCTAssertFalse(InstallCheck.run(defaults: defaults, store: store, hasEarlierData: false))
        XCTAssertFalse(InstallCheck.run(defaults: defaults, store: store, hasEarlierData: true))
        XCTAssertTrue(hasSignIn())
    }

    /// A restore brings UserDefaults (the marker), whether or not the app's
    /// files come with it.
    func testARestoreFromABackupKeepsTheSignIn() {
        defaults.set(true, forKey: InstallCheck.markerKey)
        signIn()
        XCTAssertFalse(InstallCheck.run(defaults: defaults, store: store, hasEarlierData: false))
        XCTAssertTrue(hasSignIn())
    }

    // MARK: At launch

    @MainActor
    func testTheAppOpensSignedOutOnTheFirstLaunchAfterAnInstall() async {
        let app = AppState(launch: LaunchOptions(), secureStore: store, defaults: defaults,
                           earlierInstallData: false)
        signIn(app.environment)
        await app.start()
        XCTAssertEqual(app.phase, .signedOut)
        XCTAssertFalse(hasSignIn(app.environment))
        XCTAssertTrue(defaults.bool(forKey: InstallCheck.markerKey))
    }
}
