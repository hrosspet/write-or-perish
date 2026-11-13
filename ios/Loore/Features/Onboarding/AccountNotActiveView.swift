import SwiftUI

/// Payments on (#697; Peter, 2026-11-13): nobody waits for approval, so the
/// only account that is not let in is one the admin blocked. The server
/// refuses it everything else. One calm screen: the plain fact and the
/// address (web `AccountNotActive` on `/alpha-thank-you`), signing out, and
/// "Delete my account" (every account can delete itself). Nothing about paying.
struct AccountNotActiveView: View {
    @Environment(AppState.self) private var app
    @State private var deleteModel: DeleteAccountModel?

    static let message = "This account is not active. Write to info@loore.org."

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 0) {
                    LooreLogo(size: 40)
                        .opacity(0.6)
                        .padding(.bottom, 32)
                    Text(Self.message)
                        .font(LooreFont.serif(28, .light, relativeTo: .title))
                        .foregroundStyle(LooreColor.textPrimary)
                        .multilineTextAlignment(.center)
                        .lineSpacing(4)
                        .padding(.bottom, 40)
                        .accessibilityAddTraits(.isHeader)
                        .accessibilityIdentifier("notActive.message")
                    Button("Sign out") { Task { await app.signOut() } }
                        .buttonStyle(.looreLink)
                        .accessibilityIdentifier("notActive.signOut")
                    if let deleteModel, deleteModel.isAvailable {
                        DeleteAccountSection(model: deleteModel)
                            .frame(maxWidth: 420)
                            .padding(.top, 48)
                    }
                }
                .padding(.horizontal, LooreSpacing.lg)
                .padding(.vertical, 72)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
            .background(LooreColor.bgDeep.ignoresSafeArea())
        }
        .onAppear {
            if deleteModel == nil { deleteModel = DeleteAccountModel(app: app) }
        }
    }
}
