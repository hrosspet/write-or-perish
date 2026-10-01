import SwiftUI
import WebKit

/// A signed-in web flow that ends on a frontend page (design doc §6: Connect X
/// and the X bookmarks connect). The web view carries the app's cookies; when
/// it is about to open a frontend page, the navigation is cancelled, the sheet
/// closes and `onLanding` gets that URL (e.g. `/account?x_login=linked#x`).
struct CookieWebFlowSheet: View {
    let title: String
    let startURL: URL
    let onLanding: (URL) -> Void

    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var isLoading = true
    @State private var finished = false

    var body: some View {
        NavigationStack {
            AuthenticatedWebView(url: startURL, cookies: app.api.backendCookies(), decide: decide, isLoading: $isLoading)
                .ignoresSafeArea(edges: .bottom)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { dismiss() }
                    }
                    ToolbarItem(placement: .principal) {
                        HStack(spacing: 8) {
                            Text(title)
                                .font(LooreFont.sans(15, .regular))
                                .foregroundStyle(LooreColor.textPrimary)
                            if isLoading { ProgressView().controlSize(.small) }
                        }
                    }
                }
        }
    }

    private func decide(_ url: URL) -> WKNavigationActionPolicy {
        guard WebLoginRouting.isFrontendLanding(url, environment: app.environment) else { return .allow }
        if !finished {
            finished = true
            DispatchQueue.main.async {
                onLanding(url)
                dismiss()
            }
        }
        return .cancel
    }

    /// A query value of a landing URL (`x_login`, `x_connect`).
    static func query(_ url: URL, _ name: String) -> String? {
        URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == name }?.value
    }
}
