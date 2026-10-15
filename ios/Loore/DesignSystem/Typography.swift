import SwiftUI
import UIKit

/// The bundled typefaces (SIL OFL; licence files in Resources/Fonts).
/// - Cormorant Garamond: the Google Fonts repository's variable fonts; each named
///   instance has its own PostScript name ("CormorantGaramond-Light"), which
///   CoreText resolves to the right weight.
/// - Outfit: the upstream project's static TTFs (Outfitio/Outfit-Fonts). The
///   Google Fonts variable file's named instances carry no PostScript names, so
///   iOS cannot address them by weight (DesignSystemTests caught this).
enum LooreFontFace {
    enum SerifWeight: String {
        case light = "Light", regular = "Regular", medium = "Medium", semibold = "SemiBold", bold = "Bold"
    }

    enum SansWeight: String {
        case extraLight = "ExtraLight", light = "Light", regular = "Regular", medium = "Medium",
             semibold = "SemiBold", bold = "Bold"
    }

    /// Cormorant Garamond: headings, page titles, dialog titles.
    static func serifName(_ weight: SerifWeight, italic: Bool = false) -> String {
        if italic {
            return weight == .regular ? "CormorantGaramond-Italic" : "CormorantGaramond-\(weight.rawValue)Italic"
        }
        return "CormorantGaramond-\(weight.rawValue)"
    }

    /// Outfit: body and UI.
    static func sansName(_ weight: SansWeight) -> String {
        "Outfit-\(weight.rawValue)"
    }

    /// Every face the app uses (checked by a unit test).
    static let allNames: [String] =
        [SerifWeight.light, .regular, .medium, .semibold, .bold].map { serifName($0) }
        + [SerifWeight.light, .regular, .medium].map { serifName($0, italic: true) }
        + [SansWeight.extraLight, .light, .regular, .medium, .semibold, .bold].map { sansName($0) }
}

/// Text styles. Sizes are the web's rem values at 16pt per rem; they scale with
/// Dynamic Type relative to the given text style.
enum LooreFont {
    static func serif(_ size: CGFloat, _ weight: LooreFontFace.SerifWeight = .light, italic: Bool = false,
                      relativeTo style: Font.TextStyle = .title) -> Font {
        .custom(LooreFontFace.serifName(weight, italic: italic), size: size, relativeTo: style)
    }

    static func sans(_ size: CGFloat, _ weight: LooreFontFace.SansWeight = .light,
                     relativeTo style: Font.TextStyle = .body) -> Font {
        .custom(LooreFontFace.sansName(weight), size: size, relativeTo: style)
    }

    static func rem(_ value: CGFloat) -> CGFloat { value * 16 }

    /// Monospaced text (tokens, prompt sources) that scales with Dynamic Type.
    static func mono(_ size: CGFloat, relativeTo style: Font.TextStyle = .body) -> Font {
        .custom("Menlo", size: size, relativeTo: style)
    }

    /// Outfit has no italic face; browsers slant it synthetically (about 12°)
    /// for `font-style: italic`, and so does this. Scaled for Dynamic Type
    /// when created (views rebuild on a size change).
    static func sansOblique(_ size: CGFloat, _ weight: LooreFontFace.SansWeight = .light,
                            relativeTo style: Font.TextStyle = .body) -> Font {
        let slant = CGAffineTransform(a: 1, b: 0, c: 0.21, d: 1, tx: 0, ty: 0)
        let descriptor = UIFontDescriptor(fontAttributes: [.name: LooreFontFace.sansName(weight), .matrix: slant])
        let base = UIFont(descriptor: descriptor, size: size)
        return Font(UIFontMetrics(forTextStyle: uiTextStyle(style)).scaledFont(for: base))
    }

    /// The reading serif for long stored text, where the web sets Newsreader
    /// (`--reading-serif`): the app bundles no Newsreader, so this is the
    /// system's text serif, New York, with real bold and italic faces. Scaled
    /// for Dynamic Type when created, like `sansOblique`.
    static func reading(_ size: CGFloat, bold: Bool = false, italic: Bool = false,
                        relativeTo style: Font.TextStyle = .body) -> Font {
        Font(UIFontMetrics(forTextStyle: uiTextStyle(style)).scaledFont(for: readingUIFont(size, bold: bold, italic: italic)))
    }

    static func readingUIFont(_ size: CGFloat, bold: Bool = false, italic: Bool = false) -> UIFont {
        let system = UIFont.systemFont(ofSize: size, weight: bold ? .bold : .regular)
        var descriptor = system.fontDescriptor.withDesign(.serif) ?? system.fontDescriptor
        if italic, let slanted = descriptor.withSymbolicTraits(descriptor.symbolicTraits.union(.traitItalic)) {
            descriptor = slanted
        }
        return UIFont(descriptor: descriptor, size: size)
    }

    /// The reading serif's own line height as a multiple of its size (for CSS
    /// line-height conversions; Outfit's is about 1.26).
    static let readingLineHeight: CGFloat = readingUIFont(100).lineHeight / 100

    private static func uiTextStyle(_ style: Font.TextStyle) -> UIFont.TextStyle {
        switch style {
        case .largeTitle: return .largeTitle
        case .title: return .title1
        case .title2: return .title2
        case .title3: return .title3
        case .headline: return .headline
        case .subheadline: return .subheadline
        case .callout: return .callout
        case .footnote: return .footnote
        case .caption: return .caption1
        case .caption2: return .caption2
        default: return .body
        }
    }

    // Presets (map A §5.2)
    /// Page titles: serif 300, 2rem.
    static let pageTitle = serif(32, .light, relativeTo: .largeTitle)
    /// Home hero "What's on your mind?": serif 300, `clamp(1.8rem, 4.5vw, 2.8rem)` = 1.8rem on a phone.
    static let hero = serif(28.8, .light, relativeTo: .largeTitle)
    /// Dialog titles: serif 400, 1.4rem.
    static let dialogTitle = serif(22.4, .regular, relativeTo: .title2)
    /// Card titles (Home cards): serif 1.5rem.
    static let cardTitle = serif(24, .regular, relativeTo: .title2)
    /// Body: sans 300, .92rem.
    static let body = sans(14.7, .light, relativeTo: .body)
    /// Larger reading text: sans 300, 1.05rem.
    static let bodyLarge = sans(16.8, .light, relativeTo: .body)
    /// Menu rows and nav links: sans 300, .85rem.
    static let menu = sans(13.6, .light, relativeTo: .callout)
    /// Buttons: sans 400, .9rem.
    static let button = sans(14.4, .regular, relativeTo: .callout)
    /// Meta, footers: sans 300, .75rem.
    static let meta = sans(12, .light, relativeTo: .caption)
    /// Eyebrow labels: sans, .7rem, uppercase, wide tracking.
    static let eyebrow = sans(11.2, .regular, relativeTo: .caption2)
    /// Tags: .65rem uppercase.
    static let tag = sans(10.4, .regular, relativeTo: .caption2)
    /// Wordmark "LOORE": serif 300, 1.1rem, tracking .35em.
    static let wordmark = serif(17.6, .light, relativeTo: .headline)
}

extension UIFont {
    /// UIKit counterpart for bar appearances.
    static func loore(_ name: String, size: CGFloat) -> UIFont {
        UIFont(name: name, size: size) ?? .systemFont(ofSize: size)
    }
}
