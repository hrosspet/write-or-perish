import Foundation

/// The server's limits on one recording (`backend/utils/recording_limits.py`,
/// web `utils/recordingLimits.js`).
///
/// At the monthly usage limit, or past the length one recording may have
/// transcribed, the server keeps the rest of a recording as audio without
/// turning it into text. The transcription stream says so with a `chunk_error`
/// that carries one of these codes; the recording goes on, and its text ends
/// with a note. Not a failure: no error sound, one message per recording.
enum RecordingLimits {
    static let spendCapCode = "monthly_spend_limit_reached"
    static let tooLongCode = "recording_too_long"

    /// The message for a `chunk_error` code, or nil for a failure.
    static func untranscribedMessage(_ code: String?) -> String? {
        switch code {
        case spendCapCode?:
            return "You’ve reached your monthly usage limit. The rest of this recording is saved as audio, but not turned into text."
        case tooLongCode?:
            return "This recording is longer than six hours. The rest of it is saved as audio, but not turned into text."
        default:
            return nil
        }
    }
}
