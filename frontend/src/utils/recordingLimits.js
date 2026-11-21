// The server's limits on one recording and on an account's recordings
// (backend/utils/recording_limits.py).
//
// Audio kept without its text: past the length one recording may have
// transcribed (two hours, MAX_TRANSCRIBED_SECONDS; Peter, 2026-11-21), the
// server stores the rest of a recording as audio and does not turn it into
// text. Its transcription stream says so with a `chunk_error` that carries
// this code; the recording goes on, and its text ends with a note. Not a
// failure: no error sound, one message per recording.
export const UNTRANSCRIBED_MESSAGES = {
  recording_too_long:
    'This recording is longer than two hours. The rest of it is saved as '
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

// Past what one recording may hold: about four hours
// (`chunk_index_out_of_range`, backend MAX_STREAMING_CHUNKS), its bytes
// (`recording_too_large`), or one chunk's size (`chunk_too_large`). Every
// chunk before it is on the server. For the first two the server takes no
// later chunk either; for `chunk_too_large` it would take a smaller one, but
// the recording stops all the same. The recording stops and is finalized
// with the chunks taken (iPhone `ChunkUploader.limitCodes`). Keyed on the
// code, never the words.
const BOUND_CHUNK_CODES = new Set([
  'chunk_too_large', 'chunk_index_out_of_range', 'recording_too_large',
]);

// The server's words at the four-hour bound (backend LONGEST_RECORDING_TEXT),
// for an answer that carries none.
export const LONGEST_RECORDING_MESSAGE =
  'This recording has reached the longest Loore takes, about four hours, and '
  + 'has stopped. Everything before this is saved.';

// The server's words for a chunk past the recording's bound, or null.
export function boundRefusalMessage(err) {
  const data = err?.response?.data;
  if (!data || !BOUND_CHUNK_CODES.has(data.code)) return null;
  if (data.error) return data.error;
  return data.code === 'chunk_index_out_of_range'
    ? LONGEST_RECORDING_MESSAGE : RECORDING_LIMIT_MESSAGE;
}

// A chunk past the count a finalizing recording announced
// (`chunk_not_announced`): the recording was finished without it. Not the
// bound: nothing is said or stopped, and it is not sent again.
export function isNotAnnouncedChunk(err) {
  return err?.response?.data?.code === 'chunk_not_announced';
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
