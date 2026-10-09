import SwiftUI

/// Home (web `HomePage`, route `/`): greeting, "What's on your mind?", and the
/// cards. A user who gleans (#436) gets two cards by purpose, Reflect and
/// Glean, each with Voice and Text, and Share (flag) in its own row below; a
/// user without Glean keeps the mode cards: Voice, Text, Share (flag). The
/// admin-only Read card is gone, as on the web (#474).
struct HomeView: View {
    @Environment(AppState.self) private var app
    @State private var greeting = LooreDateFormat.greeting()

    var body: some View {
        let gleans = app.capabilities.gleanEnabled
        ScrollView {
            VStack(spacing: 0) {
                Spacer(minLength: 24)
                Text(greeting)
                    .font(LooreFont.serif(20, .light, relativeTo: .title3))
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.bottom, 8)
                    .looreFadeIn(delay: 0, offset: 12)
                Text("What's on your mind?")
                    .font(LooreFont.hero)
                    .foregroundStyle(LooreColor.textPrimary)
                    .multilineTextAlignment(.center)
                    .padding(.bottom, gleans ? 28 : 40)
                    .looreFadeIn(delay: 0.2, offset: 12)
                    .accessibilityAddTraits(.isHeader)

                if gleans {
                    purposeCards
                } else {
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .top, spacing: 16) { cards }
                            .frame(minWidth: 640)
                        VStack(spacing: 16) { cards }
                    }
                    .frame(maxWidth: 900)
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.vertical, 40)
            .frame(maxWidth: .infinity)
        }
        .loorePageBackground(glow: true)
        .toolbar(.hidden, for: .navigationBar)
        .onAppear { greeting = LooreDateFormat.greeting() }
    }

    /// Without Glean: today's mode cards.
    @ViewBuilder private var cards: some View {
        WorkflowCard(title: "Voice", description: "Speak what's present.", delay: 0.4) {
            LooreLogo(size: 42)
        } action: {
            app.open(.voice(parentId: nil, resumeLLMId: nil))
        }
        .accessibilityIdentifier("home.voice")
        WorkflowCard(title: "Text", description: "Type what's on your mind.", delay: 0.52) {
            HomeIcons.text
        } action: {
            app.open(.textMode())
        }
        .accessibilityIdentifier("home.text")
        if app.capabilities.shareEnabled { shareCard(delay: 0.64) }
    }

    /// The Share card, the same with and without Glean (Peter: "Share stays as today").
    private func shareCard(delay: Double) -> some View {
        WorkflowCard(title: "Share", description: "Give something outward.", delay: delay) {
            HomeIcons.share
        } action: {
            app.open(.share)
        }
        .accessibilityIdentifier("home.share")
    }

    /// With Glean (#436): Reflect and Glean, side by side where they fit and
    /// stacked on a phone; Share in its own row below.
    private var purposeCards: some View {
        VStack(spacing: 24) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .top, spacing: 16) { purposeCardViews }
                    .frame(minWidth: 520)
                VStack(spacing: 16) { purposeCardViews }
            }
            .frame(maxWidth: 560)
            if app.capabilities.shareEnabled {
                shareCard(delay: 0.54).frame(maxWidth: 560)
            }
        }
    }

    @ViewBuilder private var purposeCardViews: some View {
        ForEach(Array(HomePurpose.allCases.enumerated()), id: \.element) { index, purpose in
            PurposeCard(purpose: purpose, delay: 0.3 + Double(index) * 0.12) { mode in
                app.open(purpose.route(mode))
            }
        }
    }
}

/// The home screen's cards by purpose (#436, web `purposeCards`).
enum HomePurpose: String, CaseIterable, Hashable {
    case reflect, glean

    enum Mode: String, CaseIterable { case voice = "Voice", text = "Text" }

    var title: String { self == .reflect ? "Reflect" : "Glean" }

    var line: String {
        self == .reflect ? "Talk it through with Loore." : "Reflect, and Loore gleans for you."
    }

    /// Glean opens the same screens as a Glean session: the thread it starts is
    /// marked, so every turn of it offers Glean (#435).
    func route(_ mode: Mode) -> AppRoute {
        let glean = self == .glean
        switch mode {
        case .voice: return .voice(parentId: nil, resumeLLMId: nil, glean: glean)
        case .text: return .textMode(glean: glean)
        }
    }
}

/// One purpose card (web `PurposeCard`): serif title, a short rule, the line,
/// then its Voice and Text buttons.
struct PurposeCard: View {
    let purpose: HomePurpose
    var delay: Double = 0
    let open: (HomePurpose.Mode) -> Void

    var body: some View {
        VStack(spacing: 10) {
            Text(purpose.title)
                .font(LooreFont.serif(24.8, .regular, relativeTo: .title2))
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.top, 4)
                .accessibilityAddTraits(.isHeader)
            Rectangle()
                .fill(LooreColor.accentGlow)
                .frame(width: 32, height: 1)
                .padding(.vertical, 2)
                .accessibilityHidden(true)
            Text(purpose.line)
                .font(LooreFont.sans(14.1, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .lineSpacing(3)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                ForEach(HomePurpose.Mode.allCases, id: \.self) { mode in
                    Button { open(mode) } label: {
                        HStack(spacing: 7) {
                            Image(systemName: mode == .voice ? "mic" : "text.alignleft")
                                .font(.system(size: 13, weight: .light))
                                .accessibilityHidden(true)
                            Text(mode.rawValue)
                        }
                        .font(LooreFont.sans(14.1, .light))
                        .foregroundStyle(LooreColor.textPrimary)
                        .padding(.horizontal, 16)
                        .frame(minHeight: 44)
                        .overlay(Capsule().strokeBorder(LooreColor.borderHover))
                        .contentShape(Capsule())
                    }
                    .buttonStyle(CardPressStyle())
                    // A screen reader tells the two Voice buttons apart (web: "Glean: Voice").
                    .accessibilityLabel("\(purpose.title): \(mode.rawValue)")
                    .accessibilityIdentifier("home.\(purpose.rawValue).\(mode.rawValue.lowercased())")
                }
            }
            .padding(.top, 6)
        }
        .frame(maxWidth: .infinity)
        .padding(EdgeInsets(top: 24, leading: 20, bottom: 20, trailing: 20))
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.large))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.border))
        .looreFadeIn(delay: delay, offset: 20)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("home.\(purpose.rawValue)")
    }
}

/// A Home mode card (web `WorkflowCard`): card surface, 48pt icon slot at 70%,
/// serif title, muted one-line description; staggered entrance.
struct WorkflowCard<Icon: View>: View {
    let title: String
    let description: String
    var delay: Double = 0
    @ViewBuilder var icon: () -> Icon
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 0) {
                icon()
                    .frame(width: 48, height: 48)
                    .opacity(0.7)
                    .padding(.bottom, 24)
                Text(title)
                    .font(LooreFont.cardTitle)
                    .foregroundStyle(LooreColor.textPrimary)
                    .padding(.bottom, 11)
                Text(description)
                    .font(LooreFont.sans(14.1, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .lineSpacing(4)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(EdgeInsets(top: 35, leading: 29, bottom: 32, trailing: 29))
            .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.large))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.border))
            .contentShape(RoundedRectangle(cornerRadius: LooreRadius.large))
        }
        .buttonStyle(CardPressStyle())
        .looreFadeIn(delay: delay, offset: 20)
        .accessibilityElement(children: .combine)
        .accessibilityHint(description)
    }
}

/// A gentle press state for tappable cards (the web's hover lift, for touch).
struct CardPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .brightness(configuration.isPressed ? 0.02 : 0)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

/// The Home cards' line icons, from `HomePage.js`.
enum HomeIcons {
    private static let box = CGSize(width: 42, height: 42)

    static var text: some View {
        ZStack {
            SVGShape("M8 12 C8 9.8 9.8 8 12 8 L30 8 C32.2 8 34 9.8 34 12 L34 24 C34 26.2 32.2 28 30 28 L16 28 L10 33 L10 28 L12 28 C9.8 28 8 26.2 8 24 Z", viewBox: box)
                .stroke(LooreColor.accent, lineWidth: 1.4)
            SVGShape("M14 15 L28 15 M14 20 L23 20", viewBox: box)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: 1.4, lineCap: .round))
        }
        .frame(width: 42, height: 42)
        .accessibilityHidden(true)
    }

    static var share: some View {
        ZStack {
            SVGShape("M16 9 L26 9 L29.5 13.5 L21 21.5 L12.5 13.5 Z", viewBox: box)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: 1.3, lineJoin: .round))
            SVGShape("M12.5 13.5 L29.5 13.5 M18.5 9 L18 13.5 L21 21.5 M23.5 9 L24 13.5 L21 21.5", viewBox: box)
                .stroke(LooreColor.accent.opacity(0.75), style: StrokeStyle(lineWidth: 1, lineJoin: .round))
            SVGShape("M21 3.5 V6 M11 5 L13 7 M31 5 L29 7", viewBox: box)
                .stroke(LooreColor.accent.opacity(0.7), style: StrokeStyle(lineWidth: 1.1, lineCap: .round))
            SVGShape("M2.5 39 C5.5 37.5 8.5 36.2 10.5 34.4 C11.3 33.8 11.6 33 12.9 32.5 C15.5 31.4 19 31.2 24 30.9 C29 31.2 33.5 30 37.2 28.4 C38.3 27.9 38.3 26.7 37.2 26.5 C33 25.9 28 26.6 24.5 27.9 C24.2 27 24.9 26.1 24 25.7 C22.8 25.2 19.5 25.3 16.8 26.2 C14 27.1 11.8 28.5 10.3 30 C7.2 31.2 4.5 32.6 2.5 34", viewBox: box)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: 1.4, lineCap: .round, lineJoin: .round))
            SVGShape("M24.5 27.9 C21.5 27.9 18.5 27.9 16.2 28.3", viewBox: box)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: 1.4, lineCap: .round))
        }
        .frame(width: 42, height: 42)
        .accessibilityHidden(true)
    }
}
