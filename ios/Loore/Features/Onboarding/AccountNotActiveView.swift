import SwiftUI

/// Payments on (#697; Peter, 2026-11-13): nobody waits for approval, so the
/// only account that is not let in is one the admin blocked. The server
/// refuses it everything else. One calm screen: the plain fact and the
/// address (web `AccountNotActive` on `/alpha-thank-you`), "Download your
/// data" (Peter, 2026-11-13: "yes, Ben should be able to export"; the More
/// menu's export), signing out, and "Delete my account" (every account can
/// delete itself). Nothing about paying: the web has the way to cancel a
/// subscription that still runs.
struct AccountNotActiveView: View {
    @Environment(AppState.self) private var app
    @State private var deleteModel: DeleteAccountModel?
    @State private var exporting = false
    @State private var exportFile: ExportFile?

    static let message = "This account is not active. Write to info@loore.org."
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
                        .accessibilityIdentifier("notActive.message")
                    Button(exporting ? "Preparing your data…" : Self.downloadLabel) { download() }
                        .buttonStyle(.looreLink)
                        .disabled(exporting)
                        .padding(.bottom, 20)
                        .accessibilityIdentifier("notActive.export")
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
        .sheet(item: $exportFile) { file in
            // The file goes when the sheet closes, shared or not.
            ShareSheet(items: [file.url]) {
                try? FileManager.default.removeItem(at: file.url)
            }
        }
    }

    /// `GET /api/export/threads` to a temporary file, then the share sheet
    /// (as the More menu's "Export data").
    private func download() {
        guard !exporting else { return }
        exporting = true
        Task {
            defer { exporting = false }
            do {
                var request = APIRequest(.get, APIPath.exportThreads)
                request.accept = "text/plain,*/*"
                request.timeout = 120
                let (tempURL, _) = try await app.api.download(request)
                let day = LooreDateFormat.ymd(Date()).replacingOccurrences(of: "/", with: "-")
                let target = FileManager.default.temporaryDirectory
                    .appendingPathComponent("loore-export-\(day).txt")
                try? FileManager.default.removeItem(at: target)
                try FileManager.default.moveItem(at: tempURL, to: target)
                exportFile = ExportFile(url: target)
            } catch {
                app.toasts.show("The download couldn't start. Please try again.")
            }
        }
    }
}
