import Foundation

/// Debug-only launch arguments (design doc §12). Release builds ignore every one
/// of them: `LaunchOptions.current` is empty outside `#if DEBUG`.
///
/// | Argument | Effect |
/// |---|---|
/// | `-LooreEnvironment local\|staging\|production` | backend for this launch |
/// | `-LooreSessionCookie <value>` | inject a Flask `session` cookie (signed in the backend container) |
/// | `-LooreRoute /node/123` | open a screen directly after sign-in |
/// | `-LooreDebugAudioFile <path>` | feed an audio file into the recorder instead of the mic (M3) |
/// | `-LooreDebugVoiceAutoStart YES` | with the audio file on `/voice`: record at once (read from UserDefaults) |
/// | `-LooreDebugListenNode <id>` | play a node's audio in the global player at launch (UserDefaults) |
/// | `-LooreTheme light\|dark` | force the theme for this launch (screenshots) |
/// | `-LooreResetState YES` | forget stored cookies and preferences at launch |
/// | `-LooreSkipUpdates YES` | do not fetch `/api/updates` at launch (screenshots) |
struct LaunchOptions: Equatable, Sendable {
    var environment: AppEnvironment?
    var sessionCookie: String?
    var route: String?
    var debugAudioFile: String?
    var theme: String?
    var resetState = false
    var skipUpdates = false

    static let current: LaunchOptions = {
        #if DEBUG
        return LaunchOptions(arguments: ProcessInfo.processInfo.arguments)
        #else
        return LaunchOptions()
        #endif
    }()

    init() {}

    /// Parses `-Key value` pairs. Unknown arguments are ignored.
    init(arguments: [String]) {
        var values: [String: String] = [:]
        var i = 0
        while i < arguments.count {
            let arg = arguments[i]
            if arg.hasPrefix("-Loore"), i + 1 < arguments.count {
                values[String(arg.dropFirst())] = arguments[i + 1]
                i += 2
            } else {
                i += 1
            }
        }
        environment = values["LooreEnvironment"].flatMap { AppEnvironment(rawValue: $0.lowercased()) }
        sessionCookie = values["LooreSessionCookie"].flatMap { $0.isEmpty ? nil : $0 }
        route = values["LooreRoute"].flatMap { $0.isEmpty ? nil : $0 }
        debugAudioFile = values["LooreDebugAudioFile"].flatMap { $0.isEmpty ? nil : $0 }
        theme = values["LooreTheme"].flatMap { ["light", "dark"].contains($0) ? $0 : nil }
        resetState = Self.isTruthy(values["LooreResetState"])
        skipUpdates = Self.isTruthy(values["LooreSkipUpdates"])
    }

    private static func isTruthy(_ value: String?) -> Bool {
        guard let v = value?.lowercased() else { return false }
        return ["1", "yes", "true"].contains(v)
    }
}
