import SwiftUI

/// Account. M4 builds the native page (username, email, X, plan, settings,
/// Voice cue volume). M1 provides the frame: a web fallback, the app version,
/// and in Debug builds the hidden environment switcher (long-press the version).
struct AccountView: View {
    @Environment(AppState.self) private var app
    @State private var showEnvironmentSwitcher = false

    static var versionText: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "Loore \(version) (\(build))"
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                PageHeader(title: "Account")
                if let user = app.user {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("USERNAME")
                            .font(LooreFont.eyebrow)
                            .tracking(1.2)
                            .foregroundStyle(LooreColor.textMuted)
                        Text(user.username)
                            .font(LooreFont.bodyLarge)
                            .foregroundStyle(LooreColor.textPrimary)
                    }
                }
                Text("The full Account page isn't in the app yet. It works on the web in the meantime.")
                    .font(LooreFont.body)
                    .foregroundStyle(LooreColor.textSecondary)
                    .lineSpacing(5)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Open on the web") {
                    app.router.presentedWeb = WebPresentation(url: app.environment.frontendURL(path: "/account"),
                                                              kind: .authenticated, title: "Account")
                }
                .buttonStyle(.loorePrimary)

                versionLabel
                    .padding(.top, 24)
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .looreReadableWidth()
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showEnvironmentSwitcher) {
            #if DEBUG
            EnvironmentSwitcher()
            #endif
        }
    }

    @ViewBuilder private var versionLabel: some View {
        let label = Text(Self.versionText + environmentSuffix)
            .font(LooreFont.meta)
            .foregroundStyle(LooreColor.textMuted)
            .accessibilityIdentifier("account.version")
        #if DEBUG
        label
            .onLongPressGesture(minimumDuration: 0.6) { showEnvironmentSwitcher = true }
            .accessibilityAction(named: "Switch environment") { showEnvironmentSwitcher = true }
        #else
        label
        #endif
    }

    private var environmentSuffix: String {
        #if DEBUG
        return " · \(app.environment.displayName)"
        #else
        return ""
        #endif
    }
}

#if DEBUG
/// Debug-only backend switcher. Switching signs out (design doc §2).
private struct EnvironmentSwitcher: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var switching = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(AppEnvironment.allCases) { env in
                        Button {
                            guard env != app.environment else { return }
                            switching = true
                            Task {
                                await app.switchEnvironment(to: env)
                                switching = false
                                dismiss()
                            }
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(env.displayName).foregroundStyle(LooreColor.textPrimary)
                                    Text(env.backendOrigin.absoluteString)
                                        .font(LooreFont.meta)
                                        .foregroundStyle(LooreColor.textMuted)
                                }
                                Spacer()
                                if env == app.environment {
                                    Image(systemName: "checkmark").foregroundStyle(LooreColor.accent)
                                }
                            }
                        }
                        .disabled(switching)
                    }
                } footer: {
                    Text("Switching signs you out of the current backend.")
                }
            }
            .navigationTitle("Environment")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            }
        }
        .presentationDetents([.medium])
    }
}
#endif
