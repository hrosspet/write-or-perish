import XCTest
@testable import Loore

final class CookieVaultTests: XCTestCase {
    private let day: TimeInterval = 24 * 60 * 60
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func cookie(_ name: String, _ value: String, domain: String = "loore.org", expires: Date? = nil) -> HTTPCookie {
        StoredCookie(name: name, value: value, domain: domain, expires: expires, isSecure: true)
            .makeCookie(sessionOnly: expires == nil)!
    }

    func testCapturesOnlyAuthCookiesForTheHost() {
        let store = InMemorySecureStore()
        let vault = CookieVault(store: store, environment: .production)
        let changed = vault.capture(from: [
            cookie("session", "s1"),
            cookie("remember_token", "5|abc", expires: now.addingTimeInterval(30 * day)),
            cookie("other", "x"),
            cookie("session", "foreign", domain: "staging.loore.org"),
        ], now: now)
        XCTAssertTrue(changed)
        let record = try? XCTUnwrap(vault.load())
        XCTAssertEqual(record?.cookies.map(\.name), ["remember_token", "session"])
        XCTAssertEqual(record?.cookie(named: "session")?.value, "s1")
        XCTAssertEqual(record?.signedInAt, now)
        XCTAssertEqual(record?.expiresAt.timeIntervalSince1970 ?? 0,
                       now.addingTimeInterval(30 * day).timeIntervalSince1970, accuracy: 1)
    }

    func testNoAuthCookiesMeansNoChange() {
        let vault = CookieVault(store: InMemorySecureStore(), environment: .production)
        XCTAssertFalse(vault.capture(from: [cookie("other", "x")], now: now))
        XCTAssertNil(vault.load())
    }

    func testRestoreHonoursTheCookieExpiry() {
        let vault = CookieVault(store: InMemorySecureStore(), environment: .production)
        vault.capture(from: [cookie("session", "s1"),
                             cookie("remember_token", "5|abc", expires: now.addingTimeInterval(30 * day))], now: now)

        let early = vault.cookiesToRestore(now: now.addingTimeInterval(29 * day))
        XCTAssertEqual(Set(early.map(\.name)), ["session", "remember_token"])
        let session = early.first { $0.name == "session" }
        XCTAssertNil(session?.expiresDate, "the Flask session cookie is restored as a session cookie")
        XCTAssertTrue(session?.isHTTPOnly == true)
        XCTAssertTrue(session?.isSecure == true)

        // 30 days after sign-in: nothing is restored even though the server would accept the token.
        XCTAssertEqual(vault.cookiesToRestore(now: now.addingTimeInterval(30 * day + 1)), [])
        XCTAssertNil(vault.load(), "an expired record is deleted")
    }

    func testRefreshedSessionKeepsTheSignInDate() {
        let vault = CookieVault(store: InMemorySecureStore(), environment: .production)
        let expiry = now.addingTimeInterval(30 * day)
        vault.capture(from: [cookie("session", "s1"), cookie("remember_token", "5|abc", expires: expiry)], now: now)
        let later = now.addingTimeInterval(10 * day)
        XCTAssertTrue(vault.capture(from: [cookie("session", "s2")], now: later))
        let record = vault.load()
        XCTAssertEqual(record?.cookie(named: "session")?.value, "s2")
        XCTAssertEqual(record?.cookie(named: "remember_token")?.value, "5|abc", "kept from before")
        XCTAssertEqual(record?.signedInAt, now)
        XCTAssertFalse(vault.capture(from: [cookie("session", "s2")], now: later), "unchanged: no Keychain write")
    }

    func testNewRememberTokenIsANewSignIn() {
        let vault = CookieVault(store: InMemorySecureStore(), environment: .production)
        vault.capture(from: [cookie("remember_token", "5|old", expires: now.addingTimeInterval(30 * day))], now: now)
        let later = now.addingTimeInterval(20 * day)
        vault.capture(from: [cookie("remember_token", "5|new", expires: later.addingTimeInterval(30 * day))], now: later)
        XCTAssertEqual(vault.load()?.signedInAt, later)
        XCTAssertEqual(vault.cookiesToRestore(now: now.addingTimeInterval(40 * day)).count, 1)
    }

    func testSessionOnlySignInGetsThirtyDays() {
        // `-LooreSessionCookie`: a session cookie and no remember token.
        let vault = CookieVault(store: InMemorySecureStore(), environment: .local)
        vault.capture(from: [cookie("session", "dev", domain: "localhost")], now: now)
        XCTAssertEqual(vault.load()?.expiresAt, now.addingTimeInterval(CookieVault.fallbackLifetime))
        XCTAssertEqual(vault.cookiesToRestore(now: now.addingTimeInterval(day)).first?.domain, "localhost")
    }

    func testEnvironmentsAreSeparate() {
        let store = InMemorySecureStore()
        CookieVault(store: store, environment: .production).capture(from: [cookie("session", "p")], now: now)
        let staging = CookieVault(store: store, environment: .staging)
        XCTAssertNil(staging.load())
        staging.capture(from: [cookie("session", "s", domain: "staging.loore.org")], now: now)
        staging.clear()
        XCTAssertNotNil(CookieVault(store: store, environment: .production).load())
    }

    func testRestoreIntoStorage() {
        let vault = CookieVault(store: InMemorySecureStore(), environment: .production)
        vault.capture(from: [cookie("session", "s1"),
                             cookie("remember_token", "t", expires: Date().addingTimeInterval(30 * day))])
        let storage = URLSessionConfiguration.ephemeral.httpCookieStorage!
        XCTAssertTrue(vault.restore(into: storage))
        let names = Set((storage.cookies(for: URL(string: "https://loore.org/api/dashboard/")!) ?? []).map(\.name))
        XCTAssertEqual(names, ["session", "remember_token"])
    }

    /// Review M1: `HTTPCookieStorage.shared` writes the `remember_token` (it has an
    /// expiry) to a plaintext, backed-up file. The app's jar must be in memory.
    @MainActor
    func testTheAppJarIsInMemoryAndTheSharedJarIsCleared() {
        let token = StoredCookie(name: "remember_token", value: "5|m1-probe", domain: "loore.org",
                                 expires: Date().addingTimeInterval(30 * day), isSecure: true)
            .makeCookie(sessionOnly: false)!
        let shared = HTTPCookieStorage.shared
        shared.setCookie(token) // what an earlier build left behind
        let app = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(),
                           defaults: UserDefaults(suiteName: "loore-tests-\(UUID().uuidString)")!)
        XCTAssertFalse((shared.cookies ?? []).contains { $0.value == "5|m1-probe" }, "cleared at launch")

        let api = app.api
        XCTAssertFalse(api.cookieStorage === shared)
        XCTAssertTrue(api.session.configuration.httpCookieStorage === api.cookieStorage)
        api.cookieStorage.setCookie(token)
        XCTAssertFalse((shared.cookies ?? []).contains { $0.value == "5|m1-probe" })

        // The voice uploader's Cookie header comes from the app's jar.
        let header = ChunkUploader.shared.cookieHeader(URL(string: "https://loore.org/api/drafts/x/audio-chunk")!)
        XCTAssertEqual(header["Cookie"], "remember_token=5|m1-probe")
        api.cookieStorage.deleteCookie(token)
    }

    func testDomainMatching() {
        XCTAssertTrue(CookieVault.domain("loore.org", matches: "loore.org"))
        XCTAssertTrue(CookieVault.domain(".loore.org", matches: "staging.loore.org"))
        XCTAssertTrue(CookieVault.domain(".loore.org", matches: "loore.org"))
        XCTAssertFalse(CookieVault.domain("loore.org", matches: "staging.loore.org"))
        XCTAssertFalse(CookieVault.domain("evil-loore.org", matches: "loore.org"))
    }
}

final class MagicLinkTests: XCTestCase {
    private let token = "eyJlbWFpbCI6InNlb3dyaXRlckBleGFtcGxlLmludmFsaWQifQ.aNwXYg.Qv9tKf3n2kLm8sPq1rT5uVw7xYz0AbCdEfGhIjK"

    func testFullURL() {
        XCTAssertEqual(MagicLink.extractToken(from: "https://loore.org/auth/magic-link/verify?token=\(token)"), token)
        XCTAssertEqual(MagicLink.extractToken(from: "http://localhost:5010/auth/magic-link/verify?token=\(token)"), token)
    }

    func testURLInsideText() {
        let pasted = "Sign in to Loore:\n  https://loore.org/auth/magic-link/verify?token=\(token).\nThis link expires in 15 minutes."
        XCTAssertEqual(MagicLink.extractToken(from: pasted), token)
        XCTAssertEqual(MagicLink.linkHost(in: pasted), "loore.org")
    }

    func testBareTokenAndWhitespace() {
        XCTAssertEqual(MagicLink.extractToken(from: "  \(token)\n"), token)
    }

    func testPercentEncodedAndAmpersands() {
        XCTAssertEqual(MagicLink.extractToken(from: "https://loore.org/auth/magic-link/verify?utm=x&amp;token=\(token)"), token)
        XCTAssertEqual(MagicLink.extractToken(from: "token=\(token)&next=%2F"), token)
    }

    func testRejectsNonsense() {
        XCTAssertNil(MagicLink.extractToken(from: ""))
        XCTAssertNil(MagicLink.extractToken(from: "hello there"))
        XCTAssertNil(MagicLink.extractToken(from: "https://loore.org/log"))
        XCTAssertNil(MagicLink.extractToken(from: "short"))
    }

    func testRedirectOutcomes() {
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "https://loore.org/dashboard"), .success)
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "http://localhost:3001/welcome"), .success)
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "https://loore.org/login?error=invalid_or_expired"),
                       .failure(code: "invalid_or_expired"))
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "/login?error=confirm_needs_account&returnUrl=%2Fconfirm-email"),
                       .failure(code: "confirm_needs_account"))
        XCTAssertEqual(MagicLink.outcome(status: 200, location: nil), .failure(code: nil))
    }

    func testLandingPathFollowsTheLinksNextURL() {
        XCTAssertNil(MagicLink.landingPath(location: "https://loore.org/dashboard"))
        XCTAssertNil(MagicLink.landingPath(location: "https://loore.org/"))
        XCTAssertNil(MagicLink.landingPath(location: nil))
        XCTAssertEqual(MagicLink.landingPath(location: "http://localhost:3001/welcome"), "/welcome")
        XCTAssertEqual(MagicLink.landingPath(location: "https://loore.org/confirm-email?token=abc"), "/confirm-email?token=abc")
        XCTAssertEqual(MagicLink.landingPath(location: "/account#email"), "/account#email")
    }

    func testMessagesMatchTheWeb() {
        XCTAssertEqual(MagicLink.message(for: "link_already_used"),
                       "This sign-in link has already been used. Please request a new one.")
        XCTAssertEqual(MagicLink.message(for: "invalid_or_expired"),
                       "This sign-in link is invalid or has expired. Please request a new one.")
        XCTAssertEqual(MagicLink.message(for: "something_new"), MagicLink.genericFailure)
    }

    func testWebLoginLanding() {
        let prod = AppEnvironment.production
        XCTAssertFalse(WebLoginRouting.isFrontendLanding(URL(string: "https://loore.org/auth/login?next=/profile")!, environment: prod))
        XCTAssertFalse(WebLoginRouting.isFrontendLanding(URL(string: "https://loore.org/auth/twitter/authorized?oauth_token=x")!, environment: prod))
        XCTAssertFalse(WebLoginRouting.isFrontendLanding(URL(string: "https://api.twitter.com/oauth/authenticate?oauth_token=x")!, environment: prod))
        XCTAssertTrue(WebLoginRouting.isFrontendLanding(URL(string: "https://loore.org/profile")!, environment: prod))
        XCTAssertTrue(WebLoginRouting.isFrontendLanding(URL(string: "https://loore.org/login?error=x_try_again")!, environment: prod))
        XCTAssertEqual(WebLoginRouting.loginErrorCode(URL(string: "https://loore.org/login?error=x_try_again")!), "x_try_again")
        XCTAssertNil(WebLoginRouting.loginErrorCode(URL(string: "https://loore.org/profile")!))

        // Local: backend :5010, frontend :3001 on the same host.
        let local = AppEnvironment.local
        XCTAssertFalse(WebLoginRouting.isFrontendLanding(URL(string: "http://localhost:5010/profile")!, environment: local))
        XCTAssertTrue(WebLoginRouting.isFrontendLanding(URL(string: "http://localhost:3001/profile")!, environment: local))
    }
}

final class LaunchOptionsTests: XCTestCase {
    func testParsesKnownArguments() {
        let options = LaunchOptions(arguments: ["/app", "-LooreEnvironment", "STAGING", "-LooreSessionCookie", "abc.def",
                                                "-LooreRoute", "/node/123", "-LooreTheme", "light",
                                                "-LooreResetState", "YES", "-LooreDebugAudioFile", "/tmp/a.m4a",
                                                "-NSDoubleLocalizedStrings", "YES"])
        XCTAssertEqual(options.environment, .staging)
        XCTAssertEqual(options.sessionCookie, "abc.def")
        XCTAssertEqual(options.route, "/node/123")
        XCTAssertEqual(options.theme, "light")
        XCTAssertTrue(options.resetState)
        XCTAssertFalse(options.skipUpdates)
        XCTAssertEqual(options.debugAudioFile, "/tmp/a.m4a")
    }

    func testIgnoresBadValues() {
        let options = LaunchOptions(arguments: ["-LooreEnvironment", "mars", "-LooreTheme", "sepia", "-LooreRoute"])
        XCTAssertNil(options.environment)
        XCTAssertNil(options.theme)
        XCTAssertNil(options.route)
    }
}
