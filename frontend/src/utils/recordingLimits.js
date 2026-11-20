// The server's limits on one recording and on an account's recordings
// (backend/utils/recording_limits.py).
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

// A chunk the server refuses for good: the recording is past what one
// recording may hold, or it held no audio for too long and its first chunk
// is refused as a new start would be (the usage limit, the account's open
// recordings). Retrying cannot help; the chunks before it are kept.
// `usage_catching_up` (503): Loore is still transcribing the account's
// earlier audio; not the usage limit, so the banner and the capped state are
// not set. The recording stops in the server's words, its audio kept in the
// page; a new start goes through once the transcription caught up.
const FINAL_CHUNK_CODES = new Set([
  'chunk_too_large', 'chunk_index_out_of_range', 'recording_too_large',
  'too_many_recordings', 'usage_catching_up',
]);

export const RECORDING_LIMIT_MESSAGE =
  'This recording is larger than Loore accepts. Everything before this part '
  + 'is saved.';

export function isRecordingLimitCode(code) {
  return FINAL_CHUNK_CODES.has(code);
}

const SPEND_CAP_ERROR = 'monthly_spend_limit_reached';

// The server's words for a refused chunk, start or continue that no retry
// can help, or null for any other failure.
export function finalRefusalMessage(err) {
  const data = err?.response?.data;
  if (!data) return null;
  if (data.error === SPEND_CAP_ERROR) return data.message || null;
  if (FINAL_CHUNK_CODES.has(data.code)) {
    return data.error || RECORDING_LIMIT_MESSAGE;
  }
  return null;
}

// A new recording refused while the account holds several open
// recordings, after many starts without audio, or while Loore is still
// transcribing the account's earlier audio (`usage_catching_up`, not the
// usage limit): shown in the server's words; the person can try again.
const START_REFUSAL_CODES = new Set([
  'too_many_recordings', 'too_many_starts', 'usage_catching_up',
]);

export const TOO_MANY_RECORDINGS_MESSAGE =
  'You have several recordings that are not finished. Save, finish or '
  + 'discard one of them, then start a new one.';

export function isStartRefusal(err) {
  return START_REFUSAL_CODES.has(err?.response?.data?.code);
}
