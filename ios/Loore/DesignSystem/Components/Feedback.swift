import SwiftUI
import Observation

/// The web's pill switch (32×18, knob 14pt; on = accent, off = border).
struct LoorePillToggleStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        Button {
            configuration.isOn.toggle()
        } label: {
            HStack {
                configuration.label
                Spacer(minLength: 12)
                LoorePillSwitch(isOn: configuration.isOn)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityValue(configuration.isOn ? "On" : "Off")
    }
}

/// The switch graphic alone, for rows whose tap does something other than flip
/// (the craft toggle asks first).
struct LoorePillSwitch: View {
    let isOn: Bool

    var body: some View {
        ZStack(alignment: isOn ? .trailing : .leading) {
            Capsule()
                .fill(isOn ? LooreColor.accent : LooreColor.border)
                .frame(width: 32, height: 18)
            Circle()
                .fill(LooreColor.textPrimary)
                .frame(width: 14, height: 14)
                .padding(.horizontal, 2)
        }
        .animation(LooreMotion.micro, value: isOn)
        .accessibilityHidden(true)
    }
}

extension ToggleStyle where Self == LoorePillToggleStyle {
    static var loorePill: LoorePillToggleStyle { LoorePillToggleStyle() }
}

// MARK: - Dialogs

/// The web's dialog shell (map A §4.1): dimmed, blurred backdrop; a centred card
/// (`bg-card`, radius 12, 2rem padding, max 480pt); serif title; body text;
/// actions below. Present it with `.looreDialog(isPresented:)`.
struct LooreDialogCard<Content: View>: View {
    var title: String?
    var icon: AnyView?
    @ViewBuilder var content: () -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let title {
                    HStack(spacing: 10) {
                        if let icon { icon }
                        Text(title)
                            .font(LooreFont.dialogTitle)
                            .foregroundStyle(LooreColor.textPrimary)
                            .accessibilityAddTraits(.isHeader)
                    }
                }
                content()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(LooreSpacing.dialog)
        }
        .scrollBounceBehavior(.basedOnSize)
        .frame(maxWidth: 480)
        .fixedSize(horizontal: false, vertical: true)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.large))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.border))
        .padding(.horizontal, LooreSpacing.md)
    }
}

/// Body text style for dialogs: sans 300, .92rem, secondary, line spacing 1.6.
struct DialogBodyText: View {
    let text: String

    var body: some View {
        Text(text)
            .font(LooreFont.body)
            .foregroundStyle(LooreColor.textSecondary)
            .lineSpacing(5)
            .fixedSize(horizontal: false, vertical: true)
    }
}

private struct LooreDialogPresenter<Card: View>: ViewModifier {
    @Binding var isPresented: Bool
    var dismissOnBackdropTap: Bool
    @ViewBuilder var card: () -> Card
    /// Mirrors `isPresented`, flipped without the cover's slide-up animation:
    /// the container fades itself in and out instead.
    @State private var coverShown = false

    func body(content: Content) -> some View {
        content
            .fullScreenCover(isPresented: $coverShown) {
                DialogContainer(isPresented: $isPresented, dismissOnBackdropTap: dismissOnBackdropTap, card: card)
                    .presentationBackground(.clear)
            }
            .onChange(of: isPresented) { _, newValue in
                var transaction = Transaction()
                transaction.disablesAnimations = true
                withTransaction(transaction) { coverShown = newValue }
            }
            .onAppear {
                if isPresented { coverShown = true }
            }
    }
}

private struct DialogContainer<Card: View>: View {
    @Binding var isPresented: Bool
    var dismissOnBackdropTap: Bool
    @ViewBuilder var card: () -> Card
    @State private var shown = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            Rectangle()
                .fill(.ultraThinMaterial)
                .overlay(LooreColor.dialogBackdrop)
                .ignoresSafeArea()
                .onTapGesture {
                    if dismissOnBackdropTap { isPresented = false }
                }
                .accessibilityHidden(true)
            card()
                .scaleEffect(shown || reduceMotion ? 1 : 0.97)
        }
        .opacity(shown ? 1 : 0)
        .onAppear {
            withAnimation(.easeOut(duration: 0.2)) { shown = true }
        }
    }
}

extension View {
    /// Presents a dialog card over everything (tab bar included).
    func looreDialog<Card: View>(isPresented: Binding<Bool>, dismissOnBackdropTap: Bool = true,
                                 @ViewBuilder card: @escaping () -> Card) -> some View {
        modifier(LooreDialogPresenter(isPresented: isPresented, dismissOnBackdropTap: dismissOnBackdropTap, card: card))
    }
}

// MARK: - Toasts

/// App-wide toasts (web `ToastContext`): plain strings, bottom-centre stack,
/// tap to dismiss, 3 s by default.
@MainActor
@Observable
final class ToastCenter {
    struct Toast: Identifiable, Equatable {
        let id: Int
        let message: String
    }

    private(set) var toasts: [Toast] = []
    private var nextId = 0

    @discardableResult
    func show(_ message: String, duration: TimeInterval = 3) -> Int {
        nextId += 1
        let id = nextId
        toasts.append(Toast(id: id, message: message))
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(duration * 1_000_000_000))
            self?.dismiss(id)
        }
        return id
    }

    func dismiss(_ id: Int) {
        toasts.removeAll { $0.id == id }
    }

    func clear() {
        toasts.removeAll()
    }
}

/// Renders the toast stack; place it in an overlay at the bottom of the root view.
struct ToastStack: View {
    let center: ToastCenter

    var body: some View {
        VStack(spacing: 8) {
            ForEach(center.toasts) { toast in
                Text(toast.message)
                    .font(LooreFont.menu)
                    .foregroundStyle(LooreColor.textSecondary)
                    .multilineTextAlignment(.center)
                    .padding(.vertical, 10)
                    .padding(.horizontal, 20)
                    .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.small))
                    .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.border))
                    .shadow(color: LooreColor.shadow.opacity(0.3), radius: 10, y: 4)
                    .onTapGesture { center.dismiss(toast.id) }
                    .transition(.opacity.combined(with: .offset(y: 8)))
                    .accessibilityAddTraits(.isStaticText)
                    .accessibilityAction { center.dismiss(toast.id) }
            }
        }
        .padding(.horizontal, LooreSpacing.md)
        .animation(LooreMotion.quick, value: center.toasts)
    }
}

/// "LIMIT REACHED" banner (web `SpendCapBanner`): shown after a 402 until dismissed.
struct SpendCapBanner: View {
    let message: String
    let onDismiss: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            Text("LIMIT REACHED")
                .font(LooreFont.eyebrow)
                .tracking(1.3)
                .foregroundStyle(LooreColor.accent)
                .fixedSize()
            Text(message)
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .lineSpacing(3)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: onDismiss) {
                Text("×").font(LooreFont.sans(19, .light)).foregroundStyle(LooreColor.textMuted)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Dismiss")
        }
        .padding(.vertical, 12)
        .padding(.horizontal, 16)
        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.small))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.accent))
        .shadow(color: LooreColor.shadow.opacity(0.25), radius: 10, y: 4)
        .frame(maxWidth: 680)
        .padding(.horizontal, LooreSpacing.md)
        .transition(.opacity.combined(with: .offset(y: 8)))
    }
}
