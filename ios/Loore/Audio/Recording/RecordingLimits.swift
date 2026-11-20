import Foundation

/// The server's limits on one recording (`backend/utils/recording_limits.py`,
/// web `utils/recordingLimits.js`).
///
/// Past the length one recording may have transcribed, the server keeps the
/// rest of a recording as audio without turning it into text. The
/// transcription stream says so with a `chunk_error` that carries this code;
/// the recording goes on, and its text ends with a note. Not a failure: no
/// error sound, one message per recording.
enum RecordingLimits {
    static let tooLongCode = "recording_too_long"

    /// The message for a `chunk_error` code, or nil for a failure.
    static func untranscribedMessage(_ code: String?) -> String? {
        switch code {
        case tooLongCode?:
            return "This recording is longer than six hours. The rest of it is saved as audio, but not turned into text."
        default:
            return nil
        }
    }
}
