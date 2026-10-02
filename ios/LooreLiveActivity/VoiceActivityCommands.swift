/// The extension's side of the shared intents. Their `perform` runs in the app
/// (they are `LiveActivityIntent`s), where the real `VoiceActivityCommands`
/// reaches the conversation; this one only lets the extension compile them.
enum VoiceActivityCommands {
    static func run(_ command: VoiceActivityCommand) async {}
}
