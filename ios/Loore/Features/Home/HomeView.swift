import SwiftUI

/// Reflect (web `HomePage`, route `/`): greeting, "What's on your mind?", and
/// the mode cards: Voice (M3), Text, Share (flag), Read (admins).
struct HomeView: View {
    @Environment(AppState.self) private var app
    @State private var greeting = LooreDateFormat.greeting()
    @State private var readStarting = false

    var body: some View {
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
                    .padding(.bottom, 40)
                    .looreFadeIn(delay: 0.2, offset: 12)
                    .accessibilityAddTraits(.isHeader)

                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: 16) { cards }
                        .frame(minWidth: 640)
                    VStack(spacing: 16) { cards }
                }
                .frame(maxWidth: 900)
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.vertical, 40)
            .frame(maxWidth: .infinity)
        }
        .loorePageBackground(glow: true)
        .toolbar(.hidden, for: .navigationBar)
        .onAppear { greeting = LooreDateFormat.greeting() }
    }

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
            app.open(.textMode)
        }
        .accessibilityIdentifier("home.text")
        if app.capabilities.shareEnabled {
            WorkflowCard(title: "Share", description: "Give something outward.", delay: 0.64) {
                HomeIcons.share
            } action: {
                app.open(.share)
            }
            .accessibilityIdentifier("home.share")
        }
        if app.capabilities.isAdmin {
            WorkflowCard(title: "Read", description: readStarting ? "Starting…" : "What's worth your time today.",
                         delay: 0.76) {
                HomeIcons.read
            } action: {
                startRead()
            }
            .disabled(readStarting)
            .accessibilityIdentifier("home.read")
        }
    }

    /// Admin-only Community Archive read (`POST /api/read/start`, billed):
    /// the click creates the thread and lands on the reply (or the prompt).
    private func startRead() {
        guard !readStarting else { return }
        readStarting = true
        let stored = UserDefaults.standard.object(forKey: DefaultsKey.autoGenerate)
        let autoGenerate = stored == nil ? true : UserDefaults.standard.bool(forKey: DefaultsKey.autoGenerate)
        Task {
            do {
                struct Answer: Decodable { var llm_node_id: Int?; var prompt_node_id: Int? }
                let answer: Answer = try await app.api.post(APIPath.readStart, json: .object(["auto_generate": .bool(autoGenerate)]))
                readStarting = false
                if let id = answer.llm_node_id ?? answer.prompt_node_id { app.open(.thread(id: id, awaitLLM: nil)) }
            } catch {
                readStarting = false
                if SpendCap.isSpendCapError(error) { return }
                app.toasts.show((error as? APIError)?.userMessage(fallback: "Could not start the read.")
                                ?? "Could not start the read.", duration: 6)
            }
        }
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

    /// An open book, one page marked (the web's Read card icon).
    static var read: some View {
        ZStack {
            SVGShape("M6 11 C11 9.5 16 9.8 21 12.5 C26 9.8 31 9.5 36 11 L36 32 C31 30.5 26 30.8 21 33.5 C16 30.8 11 30.5 6 32 Z", viewBox: box)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: 1.4, lineJoin: .round))
            SVGShape("M21 12.5 L21 33.5", viewBox: box)
                .stroke(LooreColor.accent.opacity(0.7), lineWidth: 1.1)
            SVGShape("M10 16.5 C13 15.8 15.5 16 18 17.2 M10 21 C13 20.3 15.5 20.5 18 21.7 M10 25.5 C13 24.8 15.5 25 18 26.2 M24 16.5 C27 15.8 29.5 16 32 17.2 M24 21 C27 20.3 29.5 20.5 32 21.7", viewBox: box)
                .stroke(LooreColor.accent.opacity(0.6), style: StrokeStyle(lineWidth: 1, lineCap: .round))
            Circle().fill(LooreColor.accent).frame(width: 3.2, height: 3.2).position(x: 28, y: 26)
        }
        .frame(width: 42, height: 42)
        .accessibilityHidden(true)
    }

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
