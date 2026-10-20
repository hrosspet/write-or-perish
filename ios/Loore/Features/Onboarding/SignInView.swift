import SwiftUI

/// Native sign-in (design doc §4; web `LoginPage.js`). Email magic link, with the
/// link pasted back into the app, and "Sign in with X" in a web view.
struct SignInView: View {
    @Environment(AppState.self) private var app

    private enum Step { case start, email, sent, paste }

    @State private var step: Step = .start
    @State private var email = ""
    @State private var sending = false
    @State private var emailError: String?
    @State private var pasted = ""
    @State private var verifying = false
    @State private var linkError: String?
    @State private var showXLogin = false
    @State private var xError: String?
    @FocusState private var focused: Field?

    private enum Field { case email, link }

    var body: some View {
        GeometryReader { proxy in
        ScrollView {
            VStack(spacing: 0) {
                Text("LOORE")
                    .font(LooreFont.wordmark)
                    .tracking(6.2)
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.bottom, 40)
                    .looreFadeIn(delay: 0.1, offset: 16)
                    .accessibilityAddTraits(.isHeader)

                card
                    .looreFadeIn(delay: 0.25, offset: 16)

                aboutLinks
                    .padding(.top, 28)
                    .looreFadeIn(delay: 0.4, offset: 16)
            }
            .padding(.horizontal, LooreSpacing.lg)
            .padding(.vertical, 40)
            .frame(maxWidth: 440)
            // Centred vertically like the web's login page; scrolls when taller.
            .frame(maxWidth: .infinity, minHeight: proxy.size.height)
        }
        .scrollDismissesKeyboard(.interactively)
        }
        .background(SignInBackdrop())
        .sheet(isPresented: $showXLogin) {
            WebLoginSheet(environment: app.environment) { result in
                handleXLogin(result)
            }
        }
    }

    /// The web NavBar's "About" dropdown for signed-out visitors (Why Loore, Vision,
    /// How To), opened as web pages.
    private var aboutLinks: some View {
        HStack(spacing: 20) {
            Button("Why Loore") { app.open(.webPage(path: "/why-loore")) }
            Button("Vision") { app.open(.webPage(path: "/vision")) }
            Button("How To") { app.open(.webPage(path: "/how-to")) }
        }
        .buttonStyle(.plain)
        .font(LooreFont.sans(13.6, .light))
        .foregroundStyle(LooreColor.textMuted)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("signIn.about")
    }

    private var card: some View {
        VStack(spacing: 0) {
            Text("Welcome back")
                .font(LooreFont.serif(28.8, .light, relativeTo: .largeTitle))
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.bottom, 8)
            Text("Sign in to continue your lore")
                .font(LooreFont.sans(15.2, .light))
                .foregroundStyle(LooreColor.textMuted)
                .multilineTextAlignment(.center)
                .padding(.bottom, 32)

            if let xError {
                errorText(xError).padding(.bottom, 16)
            }

            switch step {
            case .start, .email:
                xButton
                Text("Sign in with X makes a new account unless your X is already connected to one. To add X to an email account, sign in with email, then use Connect X under Account.")
                    .font(LooreFont.sans(12.5, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .multilineTextAlignment(.center)
                    .lineSpacing(3)
                    .padding(.bottom, 4)
                if step == .start {
                    divider
                    Button {
                        withAnimation(LooreMotion.quick) { step = .email }
                        focused = .email
                    } label: {
                        Label {
                            Text("Sign in with Email")
                        } icon: {
                            Image(systemName: "envelope.fill")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(SignInButtonStyle(emphasis: .secondary))
                    .accessibilityIdentifier("signin.email")

                    Button("I already have a sign-in link") {
                        withAnimation(LooreMotion.quick) { step = .paste }
                    }
                    .buttonStyle(.looreQuiet)
                    .padding(.top, 14)
                    .accessibilityIdentifier("signin.haveLink")
                } else {
                    emailForm
                }
            case .sent:
                sentConfirmation
                pasteSection
            case .paste:
                pasteSection
                Button("Send me a new link") {
                    withAnimation(LooreMotion.quick) { step = .email }
                }
                .buttonStyle(.looreQuiet)
                .padding(.top, 14)
            }
        }
        .padding(EdgeInsets(top: 40, leading: 28, bottom: 32, trailing: 28))
        .frame(maxWidth: .infinity)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.large))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.border))
        .shadow(color: LooreColor.shadow.opacity(0.3), radius: 30, y: 16)
    }

    // MARK: Pieces

    private var xButton: some View {
        Button {
            xError = nil
            showXLogin = true
        } label: {
            HStack(spacing: 10) {
                XLogo(size: 18)
                Text("Sign in with X")
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(SignInButtonStyle(emphasis: .primary))
        .padding(.bottom, 12)
        .accessibilityIdentifier("signin.x")
    }

    private var divider: some View {
        HStack(spacing: 12) {
            HairlineDivider()
            Text("or")
                .font(LooreFont.sans(12, .regular))
                .tracking(1)
                .foregroundStyle(LooreColor.textMuted)
            HairlineDivider()
        }
        .padding(.vertical, 16)
    }

    private var emailForm: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("EMAIL ADDRESS")
                .font(LooreFont.sans(11.2, .regular))
                .tracking(1.7)
                .foregroundStyle(LooreColor.textMuted)
                .padding(.bottom, 8)
            TextField("", text: $email, prompt: loorePrompt("you@example.com"))
                .textFieldStyle(LooreTextFieldStyle(isFocused: focused == .email))
                .keyboardType(.emailAddress)
                .textContentType(.emailAddress)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.send)
                .focused($focused, equals: .email)
                .onSubmit(sendLink)
                .padding(.bottom, 12)
                .accessibilityIdentifier("signin.emailField")
            if let emailError {
                errorText(emailError).padding(.bottom, 12)
            }
            Button(sending ? "Sending..." : "Send Sign-in Link", action: sendLink)
                .buttonStyle(SignInButtonStyle(emphasis: .submit))
                .disabled(sending || email.trimmingCharacters(in: .whitespaces).isEmpty)
                .accessibilityIdentifier("signin.send")
        }
        .padding(.top, 16)
    }

    private var sentConfirmation: some View {
        VStack(spacing: 8) {
            Text("Check your inbox")
                .font(LooreFont.serif(19.2, .light))
                .foregroundStyle(LooreColor.accent)
            (Text("We sent a sign-in link to ")
             + Text(email.trimmingCharacters(in: .whitespaces)).foregroundColor(LooreColor.textPrimary)
             + Text(".\nIt expires in 15 minutes."))
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textMuted)
                .multilineTextAlignment(.center)
                .lineSpacing(4)
        }
        .padding(.bottom, 24)
    }

    private var pasteSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("PASTE THE LINK FROM THE EMAIL")
                .font(LooreFont.sans(11.2, .regular))
                .tracking(1.7)
                .foregroundStyle(LooreColor.textMuted)
                .padding(.bottom, 8)
            Text("In the email, touch and hold the sign-in button, choose Copy Link, then paste it here.")
                .font(LooreFont.sans(13.6, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 12)
            HStack(spacing: 8) {
                TextField("", text: $pasted, prompt: loorePrompt("Sign-in link"), axis: .vertical)
                    .lineLimit(1...3)
                    .textFieldStyle(LooreTextFieldStyle(isFocused: focused == .link))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                    .focused($focused, equals: .link)
                    .accessibilityIdentifier("signin.linkField")
                PasteButton(payloadType: String.self) { strings in
                    guard let first = strings.first else { return }
                    Task { @MainActor in
                        pasted = first
                        verify()
                    }
                }
                .labelStyle(.iconOnly)
                .buttonBorderShape(.roundedRectangle(radius: LooreRadius.small))
                .tint(LooreColor.accent)
                .accessibilityIdentifier("signin.paste")
            }
            .padding(.bottom, 12)
            if let linkError {
                errorText(linkError).padding(.bottom, 12)
            }
            Button(verifying ? "Signing in..." : "Sign in", action: verify)
                .buttonStyle(SignInButtonStyle(emphasis: .submit))
                .disabled(verifying || pasted.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("signin.submitLink")
            Text("A link that creates a new account works once. If you already opened it in Safari, send yourself a new one.")
                .font(LooreFont.sans(12, .light))
                .foregroundStyle(LooreColor.textMuted)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 14)
        }
    }

    private func errorText(_ text: String) -> some View {
        Text(text)
            .font(LooreFont.sans(13.6, .light))
            .foregroundStyle(LooreColor.accent)
            .lineSpacing(3)
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityIdentifier("signin.error")
    }

    // MARK: Actions

    private func sendLink() {
        let address = email.trimmingCharacters(in: .whitespaces)
        guard !sending, !address.isEmpty else { return }
        sending = true
        emailError = nil
        Task {
            do {
                _ = try await app.auth.sendMagicLink(email: address)
                withAnimation(LooreMotion.quick) { step = .sent }
            } catch {
                emailError = (error as? AuthFailure)?.message ?? "Something went wrong. Please try again."
            }
            sending = false
        }
    }

    private func verify() {
        let text = pasted
        guard !verifying, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        verifying = true
        linkError = nil
        focused = nil
        Task {
            do {
                let landing = try await app.auth.verifyMagicLink(pasted: text)
                await app.signInCompleted(landing: landing)
            } catch {
                linkError = (error as? AuthFailure)?.message ?? MagicLink.genericFailure
            }
            verifying = false
        }
    }

    private func handleXLogin(_ result: Result<WebLoginResult, AuthFailure>) {
        switch result {
        case .success(let login):
            do {
                try app.auth.adoptWebLoginCookies(login.cookies)
                let landing = WebLoginRouting.appLanding(login.landing)
                Task { await app.signInCompleted(landing: landing) }
            } catch {
                xError = (error as? AuthFailure)?.message
            }
        case .failure(let failure):
            xError = failure.message
        }
    }
}

/// The login page's backdrop: two soft radial gradients on `bg-deep`.
private struct SignInBackdrop: View {
    var body: some View {
        GeometryReader { proxy in
            ZStack {
                LooreColor.bgDeep
                RadialGradient(colors: [LooreColor.gradientOverlay1, .clear],
                               center: UnitPoint(x: 0.5, y: 0), startRadius: 0,
                               endRadius: proxy.size.height * 0.6)
                RadialGradient(colors: [LooreColor.gradientOverlay2, .clear],
                               center: UnitPoint(x: 0.8, y: 0.2), startRadius: 0,
                               endRadius: proxy.size.width * 0.5)
            }
        }
        .ignoresSafeArea()
    }
}

/// The login page's buttons (`.loore-login-btn-*`): full width, radius 8.
private struct SignInButtonStyle: ButtonStyle {
    enum Emphasis { case primary, secondary, submit }
    let emphasis: Emphasis
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(LooreFont.sans(15, emphasis == .secondary ? .light : .regular))
            .foregroundStyle(foreground(configuration.isPressed))
            .padding(.vertical, 13)
            .padding(.horizontal, 20)
            .frame(maxWidth: .infinity)
            .background(configuration.isPressed ? LooreColor.accentSubtle : Color.clear,
                        in: RoundedRectangle(cornerRadius: LooreRadius.small))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.small)
                .strokeBorder(emphasis == .submit ? LooreColor.accent : LooreColor.border, lineWidth: 1))
            .contentShape(RoundedRectangle(cornerRadius: LooreRadius.small))
            .opacity(isEnabled ? 1 : 0.5)
    }

    private func foreground(_ pressed: Bool) -> Color {
        switch emphasis {
        case .primary: return LooreColor.textPrimary
        case .secondary: return pressed ? LooreColor.textPrimary : LooreColor.textSecondary
        case .submit: return LooreColor.accent
        }
    }
}
