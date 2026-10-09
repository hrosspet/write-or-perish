import SwiftUI

// "Delete my account" (#269), with the web's wording and behaviour: the
// Account page section and dialog (`AccountPage` `DeleteAccountSection`,
// `DeleteAccountDialog`) and the emailed link's confirmation
// (`ConfirmAccountDeletionPage`).
//
// An account with an email address confirms from a mailed link that works
// only inside a signed-in session of the account. The app has no universal
// links, so a tapped link opens Safari, where the app's session is not. The
// user copies the link from the email and pastes it here; the app posts the
// token with its own session (as for the email change, #260).

/// The Account page's deletion section and dialog.
@MainActor
@Observable
final class DeleteAccountModel {
    private let app: AppState
    /// The username typed into the dialog.
    var typed = ""
    private(set) var busy = false
    private(set) var error: String?
    /// The confirmation link went to the account's address.
    private(set) var linkSent = false
    /// The clock the dialog's dates count from (tests pin it).
    @ObservationIgnored var now: () -> Date = { Date() }

    init(app: AppState) {
        self.app = app
    }

    var user: CurrentUser? { app.user }
    var info: AccountDeletionInfo? { app.user?.accountDeletion }
    /// Offered only when the server has account deletion (the app and the
    /// backend are released separately).
    var isAvailable: Bool { info != nil }
    var refusal: String? { info?.refusal?.message }
    var graceDays: Int { info?.graceDays ?? 30 }
    var confirmsByEmail: Bool { info?.confirmByEmail == true }
    var username: String { user?.username ?? "" }
    /// The day the account is deleted if the user confirms now.
    var deletionDate: String { AccountDeletion.formatDate(AccountDeletion.dateAfter(days: graceDays, from: now())) }

    var matches: Bool { !username.isEmpty && typed.jsTrimmed.lowercased() == username.lowercased() }
    var canConfirm: Bool { matches && !busy }

    // MARK: Section

    var sectionText: String {
        "Hides your account at once and signs you out everywhere. After \(graceDays) days the account and "
            + "everything in it are deleted. Until then, signing in lets you restore it."
    }

    var linkSentText: String {
        let minutes = Int((Double(info?.linkExpiresIn ?? 3600) / 60).rounded())
        return "Check your email: Loore sent a confirmation link to \(user?.email ?? ""). Nothing changes until "
            + "you open it and confirm there. The link works for \(minutes) minutes."
    }

    /// The app's way to "open it and confirm there" (the email's button is "Review and confirm").
    static let pasteInstruction =
        "To confirm in the app, touch and hold Review and confirm in the email, choose Copy Link, then paste it here."

    // MARK: Dialog

    /// The dialog's paragraphs before the email note and the username field.
    var dialogParagraphs: [String] {
        let date = deletionDate
        var paragraphs = [
            "Your account is hidden at once and you are signed out everywhere. Nobody can see your public writing any more.",
            "After \(graceDays) days, on \(date), Loore deletes the account and everything in it: your entries and "
                + "recordings, the AI's replies, your profile, intentions and other documents, your todo list, drafts, "
                + "shares, saved references, imports, poll answers, settings and sign-in. If other people replied to "
                + "your entries, their replies stay and your entry shows as deleted. Loore keeps a record of what your "
                + "AI use cost, without your name.",
            "Until then you can restore your account by signing in. Afterwards it cannot be restored. Nobody else "
                + "can take your username for \(info?.usernameReserveDays ?? 365) days after the deletion.",
        ]
        if let writing = user?.dataDeletion?.scheduledWritingDeletion {
            paragraphs.append("This replaces your request to delete all your writing on \(AccountDeletion.formatDate(writing)): "
                + "everything is deleted on \(date) instead, and restoring your account cancels both.")
        }
        if user?.dataDeletion?.xConnected == true {
            paragraphs.append("Loore also forgets your X connection for bookmarks. To remove Loore's access on X as well: "
                + AccountDeletion.xRemoveAccessSteps)
        }
        return paragraphs
    }

    var confirmTitle: String {
        if busy { return "One moment…" }
        return confirmsByEmail ? "Email me the confirmation link" : "Delete my account"
    }

    var confirmSubtitle: String {
        confirmsByEmail ? "Nothing changes until you confirm from the link."
            : "Signs you out now. You can restore it until \(deletionDate)."
    }

    /// The dialog opens empty (the web resets it on open).
    func dialogOpened() {
        typed = ""
        error = nil
    }

    /// `POST /api/account/delete {"confirm": "<username>"}`. An account without
    /// email is scheduled at once and signed out; one with email gets the link.
    /// Returns true when the dialog should close.
    func requestDeletion() async -> Bool {
        guard canConfirm else { return false }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let answer: AccountDeletionAnswer = try await app.api.post(
                APIPath.accountDelete, json: ["confirm": .string(typed.jsTrimmed)])
            if answer.isScheduled {
                await app.accountDeletionScheduled(deleteOn: answer.deleteOn)
            } else {
                linkSent = true
            }
            return true
        } catch let failure as APIError {
            error = failure.serverMessage ?? Self.requestFailed
        } catch {
            self.error = Self.requestFailed
        }
        return false
    }

    static let requestFailed = "Could not start the deletion. Please try again."
}

/// The section at the bottom of the Account page (anchor `delete-account`).
struct DeleteAccountSection: View {
    @Bindable var model: DeleteAccountModel
    @State private var dialogShown = false
    @State private var pasteShown = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Delete my account")
                .font(LooreFont.serif(18.4, .light, relativeTo: .title3))
                .foregroundStyle(LooreColor.textPrimary)
                .accessibilityAddTraits(.isHeader)
                .padding(.bottom, 4)
            if model.linkSent {
                Text(model.linkSentText)
                    .font(LooreFont.sans(13.6, .regular))
                    .foregroundStyle(LooreColor.textSecondary)
                    .lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("deleteAccount.linkSent")
                sectionHelper(DeleteAccountModel.pasteInstruction)
                Button("Paste the link") { pasteShown = true }
                    .buttonStyle(.looreOutline)
                    .accessibilityIdentifier("deleteAccount.pasteLink")
            } else {
                sectionHelper(model.sectionText)
                if let refusal = model.refusal {
                    sectionHelper(refusal)
                        .accessibilityIdentifier("deleteAccount.refusal")
                } else {
                    Button {
                        model.dialogOpened()
                        dialogShown = true
                    } label: {
                        Text("Delete my account…").foregroundStyle(LooreColor.error)
                    }
                    .buttonStyle(.looreOutline)
                    .accessibilityIdentifier("deleteAccount.open")
                    if model.confirmsByEmail {
                        // The app may have been closed while the user was in Mail.
                        Button("I already have a confirmation link") { pasteShown = true }
                            .buttonStyle(.looreQuiet)
                            .accessibilityIdentifier("deleteAccount.haveLink")
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .sheet(isPresented: $dialogShown) {
            DeleteAccountSheet(model: model)
        }
        .sheet(isPresented: $pasteShown) {
            ConfirmAccountDeletionPasteSheet()
        }
    }

    private func sectionHelper(_ text: String) -> some View {
        Text(text)
            .font(LooreFont.sans(13.6, .light))
            .foregroundStyle(LooreColor.textMuted)
            .lineSpacing(4)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The web's `DeleteAccountDialog` as a sheet (it is long, and has a text
/// field the keyboard must not cover). Both choices are buttons; the delete
/// one stays disabled until the username is typed.
struct DeleteAccountSheet: View {
    @Bindable var model: DeleteAccountModel
    @Environment(\.dismiss) private var dismiss
    @FocusState private var fieldFocused: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("Delete your account?")
                    .font(LooreFont.dialogTitle)
                    .foregroundStyle(LooreColor.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                    .padding(.bottom, 2)
                ForEach(model.dialogParagraphs, id: \.self) { paragraph in
                    DialogBodyText(text: paragraph)
                }
                if model.confirmsByEmail {
                    bodyText(Text("To make sure it is you, Loore emails a confirmation link to ")
                        + Text(model.user?.email ?? "").fontWeight(.semibold)
                        + Text(". Nothing happens until you open it and confirm there."))
                }
                bodyText(Text("To confirm, type your username, ") + Text(model.username).fontWeight(.semibold) + Text("."))
                    .accessibilityHidden(true)
                TextField("", text: $model.typed)
                    .textFieldStyle(LooreTextFieldStyle(isFocused: fieldFocused))
                    .focused($fieldFocused)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .textContentType(.username)
                    .submitLabel(.done)
                    .onSubmit(confirm)
                    .accessibilityLabel("To confirm, type your username, \(model.username).")
                    .accessibilityIdentifier("deleteAccount.confirmField")
                if let error = model.error {
                    Text(error)
                        .font(LooreFont.body)
                        .foregroundStyle(LooreColor.error)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("deleteAccount.error")
                }
                VStack(spacing: 8) {
                    ChoiceButton(title: model.confirmTitle, subtitle: model.confirmSubtitle, tone: .destructive, action: confirm)
                        .disabled(!model.canConfirm)
                        .opacity(model.canConfirm ? 1 : 0.45)
                        .accessibilityIdentifier("deleteAccount.confirm")
                    ChoiceButton(title: "Keep my account") { dismiss() }
                        .accessibilityIdentifier("deleteAccount.keep")
                }
                .padding(.top, 4)
            }
            .padding(LooreSpacing.gutter)
            .frame(maxWidth: 520, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .presentationDetents([.large])
        .presentationBackground(LooreColor.bgCard)
        .interactiveDismissDisabled(model.busy)
    }

    private func bodyText(_ text: Text) -> some View {
        text
            .font(LooreFont.body)
            .foregroundStyle(LooreColor.textSecondary)
            .lineSpacing(5)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func confirm() {
        guard model.canConfirm else { return }
        fieldFocused = false
        Task {
            if await model.requestDeletion() { dismiss() }
        }
    }
}

// MARK: - The emailed link

/// The confirmation the mailed link leads to (web `ConfirmAccountDeletionPage`):
/// it asks again, with both choices, before it posts anything. The app is
/// always signed in here, and the token only counts in the session of the
/// account it was sent for.
@MainActor
@Observable
final class ConfirmAccountDeletionModel {
    struct Failure: Equatable {
        var text: String
        var reason: String
    }

    private let app: AppState
    let token: String?
    private(set) var busy = false
    private(set) var failure: Failure?
    @ObservationIgnored var now: () -> Date = { Date() }

    init(app: AppState, token: String?) {
        self.app = app
        self.token = token?.isEmpty == true ? nil : token
    }

    var username: String { app.user?.username ?? "" }

    var heading: String { token == nil ? "This link is incomplete" : "Delete @\(username)?" }

    var message: String {
        guard token != nil else {
            return "Open the link from the email again, or ask for the deletion again on the Account page."
        }
        let days = app.user?.accountDeletion?.graceDays ?? 30
        let date = AccountDeletion.formatDate(AccountDeletion.dateAfter(days: days, from: now()))
        return "When you confirm, your account is hidden and you are signed out everywhere. On \(date) it is "
            + "deleted with everything in it. Until then you can restore it by signing in. Restoring it also "
            + "cancels a request to delete all your writing, if one is waiting."
    }

    /// The error line; for another account's link it says whose session this is.
    var failureText: String? {
        guard let failure else { return nil }
        return failure.reason == "other_account" ? "\(failure.text) You are signed in as @\(username)." : failure.text
    }

    var offersSignOut: Bool { failure?.reason == "other_account" }

    /// `POST /api/account/delete/confirm {"token"}`: scheduled and signed out.
    func confirm() async {
        guard let token, !busy else { return }
        busy = true
        failure = nil
        defer { busy = false }
        do {
            let answer: AccountDeletionAnswer = try await app.api.post(
                APIPath.accountDeleteConfirm, json: ["token": .string(token)])
            await app.accountDeletionScheduled(deleteOn: answer.deleteOn)
        } catch let error as APIError {
            failure = Failure(text: error.serverMessage ?? Self.confirmFailed, reason: error.reason ?? "failed")
        } catch {
            failure = Failure(text: Self.confirmFailed, reason: "failed")
        }
    }

    static let confirmFailed = "Could not confirm. Please try again."
}

struct ConfirmAccountDeletionView: View {
    let token: String?
    /// Set when shown in the paste sheet: "Keep my account" closes it.
    var onClose: (() -> Void)?

    @Environment(AppState.self) private var app
    @State private var model: ConfirmAccountDeletionModel?

    var body: some View {
        AccountMessagePage(heading: model?.heading ?? "Delete your account", message: model?.message ?? "One moment.") {
            if let model { actions(model) }
        }
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            if model == nil { model = ConfirmAccountDeletionModel(app: app, token: token) }
        }
    }

    @ViewBuilder private func actions(_ model: ConfirmAccountDeletionModel) -> some View {
        if model.token == nil {
            PageLinkButton(title: "Back to your account →", action: keep)
        } else {
            VStack(spacing: 8) {
                PageChoiceButton(title: model.busy ? "One moment…" : "Delete my account", color: LooreColor.error) {
                    Task { await model.confirm() }
                }
                .disabled(model.busy)
                .accessibilityIdentifier("confirmAccountDeletion.confirm")
                PageChoiceButton(title: "Keep my account", action: keep)
                    .accessibilityIdentifier("confirmAccountDeletion.keep")
                if let text = model.failureText {
                    Text(text)
                        .font(LooreFont.sans(14.4, .light))
                        .foregroundStyle(LooreColor.error)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 8)
                        .accessibilityIdentifier("confirmAccountDeletion.error")
                    if model.offersSignOut {
                        PageLinkButton(title: "Sign out and use the other account →") {
                            Task { await app.signOut() }
                        }
                    }
                }
            }
            .frame(maxWidth: 360)
        }
    }

    private func keep() {
        if let onClose { onClose() } else { app.open(.account(anchor: "delete-account")) }
    }
}

/// Paste the link from "Confirm the deletion of your Loore account": the
/// token only counts inside this app's session, so the link is pasted here,
/// then confirmed in the same sheet.
struct ConfirmAccountDeletionPasteSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var link = ""
    @State private var token: String?

    var body: some View {
        NavigationStack {
            Group {
                if let token {
                    ConfirmAccountDeletionView(token: token, onClose: { dismiss() })
                } else {
                    form
                }
            }
            .navigationTitle("Delete my account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            }
        }
        .presentationDetents([.large])
        .presentationBackground(LooreColor.bgDeep)
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 14) {
            DialogBodyText(text: "Copy the link from the confirmation email and paste it here. It works only in the account that asked for it.")
            HStack(spacing: 8) {
                TextField("", text: $link, prompt: loorePrompt("https://loore.org/confirm-account-deletion?token=…"), axis: .vertical)
                    .lineLimit(1...3)
                    .textFieldStyle(LooreTextFieldStyle())
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .accessibilityLabel("The link from the email")
                    .accessibilityIdentifier("confirmAccountDeletion.pasteField")
                PasteButton(payloadType: String.self) { strings in
                    guard let first = strings.first else { return }
                    Task { @MainActor in
                        link = first
                        token = AccountDeletion.token(fromPasted: first)
                    }
                }
                .labelStyle(.iconOnly)
                .buttonBorderShape(.roundedRectangle(radius: LooreRadius.small))
                .tint(LooreColor.accent)
            }
            if !link.jsTrimmed.isEmpty, AccountDeletion.token(fromPasted: link) == nil {
                Text("That is not the link from the confirmation email.")
                    .font(LooreFont.sans(13.6, .light))
                    .foregroundStyle(LooreColor.accent)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Continue") { token = AccountDeletion.token(fromPasted: link) }
                    .buttonStyle(.loorePrimary)
                    .disabled(AccountDeletion.token(fromPasted: link) == nil)
                    .accessibilityIdentifier("confirmAccountDeletion.pasteContinue")
            }
            Spacer()
        }
        .padding(LooreSpacing.gutter)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(LooreColor.bgDeep)
    }
}
