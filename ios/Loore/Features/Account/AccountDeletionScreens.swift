import SwiftUI

// The one-message pages around account deletion (#269), with the web's
// wording (`AccountRestorePage`, `AccountDeletedPage`, `accountPageStyles`).

/// Where a sign-in into a deleted account lands during its grace period (web
/// `AccountRestorePage`). The person is not signed in yet: the session cookie
/// only holds the question, for a few minutes. Both choices are shown.
struct AccountRestoreView: View {
    @Environment(AppState.self) private var app
    @State private var model: AccountRestoreModel?

    var body: some View {
        AccountMessagePage(heading: model?.heading ?? "One moment", message: model?.message ?? "") {
            if let model { actions(model) }
        }
        .accessibilityIdentifier("accountRestore")
        .task {
            if model == nil {
                let created = AccountRestoreModel(app: app)
                model = created
                await created.load()
            }
        }
    }

    @ViewBuilder private func actions(_ model: AccountRestoreModel) -> some View {
        switch model.state {
        case .loading:
            EmptyView()
        case .offer(let offer) where offer.restorable:
            VStack(spacing: 8) {
                PageChoiceButton(title: "Restore my account", color: LooreColor.accent) {
                    Task { await model.restore() }
                }
                .disabled(model.busy)
                .accessibilityIdentifier("accountRestore.restore")
                PageChoiceButton(title: "Keep it deleted") {
                    Task { await model.keepDeleted() }
                }
                .disabled(model.busy)
                .accessibilityIdentifier("accountRestore.keepDeleted")
                if let error = model.error {
                    Text(error)
                        .font(LooreFont.sans(14.4, .light))
                        .foregroundStyle(LooreColor.error)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 8)
                        .accessibilityIdentifier("accountRestore.error")
                }
            }
            .frame(maxWidth: 360)
        case .none:
            PageLinkButton(title: "Sign in →") { Task { await app.leaveAccountDeletionScreen() } }
        case .offer, .declined:
            PageLinkButton(title: "Back to Loore →") { Task { await app.leaveAccountDeletionScreen() } }
        }
    }
}

/// The restore page's states and calls: `GET`, `POST /api/account/restore`
/// and `POST /api/account/restore/decline`, with the app's cookie jar (it
/// holds the session cookie the sign-in set).
@MainActor
@Observable
final class AccountRestoreModel {
    enum State: Equatable {
        case loading
        case offer(RestoreOffer)
        /// No restore question in the session (expired, or never asked).
        case none
        case declined
    }

    private let app: AppState
    private(set) var state: State = .loading
    private(set) var busy = false
    private(set) var error: String?

    init(app: AppState) {
        self.app = app
    }

    func load() async {
        do {
            let offer: RestoreOffer = try await app.api.get(APIPath.accountRestore)
            state = .offer(offer)
        } catch {
            // The web: any failure means there is nothing to restore here.
            state = .none
        }
    }

    /// Restores the account; on success the app signs in as after any sign-in.
    func restore() async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let _: EmptyResponse = try await app.api.post(APIPath.accountRestore)
            await app.restoreCompleted()
        } catch let failure as APIError where failure.code == "no_offer" {
            // The question expired while it was open: say so, and offer the sign-in.
            state = .none
        } catch let failure as APIError {
            error = failure.serverMessage ?? Self.restoreFailed
        } catch {
            self.error = Self.restoreFailed
        }
    }

    /// Keeps the account deleted. A failed call changes nothing: the offer
    /// expires on its own (the web ignores the error too).
    func keepDeleted() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        let _: EmptyResponse? = try? await app.api.post(APIPath.accountRestoreDecline)
        state = .declined
    }

    static let restoreFailed = "Could not restore the account. Please try again."

    var heading: String {
        switch state {
        case .loading: return "One moment"
        case .declined: return "Your account stays deleted"
        case .none: return "Nothing to restore here"
        case .offer(let offer): return offer.restorable ? "Restore your account?" : "Your account is being deleted"
        }
    }

    var message: String {
        switch state {
        case .loading: return ""
        case .declined: return "You are signed out. Nothing else changes."
        case .none: return "Sign in again to see your account's options."
        case .offer(let offer) where !offer.restorable:
            return "The deletion of @\(offer.username) has started and cannot be undone."
        case .offer(let offer):
            let date = AccountDeletion.formatDate(offer.deleteOn)
            return "You deleted @\(offer.username). You can restore it until \(date); after that it is deleted "
                + "forever, with everything in it. Restore it to keep using Loore, or keep it deleted. Restoring it "
                + "also cancels a request to delete all your writing, if one is waiting."
        }
    }
}

/// After the user's own deletion is scheduled (web `AccountDeletedPage`): the
/// session has ended, so the date comes from the server's answer.
struct AccountDeletedView: View {
    let deleteOn: Date?
    @Environment(AppState.self) private var app

    static func message(deleteOn: Date?) -> String {
        let date = AccountDeletion.formatDate(deleteOn)
        let when = date.isEmpty
            ? "For 30 days you can restore it by signing in;"
            : "Until \(date) you can restore it by signing in;"
        return "You are signed out. \(when) after that it is deleted forever, with everything in it."
    }

    var body: some View {
        AccountMessagePage(heading: "Your account is deleted", message: Self.message(deleteOn: deleteOn)) {
            PageLinkButton(title: "Back to Loore →") { Task { await app.leaveAccountDeletionScreen() } }
        }
        .accessibilityIdentifier("accountDeleted")
    }
}

// MARK: - Shared layout

/// The web's one-message page: centred serif heading, body text, actions.
struct AccountMessagePage<Actions: View>: View {
    let heading: String
    let message: String
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        GeometryReader { proxy in
        ScrollView {
            VStack(spacing: 0) {
                Text(heading)
                    .font(LooreFont.serif(28.8, .light, relativeTo: .largeTitle))
                    .foregroundStyle(LooreColor.textPrimary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 18)
                    .accessibilityAddTraits(.isHeader)
                if !message.isEmpty {
                    Text(message)
                        .font(LooreFont.sans(16, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(8)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: 480)
                        .padding(.bottom, 22)
                        .accessibilityAddTraits(.updatesFrequently)
                }
                actions()
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 64)
            .frame(maxWidth: 520)
            // Centred vertically like the web's page; scrolls when taller.
            .frame(maxWidth: .infinity, minHeight: proxy.size.height)
        }
        .scrollBounceBehavior(.basedOnSize)
        }
        .loorePageBackground()
    }
}

/// The web's `choiceStyle` button: centred label on `bg-deep`, 1px border.
struct PageChoiceButton: View {
    let title: String
    var color: Color = LooreColor.textSecondary
    let action: () -> Void
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(14.7, .regular))
                .foregroundStyle(color)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
                .padding(.horizontal, 16)
                .background(LooreColor.bgDeep, in: RoundedRectangle(cornerRadius: LooreRadius.control))
                .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
                .contentShape(Rectangle())
                .opacity(isEnabled ? 1 : 0.5)
        }
        .buttonStyle(.plain)
    }
}

/// The web's `linkStyle`: accent text with a faint underline.
struct PageLinkButton: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(14.7, .light))
                .foregroundStyle(LooreColor.accent)
                .padding(.bottom, 2)
                .overlay(alignment: .bottom) { Rectangle().fill(LooreColor.accentGlow).frame(height: 1) }
                .frame(minHeight: 44)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title.replacingOccurrences(of: " →", with: ""))
    }
}
