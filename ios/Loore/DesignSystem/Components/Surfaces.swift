import SwiftUI

/// Card surface: `bg-card`, 1px border, rounded (web Bubble / cards / dialogs).
struct LooreCardModifier: ViewModifier {
    var padding: EdgeInsets
    var radius: CGFloat
    var accentLeftBorder = false

    func body(content: Content) -> some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: radius))
            .overlay(RoundedRectangle(cornerRadius: radius).strokeBorder(LooreColor.border, lineWidth: 1))
            .overlay(alignment: .leading) {
                if accentLeftBorder {
                    // The focal node's 3px accent left border.
                    UnevenRoundedRectangle(topLeadingRadius: radius, bottomLeadingRadius: radius)
                        .fill(LooreColor.accent)
                        .frame(width: 3)
                }
            }
    }
}

extension View {
    /// Wraps the view in a card. Defaults are the web's large card (radius 12, 2rem padding).
    func looreCard(padding: CGFloat = LooreSpacing.xl, radius: CGFloat = LooreRadius.large,
                   accentLeftBorder: Bool = false) -> some View {
        modifier(LooreCardModifier(padding: EdgeInsets(top: padding, leading: padding, bottom: padding, trailing: padding),
                                   radius: radius, accentLeftBorder: accentLeftBorder))
    }

    func looreCard(padding: EdgeInsets, radius: CGFloat = LooreRadius.large, accentLeftBorder: Bool = false) -> some View {
        modifier(LooreCardModifier(padding: padding, radius: radius, accentLeftBorder: accentLeftBorder))
    }

    /// The page background with the web's soft radial glow near the top.
    func loorePageBackground(glow: Bool = false) -> some View {
        background {
            ZStack {
                LooreColor.bgDeep
                if glow {
                    GeometryReader { proxy in
                        RadialGradient(colors: [LooreColor.pageGlow, .clear],
                                       center: UnitPoint(x: 0.5, y: 0.3),
                                       startRadius: 0,
                                       endRadius: max(proxy.size.width, 320) * 0.8)
                    }
                }
            }
            .ignoresSafeArea()
        }
    }

    /// Caps the reading width and centres the column (web max-width 720–800px).
    func looreReadableWidth(_ width: CGFloat = LooreSpacing.maxContentWidth) -> some View {
        frame(maxWidth: width).frame(maxWidth: .infinity)
    }
}

/// Page title: serif 300, 2rem, with the web's 40×1pt accent rule under it.
struct PageHeader<Trailing: View>: View {
    let title: String
    var subtitle: String?
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(title)
                    .font(LooreFont.pageTitle)
                    .foregroundStyle(LooreColor.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 8)
                trailing()
            }
            AccentRule()
            if let subtitle {
                Text(subtitle)
                    .font(LooreFont.body)
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.top, 2)
            }
        }
    }
}

extension PageHeader where Trailing == EmptyView {
    init(title: String, subtitle: String? = nil) {
        self.init(title: title, subtitle: subtitle, trailing: { EmptyView() })
    }
}

/// The short accent hairline under page titles (40 × 1pt, accent at 50%).
struct AccentRule: View {
    var width: CGFloat = 40

    var body: some View {
        Rectangle()
            .fill(LooreColor.accent.opacity(0.5))
            .frame(width: width, height: 1)
            .accessibilityHidden(true)
    }
}

/// A full-width 1pt divider in the border colour.
struct HairlineDivider: View {
    var body: some View {
        Rectangle().fill(LooreColor.border).frame(height: 1).accessibilityHidden(true)
    }
}

/// Small uppercase label with wide tracking ("GET NOTIFIED", "WHAT HAPPENS NEXT").
struct Eyebrow: View {
    let text: String
    var color: Color = LooreColor.accent
    var opacity: Double = 0.7

    var body: some View {
        Text(text.uppercased())
            .font(LooreFont.eyebrow)
            .tracking(1.7)
            .foregroundStyle(color.opacity(opacity))
    }
}

/// Uppercase pill tag (Log cards: "Voice Note", "Pinned", prompt labels).
struct LooreTag: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(LooreFont.tag)
            .tracking(0.8)
            .foregroundStyle(LooreColor.accentDim)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(LooreColor.accentSubtle, in: RoundedRectangle(cornerRadius: LooreRadius.tag))
    }
}

/// Selectable rounded pill (ArtifactsNav bubbles; model and filter pickers).
struct LoorePill: View {
    let title: String
    var isSelected = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(isSelected ? LooreColor.textPrimary : LooreColor.textMuted)
                .padding(.vertical, 6)
                .padding(.horizontal, 14)
                .background(isSelected ? LooreColor.bgCard : Color.clear, in: Capsule())
                .overlay(Capsule().strokeBorder(isSelected ? LooreColor.accent : LooreColor.border, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

/// Plain muted loading text ("Loading...", "Loading log..."), as on the web.
struct LoadingLine: View {
    var text = "Loading..."

    var body: some View {
        Text(text)
            .font(LooreFont.body)
            .foregroundStyle(LooreColor.textMuted)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 40)
    }
}

/// A thin accent ring with a gap, turning: the app's own loading indicator
/// (the system spinner's spokes do not fit the design).
struct SpinnerRing: View {
    var size: CGFloat = 26
    var lineWidth: CGFloat = 2.5

    var body: some View {
        TimelineView(.animation) { context in
            let turn = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 0.9) / 0.9
            Circle()
                .trim(from: 0, to: 0.72)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round))
                .rotationEffect(.degrees(turn * 360))
        }
        .frame(width: size, height: size)
        .accessibilityLabel("Loading")
    }
}

/// A line of accent text for errors (the web replaces the page with it).
struct ErrorLine: View {
    let text: String
    var retry: (() -> Void)?

    var body: some View {
        VStack(spacing: 12) {
            Text(text)
                .font(LooreFont.body)
                .foregroundStyle(LooreColor.accent)
                .multilineTextAlignment(.center)
            if let retry {
                Button("Try again", action: retry).buttonStyle(.looreOutline)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 32)
    }
}

/// The web's form fields: `bg-input` fill, 1px border, radius 8, accent border when focused.
struct LooreTextFieldStyle: TextFieldStyle {
    var isFocused = false

    func _body(configuration: TextField<Self._Label>) -> some View {
        configuration
            .font(LooreFont.sans(15, .light))
            .foregroundStyle(LooreColor.textPrimary)
            .padding(.vertical, 12)
            .padding(.horizontal, 14)
            .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.small))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.small)
                .strokeBorder(isFocused ? LooreColor.accent : LooreColor.border, lineWidth: 1))
    }
}

/// Placeholder text in the muted colour (SwiftUI's default placeholder is too light on dark).
func loorePrompt(_ text: String) -> Text {
    Text(text).foregroundColor(LooreColor.textMuted)
}
