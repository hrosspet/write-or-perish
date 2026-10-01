import SwiftUI

/// First visit after approval (web `WelcomePage`, map E §11.2): the hero, the
/// first-entry prompt (opens Write New Entry), the prefill consent card, the
/// import card (opens the import picker), the How-to link and closing lines.
struct WelcomeView: View {
    @Environment(AppState.self) private var app
    @State private var writing = false
    @State private var importing = false

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                hero
                card(eyebrow: "Your first entry",
                     prompt: "What brought you to Loore — and what are you hoping to find here?") {
                    CTAButton(title: "Start writing") { writing = true }
                        .accessibilityIdentifier("welcome.startWriting")
                    Text("You can type or record a voice note — whatever feels natural.")
                        .font(LooreFont.sans(12.5, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .padding(.top, 16)
                }
                .padding(.top, 16)
                .padding(.bottom, 64)
                .looreFadeIn()

                PrefillConsentCard(eyebrow: "Already on X?")
                    .padding(.bottom, 40)
                    .looreFadeIn()

                card(eyebrow: "Already have a journal?",
                     prompt: "Import your Obsidian journals, markdown files, or exported tweets. Your lore doesn't start from zero.") {
                    CTAButton(title: "Import data") { importing = true }
                        .accessibilityIdentifier("welcome.import")
                }
                .padding(.bottom, 40)
                .looreFadeIn()

                Button { app.open(.webPage(path: "/how-to")) } label: {
                    Text("See practical tips & workflows →")
                        .font(LooreFont.sans(14.1, .light))
                        .foregroundStyle(LooreColor.accent)
                        .padding(.bottom, 2)
                        .overlay(alignment: .bottom) { Rectangle().fill(LooreColor.accentGlow).frame(height: 1) }
                }
                .buttonStyle(.plain)
                .looreFadeIn(delay: 0.08)

                closing
            }
            .padding(.horizontal, 32)
            .frame(maxWidth: 580)
            .frame(maxWidth: .infinity)
        }
        .loorePageBackground(glow: true)
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $writing) { WriteNewEntrySheet() }
        .sheet(isPresented: $importing) {
            NavigationStack {
                ScrollView {
                    ImportDataSection(compact: true)
                        .padding(LooreSpacing.gutter)
                }
                .background(LooreColor.bgCard)
                .navigationTitle("Import Data")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Close") { importing = false } }
                }
            }
            .presentationBackground(LooreColor.bgCard)
        }
    }

    private var hero: some View {
        VStack(spacing: 0) {
            LooreLogo(size: 32).opacity(0.5).padding(.bottom, 24).looreFadeIn()
            (Text("Welcome to ")
             + Text("Loore").font(LooreFont.serif(32, .light, italic: true, relativeTo: .largeTitle)).foregroundColor(LooreColor.accent)
             + Text("."))
                .font(LooreFont.serif(32, .light, relativeTo: .largeTitle))
                .foregroundStyle(LooreColor.textPrimary)
                .multilineTextAlignment(.center)
                .padding(.bottom, 19)
                .looreFadeIn(delay: 0.1)
                .accessibilityAddTraits(.isHeader)
            Text("You're one of the first people here. This is an alpha — things are raw, evolving, alive. Your experience and your feedback shape what Loore becomes.")
                .font(LooreFont.bodyLarge)
                .foregroundStyle(LooreColor.textSecondary)
                .lineSpacing(9)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .looreFadeIn(delay: 0.18)
        }
        .padding(.top, 64)
        .padding(.bottom, 40)
    }

    private func card<Content: View>(eyebrow: String, prompt: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(spacing: 0) {
            Text(eyebrow.uppercased())
                .font(LooreFont.sans(10.9, .regular))
                .tracking(1.96)
                .foregroundStyle(LooreColor.accent.opacity(0.6))
                .padding(.bottom, 19)
            Text(prompt)
                .font(LooreFont.sans(17.6, .light))
                .foregroundStyle(LooreColor.textPrimary)
                .lineSpacing(8)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 29)
            content()
        }
        .padding(.vertical, 40)
        .padding(.horizontal, 28)
        .frame(maxWidth: .infinity)
        .background {
            ZStack {
                LooreColor.bgCard
                RadialGradient(colors: [LooreColor.accentGlow, .clear], center: UnitPoint(x: 0.5, y: 0), startRadius: 0,
                               endRadius: 150)
                    .opacity(0.5)
            }
        }
        .overlay(Rectangle().strokeBorder(LooreColor.border))
    }

    private var closing: some View {
        VStack(spacing: 0) {
            AccentRule().opacity(0.6).padding(.bottom, 40)
            Text("There's no wrong way to do this.")
                .font(LooreFont.serif(19.2, .light, relativeTo: .title3))
                .foregroundStyle(LooreColor.textSecondary)
                .padding(.bottom, 13)
            Text("Write about today. Talk about a dream. Process something that's been sitting in you. Loore will meet you wherever you are.")
                .font(LooreFont.sans(15.2, .light))
                .foregroundStyle(LooreColor.textMuted)
                .lineSpacing(9)
                .padding(.bottom, 24)
            Text("If something's broken or feels wrong, tell us.\nThis is ours to shape together.")
                .font(LooreFont.sans(13.6, .light))
                .foregroundStyle(LooreColor.textMuted.opacity(0.7))
        }
        .multilineTextAlignment(.center)
        .fixedSize(horizontal: false, vertical: true)
        .padding(.top, 32)
        .padding(.bottom, 80)
        .looreFadeIn()
    }
}
