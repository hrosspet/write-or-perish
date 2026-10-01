import SwiftUI

/// More: the web NavBar's ⋮ menu as a screen (design doc §5; map A §2.1), in the
/// web's order: Import data · Admin · Account · My public page · References ·
/// Light mode · Craft mode (+ Write new entry, Export data, Prompts) · About · Logout.
struct MoreView: View {
    @Environment(AppState.self) private var app
    @Environment(\.colorScheme) private var systemScheme
    @State private var craftDialog = false
    @State private var craftGlow = false
    @State private var exporting = false
    @State private var exportFile: ExportFile?
    @State private var confirmLogout = false
    @State private var writingNewEntry = false

    var body: some View {
        let caps = app.capabilities
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                PageHeader(title: "More")
                    .padding(.bottom, 20)

                MenuGroup {
                    MenuRow(title: "Import data") { app.open(.importData(anchor: nil)) }
                        .accessibilityIdentifier("more.import")
                }

                MenuGroup {
                    if caps.isAdmin {
                        MenuRow(title: "Admin") { app.open(.admin) }
                            .accessibilityIdentifier("more.admin")
                    }
                    MenuRow(title: "Account") { app.open(.account(anchor: nil)) }
                        .accessibilityIdentifier("more.account")
                    if caps.shareEnabled, let username = app.user?.username, !username.isEmpty {
                        MenuRow(title: "My public page") { app.open(.webPage(path: "/@\(username)")) }
                            .accessibilityIdentifier("more.publicPage")
                    }
                    // The web has no menu entry for References (map A §7.2); the app lists it here.
                    MenuRow(title: "References") { app.open(.references) }
                        .accessibilityIdentifier("more.references")
                }

                MenuGroup {
                    lightModeRow
                    craftModeRow
                    if caps.craftMode {
                        VStack(spacing: 0) {
                            MenuRow(title: "Write new entry", craft: true) { writingNewEntry = true }
                                .accessibilityIdentifier("more.writeNew")
                            MenuRow(title: exporting ? "Exporting…" : "Export data", craft: true) { export() }
                                .disabled(exporting)
                                .accessibilityIdentifier("more.export")
                            MenuRow(title: "Prompts", craft: true) { app.open(.prompts) }
                        }
                        .modifier(CraftGlow(active: $craftGlow))
                        .transition(.opacity)
                    }
                }

                MenuGroup(label: "About") {
                    MenuRow(title: "Why Loore") { app.open(.webPage(path: "/why-loore")) }
                    MenuRow(title: "Vision") { app.open(.webPage(path: "/vision")) }
                    MenuRow(title: "How To") { app.open(.webPage(path: "/how-to")) }
                }

                MenuGroup {
                    MenuRow(title: "Logout") { confirmLogout = true }
                        .accessibilityIdentifier("more.logout")
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 40)
            .looreReadableWidth(560)
            .animation(LooreMotion.quick, value: caps.craftMode)
        }
        .loorePageBackground()
        .toolbar(.hidden, for: .navigationBar)
        .looreDialog(isPresented: $craftDialog) {
            CraftModeDialog(
                onConfirm: {
                    craftDialog = false
                    Task {
                        await app.setCraftMode(true)
                        craftGlow = true
                    }
                    app.toasts.show("Craft mode on. Its controls carry the sliders icon.", duration: 5)
                },
                onCancel: { craftDialog = false })
        }
        .confirmationDialog("Log out of Loore?", isPresented: $confirmLogout, titleVisibility: .visible) {
            Button("Logout", role: .destructive) { Task { await app.signOut() } }
            Button("Cancel", role: .cancel) {}
        }
        .sheet(isPresented: $writingNewEntry) {
            WriteNewEntrySheet()
        }
        .sheet(item: $exportFile) { file in
            ShareSheet(items: [file.url]) {
                try? FileManager.default.removeItem(at: file.url)
            }
        }
    }

    private var lightModeRow: some View {
        let isLight = app.theme.isLight(system: systemScheme)
        return Button {
            app.theme.toggle(system: systemScheme)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: isLight ? "sun.max" : "moon")
                    .font(.system(size: 13, weight: .light))
                Text("Light mode")
                Spacer()
                LoorePillSwitch(isOn: isLight)
            }
            .menuRowStyle()
        }
        .buttonStyle(MenuRowButtonStyle())
        .accessibilityLabel("Light mode")
        .accessibilityValue(isLight ? "On" : "Off")
        .accessibilityIdentifier("more.lightMode")
    }

    private var craftModeRow: some View {
        let on = app.capabilities.craftMode
        return Button {
            if on {
                Task { await app.setCraftMode(false) }
                app.toasts.show("Craft mode off.")
            } else {
                craftDialog = true
            }
        } label: {
            HStack(spacing: 8) {
                CraftIcon(size: 14)
                Text("Craft mode")
                Spacer()
                LoorePillSwitch(isOn: on)
            }
            .menuRowStyle()
        }
        .buttonStyle(MenuRowButtonStyle())
        .accessibilityLabel("Craft mode")
        .accessibilityValue(on ? "On" : "Off")
        .accessibilityHint("Shows extra controls: privacy & AI usage per entry, auto-generate toggle, model picker, prompt editing, export.")
        .accessibilityIdentifier("more.craftMode")
    }

    /// "Export data" (craft): `GET /api/export/threads` to a temporary file, then the share sheet.
    private func export() {
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
                let target = FileManager.default.temporaryDirectory.appendingPathComponent("loore-export-\(day).txt")
                try? FileManager.default.removeItem(at: target)
                try FileManager.default.moveItem(at: tempURL, to: target)
                exportFile = ExportFile(url: target)
            } catch {
                app.toasts.show("Export failed. Please try again.")
            }
        }
    }
}

// MARK: - Menu building blocks

/// A group of menu rows with an optional small uppercase label, separated from
/// the next group by the web's 1pt divider.
private struct MenuGroup<Content: View>: View {
    var label: String?
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let label {
                Text(label.uppercased())
                    .font(LooreFont.sans(11.2, .light))
                    .tracking(0.9)
                    .foregroundStyle(LooreColor.textMuted.opacity(0.6))
                    .padding(.horizontal, 16)
                    .padding(.top, 6)
                    .padding(.bottom, 4)
            }
            content()
        }
        .padding(.vertical, 6)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.small))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.border))
        .padding(.bottom, 14)
    }
}

/// One menu item (web dropdown item: sans 300, .85rem, muted; 10×16 padding).
private struct MenuRow: View {
    let title: String
    var craft = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if craft { CraftIcon(size: 14) }
                Text(title)
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .light))
                    .opacity(0.5)
                    .accessibilityHidden(true)
            }
            .menuRowStyle()
        }
        .buttonStyle(MenuRowButtonStyle())
    }
}

private struct MenuRowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(configuration.isPressed ? LooreColor.bgCardHover : Color.clear)
    }
}

private extension View {
    func menuRowStyle() -> some View {
        self
            .font(LooreFont.sans(15, .light))
            .foregroundStyle(LooreColor.textSecondary)
            .padding(.vertical, 12)
            .padding(.horizontal, 16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
    }
}

/// One-shot glow on the craft items after "Turn on" (web `.craft-glow`, 2.4 s; 1.2 s with Reduce Motion).
private struct CraftGlow: ViewModifier {
    @Binding var active: Bool
    @State private var intensity: Double = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: LooreRadius.control)
                    .fill(LooreColor.bgCard)
                    .shadow(color: LooreColor.accent.opacity(0.9 * intensity), radius: 12)
                    .shadow(color: LooreColor.accentGlow.opacity(intensity), radius: 26)
            )
            .onChange(of: active) { _, on in
                guard on else { return }
                intensity = 1
                let duration = reduceMotion ? 1.2 : 2.4
                withAnimation(.easeOut(duration: duration * 0.45).delay(duration * 0.55)) { intensity = 0 }
                Task {
                    try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
                    active = false
                }
            }
    }
}

// MARK: - Craft mode dialog

/// Asked before craft mode turns on (web `CraftModeDialog`); off is a plain flip.
struct CraftModeDialog: View {
    let onConfirm: () -> Void
    let onCancel: () -> Void

    var body: some View {
        LooreDialogCard(title: "Turn on craft mode?",
                        icon: AnyView(CraftIcon(size: 18).foregroundStyle(LooreColor.accent))) {
            VStack(alignment: .leading, spacing: 10) {
                DialogBodyText(text: "It shows extra controls for steering the details:")
                VStack(alignment: .leading, spacing: 4) {
                    bullet("privacy and AI usage on each entry")
                    bullet("a switch for auto-generating responses, model picker and voice upload on threads")
                    bullet("prompt editing and data export in More")
                }
                .padding(.leading, 4)
                DialogBodyText(text: "Everything it adds carries the sliders icon. Turn it off any time in More or under Account.")
            }
            .padding(.bottom, 8)
            VStack(spacing: 8) {
                ChoiceButton(title: "Turn on", tone: .accent, action: onConfirm)
                    .accessibilityIdentifier("craft.turnOn")
                ChoiceButton(title: "Not now", subtitle: "Keep Loore's current simple UI.", action: onCancel)
                    .accessibilityIdentifier("craft.notNow")
            }
        }
    }

    private func bullet(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("•")
            DialogBodyText(text: text)
        }
        .font(LooreFont.body)
        .foregroundStyle(LooreColor.textSecondary)
    }
}

// MARK: - Share sheet

struct ExportFile: Identifiable {
    let id = UUID()
    let url: URL
}

/// `UIActivityViewController` wrapper; `onComplete` runs when the sheet closes.
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    var onComplete: () -> Void = {}

    func makeUIViewController(context: Context) -> UIActivityViewController {
        let controller = UIActivityViewController(activityItems: items, applicationActivities: nil)
        controller.completionWithItemsHandler = { _, _, _, _ in onComplete() }
        return controller
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
