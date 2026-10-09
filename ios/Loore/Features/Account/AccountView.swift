import SwiftUI

/// Account (web `AccountPage`, map E §6): username, email (verification flow,
/// pasted confirmation link), X, plan, the settings that save on change,
/// Voice (M3), "Delete my account" (#269), the app version and, in Debug
/// builds, the hidden environment switcher (long-press the version line).
/// `anchor` is the web hash (`email`, `x`, `model`, `references`, `craft`,
/// `delete-account`).
struct AccountView: View {
    var anchor: String?

    @Environment(AppState.self) private var app
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var model: AccountModel?
    @State private var deleteModel: DeleteAccountModel?
    @State private var showEnvironmentSwitcher = false
    @State private var connectingX = false
    @State private var pastingConfirmation = false

    static var versionText: String {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "Loore \(version) (\(build))"
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Account")
                        .font(LooreFont.serif(22.4, .light, relativeTo: .title))
                        .foregroundStyle(LooreColor.textPrimary)
                        .accessibilityAddTraits(.isHeader)
                        .padding(.bottom, 24)
                    if let model, let user = app.user {
                        AccountForm(model: model, user: user, connectX: { connectingX = true },
                                    pasteConfirmation: { pastingConfirmation = true })
                    }
                    VoiceSettingsSection()
                        .padding(.top, 32)
                    if let deleteModel, deleteModel.isAvailable {
                        DeleteAccountSection(model: deleteModel)
                            .padding(.top, 40)
                            .id("delete-account")
                    }
                    versionLabel
                        .padding(.top, 32)
                }
                .padding(.horizontal, LooreSpacing.gutter)
                .padding(.top, 24)
                .padding(.bottom, 48)
                .looreReadableWidth(600)
            }
            .scrollDismissesKeyboard(.interactively)
            .onAppear {
                if model == nil { model = AccountModel(app: app) }
                if deleteModel == nil { deleteModel = DeleteAccountModel(app: app) }
                if let anchor {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                        withAnimation(reduceMotion ? nil : .default) { proxy.scrollTo(anchor, anchor: .top) }
                    }
                }
            }
        }
        .loorePageBackground()
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $connectingX) {
            CookieWebFlowSheet(title: "Connect X", startURL: app.environment.url(path: APIPath.xConnect)) { url in
                Task { await model?.xLoginReturned(CookieWebFlowSheet.query(url, "x_login")) }
            }
        }
        .sheet(isPresented: $pastingConfirmation) {
            ConfirmEmailPasteSheet()
        }
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

/// The rows, in the web's order.
private struct AccountForm: View {
    @Bindable var model: AccountModel
    let user: CurrentUser
    let connectX: () -> Void
    let pasteConfirmation: () -> Void

    @Environment(AppState.self) private var app
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var preferredModel: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            usernameRow
            emailRow.id("email")
            xRow.id("x")
            AccountRow(label: "Plan") {
                Text(user.plan.rawString.isEmpty ? "Free" : user.plan.rawString.capitalized)
                    .modifier(AccountFieldStyle(plain: true))
            }

            Text("Settings")
                .font(LooreFont.serif(18.4, .light, relativeTo: .title3))
                .foregroundStyle(LooreColor.textPrimary)
                .accessibilityAddTraits(.isHeader)
                .padding(.top, 12)
                .padding(.bottom, 20)

            AccountRow(label: "Default model", helper: "Used for profile generation and LLM responses.") {
                ModelPicker(nodeId: nil, selectedModel: $preferredModel)
                    .onChange(of: preferredModel) { old, new in
                        guard let new, new != user.preferredModel, old != nil || user.preferredModel != nil else { return }
                        Task { await model.saveField("preferred_model", .string(new)) }
                    }
            }
            .id("model")
            .onAppear { if preferredModel == nil { preferredModel = user.preferredModel } }

            selectRow("Default privacy", field: "default_privacy_level", helper: "Default visibility for new entries.",
                      options: [("private", "Private"), ("public", "Public")],
                      value: user.defaultPrivacyLevel.rawString) { .string($0) }
            if user.externalContentAvailable {
                AccountRow(label: "External references") {
                    onOffSelect(field: "external_content_enabled", isOn: user.externalContentEnabled, onTitle: "On (experimental)")
                    externalHelper
                }
                .id("references")
            }
            if user.shareV1Available {
                AccountRow(label: "Public sharing",
                           helper: "The public side of Loore: publish shares to your public page and the Commons, and respond in public threads. Experimental.") {
                    onOffSelect(field: "public_sharing_enabled", isOn: user.publicSharingEnabled, onTitle: "On (experimental)")
                }
            }
            selectRow("Default AI usage", field: "default_ai_usage", helper: "Controls how AI can use your new entries by default.",
                      options: [("none", "None"), ("chat", "Chat"), ("train", "Train")],
                      value: user.defaultAIUsage.rawString) { .string($0) }
                .id(VoiceAIBlock.accountAnchor)
            AccountRow(labelView: AnyView(HStack(spacing: 6) { CraftIcon(size: 13); Text("Craft mode") }),
                       helper: "Shows extra controls for people who want to steer the details: privacy and AI usage on each entry, the auto-generate switch and model picker on threads, audio upload, prompt editing and data export. Off is the simpler Loore. Menu items and controls added by craft mode carry the sliders icon.") {
                onOffSelect(field: "craft_mode", isOn: app.capabilities.craftMode, onTitle: "On")
            }
            .id("craft")
            AccountRow(label: "AI Preferences", helper: "Tone, style, boundaries. Updated automatically during Voice sessions.") {
                Button { app.open(.artifacts(kind: "ai_preferences")) } label: {
                    HStack {
                        Text("How AI interacts with you").foregroundStyle(LooreColor.textPrimary)
                        Spacer()
                        Text("View").foregroundStyle(LooreColor.accent)
                    }
                    .modifier(AccountFieldStyle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("account.aiPreferences")
            }
        }
    }

    // MARK: Rows

    private var usernameRow: some View {
        AccountRow(label: "Username", helper: "Letters, numbers, and underscores only.", message: model.usernameMessage) {
            AdaptiveStack(spacing: 8) {
                TextField("", text: $model.username)
                    .accessibilityLabel("Username")
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.done)
                    .onSubmit { Task { await model.saveUsername() } }
                    .onChange(of: model.username) { _, _ in model.usernameMessage = nil }
                    .modifier(AccountFieldStyle())
                    .accessibilityIdentifier("account.username")
                Button(model.usernameSaving ? "Saving..." : "Save") { Task { await model.saveUsername() } }
                    .buttonStyle(.loorePrimary)
                    .disabled(model.usernameSaving || model.usernameUnchanged)
                    .keyboardShortcut(.return, modifiers: .command)
            }
        }
    }

    private var emailRow: some View {
        AccountRow(label: "Email", message: model.emailMessage) {
            VStack(alignment: .leading, spacing: 4) {
                AdaptiveStack(spacing: 8) {
                    TextField("", text: $model.emailInput,
                              prompt: loorePrompt(user.email ?? "Add an email to sign in with"))
                        .keyboardType(.emailAddress)
                        .textContentType(.emailAddress)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.send)
                        .onSubmit { Task { await model.sendEmailLink(model.emailInput) } }
                        .onChange(of: model.emailInput) { _, _ in model.emailMessage = nil }
                        .modifier(AccountFieldStyle())
                        .accessibilityLabel(user.email.map { "New email address (current: \($0))" } ?? "Email address")
                        .accessibilityIdentifier("account.email")
                    Button(model.emailSaving ? "Sending..." : "Send confirmation link") {
                        Task { await model.sendEmailLink(model.emailInput) }
                    }
                    .buttonStyle(.loorePrimary)
                    .disabled(model.emailSaving || model.emailInput.jsTrimmed.isEmpty)
                    .fixedSize(horizontal: !typeSize.isAccessibilitySize, vertical: true)
                }
                if let notice = model.pendingNotice, let pending = user.pendingEmail {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(notice)
                        FlowLayout(spacing: 4, lineSpacing: 2) {
                            inlineAction(model.resendTitle) { Task { await model.sendEmailLink(pending, resend: true) } }
                            Text("·")
                            inlineAction("Cancel") { Task { await model.cancelPendingEmail() } }
                            Text("·")
                            inlineAction("Paste the link") { pasteConfirmation() }
                                .accessibilityIdentifier("account.pasteConfirmation")
                        }
                    }
                    .helperStyle()
                }
            }
        } footer: {
            FlowLayout(spacing: 4, lineSpacing: 2) {
                Text(model.emailHelper)
                if model.showsRemoveEmail {
                    inlineAction("Remove email") { Task { await model.removeEmail() } }
                }
            }
            .helperStyle()
        }
    }

    private var xRow: some View {
        AccountRow(label: "X", message: model.xMessage) {
            if let connected = model.xConnectedText {
                Text(connected).modifier(AccountFieldStyle(plain: true))
            } else {
                Button("Connect X", action: connectX)
                    .buttonStyle(.loorePrimary)
                    .accessibilityIdentifier("account.connectX")
            }
        } footer: {
            FlowLayout(spacing: 4, lineSpacing: 2) {
                Text(model.xHelper)
                if model.showsDisconnectX {
                    inlineAction("Disconnect X") { Task { await model.disconnectX() } }
                        .disabled(model.xSaving)
                }
            }
            .helperStyle()
        }
    }

    private var externalHelper: some View {
        (Text("Let Loore also search your saved external references (imported tweets, bookmarks and clipped web pages) during conversations, and quote what it finds. Your own archive is always searchable; this switch only adds references. Import bookmarks or set up the Chrome clipper on the ")
         + Text("[Import page](loore-app://import)").foregroundColor(LooreColor.accent)
         + Text(". Experimental."))
            .helperStyle()
            .environment(\.openURL, OpenURLAction { _ in
                app.open(.importData(anchor: nil))
                return .handled
            })
    }

    private func selectRow(_ label: String, field: String, helper: String, options: [(String, String)], value: String,
                           encode: @escaping (String) -> JSONValue) -> some View {
        AccountRow(label: label, helper: helper) {
            SelectField(options: options, selection: Binding(get: { value }, set: { new in
                guard new != value else { return }
                Task { await model.saveField(field, encode(new)) }
            }), disabled: model.isSaving(field))
        }
    }

    private func onOffSelect(field: String, isOn: Bool, onTitle: String) -> some View {
        SelectField(options: [(false, "Off"), (true, onTitle)], selection: Binding(get: { isOn }, set: { new in
            guard new != isOn else { return }
            Task { await model.saveField(field, .bool(new)) }
        }), disabled: model.isSaving(field))
    }

    private func inlineAction(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .foregroundStyle(LooreColor.accent)
            .disabled(model.emailSaving)
    }
}

/// Label, control, optional message and helper (web `rowStyle`, 1.25rem apart).
private struct AccountRow<Content: View, Footer: View>: View {
    var label: String = ""
    var labelView: AnyView?
    var helper: String?
    var message: AccountModel.Message?
    @ViewBuilder var content: () -> Content
    @ViewBuilder var footer: () -> Footer

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Group {
                if let labelView { labelView } else { Text(label) }
            }
            .font(LooreFont.sans(13.6, .regular))
            .foregroundStyle(LooreColor.textSecondary)
            content()
            if let message {
                Text(message.text)
                    .helperStyle(color: message.kind == .error ? LooreColor.accent : LooreColor.textMuted)
                    .accessibilityIdentifier("account.message")
            }
            footer()
            if let helper {
                Text(helper).helperStyle()
            }
        }
        .padding(.bottom, 20)
    }
}

extension AccountRow where Footer == EmptyView {
    init(label: String = "", labelView: AnyView? = nil, helper: String? = nil, message: AccountModel.Message? = nil,
         @ViewBuilder content: @escaping () -> Content) {
        self.init(label: label, labelView: labelView, helper: helper, message: message, content: content, footer: { EmptyView() })
    }
}

/// The web's `inputStyle` (bg-input, 1px border, radius 6, sans .95rem 300).
private struct AccountFieldStyle: ViewModifier {
    var plain = false

    func body(content: Content) -> some View {
        content
            .font(LooreFont.sans(15.2, .light))
            .foregroundStyle(LooreColor.textPrimary)
            .padding(.vertical, 10)
            .padding(.horizontal, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(plain ? Color.clear : LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
    }
}

private extension View {
    /// `helperStyle`: sans .78rem 300, muted.
    func helperStyle(color: Color = LooreColor.textMuted) -> some View {
        self
            .font(LooreFont.sans(12.5, .light))
            .foregroundStyle(color)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
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
