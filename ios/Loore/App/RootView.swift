import SwiftUI

/// Chooses the top-level screen from the auth phase and runs the gating
/// sequence after sign-in (design doc §4.5, map B §3.6):
/// signed out → sign-in; not approved → waitlist; terms out of date → the
/// blocking Terms screen over whichever of those is underneath; then the app,
/// with the Updates sheet once per launch.
struct RootView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        @Bindable var router = app.router
        ZStack {
            LooreColor.bgDeep.ignoresSafeArea()
            content
            if app.phase == .signedIn, app.needsTerms {
                TermsView()
                    .transition(.opacity)
                    .zIndex(10)
            }
        }
        .overlay(alignment: .bottom) {
            BottomNotices()
        }
        .sheet(item: $router.presentedWeb) { presentation in
            WebPresentationView(presentation: presentation, cookies: app.api.backendCookies())
        }
        .sheet(isPresented: updatesBinding) {
            if let payload = app.pendingUpdates {
                UpdatesSheet(payload: payload)
            }
        }
        .animation(LooreMotion.quick, value: app.phase)
        .animation(LooreMotion.quick, value: app.needsTerms)
        .task { await app.start() }
    }

    @ViewBuilder private var content: some View {
        switch app.phase {
        case .launching:
            LaunchView()
        case .signedOut:
            SignInView()
        case .unreachable(let message):
            UnreachableView(message: message)
        case .signedIn:
            if app.isApproved {
                MainTabView()
            } else {
                WaitlistView()
            }
        }
    }

    /// The Updates sheet shows only when nothing blocks it (never over Terms).
    private var updatesBinding: Binding<Bool> {
        Binding(
            get: { app.pendingUpdates != nil && app.phase == .signedIn && !app.needsTerms && app.isApproved },
            set: { if !$0 { app.pendingUpdates = nil } }
        )
    }
}

/// Toasts and the spend-cap banner, bottom centre above the tab bar and, while it
/// shows, above the mini-player (web `--floating-player-offset`).
private struct BottomNotices: View {
    @Environment(AppState.self) private var app

    private var bottomOffset: CGFloat {
        guard app.phase == .signedIn && app.isApproved else { return 24 }
        return 64 + (app.audio.showsMiniPlayer ? app.audio.miniPlayerHeight : 0)
    }

    var body: some View {
        VStack(spacing: 8) {
            ToastStack(center: app.toasts)
            if let message = app.spendCapBannerMessage {
                SpendCapBanner(message: message) { app.spendCapBannerMessage = nil }
            }
        }
        .padding(.bottom, bottomOffset)
        .animation(LooreMotion.quick, value: app.spendCapBannerMessage)
    }
}

/// Shown while cookies are restored and the user loads.
struct LaunchView: View {
    var body: some View {
        VStack(spacing: 18) {
            LooreLogo(size: 40)
                .opacity(0.7)
            Text("LOORE")
                .font(LooreFont.wordmark)
                .tracking(6)
                .foregroundStyle(LooreColor.textMuted)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .loorePageBackground()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Loore is loading")
    }
}

/// Cookies exist but the backend could not be reached.
struct UnreachableView: View {
    let message: String
    @Environment(AppState.self) private var app
    @State private var retrying = false

    var body: some View {
        VStack(spacing: 20) {
            LooreLogo(size: 32).opacity(0.6)
            Text(message)
                .font(LooreFont.bodyLarge)
                .foregroundStyle(LooreColor.textSecondary)
                .multilineTextAlignment(.center)
            Button(retrying ? "Trying..." : "Try again") {
                Task {
                    retrying = true
                    await app.loadUser()
                    retrying = false
                }
            }
            .buttonStyle(.loorePrimary)
            .disabled(retrying)
            Button("Sign out") {
                Task { await app.signOut() }
            }
            .buttonStyle(.looreQuiet)
        }
        .padding(LooreSpacing.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .loorePageBackground()
    }
}
