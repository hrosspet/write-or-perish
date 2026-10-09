import Foundation

// Account deletion and restore (#269): the server's answers, the pasted
// confirmation link, and the wording the web shares between its pages
// (`utils/dataDeletion.js`).

/// `user.account_deletion` in `GET /api/dashboard/`: what the Account page
/// states about deleting the account. Absent on a server without account
/// deletion, and then the app offers none.
struct AccountDeletionInfo: Decodable, Equatable, Sendable {
    struct Refusal: Decodable, Equatable, Sendable {
        var code: String
        var message: String
    }

    var graceDays: Int
    var usernameReserveDays: Int
    /// An account with an email address confirms from a mailed link.
    var confirmByEmail: Bool
    /// Seconds the mailed link works.
    var linkExpiresIn: Int
    /// Why this account cannot be deleted (the last admin), if it can't.
    var refusal: Refusal?
    /// The account signs in with X.
    var xSignIn: Bool
    /// This session holds that sign-in's token, which the deletion request
    /// revokes at X (#464; Peter, 2026-10-09).
    var xSignInRevocable: Bool

    enum CodingKeys: String, CodingKey {
        case refusal
        case graceDays = "grace_days"
        case usernameReserveDays = "username_reserve_days"
        case confirmByEmail = "confirm_by_email"
        case linkExpiresIn = "link_expires_in"
        case xSignIn = "x_sign_in"
        case xSignInRevocable = "x_sign_in_revocable"
    }

    init(graceDays: Int = 30, usernameReserveDays: Int = 365, confirmByEmail: Bool = false,
         linkExpiresIn: Int = 3600, refusal: Refusal? = nil, xSignIn: Bool = false,
         xSignInRevocable: Bool = false) {
        self.graceDays = graceDays
        self.usernameReserveDays = usernameReserveDays
        self.confirmByEmail = confirmByEmail
        self.linkExpiresIn = linkExpiresIn
        self.refusal = refusal
        self.xSignIn = xSignIn
        self.xSignInRevocable = xSignInRevocable
    }

    // The web's fallbacks (`info?.grace_days || 30`, …).
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        graceDays = c.tolerant(.graceDays, default: 30)
        usernameReserveDays = c.tolerant(.usernameReserveDays, default: 365)
        confirmByEmail = c.tolerant(.confirmByEmail, default: false)
        linkExpiresIn = c.tolerant(.linkExpiresIn, default: 3600)
        refusal = c.tolerant(.refusal)
        xSignIn = c.tolerant(.xSignIn, default: false)
        xSignInRevocable = c.tolerant(.xSignInRevocable, default: false)
    }
}

/// `user.data_deletion`: the user's "Delete all my writing" request (#268),
/// which the account deletion replaces, and whether X is connected.
struct DataDeletionStatus: Decodable, Equatable, Sendable {
    var status: String?
    var purgeAt: Date?
    var xConnected: Bool

    enum CodingKeys: String, CodingKey {
        case status
        case purgeAt = "purge_at"
        case xConnected = "x_connected"
    }

    init(status: String? = nil, purgeAt: Date? = nil, xConnected: Bool = false) {
        self.status = status
        self.purgeAt = purgeAt
        self.xConnected = xConnected
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = c.tolerant(.status)
        purgeAt = c.tolerant(.purgeAt)
        xConnected = c.tolerant(.xConnected, default: false)
    }

    /// A writing deletion waiting out its grace period: its date.
    var scheduledWritingDeletion: Date? { status == "scheduled" ? purgeAt : nil }
}

/// `POST /api/account/delete` and `POST /api/account/delete/confirm` (202):
/// `scheduled` (signed out on the server, `delete_on` set) or `confirm_email`
/// (a link went to the account's address).
struct AccountDeletionAnswer: Decodable, Equatable, Sendable {
    var status: String
    var deleteOn: Date?
    var expiresIn: Int?

    enum CodingKeys: String, CodingKey {
        case status
        case deleteOn = "delete_on"
        case expiresIn = "expires_in"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = c.tolerant(.status, default: "")
        deleteOn = c.tolerant(.deleteOn)
        expiresIn = c.tolerant(.expiresIn)
    }

    var isScheduled: Bool { status == "scheduled" }
}

/// `GET /api/account/restore`: the deleted account a sign-in just reached.
struct RestoreOffer: Decodable, Equatable, Sendable {
    var username: String
    var deleteOn: Date?
    var restorable: Bool

    enum CodingKeys: String, CodingKey {
        case username, restorable
        case deleteOn = "delete_on"
    }

    init(username: String, deleteOn: Date?, restorable: Bool) {
        self.username = username
        self.deleteOn = deleteOn
        self.restorable = restorable
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        username = c.tolerant(.username, default: "")
        deleteOn = c.tolerant(.deleteOn)
        restorable = c.tolerant(.restorable, default: false)
    }
}

enum AccountDeletion {
    /// Where a sign-in into a deleted account lands (`offer_restore`): the
    /// browser holds the restore question, not a sign-in.
    static let restorePath = "/account-restore"

    /// True when a sign-in's landing (a path, or a frontend URL) is the restore page.
    static func isRestoreLanding(_ landing: String?) -> Bool {
        guard let landing, let components = URLComponents(string: landing) else { return false }
        return components.path == restorePath
    }

    /// `utils/dataDeletion.js` `X_REMOVE_ACCESS_STEPS`.
    static let xRemoveAccessSteps =
        "on X, open Settings and privacy, then Security and account access, " +
        "Apps and sessions, Connected apps, choose Loore and revoke its permissions."

    /// The day a deletion happens, "November 5, 2026" (the web's
    /// `formatDeletionDate`, in the app's en-US date style).
    static func formatDate(_ date: Date?, locale: Locale = Locale(identifier: "en_US"),
                           timeZone: TimeZone = .current) -> String {
        guard let date else { return "" }
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = timeZone
        formatter.setLocalizedDateFormatFromTemplate("MMMMdyyyy")
        return formatter.string(from: date)
    }

    /// The grace period's end if the deletion started now (the web's
    /// `Date.now() + days * DAY_MS`).
    static func dateAfter(days: Int, from now: Date = Date()) -> Date {
        now.addingTimeInterval(TimeInterval(days) * 24 * 3600)
    }

    static let confirmPath = "/confirm-account-deletion"

    /// The token of a pasted `/confirm-account-deletion?token=…` link (full
    /// URL, a path, the link inside copied text, or the bare token). Links to
    /// other pages (a sign-in link, the email confirmation) give nil.
    static func token(fromPasted text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let pattern = #"https?://[^\s"'<>]+|/confirm-account-deletion[^\s"'<>]*"#
        if let range = trimmed.range(of: pattern, options: .regularExpression) {
            var candidate = String(trimmed[range])
            if candidate.hasPrefix("/") { candidate = "https://loore.org" + candidate }
            // A link copied out of prose can carry a trailing full stop or bracket.
            while let last = candidate.last, ".,;)]".contains(last) { candidate.removeLast() }
            candidate = candidate.replacingOccurrences(of: "&amp;", with: "&")
            guard let components = URLComponents(string: candidate), components.path == confirmPath else { return nil }
            let value = components.queryItems?.first { $0.name == "token" }?.value
            return value?.isEmpty == false ? value : nil
        }
        // A bare token: itsdangerous's URL-safe characters, no spaces.
        let allowed = CharacterSet(charactersIn:
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
        guard trimmed.count >= 16, trimmed.unicodeScalars.allSatisfy(allowed.contains) else { return nil }
        return trimmed
    }
}
