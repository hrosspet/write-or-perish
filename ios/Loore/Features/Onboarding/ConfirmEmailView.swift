import SwiftUI

/// Reads the token of a pasted `/confirm-email?token=…` link (full URL, a
/// path, or the bare token).
enum ConfirmEmailLink {
    static func token(from text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if trimmed.contains("confirm-email") || trimmed.contains("token=") {
            let candidate = trimmed.hasPrefix("/") ? "https://loore.org" + trimmed : trimmed
            let value = URLComponents(string: candidate)?.queryItems?.first { $0.name == "token" }?.value
            return value?.isEmpty == false ? value : nil
        }
        // A bare token: URL-safe characters only, no spaces.
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_.~"))
        guard trimmed.count >= 16, trimmed.unicodeScalars.allSatisfy(allowed.contains) else { return nil }
        return trimmed
    }
}

/// Confirms a new sign-in email (web `ConfirmEmailPage`, map E §11.3): posts the
/// token once, then shows the outcome. The app is always signed in here (the
/// token only counts inside the session of the account that asked for it).
struct ConfirmEmailView: View {
    let token: String?
    /// Set when shown in a sheet (the waitlist): "Continue" closes it.
    var onClose: (() -> Void)?

    @Environment(AppState.self) private var app
    @State private var model: ConfirmEmailModel?

    var body: some View {
        VStack(spacing: 0) {
            if let model {
                Text(model.heading)
                    .font(LooreFont.serif(28.8, .light, relativeTo: .largeTitle))
                    .foregroundStyle(LooreColor.textPrimary)
                    .multilineTextAlignment(.center)
                    .padding(.bottom, 16)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("confirmEmail.heading")
                bodyText(model)
                    .font(LooreFont.sans(15.2, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                    .lineSpacing(6)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 24)
                    .accessibilityAddTraits(.updatesFrequently)
                action(model)
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 64)
        .frame(maxWidth: 520)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            if model == nil {
                let created = ConfirmEmailModel(app: app, token: token)
                model = created
                created.startOnce()
            }
        }
    }

    private func bodyText(_ model: ConfirmEmailModel) -> Text {
        if case .confirmed(let email) = model.state {
            return Text(email ?? "").font(LooreFont.sans(15.2, .regular)).foregroundColor(LooreColor.textPrimary)
                + Text(" is now your sign-in address.")
        }
        return Text(model.body)
    }

    @ViewBuilder private func action(_ model: ConfirmEmailModel) -> some View {
        switch model.action {
        case .none:
            EmptyView()
        case .back:
            linkButton("\(model.backLabel) →") {
                if let onClose { onClose() } else if model.approved { app.open(.account(anchor: "email")) }
            }
        case .retry:
            linkButton("Try again") { model.confirm() }
        case .signOut:
            linkButton("Sign out and use the other account →") {
                Task { await app.signOut() }
            }
        }
    }

    private func linkButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(15.2, .regular))
                .foregroundStyle(LooreColor.accent)
                .padding(.bottom, 2)
                .overlay(alignment: .bottom) { Rectangle().fill(LooreColor.accentGlow).frame(height: 1) }
                .frame(minHeight: 44)
        }
        .accessibilityLabel(title.replacingOccurrences(of: " →", with: ""))
        .buttonStyle(.plain)
        .accessibilityIdentifier("confirmEmail.action")
    }
}

/// The page's states and its one `POST /api/dashboard/email/confirm` per visit.
@MainActor
@Observable
final class ConfirmEmailModel {
    enum State: Equatable {
        case confirming
        case confirmed(email: String?)
        case failed(reason: String, text: String)
    }

    enum Action: Equatable { case none, back, retry, signOut }

    private let app: AppState
    let token: String?
    private(set) var state: State = .confirming
    private var started = false
    /// The latest `confirm()` POST and its outcome; tests await it.
    @ObservationIgnored private(set) var confirmTask: Task<Void, Never>?

    init(app: AppState, token: String?) {
        self.app = app
        self.token = token?.isEmpty == true ? nil : token
    }

    var approved: Bool { app.user?.approved == true }
    var backLabel: String { approved ? "Back to your account" : "Continue" }

    /// Posts once per visit (the web's StrictMode-safe `started` ref).
    func startOnce() {
        guard !started, token != nil, app.user != nil else { return }
        started = true
        confirm()
    }

    func confirm() {
        guard let token else { return }
        state = .confirming
        confirmTask = Task {
            do {
                let answer: EmailState = try await app.api.post(APIPath.emailConfirm, json: ["token": .string(token)])
                app.applyEmailState(answer)
                state = .confirmed(email: answer.email)
            } catch let error as APIError {
                let reason = error.reason ?? "failed"
                state = .failed(reason: reason,
                                text: error.serverMessage ?? "Could not confirm the address. Please try again.")
            } catch {
                state = .failed(reason: "failed", text: "Could not confirm the address. Please try again.")
            }
        }
    }

    var heading: String {
        if token == nil { return "This link is incomplete" }
        switch state {
        case .confirming: return "Confirming your email"
        case .confirmed: return "Email confirmed"
        case .failed: return "Not confirmed"
        }
    }

    var body: String {
        if token == nil { return "Open the link from the confirmation email again, or request a new one." }
        switch state {
        case .confirming: return "One moment."
        case .confirmed(let email): return "\(email ?? "") is now your sign-in address."
        case .failed(let reason, let text):
            return reason == "other_account" ? "\(text) You are signed in as @\(app.user?.username ?? "")." : text
        }
    }

    var action: Action {
        if token == nil { return .back }
        switch state {
        case .confirming: return .none
        case .confirmed: return .back
        case .failed(let reason, _):
            if reason == "failed" { return .retry }
            if reason == "other_account" { return .signOut }
            return .back
        }
    }
}

/// Paste a `/confirm-email?token=…` link (design doc §4.6): the token only
/// counts inside this app's session, so the mailed link is pasted here, then
/// confirmed in the same sheet. Used by Account and the waitlist.
struct ConfirmEmailPasteSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var link = ""
    @State private var token: String?

    var body: some View {
        NavigationStack {
            Group {
                if let token {
                    ConfirmEmailView(token: token, onClose: { dismiss() })
                } else {
                    form
                }
            }
            .navigationTitle("Confirm email")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationBackground(LooreColor.bgDeep)
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 14) {
            DialogBodyText(text: "Copy the link from the confirmation email and paste it here. It works only in the account that asked for it.")
            TextField("", text: $link, prompt: loorePrompt("https://loore.org/confirm-email?token=…"), axis: .vertical)
                .textFieldStyle(LooreTextFieldStyle())
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .accessibilityIdentifier("confirmEmail.pasteField")
            HStack(spacing: 8) {
                Button("Paste") { link = UIPasteboard.general.string ?? link }
                    .buttonStyle(.looreOutline)
                Spacer()
                Button("Confirm") { token = ConfirmEmailLink.token(from: link) }
                    .buttonStyle(.loorePrimary)
                    .disabled(ConfirmEmailLink.token(from: link) == nil)
                    .accessibilityIdentifier("confirmEmail.pasteConfirm")
            }
            Spacer()
        }
        .padding(LooreSpacing.gutter)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(LooreColor.bgDeep)
    }
}
