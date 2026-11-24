import Foundation

/// The stored sign-in belongs to one install of the app. iOS keeps Keychain
/// items when an app is deleted, so the first launch after an install forgets
/// what an earlier install stored there.
///
/// How the three cases are told apart. Deleting the app deletes its
/// UserDefaults and its files; an update and a restore from a backup keep them.
/// - Marker present (`markerKey` in UserDefaults): this install has run before.
///   Nothing is cleared. A restore from a backup made by a build with this
///   check brings the marker along, so it reads the same.
/// - Marker absent, and the app's data shows an earlier run (the voice upload
///   folder exists, which every launch of every build creates, or one of the
///   app's own UserDefaults keys is set): an update from a build made before
///   this check, or a restore of such a build's backup. Nothing is cleared;
///   the marker is set.
/// - Marker absent and no data at all: the first launch after an install. The
///   stored sign-in of every environment is removed; the marker is set.
enum InstallCheck {
    static let markerKey = "loore_install_seen"

    /// UserDefaults keys builds made before this check may have written.
    static let earlierKeys = [
        DefaultsKey.theme, DefaultsKey.craftMode, DefaultsKey.autoGenerate, DefaultsKey.agenticReply,
        DefaultsKey.lastPrivacyLevel, DefaultsKey.lastAIUsage, DefaultsKey.publicReplyAck,
        DefaultsKey.homeReflectMode, DefaultsKey.homeGleanMode,
        "loore_recording_device_key", "loore_thinking_cue",
    ]

    /// Whether the app's container shows a run before this launch. Read before
    /// anything in this launch creates the upload folder.
    static func hasEarlierData(defaults: UserDefaults, uploadsFolder: URL,
                               fileManager: FileManager = .default) -> Bool {
        if fileManager.fileExists(atPath: uploadsFolder.path) { return true }
        return earlierKeys.contains { defaults.object(forKey: $0) != nil }
    }

    /// Runs the check once per install. Returns true when the stored sign-in was removed.
    @discardableResult
    static func run(defaults: UserDefaults, store: SecureStore, hasEarlierData: Bool) -> Bool {
        if defaults.bool(forKey: markerKey) { return false }
        var cleared = false
        if !hasEarlierData {
            for environment in AppEnvironment.allCases {
                CookieVault(store: store, environment: environment).clear()
            }
            cleared = true
        }
        defaults.set(true, forKey: markerKey)
        return cleared
    }
}
