import Foundation

/// Choices this phone remembers for the account that made them: the last
/// privacy level and AI usage of a new entry, the "don't show again" answer of
/// the public-reply question, and the Agentic Reply / Auto-generate switches.
/// They stay with that account: when a different account signs in here they
/// are removed, and its forms start from its own defaults. The same account
/// signing in again (after a sign-out or an ended session) finds its choices
/// as it left them. Not included: theme, craft mode, the Voice / Text choice of
/// the home cards, the thinking cue and the recording device key, which say
/// nothing about an account. The web does the same (`utils/deviceChoices.js`).
enum DeviceChoices {
    static let keys = [
        DefaultsKey.lastPrivacyLevel, DefaultsKey.lastAIUsage, DefaultsKey.publicReplyAck,
        DefaultsKey.agenticReply, DefaultsKey.autoGenerate,
    ]
    /// The id of the account the choices belong to.
    static let ownerKey = "loore_choices_owner"

    private static func clear(_ defaults: UserDefaults) {
        for key in keys { defaults.removeObject(forKey: key) }
    }

    /// `userId` is signed in. Choices of another account are removed. Choices
    /// stored before the owner was recorded are kept: they were made in this
    /// phone's current sign-in.
    static func signedIn(userId: Int, defaults: UserDefaults) {
        if let owner = defaults.object(forKey: ownerKey) as? Int, owner != userId {
            clear(defaults)
        }
        defaults.set(userId, forKey: ownerKey)
    }

    /// The app opened with nobody signed in. Choices whose owner was never
    /// recorded (stored by a build before this) cannot be matched to the next
    /// sign-in, so they are removed.
    static func signedOutAtLaunch(defaults: UserDefaults) {
        if defaults.object(forKey: ownerKey) == nil { clear(defaults) }
    }
}
