// A refused Voice turn is said aloud (#651; the thread's length first,
// #648): someone talking hands-free, or with the phone locked, does not see
// the message on the screen. One fixed sentence per refusal, by the code
// the server answers with. It is said in Loore's voice from a cached clip
// (GET /api/voice/refusal/<code>.mp3, made once per voice, as the approval
// announcement is), never as part of a reply's audio: the Voice session
// plays it when the refusal comes and stops it when a new recording starts.
// Where the clip can't play (offline, no clip, a browser that starts audio
// only inside a tap, as Safari does) the browser's own speech says the same
// sentence. The sentences are backend/utils/voice_refusals.py's, and the
// iPhone app's: plain words, no numbers, no prices.

export const SPEND_CAP_REFUSAL = 'monthly_spend_limit_reached';
export const AI_USAGE_REFUSAL = 'ai_usage_none';
export const APPROVAL_REFUSAL = 'approval_pending';
export const LENGTH_REFUSAL = 'thread_too_long';

// Said aloud in Voice mode when a turn is refused for the thread's length.
export const THREAD_LIMIT_SPOKEN = 'This thread is too long for another '
  + 'reply. Start a new thread to keep talking.';

export const REFUSAL_SENTENCES = {
  [SPEND_CAP_REFUSAL]: "You've reached your usage limit, so Loore can't "
    + 'reply until it lifts.',
  [AI_USAGE_REFUSAL]: "AI usage is set to None, so Loore can't reply here.",
  [APPROVAL_REFUSAL]: 'A change to your data is waiting for your approval. '
    + 'Approve or reject it on the screen to continue.',
  [LENGTH_REFUSAL]: THREAD_LIMIT_SPOKEN,
};

// The refusal a sentence belongs to, or null.
const refusalOf = (text) => Object.keys(REFUSAL_SENTENCES)
  .find((code) => REFUSAL_SENTENCES[code] === text) || null;

export const refusalClipUrl = (code) => (
  `${process.env.REACT_APP_BACKEND_URL || ''}/api/voice/refusal/${code}.mp3`);

// INTRODUCED CONSTANT: how long the clip may take to start before the
// browser's speech says the sentence instead. A clip already made starts
// within a second; the first request for a voice makes it (one speech
// call, about two seconds). A refusal said much later than this is no
// longer an answer to the press. Not measured.
export const CLIP_START_TIMEOUT_MS = 4000;

// Says *text* with the browser's own speech (no server, no provider).
// Returns whether it started.
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
// The clip playing or loading, its wait for the browser's speech
// (CLIP_START_TIMEOUT_MS), and the sentence it belongs to: stopSpeaking
// bumps the token and clears the wait, so a clip or a fallback that comes
// late says nothing.
let currentClip = null;
let clipTimer = null;
let sentenceToken = 0;

function dropClip() {
  clearTimeout(clipTimer);
  clipTimer = null;
  const el = currentClip;
  currentClip = null;
  if (!el) return;
  try {
    el.pause();
    el.removeAttribute('src');
    el.load();
  } catch (e) { /* nothing to stop */ }
}

// The sentence now: its clip when it is a refusal's, else (or when the clip
// can't play, or doesn't start in time) the browser's speech.
function sayNow(text, token) {
  const code = refusalOf(text);
  const AudioElement = typeof window !== 'undefined' ? window.Audio : null;
  if (!code || typeof AudioElement !== 'function') {
    speakSentence(text);
    return;
  }
  let el;
  try {
    el = new AudioElement(refusalClipUrl(code));
  } catch (e) {
    speakSentence(text);
    return;
  }
  currentClip = el;
  let settled = false;
  let timer = null;
  const fallBack = () => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    if (currentClip === el) dropClip();
    if (token === sentenceToken) speakSentence(text);
  };
  const started = () => {
    settled = true;
    clearTimeout(timer);
  };
  timer = setTimeout(fallBack, CLIP_START_TIMEOUT_MS);
  clipTimer = timer;
  el.addEventListener('error', fallBack);
  el.addEventListener('ended', () => {
    if (currentClip === el) currentClip = null;
  });
  let playing;
  try {
    playing = el.play();
  } catch (e) {
    fallBack();
    return;
  }
  // Refused (no tap to start audio from, NotAllowedError) or no source.
  if (playing && typeof playing.then === 'function') {
    playing.then(started, fallBack);
  } else {
    fallBack();
  }
}

// The refusal in Voice mode: *playSound* (the error sound every browser
// plays, before the thread's length), then the sentence once the sound is
// over. A browser that allows speech only inside a user gesture (iPhone
// Safari before primeSpeech ran) and plays no clip outside one stays silent
// for the sentence, never for the sound.
export function soundThenSay(text, playSound) {
  stopSpeaking();
  const token = sentenceToken;
  if (playSound) playSound();
  pendingSentence = setTimeout(() => {
    pendingSentence = null;
    if (token === sentenceToken) sayNow(text, token);
  }, playSound ? SENTENCE_AFTER_SOUND_MS : 0);
}

// The refusal *code*'s sentence (see REFUSAL_SENTENCES); nothing for a
// code that has none.
export function sayRefusal(code, playSound) {
  const text = REFUSAL_SENTENCES[code];
  if (text) soundThenSay(text, playSound);
}

// A new recording starts, or the Voice screen closes: a sentence still
// waiting, loading or being said stops (with its wait for the browser's
// speech), so it is never recorded into the entry nor heard on another page.
export function stopSpeaking() {
  sentenceToken += 1;
  if (pendingSentence) {
    clearTimeout(pendingSentence);
    pendingSentence = null;
  }
  dropClip();
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
  clearTimeout(clipTimer);
  clipTimer = null;
  currentClip = null;
  sentenceToken += 1;
}
