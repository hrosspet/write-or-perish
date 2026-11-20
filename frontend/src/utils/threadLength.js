// Long threads (#648; Peter, 2026-11-12). Every AI reply reads the whole
// thread above it, so the server measures the next reply's prompt
// (backend/utils/thread_size.py): from 90 % of the limit the thread's API
// response says `thread_length: "notice"`, at the limit "limit", and a
// reply asked for there is refused (409, or `llm_error_code` beside an
// entry that was saved) with the code below. No price, no token numbers.
// The iPhone app shows the same texts.

export const THREAD_TOO_LONG_CODE = 'thread_too_long';

export const THREAD_NOTICE_TEXT = 'This thread is getting long. Each AI reply '
  + 'reads all of it, so AI replies here will stop soon. You can start a new '
  + 'thread to keep going.';

// The server's text (THREAD_TOO_LONG_MESSAGE), for when none came with the
// answer.
export const THREAD_LIMIT_TEXT = 'This thread is too long for another AI '
  + 'reply. Each reply reads the whole thread, including what it quotes, and '
  + 'this one has grown too large. You can still write here. To keep talking '
  + 'with AI, start a new thread.';

// The sentence said aloud in Voice mode when a turn is refused for the
// thread's length, and the speech it goes through, now shared by every
// refusal of a Voice turn (#651): see spokenRefusal.js.
export {
  THREAD_LIMIT_SPOKEN, SENTENCE_AFTER_SOUND_MS, speakSentence, soundThenSay,
  stopSpeaking, primeSpeech, resetSpeechForTests,
} from './spokenRefusal';

// Where a new thread starts: Text mode for the thread page, a fresh Voice
// session on the Voice screen.
export const NEW_THREAD_PATH = '/textmode';
export const NEW_VOICE_THREAD_PATH = '/voice';

// The server's refusal of a reply for the thread's length (409).
export const isThreadTooLong = (err) =>
  err?.response?.status === 409
  && err?.response?.data?.code === THREAD_TOO_LONG_CODE;

// A 2xx that saved the entry but skipped its reply for the thread's length.
export const replySkippedForLength = (data) =>
  data?.llm_error_code === THREAD_TOO_LONG_CODE;
