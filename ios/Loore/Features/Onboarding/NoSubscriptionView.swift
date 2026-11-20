import SwiftUI

/// Payments on (#438; Peter, 2026-11-12): an account that never had a
/// subscription is not let in, and the server refuses it every other route.
/// The app has one calm screen for it: what the state is, signing out, and
/// "Delete my account" (every account can delete itself). No price, no
/// link, no button and no hint where to subscribe. When a subscription
/// starts, the next refresh (foreground, or a refused call) lets the
/// account in.
struct NoSubscriptionView: View {
    @Environment(AppState.self) private var app
    @State private var deleteModel: DeleteAccountModel?

    static let message = "Your account has no active subscription yet."

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
                        .accessibilityIdentifier("noSubscription.message")
                    Button("Sign out") { Task { await app.signOut() } }
                        .buttonStyle(.looreLink)
                        .accessibilityIdentifier("noSubscription.signOut")
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
