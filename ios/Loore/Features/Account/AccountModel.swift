import Foundation
import Observation

/// The Account page's logic (web `AccountPage`, map E §6): username, the
/// email verification flow (one request in flight at a time), X connect and
/// disconnect, and the settings rows that save on change.
@MainActor
@Observable
final class AccountModel {
    struct Message: Equatable {
        enum Kind { case success, info, error }
        var kind: Kind
        var text: String
    }

    /// `?x_login=<outcome>` after Connect X (#311).
    static let xLoginMessages: [String: Message] = [
        "linked": Message(kind: .success, text: "X connected. Sign in with X now opens this account."),
        "cancelled": Message(kind: .info, text: "X was not connected."),
        "failed": Message(kind: .error, text: "Could not read your X account. Please try again."),
        "other_x": Message(kind: .error, text: "This account is already connected to a different X account."),
        "taken": Message(kind: .error, text: "That X account already signs in to another Loore account, so it can't be connected here. If that account was made by accident, write to info@loore.org and we'll remove it so you can connect X here."),
        "taken_placeholder": Message(kind: .error, text: "An account was already set up for that X account, and no one has signed in to it yet. Write to info@loore.org and we'll join it with this one."),
    ]

    private let app: AppState

    var username = ""
    var usernameSaving = false
    var usernameMessage: Message?

    var emailInput = ""
    private(set) var emailSaving = false
    var emailMessage: Message?
    /// The web's `emailInFlight` ref: a double tap must not mail two links.
    private var emailInFlight = false

    var xMessage: Message?
    private(set) var xSaving = false

    /// Fields saving right now (`default_privacy_level`, …): their selects are disabled.
    private(set) var savingFields: Set<String> = []

    init(app: AppState) {
        self.app = app
        username = app.user?.username ?? ""
    }

    var user: CurrentUser? { app.user }

    // MARK: Username

    static func usernameError(_ value: String) -> String? {
        let v = value.jsTrimmed
        if v.isEmpty { return "Username cannot be empty." }
        if v.jsLength > 64 { return "Username must be 64 characters or fewer." }
        if !JSRegex.test(v, "^[a-zA-Z0-9_]+$") { return "Only letters, numbers, and underscores allowed." }
        return nil
    }

    var usernameUnchanged: Bool { username.jsTrimmed == (user?.username ?? "") }

    func saveUsername() async {
        if let error = Self.usernameError(username) {
            usernameMessage = Message(kind: .error, text: error)
            return
        }
        usernameSaving = true
        usernameMessage = nil
        defer { usernameSaving = false }
        do {
            _ = try await app.updateUser(["username": .string(username.jsTrimmed)])
            usernameMessage = Message(kind: .success, text: "Username updated.")
        } catch let error as APIError {
            usernameMessage = Message(kind: .error, text: error.userMessage(fallback: "Failed to update username."))
        } catch {
            usernameMessage = Message(kind: .error, text: "Failed to update username.")
        }
    }

    // MARK: Email (#260)

    /// "Confirmation link sent to …" / "… has expired." (nil without a pending address).
    var pendingNotice: String? {
        guard let pending = user?.pendingEmail else { return nil }
        if user?.pendingEmailExpired == true { return "The confirmation link sent to \(pending) has expired." }
        return "Confirmation link sent to \(pending). Open it to make it your sign-in address. Nothing after a few minutes? Check the spelling; an address that already signs in to Loore can't be added here."
    }

    var resendTitle: String { user?.pendingEmailExpired == true ? "Send a new link" : "Resend" }

    var emailHelper: String {
        user?.email != nil
            ? "The new address becomes yours once you confirm it from the link we send to it."
            : "You currently sign in with X only."
    }

    /// "Remove email" only when the account also signs in with X.
    var showsRemoveEmail: Bool { user?.email != nil && user?.twitterLogin == true }

    /// `POST /api/dashboard/email {email}`; at most one request in flight.
    func sendEmailLink(_ address: String, resend: Bool = false) async {
        let value = address.jsTrimmed
        guard !value.isEmpty, !emailInFlight else { return }
        emailInFlight = true
        emailSaving = true
        emailMessage = nil
        defer {
            emailInFlight = false
            emailSaving = false
        }
        do {
            let state: EmailState = try await app.api.post(APIPath.email, json: ["email": .string(value)])
            app.applyEmailState(state)
            emailInput = ""
            if resend {
                emailMessage = Message(kind: .success, text: "New link sent. The earlier one no longer works.")
            }
        } catch let error as APIError {
            emailMessage = Message(kind: .error, text: error.userMessage(fallback: "Could not send the confirmation email."))
        } catch {
            emailMessage = Message(kind: .error, text: "Could not send the confirmation email.")
        }
    }

    /// `DELETE /api/dashboard/email/pending`, adopting the server's email state.
    func cancelPendingEmail() async {
        guard !emailInFlight else { return }
        emailInFlight = true
        emailSaving = true
        emailMessage = nil
        defer {
            emailInFlight = false
            emailSaving = false
        }
        do {
            let state: EmailState = try await app.api.delete(APIPath.emailPending)
            app.applyEmailState(state)
        } catch {
            emailMessage = Message(kind: .error, text: "Could not cancel the pending change.")
        }
    }

    /// `DELETE /api/dashboard/email` (only offered with X login).
    func removeEmail() async {
        guard !emailInFlight else { return }
        emailInFlight = true
        emailSaving = true
        emailMessage = nil
        defer {
            emailInFlight = false
            emailSaving = false
        }
        do {
            let state: EmailState = try await app.api.delete(APIPath.email)
            app.applyEmailState(state)
            emailMessage = Message(kind: .success, text: "Email removed. You sign in with X.")
        } catch let error as APIError {
            emailMessage = Message(kind: .error, text: error.userMessage(fallback: "Could not remove the email."))
        } catch {
            emailMessage = Message(kind: .error, text: "Could not remove the email.")
        }
    }

    // MARK: X (#311)

    var xConnectedText: String? {
        guard user?.twitterLogin == true else { return nil }
        return user?.twitterHandle.map { "Connected as @\($0)" } ?? "Connected"
    }

    var xHelper: String {
        user?.twitterLogin == true ? "Sign in with X opens this account."
            : "Lets you sign in with X as well. X will ask you to allow Loore."
    }

    /// An X-only account cannot disconnect (it would have no way to sign in).
    var showsDisconnectX: Bool { user?.twitterLogin == true && user?.email != nil }

    /// The Connect X web flow came back with `x_login=<outcome>` (unknown → no message).
    func xLoginReturned(_ outcome: String?) async {
        xMessage = outcome.flatMap { Self.xLoginMessages[$0] }
        await app.loadUser()
    }

    func disconnectX() async {
        xSaving = true
        xMessage = nil
        defer { xSaving = false }
        do {
            let _: EmptyResponse = try await app.api.delete(APIPath.disconnectX)
            app.markXDisconnected()
            xMessage = Message(kind: .success, text: "X disconnected. You sign in with email.")
        } catch let error as APIError {
            xMessage = Message(kind: .error, text: error.userMessage(fallback: "Could not disconnect X."))
        } catch {
            xMessage = Message(kind: .error, text: "Could not disconnect X.")
        }
    }

    // MARK: Settings

    /// `PUT /api/dashboard/user {field: value}`; errors are silent (web).
    func saveField(_ field: String, _ value: JSONValue) async {
        savingFields.insert(field)
        defer { savingFields.remove(field) }
        _ = try? await app.updateUser([field: value])
    }

    func isSaving(_ field: String) -> Bool { savingFields.contains(field) }
}
