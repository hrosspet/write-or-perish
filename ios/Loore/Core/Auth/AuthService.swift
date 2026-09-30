import Foundation
import WebKit
import os

/// A sign-in step that failed, with the text to show.
struct AuthFailure: LocalizedError, Equatable {
    var message: String
    var errorDescription: String? { message }
}

/// Sign-in, cookie persistence and sign-out (design doc §4, map B §3–§4).
///
/// Auth is cookie-only: the Flask `session` and `remember_token` cookies live in
/// `HTTPCookieStorage.shared` while the app runs and in the Keychain (`CookieVault`)
/// between launches.
@MainActor
final class AuthService {
    let api: APIClient
    let vault: CookieVault
    private var cookieObserver: NSObjectProtocol?
    private let log = Logger(subsystem: "org.loore.app", category: "auth")

    init(api: APIClient, vault: CookieVault) {
        self.api = api
        self.vault = vault
    }

    deinit {
        if let cookieObserver { NotificationCenter.default.removeObserver(cookieObserver) }
    }

    var environment: AppEnvironment { api.environment }

    // MARK: Cookie persistence

    /// Keeps the Keychain copy current whenever the server sets new cookies
    /// (a refreshed `session` after a remember-token load, a new sign-in).
    func startObservingCookies() {
        guard cookieObserver == nil else { return }
        cookieObserver = NotificationCenter.default.addObserver(
            forName: .NSHTTPCookieManagerCookiesChanged, object: api.cookieStorage, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.captureCookies() }
        }
    }

    func captureCookies() {
        vault.capture(from: api.backendCookies())
    }

    /// Puts the stored cookies back into the jar. Returns false when there is no
    /// valid sign-in (none stored, or older than 30 days).
    @discardableResult
    func restoreStoredCookies() -> Bool {
        vault.restore(into: api.cookieStorage)
    }

    var hasAuthCookies: Bool {
        !CookieVault.authCookies(from: api.backendCookies(), host: environment.host).isEmpty
    }

    /// Debug `-LooreSessionCookie`: a Flask `session` cookie signed in the backend container.
    func injectSessionCookie(_ value: String) {
        let stored = StoredCookie(name: "session", value: value, domain: environment.host,
                                  isSecure: environment.usesHTTPS)
        if let cookie = stored.makeCookie(sessionOnly: true) {
            api.cookieStorage.setCookie(cookie)
            vault.capture(from: [cookie])
        }
    }

    // MARK: Magic link

    /// `POST /auth/magic-link/send`. Returns the server's message (always 200
    /// for a well-formed address, to prevent account enumeration).
    func sendMagicLink(email: String) async throws -> String {
        let trimmed = email.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            let answer: MessageResponse = try await api.post(APIPath.magicLinkSend, json: ["email": .string(trimmed)])
            return answer.message ?? ""
        } catch let error as APIError {
            if error.isOffline { throw AuthFailure(message: "Network error. Please try again.") }
            throw AuthFailure(message: error.userMessage(fallback: "Something went wrong. Please try again."))
        }
    }

    /// Verifies a pasted magic link or token: `GET /auth/magic-link/verify` with
    /// redirects disabled, then reads the 302's `Location` (map B §4.1 A).
    /// On success the auth cookies are in the jar and the Keychain.
    func verifyMagicLink(pasted: String) async throws {
        guard let token = MagicLink.extractToken(from: pasted) else {
            throw AuthFailure(message: "That doesn't look like a sign-in link. Copy the whole link from the email and paste it here.")
        }
        var request = APIRequest(.get, APIPath.magicLinkVerify, query: [URLQueryItem(name: "token", value: token)])
        request.accept = "text/html,application/json;q=0.9,*/*;q=0.8"

        let response: HTTPURLResponse
        do {
            (_, response) = try await api.data(for: request)
        } catch let error as APIError {
            if error.isOffline {
                throw AuthFailure(message: "Couldn't reach Loore. Check your connection and try again.")
            }
            throw AuthFailure(message: MagicLink.genericFailure)
        }

        // The cookies ride on the 302 itself. URLSession normally stores them;
        // store them explicitly too, so a stopped redirect can never lose them.
        adoptSetCookieHeaders(from: response)

        let location = response.value(forHTTPHeaderField: "Location")
        switch MagicLink.outcome(status: response.statusCode, location: location) {
        case .success:
            captureCookies()
            guard hasAuthCookies else {
                log.error("magic-link verify succeeded but no auth cookie was stored")
                throw AuthFailure(message: "Loore accepted the link but the sign-in cookie didn't arrive. Please try again.")
            }
        case .failure(let code):
            var message = MagicLink.message(for: code)
            if let host = MagicLink.linkHost(in: pasted),
               host != environment.host, host != environment.frontendOrigin.host {
                message += " (This link is for \(host); the app is using \(environment.displayName).)"
            }
            throw AuthFailure(message: message)
        }
    }

    private func adoptSetCookieHeaders(from response: HTTPURLResponse) {
        guard let url = response.url else { return }
        var headers: [String: String] = [:]
        for (key, value) in response.allHeaderFields {
            if let k = key as? String, let v = value as? String { headers[k] = v }
        }
        let cookies = HTTPCookie.cookies(withResponseHeaderFields: headers, for: url)
        for cookie in cookies { api.cookieStorage.setCookie(cookie) }
    }

    // MARK: Sign in with X (web view)

    /// Copies the auth cookies a `WKWebView` login collected into the app's jar.
    func adoptWebLoginCookies(_ cookies: [HTTPCookie]) throws {
        let auth = CookieVault.authCookies(from: cookies, host: environment.host)
        guard !auth.isEmpty else {
            throw AuthFailure(message: "Sign in with X did not finish. Please try again.")
        }
        for cookie in auth { api.cookieStorage.setCookie(cookie) }
        vault.capture(from: auth)
    }

    // MARK: Sign-out

    /// `GET /auth/logout` with redirects disabled (a signed-out logout would
    /// otherwise walk into X OAuth), then clears everything local regardless of the answer.
    func signOut() async {
        if hasAuthCookies {
            var request = APIRequest(.get, APIPath.logout)
            request.accept = "text/html,*/*"
            request.timeout = 10
            _ = try? await api.data(for: request)
        }
        await clearLocalSession()
    }

    /// Forgets every credential and cached response on this device.
    func clearLocalSession() async {
        vault.clear()
        for cookie in api.cookieStorage.cookies ?? [] {
            api.cookieStorage.deleteCookie(cookie)
        }
        api.session.configuration.urlCache?.removeAllCachedResponses()
        URLCache.shared.removeAllCachedResponses()
        let dataStore = WKWebsiteDataStore.default()
        await dataStore.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast)
    }
}
