import SwiftUI

/// Button looks from the web's global styles (map A §5.3).
struct LooreButtonStyle: ButtonStyle {
    enum Kind {
        /// The global button: transparent, 1px border, secondary text.
        case outline
        /// Primary = accent outline + accent text.
        case primary
        /// Filled accent with deep-background text (web: UpdatesModal "Got it" only).
        case filled
        /// Text-only accent link-button.
        case link
        /// Muted text-only button.
        case quiet
    }

    var kind: Kind = .outline
    var fullWidth = false
    var font: Font = LooreFont.button

    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        configuration.label
            .font(font)
            .lineLimit(nil)
            .multilineTextAlignment(.center)
            .foregroundStyle(foreground(pressed: pressed))
            .padding(.vertical, kind == .link || kind == .quiet ? 4 : 10)
            .padding(.horizontal, kind == .link || kind == .quiet ? 0 : 20)
            .frame(maxWidth: fullWidth ? .infinity : nil)
            .background(background(pressed: pressed), in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .overlay {
                if kind == .outline || kind == .primary {
                    RoundedRectangle(cornerRadius: LooreRadius.control)
                        .strokeBorder(borderColor(pressed: pressed), lineWidth: 1)
                }
            }
            .contentShape(RoundedRectangle(cornerRadius: LooreRadius.control))
            .opacity(isEnabled ? 1 : 0.45)
            .animation(.easeOut(duration: 0.15), value: pressed)
    }

    private func foreground(pressed: Bool) -> Color {
        switch kind {
        case .outline: return pressed ? LooreColor.textPrimary : LooreColor.textSecondary
        case .primary, .link: return pressed ? LooreColor.accentHover : LooreColor.accent
        case .filled: return LooreColor.bgDeep
        case .quiet: return pressed ? LooreColor.textSecondary : LooreColor.textMuted
        }
    }

    private func background(pressed: Bool) -> Color {
        switch kind {
        case .outline, .primary: return pressed ? LooreColor.accentGlow : .clear
        case .filled: return pressed ? LooreColor.accentHover : LooreColor.accent
        case .link, .quiet: return .clear
        }
    }

    private func borderColor(pressed: Bool) -> Color {
        switch kind {
        case .primary: return LooreColor.accent
        default: return pressed ? LooreColor.borderHover : LooreColor.border
        }
    }
}

extension ButtonStyle where Self == LooreButtonStyle {
    static var looreOutline: LooreButtonStyle { LooreButtonStyle(kind: .outline) }
    static var loorePrimary: LooreButtonStyle { LooreButtonStyle(kind: .primary) }
    static var looreFilled: LooreButtonStyle { LooreButtonStyle(kind: .filled) }
    static var looreLink: LooreButtonStyle { LooreButtonStyle(kind: .link, font: LooreFont.sans(14, .light)) }
    static var looreQuiet: LooreButtonStyle { LooreButtonStyle(kind: .quiet, font: LooreFont.sans(14, .light)) }

    static func loore(_ kind: LooreButtonStyle.Kind, fullWidth: Bool = false) -> LooreButtonStyle {
        LooreButtonStyle(kind: kind, fullWidth: fullWidth)
    }
}

/// The stacked, left-aligned choice button of the web's dialogs: bold first line,
/// muted subline, `bg-deep` fill, 1px border (Delete, CraftMode, Rename, …).
struct ChoiceButton: View {
    enum Tone { case accent, primary, normal, destructive }

    let title: String
    var subtitle: String?
    var tone: Tone = .normal
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(LooreFont.sans(14.4, .medium))
                    .foregroundStyle(titleColor)
                if let subtitle {
                    Text(subtitle)
                        .font(LooreFont.sans(13.1, .light))
                        .foregroundStyle(LooreColor.textMuted)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 10)
            .padding(.horizontal, 16)
            .background(LooreColor.bgDeep, in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var titleColor: Color {
        switch tone {
        case .accent: return LooreColor.accent
        case .primary: return LooreColor.textPrimary
        case .normal: return LooreColor.textSecondary
        case .destructive: return LooreColor.error
        }
    }
}

/// The web's square CTA (`CtaButton`): accent outline, no radius, trailing arrow.
struct CTAButton: View {
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Text(title)
                Text("→")
            }
            .font(LooreFont.sans(15.2, .regular))
            .tracking(0.9)
            .foregroundStyle(LooreColor.accent)
            .padding(.vertical, 14)
            .padding(.horizontal, 36)
            .overlay(Rectangle().strokeBorder(LooreColor.accent, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
