import Foundation

/// A cookie in a form the Keychain can hold.
struct StoredCookie: Codable, Equatable, Sendable {
    var name: String
    var value: String
    var domain: String
    var path: String
    var expires: Date?
    var isSecure: Bool
    var isHTTPOnly: Bool
    var sameSite: String?

    init(name: String, value: String, domain: String, path: String = "/", expires: Date? = nil,
         isSecure: Bool = false, isHTTPOnly: Bool = true, sameSite: String? = "Lax") {
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.expires = expires
        self.isSecure = isSecure
        self.isHTTPOnly = isHTTPOnly
        self.sameSite = sameSite
    }

    init(_ cookie: HTTPCookie) {
        name = cookie.name
        value = cookie.value
        domain = cookie.domain
        path = cookie.path
        expires = cookie.expiresDate
        isSecure = cookie.isSecure
        isHTTPOnly = cookie.isHTTPOnly
        sameSite = cookie.sameSitePolicy?.rawValue
    }

    /// Rebuilds the cookie. `sessionOnly` drops the expiry (the Flask `session`
    /// cookie is a browser-session cookie and is restored as one).
    func makeCookie(sessionOnly: Bool = false) -> HTTPCookie? {
        var properties: [HTTPCookiePropertyKey: Any] = [
            .name: name,
            .value: value,
            .domain: domain,
            .path: path,
        ]
        if isSecure { properties[.secure] = "TRUE" }
        if isHTTPOnly { properties[HTTPCookiePropertyKey("HttpOnly")] = "TRUE" }
        if let sameSite { properties[.sameSitePolicy] = sameSite }
        if !sessionOnly, let expires { properties[.expires] = expires }
        if sessionOnly || expires == nil { properties[.discard] = "TRUE" }
        return HTTPCookie(properties: properties)
    }
}

/// What the vault keeps per environment.
struct VaultRecord: Codable, Equatable, Sendable {
    var cookies: [StoredCookie]
    /// When this sign-in happened (a new `remember_token` value = a new sign-in).
    var signedInAt: Date
    /// When the app stops restoring the cookies: the `remember_token`'s own expiry
    /// (30 days after sign-in), else `signedInAt` + 30 days.
    var expiresAt: Date

    func cookie(named name: String) -> StoredCookie? {
        cookies.first { $0.name == name }
    }
}

/// Keeps the Flask `session` and `remember_token` cookies in the Keychain and
/// puts them back into `HTTPCookieStorage` at launch (design doc §4.3).
///
/// It honours the cookie's own expiry: 30 days after sign-in it stops restoring
/// them, although the server would accept the `remember_token` indefinitely
/// (map B §3.1). Re-login is then once a month, as on the web.
final class CookieVault {
    static let authCookieNames: Set<String> = ["session", "remember_token"]
    static let fallbackLifetime: TimeInterval = 30 * 24 * 60 * 60

    let store: SecureStore
    let environment: AppEnvironment

    init(store: SecureStore, environment: AppEnvironment) {
        self.store = store
        self.environment = environment
    }

    private var key: String { "cookies.\(environment.rawValue)" }

    func load() -> VaultRecord? {
        guard let data = store.data(for: key) else { return nil }
        return try? JSONDecoder().decode(VaultRecord.self, from: data)
    }

    private func save(_ record: VaultRecord) {
        if let data = try? JSONEncoder().encode(record) {
            _ = store.set(data, for: key)
        }
    }

    func clear() {
        store.remove(key)
    }

    var hasRecord: Bool { load() != nil }

    /// The auth cookies among `cookies` that belong to `host`.
    static func authCookies(from cookies: [HTTPCookie], host: String) -> [HTTPCookie] {
        cookies.filter { cookie in
            authCookieNames.contains(cookie.name) && domain(cookie.domain, matches: host)
        }
    }

    static func domain(_ cookieDomain: String, matches host: String) -> Bool {
        let d = cookieDomain.lowercased()
        let h = host.lowercased()
        if d == h { return true }
        if d.hasPrefix(".") {
            let bare = String(d.dropFirst())
            return h == bare || h.hasSuffix(d)
        }
        return false
    }

    /// Updates the stored record from the cookie jar. Returns true when the Keychain changed.
    /// Does nothing when the jar has no auth cookie (sign-out clears explicitly).
    @discardableResult
    func capture(from cookies: [HTTPCookie], now: Date = Date()) -> Bool {
        let fresh = Self.authCookies(from: cookies, host: environment.host)
        guard !fresh.isEmpty else { return false }
        let existing = load()

        var byName: [String: StoredCookie] = [:]
        for cookie in existing?.cookies ?? [] { byName[cookie.name] = cookie }
        for cookie in fresh { byName[cookie.name] = StoredCookie(cookie) }

        let newRemember = fresh.first { $0.name == "remember_token" }
        let isNewSignIn: Bool = {
            guard let existing else { return true }
            guard let newRemember else { return false }
            return existing.cookie(named: "remember_token")?.value != newRemember.value
        }()

        let signedInAt = isNewSignIn ? now : (existing?.signedInAt ?? now)
        let expiresAt: Date = {
            if let rememberExpiry = byName["remember_token"]?.expires { return rememberExpiry }
            if !isNewSignIn, let existing { return existing.expiresAt }
            return signedInAt.addingTimeInterval(Self.fallbackLifetime)
        }()

        let record = VaultRecord(cookies: byName.values.sorted { $0.name < $1.name },
                                 signedInAt: signedInAt,
                                 expiresAt: expiresAt)
        guard record != existing else { return false }
        save(record)
        return true
    }

    /// The cookies to put back into the jar, or none when the sign-in is over.
    /// An expired record is deleted.
    func cookiesToRestore(now: Date = Date()) -> [HTTPCookie] {
        guard let record = load() else { return [] }
        guard now < record.expiresAt else {
            clear()
            return []
        }
        return record.cookies.compactMap { stored in
            if let expires = stored.expires, expires <= now { return nil }
            return stored.makeCookie(sessionOnly: stored.name == "session")
        }
    }

    /// Restores the cookies into `storage`. Returns true when anything was restored.
    @discardableResult
    func restore(into storage: HTTPCookieStorage, now: Date = Date()) -> Bool {
        let cookies = cookiesToRestore(now: now)
        for cookie in cookies { storage.setCookie(cookie) }
        return !cookies.isEmpty
    }
}
