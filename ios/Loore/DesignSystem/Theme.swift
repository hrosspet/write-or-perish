import SwiftUI
import Observation

/// Light/dark choice (web `ThemeContext`, map A §4.3): per device, stored under
/// the web's key `loore_theme`. Until the user chooses, the system setting wins.
/// Once chosen there is no "system" option again, as on the web.
@MainActor
@Observable
final class ThemeManager {
    enum Choice: String { case light, dark }

    private(set) var choice: Choice?
    private let defaults: UserDefaults
    /// Debug `-LooreTheme`: overrides the stored choice for this launch only.
    private let forced: Choice?

    init(defaults: UserDefaults = .standard, forced: String? = nil) {
        self.defaults = defaults
        self.forced = forced.flatMap(Choice.init(rawValue:))
        choice = defaults.string(forKey: DefaultsKey.theme).flatMap(Choice.init(rawValue:))
    }

    /// For `.preferredColorScheme`: nil follows the system.
    var preferredColorScheme: ColorScheme? {
        switch forced ?? choice {
        case .light: return .light
        case .dark: return .dark
        case nil: return nil
        }
    }

    func isLight(system: ColorScheme) -> Bool {
        (preferredColorScheme ?? system) == .light
    }

    /// The "Light mode" switch: flips whatever is showing now and remembers it.
    func toggle(system: ColorScheme) {
        set(isLight(system: system) ? .dark : .light)
    }

    func set(_ newChoice: Choice) {
        choice = newChoice
        defaults.set(newChoice.rawValue, forKey: DefaultsKey.theme)
    }

    func forget() {
        choice = nil
        defaults.removeObject(forKey: DefaultsKey.theme)
    }
}
