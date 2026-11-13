import { notifySpendBlocked } from './spendCap';

// The server's limits on one recording (backend/utils/recording_limits.py).
//
// Audio kept without its text: at the monthly usage limit, or past the
// length one recording may have transcribed, the server stores the rest of
// a recording as audio and does not turn it into text. Its transcription
// stream says so with a `chunk_error` that carries one of these codes; the
// recording goes on, and its text ends with a note. Not a failure: no
// error sound, one message per recording.
export const UNTRANSCRIBED_MESSAGES = {
  monthly_spend_limit_reached:
    "You've reached your monthly usage limit. The rest of this recording is "
    + 'saved as audio, but not turned into text.',
  recording_too_long:
    'This recording is longer than six hours. The rest of it is saved as '
    + 'audio, but not turned into text.',
};

export function untranscribedMessage(code) {
  return (code && UNTRANSCRIBED_MESSAGES[code]) || null;
}

// The message for a `chunk_error` code of audio kept without its text, or
// null for any other error. At the usage limit it also raises the spend-cap
// banner, as a refused request does.
export function noticeUntranscribed(code) {
  const message = untranscribedMessage(code);
  if (message && code === 'monthly_spend_limit_reached') notifySpendBlocked();
  return message;
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
