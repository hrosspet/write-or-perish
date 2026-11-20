const mockGet = jest.fn();
const mockPatch = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    patch: (...args) => mockPatch(...args),
  },
}));

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { UserProvider, useUser } from './UserContext';

function Username() {
  const { user } = useUser();
  return <span>{user ? user.username : 'nobody'}</span>;
}

beforeEach(() => {
  mockGet.mockReset();
  mockPatch.mockReset();
  mockPatch.mockResolvedValue({});
});

// #481: the app load reads only `user`, so it asks the server to leave out
// the profile, whose decryption it would otherwise pay on every load.
test('the app load asks for the user without the profile', async () => {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  mockGet.mockResolvedValue({ data: { user: { id: 5, username: 'seowriter', timezone } } });
  render(<UserProvider><Username /></UserProvider>);

  await waitFor(() => expect(screen.getByText('seowriter')).toBeInTheDocument());
  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(mockGet).toHaveBeenCalledWith('/dashboard', { params: { profile: 0 } });
});

// Payments (#438): the server says the account is not let in. The user is
// read again (the app then shows the subscribe page), but not while a
// recording is in progress: the server still takes that recording's chunks
// and finalize, and leaving its screen would stop it.
describe('an account that stops being let in', () => {
  const { setRecordingActive } = require('../utils/recordingActivity');
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const recording = {};

  afterEach(() => setRecordingActive(recording, false));

  const loaded = async () => {
    mockGet.mockResolvedValue({ data: { user: { id: 5, username: 'ben', timezone } } });
    render(<UserProvider><Username /></UserProvider>);
    await waitFor(() => expect(screen.getByText('ben')).toBeInTheDocument());
    mockGet.mockClear();
  };

  test('the user is read again', async () => {
    await loaded();
    window.dispatchEvent(new CustomEvent('loore:no-subscription'));
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    expect(mockGet).toHaveBeenCalledWith('/dashboard', { params: { profile: 0 } });
  });

  test('not while a recording is in progress; once it has ended', async () => {
    await loaded();
    setRecordingActive(recording, true);
    window.dispatchEvent(new CustomEvent('loore:no-subscription'));
    window.dispatchEvent(new CustomEvent('loore:no-subscription'));
    await new Promise((r) => setTimeout(r, 0));
    expect(mockGet).not.toHaveBeenCalled();

    setRecordingActive(recording, false);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
  });

  // Review of #682: in Voice, the turn's end asks for the thread's waiting
  // changes; that request's 402 switched the app to the subscribe page and
  // the unmount cut the sentence off just after it started. The sentence
  // now holds the switch until it has been said.
  test('not while Voice says why the turn got no reply; once it is said', async () => {
    const { soundThenSay, resetSpeechForTests } = require('../utils/spokenRefusal');
    const { ACCESS_ENDED_SENTENCES } = require('../utils/spokenRefusal');
    const utterances = [];
    const savedAudio = window.Audio;
    window.Audio = undefined;
    window.speechSynthesis = { cancel: jest.fn(), speak: (u) => utterances.push(u) };
    window.SpeechSynthesisUtterance = function Utterance(text) { this.text = text; };
    try {
      await loaded();
      // No clip for this account (402): the browser's speech says it.
      soundThenSay(ACCESS_ENDED_SENTENCES.subscription_required_past_due, null);
      await new Promise((r) => setTimeout(r, 0));
      expect(utterances).toHaveLength(1);
      const cancelsAtStart = window.speechSynthesis.cancel.mock.calls.length;
      window.dispatchEvent(new CustomEvent('loore:no-subscription'));
      await new Promise((r) => setTimeout(r, 0));
      // Nothing re-read, so the screen stays and nothing cancels the speech.
      expect(mockGet).not.toHaveBeenCalled();
      expect(window.speechSynthesis.cancel).toHaveBeenCalledTimes(cancelsAtStart);

      utterances[0].onend();
      await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    } finally {
      resetSpeechForTests();
      window.Audio = savedAudio;
      delete window.speechSynthesis;
      delete window.SpeechSynthesisUtterance;
    }
  });
});
