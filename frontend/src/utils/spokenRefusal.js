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
//
// Payments on (#438): a recording accepted while the account had AI
// replies, whose access ended before its reply, is kept and transcribed;
// only the reply is skipped. Its sentence, one per reason, is the text
// the entry and the screen show (it names the state, never where or how
// to pay): the finalize's warning_code, the fallback request's 402
// `code`, or a queued reply's llm-status `error_code`.

import { setRecordingActive } from './recordingActivity';

export const SPEND_CAP_REFUSAL = 'monthly_spend_limit_reached';
export const AI_USAGE_REFUSAL = 'ai_usage_none';
export const APPROVAL_REFUSAL = 'approval_pending';
export const LENGTH_REFUSAL = 'thread_too_long';

// Said aloud in Voice mode when a turn is refused for the thread's length.
export const THREAD_LIMIT_SPOKEN = 'This thread is too long for another '
  + 'reply. Start a new thread to keep talking.';

// A recording kept after the account's access ended, by reason
// (backend subscriptions.paused_message(reason, "recording")).
export const ACCESS_ENDED_SENTENCES = {
  subscription_required_ended: 'Your subscription has ended, so Loore '
    + "doesn't reply. Your recording is saved.",
  subscription_required_past_due: "Your last payment didn't go through, so "
    + "Loore doesn't reply. Your recording is saved.",
  subscription_required_free_month: 'Your free month has ended, so Loore '
    + "doesn't reply. Your recording is saved.",
  subscription_required_none: "Loore doesn't reply on an account without a "
    + 'subscription. Your recording is saved.',
};

export const isAccessEndedRefusal = (code) => (
  Object.prototype.hasOwnProperty.call(ACCESS_ENDED_SENTENCES, code || ''));

export const REFUSAL_SENTENCES = {
  [SPEND_CAP_REFUSAL]: "You've reached your monthly usage limit, so Loore "
    + "can't reply until it resets.",
  [AI_USAGE_REFUSAL]: "AI usage is set to None, so Loore can't reply here.",
  [APPROVAL_REFUSAL]: 'A change to your data is waiting for your approval. '
    + 'Approve or reject it on the screen to continue.',
  [LENGTH_REFUSAL]: THREAD_LIMIT_SPOKEN,
  ...ACCESS_ENDED_SENTENCES,
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
// Returns whether it started; *onDone* runs when it has been said (or
// failed), and at once when it could not start.
export function speakSentence(text, onDone) {
  const done = onDone || (() => {});
  try {
    if (typeof window === 'undefined') { done(); return false; }
    const synth = window.speechSynthesis;
    const Utterance = window.SpeechSynthesisUtterance;
    if (!synth || typeof Utterance !== 'function') { done(); return false; }
    synth.cancel();
    const utterance = new Utterance(text);
    utterance.onend = done;
    utterance.onerror = done;
    synth.speak(utterance);
    return true;
  } catch (e) {
    done();
    return false;
  }
}

// While a sentence is waiting, loading or being said it counts as a
// recording in progress (utils/recordingActivity): the app does not leave
// the Voice screen for the subscribe page meanwhile (#438), which would
// stop the sentence a moment after it starts. It ends when the sentence
// has been said, is stopped, or after SENTENCE_HOLD_MAX_MS at the latest.
const SENTENCE_HOLD = {};
// INTRODUCED CONSTANT: the longest the screen waits for a sentence. The
// sentences take about 5 s; a browser that says nothing outside a gesture
// may never report the end. Not measured.
export const SENTENCE_HOLD_MAX_MS = 15000;
let sentenceHoldTimer = null;

function holdForSentence() {
  clearTimeout(sentenceHoldTimer);
  sentenceHoldTimer = setTimeout(releaseSentenceHold, SENTENCE_HOLD_MAX_MS);
  setRecordingActive(SENTENCE_HOLD, true);
}

function releaseSentenceHold() {
  clearTimeout(sentenceHoldTimer);
  sentenceHoldTimer = null;
  setRecordingActive(SENTENCE_HOLD, false);
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
  // This sentence has been said (only the latest one ends the hold).
  const said = () => { if (token === sentenceToken) releaseSentenceHold(); };
  const code = refusalOf(text);
  const AudioElement = typeof window !== 'undefined' ? window.Audio : null;
  if (!code || typeof AudioElement !== 'function') {
    speakSentence(text, said);
    return;
  }
  let el;
  try {
    el = new AudioElement(refusalClipUrl(code));
  } catch (e) {
    speakSentence(text, said);
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
    if (token === sentenceToken) speakSentence(text, said);
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
    said();
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
  holdForSentence();
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
  releaseSentenceHold();
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
  releaseSentenceHold();
}
