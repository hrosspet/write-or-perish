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

  test('not while a recording is in progress; the next refusal after it is', async () => {
    await loaded();
    setRecordingActive(recording, true);
    window.dispatchEvent(new CustomEvent('loore:no-subscription'));
    await new Promise((r) => setTimeout(r, 0));
    expect(mockGet).not.toHaveBeenCalled();

    setRecordingActive(recording, false);
    window.dispatchEvent(new CustomEvent('loore:no-subscription'));
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
  });
});
