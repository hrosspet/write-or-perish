import Foundation

/// The server's limits on one recording (`backend/utils/recording_limits.py`,
/// web `utils/recordingLimits.js`).
///
/// Past the length one recording may have transcribed (two hours; Peter,
/// 2026-11-21), the server keeps the rest of a recording as audio without
/// turning it into text. The transcription stream says so with a `chunk_error`
/// that carries this code; the recording goes on, and its text ends with a
/// note. Not a failure: no error sound, one message per recording.
enum RecordingLimits {
    static let tooLongCode = "recording_too_long"

    /// The most audio of one recording that is turned into text (backend
    /// `MAX_TRANSCRIBED_SECONDS`, Peter 2026-11-21: two hours). Voice, which
    /// does not read the transcription stream, says `tooLongMessage` once its
    /// recording has this much audio (#736): the server counts the same thing,
    /// the recording's decoded audio, never the time spent paused.
    static let transcribedSeconds: Double = 2 * 60 * 60

    static let tooLongMessage = "This recording is longer than two hours. The rest of it is saved as audio, but not turned into text."

    /// The 59-minute warning (#127; web `useLongRecordingWarning`). Nothing
    /// ends a recording at an hour: it names the two-hour transcription limit.
    static let longRecordingTitle = "You’ve been recording for 59 minutes"
    static let longRecordingAdvice = "One recording is turned into text up to two hours; for longer, continue in a new recording."
    static let longRecordingWarning = "\(longRecordingTitle). \(longRecordingAdvice)"

    /// The message for a `chunk_error` code, or nil for a failure.
    static func untranscribedMessage(_ code: String?) -> String? {
        switch code {
        case tooLongCode?:
            return tooLongMessage
        default:
            return nil
        }
    }
}
