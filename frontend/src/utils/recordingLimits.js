// The server's limits on one recording (backend/utils/recording_limits.py).
//
// Audio kept without its text: past the length one recording may have
// transcribed, the server stores the rest of a recording as audio and does
// not turn it into text. Its transcription stream says so with a
// `chunk_error` that carries this code; the recording goes on, and its text
// ends with a note. Not a failure: no error sound, one message per
// recording.
export const UNTRANSCRIBED_MESSAGES = {
  recording_too_long:
    'This recording is longer than six hours. The rest of it is saved as '
    + 'audio, but not turned into text.',
};

export function untranscribedMessage(code) {
  return (code && UNTRANSCRIBED_MESSAGES[code]) || null;
}

// A chunk the server refuses because the recording is past what one
// recording may hold: retrying cannot help. The chunks before it are kept.
const LIMIT_CODES = new Set([
  'chunk_too_large', 'chunk_index_out_of_range', 'recording_too_large',
]);

export const RECORDING_LIMIT_MESSAGE =
  'This recording is larger than Loore accepts. Everything before this part '
  + 'is saved.';

export function isRecordingLimitCode(code) {
  return LIMIT_CODES.has(code);
}

// A new recording refused while other recordings of the account are in
// progress (MAX_RECORDINGS_IN_PROGRESS): shown in the server's words.
export const TOO_MANY_RECORDINGS_CODE = 'too_many_recordings';
export const TOO_MANY_RECORDINGS_MESSAGE =
  'Other recordings of yours are still in progress. Finish one of them, '
  + 'then start this one.';
