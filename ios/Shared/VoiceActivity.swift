import ActivityKit
import AppIntents
import Foundation

// Compiled into both the app and the LooreLiveActivity extension: the extension
// draws the Live Activity and its buttons; the buttons' intents run in the app's
// process (`LiveActivityIntent`), where `VoiceActivityCommands` reaches the voice
// conversation. The extension has its own no-op `VoiceActivityCommands`.

/// The voice conversation on the lock screen and in the Dynamic Island (#397).
struct VoiceActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        enum Phase: String, Codable, Hashable {
            /// Between turns (a cancelled or failed one): Record.
            case ready
            case recording
            /// Paused by the user.
            case paused
            /// A call or another app took the microphone.
            case interrupted
            /// Stop was pressed; the last chunks upload.
            case sending
            case thinking
            case replying
            /// The reply has played: Record a reply.
            case finished
        }

        var phase: Phase
        /// While recording: when the clock read 0:00 (the lock screen's clock runs by itself).
        var clockStart: Date?
        /// While paused or interrupted: the recording's length in whole seconds.
        var elapsed: Int = 0
    }
}

/// What a Live Activity button asks the voice conversation to do.
enum VoiceActivityCommand: String, Sendable {
    case pause, resume, stop, record
}

/// Pause from the lock screen. The microphone keeps running (samples are
/// dropped), so Resume needs no new microphone start.
@available(iOS 18.0, *)
struct PauseVoiceRecordingIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Pause Recording"
    static let isDiscoverable = false

    func perform() async throws -> some IntentResult {
        await VoiceActivityCommands.run(.pause)
        return .result()
    }
}

/// Resume from the lock screen. An `AudioRecordingIntent`: after an
/// interruption the microphone has to start again, which iOS allows from the
/// background only for this kind of intent (with a Live Activity showing).
@available(iOS 18.0, *)
struct ResumeVoiceRecordingIntent: AudioRecordingIntent, LiveActivityIntent {
    static let title: LocalizedStringResource = "Resume Recording"
    static let isDiscoverable = false

    func perform() async throws -> some IntentResult {
        await VoiceActivityCommands.run(.resume)
        return .result()
    }
}

/// Stop and send from the lock screen.
@available(iOS 18.0, *)
struct StopVoiceRecordingIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Stop and Send"
    static let isDiscoverable = false

    func perform() async throws -> some IntentResult {
        await VoiceActivityCommands.run(.stop)
        return .result()
    }
}

/// Record the next turn from the lock screen, also while the reply plays (it
/// stops, like the Voice screen's Continue button).
@available(iOS 18.0, *)
struct RecordVoiceReplyIntent: AudioRecordingIntent, LiveActivityIntent {
    static let title: LocalizedStringResource = "Record a Reply"
    static let isDiscoverable = false

    func perform() async throws -> some IntentResult {
        await VoiceActivityCommands.run(.record)
        return .result()
    }
}
