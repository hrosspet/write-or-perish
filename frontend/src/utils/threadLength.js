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

// Said aloud in Voice mode when a turn is refused for the thread's length.
export const THREAD_LIMIT_SPOKEN = 'This thread is too long for another '
  + 'reply. Start a new thread to keep talking.';

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

// Says *text* with the browser's own speech (no server, no provider): the
// Voice screen's sentence for a refused turn. Returns whether it started.
export function speakSentence(text) {
  try {
    if (typeof window === 'undefined') return false;
    const synth = window.speechSynthesis;
    const Utterance = window.SpeechSynthesisUtterance;
    if (!synth || typeof Utterance !== 'function') return false;
    synth.cancel();
    synth.speak(new Utterance(text));
    return true;
  } catch (e) {
    return false;
  }
}

// The sentence waits for the error sound (three tones, ~0.6 s) to end.
export const SENTENCE_AFTER_SOUND_MS = 700;
let pendingSentence = null;

// The refusal in Voice mode: the error sound every browser plays, then the
// sentence. A browser that allows speech only inside a user gesture (iPhone
// Safari before primeSpeech ran) stays silent for the sentence, never for
// the sound.
export function soundThenSay(text, playSound) {
  stopSpeaking();
  if (playSound) playSound();
  pendingSentence = setTimeout(() => {
    pendingSentence = null;
    speakSentence(text);
  }, SENTENCE_AFTER_SOUND_MS);
}

// A new recording starts: a sentence still waiting or being said stops, so
// it is never recorded into the entry.
export function stopSpeaking() {
  if (pendingSentence) {
    clearTimeout(pendingSentence);
    pendingSentence = null;
  }
  try {
    if (typeof window !== 'undefined' && window.speechSynthesis) {
      window.speechSynthesis.cancel();
    }
  } catch (e) { /* speech unavailable */ }
}

// Called in the Record tap (a user gesture): WebKit lets a page speak
// later, outside a gesture, only after it has spoken inside one. An empty
// utterance, once per page: nothing is heard or recorded.
let speechPrimed = false;
export function primeSpeech() {
  if (speechPrimed) return;
  try {
    if (typeof window === 'undefined') return;
    const synth = window.speechSynthesis;
    const Utterance = window.SpeechSynthesisUtterance;
    if (!synth || typeof Utterance !== 'function') return;
    synth.speak(new Utterance(''));
    speechPrimed = true;
  } catch (e) { /* speech unavailable */ }
}

// Tests only.
export function resetSpeechForTests() {
  speechPrimed = false;
  if (pendingSentence) clearTimeout(pendingSentence);
  pendingSentence = null;
}
