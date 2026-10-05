import SwiftUI

/// The waitlist screen for signed-in users who are not approved yet
/// (web `AlphaThankYouPage`, route `/alpha-thank-you`). Every other API is 403
/// for them, so this screen is all they get, plus About and Logout in the ⋯ menu.
struct WaitlistView: View {
    @Environment(AppState.self) private var app

    @State private var email = ""
    @State private var loading = false
    @State private var error: String?
    /// "Use a different address": the form again over a pending address.
    @State private var editing = false
    @State private var resent = false
    @State private var pastingConfirmation = false
    @FocusState private var emailFocused: Bool

    /// The only hint about an address that already belongs to another account
    /// (the server never says so, to prevent enumeration).
    static let pendingHint = "Nothing after a few minutes? Check the spelling. An address that already signs in to Loore can't be added here."

    private var user: CurrentUser? { app.user }
    private var hasEmail: Bool { !(user?.email ?? "").trimmingCharacters(in: .whitespaces).isEmpty }
    private var pending: String? { user?.pendingEmail }
    private var showForm: Bool { user != nil && (editing || (!hasEmail && pending == nil)) }
    private var showPending: Bool { pending != nil && !editing }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 0) {
                    LooreLogo(size: 40)
                        .opacity(0.6)
                        .padding(.bottom, 32)
                        .looreFadeIn()
                    Text("You're part of this now.")
                        .font(LooreFont.serif(36, .light, relativeTo: .largeTitle))
                        .foregroundStyle(LooreColor.textPrimary)
                        .multilineTextAlignment(.center)
                        .padding(.bottom, 24)
                        .looreFadeIn(delay: 0.1)
                        .accessibilityAddTraits(.isHeader)
                    Text("Thank you for signing up for the Loore Alpha. We're letting people in gradually — to keep things intimate and to give each person the attention they deserve as they begin.")
                        .font(LooreFont.sans(16.8, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .multilineTextAlignment(.center)
                        .lineSpacing(8)
                        .frame(maxWidth: 480)
                        .padding(.bottom, 48)
                        .looreFadeIn(delay: 0.2)

                    if showForm {
                        emailFormCard.padding(.bottom, 32).looreFadeIn(delay: 0.25)
                    }
                    if showPending {
                        pendingCard.padding(.bottom, 32).looreFadeIn(delay: 0.25)
                    }
                    PrefillConsentCard()
                        .padding(.bottom, 32)
                        .looreFadeIn(delay: 0.26)
                    nextStepsCard
                        .padding(.bottom, 48)
                        .looreFadeIn(delay: 0.28)
                    Text("In the meantime — you might notice yourself already paying closer attention to the story you're living. That's the process beginning.")
                        .font(LooreFont.serif(19.2, .light, italic: true))
                        .foregroundStyle(LooreColor.textMuted)
                        .multilineTextAlignment(.center)
                        .lineSpacing(4)
                        .frame(maxWidth: 420)
                        .padding(.bottom, 40)
                        .looreFadeIn(delay: 0.36)
                    VStack(spacing: 12) {
                        Text("You can learn more about where Loore is headed.")
                            .font(LooreFont.body)
                            .foregroundStyle(LooreColor.textSecondary)
                            .multilineTextAlignment(.center)
                        Button("Read the vision →") { app.open(.webPage(path: "/vision")) }
                            .accessibilityLabel("Read the vision")
                            .buttonStyle(.looreLink)
                    }
                    .looreFadeIn(delay: 0.42)
                }
                .padding(.horizontal, LooreSpacing.lg)
                .padding(.vertical, 56)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
            .scrollDismissesKeyboard(.interactively)
            .background {
                ZStack {
                    LooreColor.bgDeep
                    RadialGradient(colors: [LooreColor.accentGlow.opacity(0.4), .clear],
                                   center: UnitPoint(x: 0.5, y: 0.3), startRadius: 0, endRadius: 250)
                }
                .ignoresSafeArea()
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    AboutAndLogoutMenu()
                }
            }
            .toolbarBackground(.hidden, for: .navigationBar)
        }
        .onAppear { email = user?.email ?? "" }
    }

    // MARK: Cards

    private var emailFormCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            Eyebrow(text: "Get notified").padding(.bottom, 16)
            Text("Leave your email and we'll let you know when your spot opens up.")
                .font(LooreFont.body)
                .foregroundStyle(LooreColor.textSecondary)
                .lineSpacing(5)
                .padding(.bottom, 19)
            TextField("", text: $email, prompt: loorePrompt("your@email.com"))
                .textFieldStyle(LooreTextFieldStyle(isFocused: emailFocused))
                .keyboardType(.emailAddress)
                .textContentType(.emailAddress)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.send)
                .focused($emailFocused)
                .onSubmit { send(email) }
                .padding(.bottom, 16)
                .accessibilityIdentifier("waitlist.email")
            if let error {
                Text(error)
                    .font(LooreFont.sans(14, .light))
                    .foregroundStyle(LooreColor.accent)
                    .padding(.bottom, 13)
            }
            HStack(spacing: 19) {
                Button(loading ? "Submitting..." : "Submit") { send(email) }
                    .buttonStyle(.loorePrimary)
                    .disabled(loading)
                    .accessibilityIdentifier("waitlist.submit")
                if editing, let pending {
                    Button("Keep \(pending)") {
                        error = nil
                        editing = false
                    }
                    .buttonStyle(.looreLink)
                    .disabled(loading)
                }
            }
        }
        .looreCard(padding: EdgeInsets(top: 32, leading: 30, bottom: 32, trailing: 30))
        .frame(maxWidth: 460)
    }

    private var pendingCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            Group {
                if user?.pendingEmailExpired == true {
                    Text("The confirmation link we sent to ")
                    + Text(pending ?? "").foregroundColor(LooreColor.textPrimary)
                    + Text(" has expired. Until the address is confirmed we have no way to reach you.")
                } else {
                    Text("We sent a confirmation link to ")
                    + Text(pending ?? "").foregroundColor(LooreColor.textPrimary)
                    + Text(". Open it to finish — until then we have no way to reach you.")
                }
            }
            .font(LooreFont.body)
            .foregroundStyle(LooreColor.textSecondary)
            .lineSpacing(5)
            Text((resent ? "New link sent. The earlier one no longer works. " : "") + Self.pendingHint)
                .font(LooreFont.body)
                .foregroundStyle(LooreColor.textMuted)
                .lineSpacing(5)
                .padding(.top, 10)
            if let error {
                Text(error)
                    .font(LooreFont.sans(14, .light))
                    .foregroundStyle(LooreColor.accent)
                    .padding(.top, 10)
            }
            HStack(spacing: 19) {
                Button(loading ? "Sending..." : (user?.pendingEmailExpired == true ? "Send a new link" : "Send it again")) {
                    if let pending { send(pending, isResend: true) }
                }
                .buttonStyle(.looreLink)
                Button("Use a different address") {
                    email = pending ?? ""
                    error = nil
                    resent = false
                    editing = true
                }
                .buttonStyle(.looreLink)
            }
            .disabled(loading)
            .padding(.top, 14)
            // The app's own step (design doc §4.6): the mailed link only counts in this session.
            Button("I have the link: paste it") { pastingConfirmation = true }
                .buttonStyle(.looreLink)
                .padding(.top, 8)
                .accessibilityIdentifier("waitlist.pasteConfirmation")
                .sheet(isPresented: $pastingConfirmation) { ConfirmEmailPasteSheet() }
        }
        .looreCard(padding: EdgeInsets(top: 22, leading: 30, bottom: 22, trailing: 30))
        .frame(maxWidth: 460)
    }

    private var nextStepsCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            Eyebrow(text: "What happens next").padding(.bottom, 19)
            VStack(alignment: .leading, spacing: 19) {
                step("01", "We'll send you an email when your spot opens up. It shouldn't be long.")
                step("02", "You'll get a warm welcome with everything you need to begin.")
                step("03", "Start journaling — by text or voice — and let your lore unfold.")
            }
        }
        .looreCard(padding: EdgeInsets(top: 32, leading: 30, bottom: 32, trailing: 30))
        .frame(maxWidth: 460)
    }

    private func step(_ number: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 16) {
            Text(number)
                .font(LooreFont.serif(17.6, .light))
                .foregroundStyle(LooreColor.accent.opacity(0.5))
            Text(text)
                .font(LooreFont.body)
                .foregroundStyle(LooreColor.textSecondary)
                .lineSpacing(5)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Actions

    /// `POST /api/dashboard/email`, one request at a time: each mails a link
    /// that voids the one before.
    private func send(_ address: String, isResend: Bool = false) {
        guard !loading else { return }
        loading = true
        error = nil
        resent = false
        Task {
            do {
                let state: EmailState = try await app.api.post(APIPath.email, json: ["email": .string(address)])
                app.applyEmailState(state)
                editing = false
                resent = isResend
            } catch let apiError as APIError {
                error = apiError.userMessage(fallback: "Error sending the confirmation link. Please try again.")
            } catch {
                self.error = "Error sending the confirmation link. Please try again."
            }
            loading = false
        }
    }
}

/// "Start from what you've already written?" (web `PrefillConsentCard`): shown to
/// X-login users who have not answered. Two equal-weight buttons, nothing preselected.
struct PrefillConsentCard: View {
    var eyebrow = "While you wait"
    @Environment(AppState.self) private var app
    @State private var busy: PrefillConsent?
    @State private var answered: PrefillConsent?
    @State private var error: String?

    var body: some View {
        if let user = app.user, answered != nil || (user.twitterLogin && user.prefillConsent == nil) {
            VStack(alignment: .leading, spacing: 0) {
                Eyebrow(text: eyebrow).padding(.bottom, 16)
                if let answered {
                    Text(answered == .yes
                         ? "Noted. We'll seed your Loore from your public tweets and have first drafts of your artifacts — like your profile and intentions — waiting when you come in."
                         : "Noted. You'll start from a blank page — you can always seed from your tweets later, from Settings.")
                        .font(LooreFont.body)
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(5)
                } else {
                    Text("Start from what you've already written?")
                        .font(LooreFont.serif(21.6, .light))
                        .foregroundStyle(LooreColor.textPrimary)
                        .padding(.bottom, 13)
                    (Text("With your okay, we'll bring in your ")
                     + Text("public tweets").foregroundColor(LooreColor.textPrimary)
                     + Text(" as @\(user.username), keep them private in your Loore, and write first drafts of your artifacts from them — like your profile and your intentions — so you don't begin from a blank page. Only your own public posts; nothing is published, and every draft is yours to edit or delete."))
                        .font(LooreFont.body)
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(5)
                        .padding(.bottom, 22)
                    HStack(spacing: 11) {
                        Button(busy == .yes ? "Saving…" : "Yes, seed from my tweets") { answer(.yes) }
                            .buttonStyle(LooreButtonStyle(kind: .primary, fullWidth: true))
                        Button(busy == .no ? "Saving…" : "Not now") { answer(.no) }
                            .buttonStyle(LooreButtonStyle(kind: .outline, fullWidth: true))
                    }
                    .disabled(busy != nil)
                    if let error {
                        Text(error)
                            .font(LooreFont.sans(13.6, .light))
                            .foregroundStyle(LooreColor.error)
                            .padding(.top, 13)
                    }
                    Text("You can change your mind later in Settings.")
                        .font(LooreFont.sans(12.5, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .padding(.top, 16)
                }
            }
            .looreCard(padding: EdgeInsets(top: 32, leading: 30, bottom: 32, trailing: 30))
            .frame(maxWidth: 460)
        }
    }

    private func answer(_ value: PrefillConsent) {
        busy = value
        error = nil
        Task {
            do {
                try await app.updateUser(["prefill_consent": .string(value.rawString)])
                answered = value
            } catch {
                self.error = "Couldn't save your answer. Please try again."
            }
            busy = nil
        }
    }
}

/// The ⋯ menu an unapproved user gets on the web: About links and Logout.
struct AboutAndLogoutMenu: View {
    @Environment(AppState.self) private var app

    var body: some View {
        Menu {
            Section("About") {
                Button("Why Loore") { app.open(.webPage(path: "/why-loore")) }
                Button("Vision") { app.open(.webPage(path: "/vision")) }
                Button("How To") { app.open(.webPage(path: "/how-to")) }
            }
            Button("Logout") { Task { await app.signOut() } }
        } label: {
            Image(systemName: "ellipsis")
                .font(.system(size: 17, weight: .regular))
                .foregroundStyle(LooreColor.textMuted)
                .frame(width: 36, height: 36)
                .contentShape(Rectangle())
        }
        .accessibilityLabel("Menu")
        .accessibilityIdentifier("waitlist.menu")
    }
}
