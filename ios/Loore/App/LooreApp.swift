import SwiftUI
import UIKit

@main
struct LooreApp: App {
    @UIApplicationDelegateAdaptor(LooreAppDelegate.self) private var appDelegate
    @State private var appState = AppState()

    init() {
        BarAppearance.apply()
        _ = WakeSignal.foreground // start listening for foreground returns
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(appState)
                .preferredColorScheme(appState.theme.preferredColorScheme)
                .tint(LooreColor.accent)
        }
    }
}

/// UIKit bar styling: tab bar and navigation bars on the surface colour, serif titles.
enum BarAppearance {
    static func apply() {
        let nav = UINavigationBarAppearance()
        nav.configureWithOpaqueBackground()
        nav.backgroundColor = LooreColor.bgDeepUI
        nav.shadowColor = .clear
        // Bar fonts follow the text size chosen at launch (capped so the bars keep their height).
        nav.titleTextAttributes = [
            .foregroundColor: LooreColor.textPrimaryUI,
            .font: scaled(UIFont.loore(LooreFontFace.serifName(.regular), size: 20), .headline, max: 28),
        ]
        nav.largeTitleTextAttributes = [
            .foregroundColor: LooreColor.textPrimaryUI,
            .font: scaled(UIFont.loore(LooreFontFace.serifName(.light), size: 34), .largeTitle, max: 44),
        ]
        let navButton = UIBarButtonItemAppearance()
        navButton.normal.titleTextAttributes = [
            .font: scaled(UIFont.loore(LooreFontFace.sansName(.regular), size: 16), .body, max: 24),
        ]
        nav.buttonAppearance = navButton
        nav.backButtonAppearance = navButton
        UINavigationBar.appearance().standardAppearance = nav
        UINavigationBar.appearance().scrollEdgeAppearance = nav
        UINavigationBar.appearance().compactAppearance = nav

        let tab = UITabBarAppearance()
        tab.configureWithOpaqueBackground()
        tab.backgroundColor = LooreColor.bgSurfaceUI
        tab.shadowColor = LooreColor.borderUI
        let item = UITabBarItemAppearance()
        item.normal.iconColor = LooreColor.textMutedUI
        item.normal.titleTextAttributes = [
            .foregroundColor: LooreColor.textMutedUI,
            .font: UIFont.loore(LooreFontFace.sansName(.light), size: 10.5),
        ]
        item.selected.iconColor = LooreColor.accentUI
        item.selected.titleTextAttributes = [
            .foregroundColor: LooreColor.accentUI,
            .font: UIFont.loore(LooreFontFace.sansName(.regular), size: 10.5),
        ]
        tab.stackedLayoutAppearance = item
        tab.inlineLayoutAppearance = item
        tab.compactInlineLayoutAppearance = item
        UITabBar.appearance().standardAppearance = tab
        UITabBar.appearance().scrollEdgeAppearance = tab
    }

    private static func scaled(_ font: UIFont, _ style: UIFont.TextStyle, max: CGFloat) -> UIFont {
        UIFontMetrics(forTextStyle: style).scaledFont(for: font, maximumPointSize: max)
    }
}
