import SwiftUI
import UIKit

// Design tokens from `frontend/src/index.css` (map A §5). Views use these, never
// literal colours, font names or magic spacing values.

extension UIColor {
    /// `0xRRGGBB` plus alpha.
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255,
                  alpha: alpha)
    }

    /// A colour that follows the interface style: dark tokens by default, light
    /// tokens when the trait collection is light (the web's `[data-theme="light"]`).
    static func looreDynamic(dark: UInt32, light: UInt32, darkAlpha: CGFloat = 1, lightAlpha: CGFloat = 1) -> UIColor {
        UIColor { traits in
            traits.userInterfaceStyle == .light
                ? UIColor(hex: light, alpha: lightAlpha)
                : UIColor(hex: dark, alpha: darkAlpha)
        }
    }
}

/// Colour tokens. 8-digit web hex values (`#RRGGBBAA`) are expressed as alpha.
enum LooreColor {
    static let bgDeepUI = UIColor.looreDynamic(dark: 0x0E0D0B, light: 0xF5EFE4)
    static let bgSurfaceUI = UIColor.looreDynamic(dark: 0x181714, light: 0xEBE4D6)
    static let bgCardUI = UIColor.looreDynamic(dark: 0x211F1B, light: 0xE0D8C7)
    static let textPrimaryUI = UIColor.looreDynamic(dark: 0xEDE8DD, light: 0x1F1C17)
    static let textMutedUI = UIColor.looreDynamic(dark: 0x736B5F, light: 0x857C6C)
    static let accentUI = UIColor.looreDynamic(dark: 0xC4956A, light: 0xA87547)
    static let borderUI = UIColor.looreDynamic(dark: 0x302C27, light: 0xCDC4B1)

    /// Page background; also button/input fill inside dialogs.
    static let bgDeep = Color(bgDeepUI)
    /// Bars (the web's NavBar).
    static let bgSurface = Color(bgSurfaceUI)
    /// Cards, dialogs, toasts, menus.
    static let bgCard = Color(bgCardUI)
    static let bgCardHover = Color(UIColor.looreDynamic(dark: 0x282520, light: 0xD6CDB9))
    /// Text fields and editors.
    static let bgInput = Color(UIColor.looreDynamic(dark: 0x151311, light: 0xFBF7EE))

    static let textPrimary = Color(textPrimaryUI)
    static let textSecondary = Color(UIColor.looreDynamic(dark: 0xA89F91, light: 0x5D564A))
    static let textMuted = Color(textMutedUI)

    /// Amber: active items, primary outline buttons, links, error lines.
    static let accent = Color(accentUI)
    static let accentHover = Color(UIColor.looreDynamic(dark: 0xD4A574, light: 0x8A5D36))
    /// Tag text and hairlines.
    static let accentDim = Color(UIColor.looreDynamic(dark: 0xA07A55, light: 0xBB8C66))
    /// Glows, `mark` highlight, pressed buttons (#c4956a40 / #a8754740).
    static let accentGlow = Color(UIColor.looreDynamic(dark: 0xC4956A, light: 0xA87547,
                                                       darkAlpha: 0x40 / 255, lightAlpha: 0x40 / 255))
    /// Tag background, button hover fill (#c4956a15 / #a8754718).
    static let accentSubtle = Color(UIColor.looreDynamic(dark: 0xC4956A, light: 0xA87547,
                                                         darkAlpha: 0x15 / 255, lightAlpha: 0x18 / 255))

    static let border = Color(borderUI)
    static let borderHover = Color(UIColor.looreDynamic(dark: 0x433E36, light: 0xB3A98F))

    static let success = Color(UIColor.looreDynamic(dark: 0x4ADE80, light: 0x1F8A4D))
    static let error = Color(UIColor.looreDynamic(dark: 0xE74C3C, light: 0xC0392B))
    static let warning = Color(UIColor.looreDynamic(dark: 0xFFC107, light: 0xB88600))
    static let info = Color(UIColor.looreDynamic(dark: 0x61DAFB, light: 0x0B6E9A))

    /// Login/landing radial backdrop.
    static let gradientOverlay1 = Color(UIColor.looreDynamic(dark: 0x1A150F, light: 0xE8DFCA))
    static let gradientOverlay2 = Color(UIColor.looreDynamic(dark: 0x1A130D, light: 0xD4C8AA,
                                                             darkAlpha: 0x08 / 255, lightAlpha: 0x20 / 255))

    /// Literal colours the web uses outside the tokens (map A §5.1).
    /// The page glow is the dark accent in both themes (`rgba(196,149,106,0.06)`).
    static let pageGlow = Color(UIColor(hex: 0xC4956A, alpha: 0.06))
    /// Dialog backdrop `rgba(0,0,0,0.7)`.
    static let dialogBackdrop = Color.black.opacity(0.7)
    static let shadow = Color.black
}

/// Spacing scale in points (1rem = 16pt, matching the web on a phone).
enum LooreSpacing {
    static let xxs: CGFloat = 4
    static let xs: CGFloat = 8
    static let sm: CGFloat = 12
    static let md: CGFloat = 16
    static let lg: CGFloat = 24
    static let xl: CGFloat = 32
    static let xxl: CGFloat = 48
    /// Page gutter (web: 24px).
    static let gutter: CGFloat = 24
    /// Card padding (web Bubble: 1.6rem 1.8rem).
    static let cardVertical: CGFloat = 25.6
    static let cardHorizontal: CGFloat = 28.8
    /// Dialog padding (web: 2rem).
    static let dialog: CGFloat = 32
    /// Reading width cap for page content (web 720–800px).
    static let maxContentWidth: CGFloat = 760
}

enum LooreRadius {
    /// Buttons and inputs.
    static let control: CGFloat = 6
    /// Toasts, menus, text areas, spend banner.
    static let small: CGFloat = 8
    /// Bubble cards and the focal node.
    static let card: CGFloat = 10
    /// Dialogs and large cards.
    static let large: CGFloat = 12
    /// Tags.
    static let tag: CGFloat = 4
    /// Pills (ArtifactsNav).
    static let pill: CGFloat = 16
}

/// Motion (map A §5.4): slow fades on the web's ease-out curve.
enum LooreMotion {
    /// `cubic-bezier(0.22, 1, 0.36, 1)`.
    static func easeOut(_ duration: Double) -> Animation {
        .timingCurve(0.22, 1, 0.36, 1, duration: duration)
    }

    static let reveal = easeOut(0.9)
    static let quick = Animation.easeOut(duration: 0.25)
    static let micro = Animation.easeInOut(duration: 0.2)
}

/// Fade-and-rise entrance (web `utils/Fade.js`): opacity 0→1 and 28pt → 0 over
/// 0.9 s after `delay`. With Reduce Motion it is a plain short fade.
struct FadeIn: ViewModifier {
    var delay: Double = 0
    var offset: CGFloat = 28
    @State private var visible = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(visible ? 1 : 0)
            .offset(y: visible || reduceMotion ? 0 : offset)
            .onAppear {
                withAnimation(reduceMotion ? .easeOut(duration: 0.2) : LooreMotion.reveal.delay(delay)) {
                    visible = true
                }
            }
    }
}

extension View {
    func looreFadeIn(delay: Double = 0, offset: CGFloat = 28) -> some View {
        modifier(FadeIn(delay: delay, offset: offset))
    }
}
