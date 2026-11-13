// #651: a refused Voice turn's sentence is said in Loore's voice from its
// cached clip, or, where that can't play, with the browser's own speech.
import {
  APPROVAL_REFUSAL, CLIP_START_TIMEOUT_MS, LENGTH_REFUSAL, REFUSAL_SENTENCES,
  SENTENCE_AFTER_SOUND_MS, SPEND_CAP_REFUSAL, THREAD_LIMIT_SPOKEN,
  refusalClipUrl, resetSpeechForTests, sayRefusal, soundThenSay, stopSpeaking,
} from './spokenRefusal';

// A clip element: play() as the browser answers it (*mode*).
let clips;
let mode;
class FakeAudio {
  constructor(src) {
    this.src = src;
    this.paused = true;
    this.listeners = {};
    clips.push(this);
  }

  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }

  fire(name) { (this.listeners[name] || []).forEach((fn) => fn()); }

  play() {
    if (mode === 'refused') {
      // Safari outside a tap: NotAllowedError, at once.
      return { then: (ok, bad) => bad(new Error('NotAllowedError')) };
    }
    if (mode === 'hangs') return { then: () => {} };
    this.paused = false;
    return { then: (ok) => ok() };
  }

  pause() { this.paused = true; }

  removeAttribute(name) { if (name === 'src') this.src = ''; }

  load() {}
}

let spoken;
let sounds;
const playSound = () => { sounds += 1; };
const afterSound = () => jest.advanceTimersByTime(SENTENCE_AFTER_SOUND_MS);

beforeEach(() => {
  jest.useFakeTimers();
  resetSpeechForTests();
  clips = [];
  mode = 'plays';
  spoken = [];
  sounds = 0;
  window.Audio = FakeAudio;
  window.speechSynthesis = { cancel: jest.fn(), speak: (u) => { spoken.push(u.text); } };
  window.SpeechSynthesisUtterance = function Utterance(text) { this.text = text; };
});

afterEach(() => {
  jest.useRealTimers();
  delete window.Audio;
  delete window.speechSynthesis;
  delete window.SpeechSynthesisUtterance;
});

test('plain words: no numbers, no prices, nothing about buying', () => {
  Object.values(REFUSAL_SENTENCES).forEach((text) => {
    expect(text).not.toMatch(/[0-9$€£%]/);
    expect(text).not.toMatch(/\b(buy|pay|price|subscri|upgrade|plan)/i);
  });
  expect(REFUSAL_SENTENCES[LENGTH_REFUSAL]).toBe(THREAD_LIMIT_SPOKEN);
});

test('the sound, then the refusal in Loore\'s voice from its clip', () => {
  sayRefusal(SPEND_CAP_REFUSAL, playSound);
  expect(sounds).toBe(1);
  expect(clips).toEqual([]);
  afterSound();
  expect(clips.map((c) => c.src)).toEqual([refusalClipUrl(SPEND_CAP_REFUSAL)]);
  expect(clips[0].src).toMatch(/\/api\/voice\/refusal\/monthly_spend_limit_reached\.mp3$/);
  expect(clips[0].paused).toBe(false);
  expect(spoken).toEqual([]);
  // Started: no speech after the wait either.
  jest.advanceTimersByTime(CLIP_START_TIMEOUT_MS);
  expect(spoken).toEqual([]);
});

test('a browser that plays no clip outside a tap says it with its own speech', () => {
  mode = 'refused';
  sayRefusal(APPROVAL_REFUSAL, playSound);
  afterSound();
  expect(spoken).toEqual([REFUSAL_SENTENCES[APPROVAL_REFUSAL]]);
});

test('a clip that fails to load (offline, 503) is said by the browser', () => {
  mode = 'hangs';
  sayRefusal(SPEND_CAP_REFUSAL, playSound);
  afterSound();
  clips[0].fire('error');
  expect(spoken).toEqual([REFUSAL_SENTENCES[SPEND_CAP_REFUSAL]]);
  expect(clips[0].src).toBe('');
});

test('a clip that does not start in time gives way to the browser\'s speech, once', () => {
  mode = 'hangs';
  sayRefusal(LENGTH_REFUSAL, playSound);
  afterSound();
  jest.advanceTimersByTime(CLIP_START_TIMEOUT_MS);
  expect(spoken).toEqual([THREAD_LIMIT_SPOKEN]);
  expect(clips[0].src).toBe('');
  clips[0].fire('error');
  expect(spoken).toEqual([THREAD_LIMIT_SPOKEN]);
});

test('a new recording stops the clip, and a late failure says nothing', () => {
  mode = 'hangs';
  sayRefusal(SPEND_CAP_REFUSAL, playSound);
  afterSound();
  stopSpeaking();
  expect(clips[0].paused).toBe(true);
  expect(clips[0].src).toBe('');
  clips[0].fire('error');
  jest.advanceTimersByTime(CLIP_START_TIMEOUT_MS);
  expect(spoken).toEqual([]);
});

test('a new recording before the sentence: nothing is played', () => {
  sayRefusal(SPEND_CAP_REFUSAL, playSound);
  stopSpeaking();
  afterSound();
  expect(clips).toEqual([]);
  expect(spoken).toEqual([]);
});

test('a second refusal replaces the first', () => {
  sayRefusal(SPEND_CAP_REFUSAL, playSound);
  afterSound();
  sayRefusal(APPROVAL_REFUSAL, playSound);
  expect(clips[0].paused).toBe(true);
  expect(clips[0].src).toBe('');
  afterSound();
  expect(clips.map((c) => c.src)).toEqual(['', refusalClipUrl(APPROVAL_REFUSAL)]);
  expect(clips[1].paused).toBe(false);
});

test('a code without a sentence says nothing; another sentence has no clip', () => {
  sayRefusal('model_read_only', playSound);
  sayRefusal(undefined, playSound);
  afterSound();
  expect(sounds).toBe(0);
  expect(clips).toEqual([]);

  soundThenSay('Something else.', playSound);
  afterSound();
  expect(clips).toEqual([]);
  expect(spoken).toEqual(['Something else.']);
});

test('no Audio element at all: the browser\'s speech', () => {
  delete window.Audio;
  sayRefusal(SPEND_CAP_REFUSAL, playSound);
  afterSound();
  expect(spoken).toEqual([REFUSAL_SENTENCES[SPEND_CAP_REFUSAL]]);
});
