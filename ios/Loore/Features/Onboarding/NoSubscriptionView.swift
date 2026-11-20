import SwiftUI

/// Payments on (#438; Peter, 2026-11-12): an account that never had a
/// subscription is not let in, and the server refuses it every other route.
/// The app has one calm screen for it: what the state is, signing out,
/// downloading its data (Peter, 2026-11-13: "yes, Ben should be able to
/// export"; the More menu's export, `DataExport`) and "Delete my account"
/// (every account can delete itself). No price, no link, no button and no
/// hint where to subscribe. When a subscription starts, the next refresh
/// (foreground, or a refused call) lets the account in.
struct NoSubscriptionView: View {
    @Environment(AppState.self) private var app
    @State private var deleteModel: DeleteAccountModel?
    @State private var exporting = false
    @State private var exportFile: ExportFile?

    static let message = "Your account has no active subscription yet."
    static let downloadLabel = "Download your data"

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
                    // "Start from your tweets?" (#697): with payments on, a new
                    // account comes here, not to the waitlist that asked it before.
                    PrefillConsentCard(eyebrow: "Already on X?")
                        .padding(.bottom, 40)
                    Button("Sign out") { Task { await app.signOut() } }
                        .buttonStyle(.looreLink)
                        .accessibilityIdentifier("noSubscription.signOut")
                    Button(exporting ? "Preparing your data…" : Self.downloadLabel) { download() }
                        .buttonStyle(.looreLink)
                        .disabled(exporting)
                        .padding(.top, 20)
                        .accessibilityIdentifier("noSubscription.export")
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
        .sheet(item: $exportFile) { file in
            // The file goes when the sheet closes, shared or not.
            ShareSheet(items: [file.url]) {
                try? FileManager.default.removeItem(at: file.url)
            }
        }
    }

    private func download() {
        guard !exporting else { return }
        exporting = true
        Task {
            defer { exporting = false }
            do {
                exportFile = ExportFile(url: try await DataExport.download(app.api))
            } catch {
                app.toasts.show(DataExport.failedMessage)
            }
        }
    }
}
