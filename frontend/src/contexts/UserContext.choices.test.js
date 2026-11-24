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
import { CHOICE_KEYS, OWNER_KEY } from '../utils/deviceChoices';

// Choices a browser remembers (last privacy level and AI usage, "don't show
// again" answers, the reply switches) stay with the account that made them.

const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

function Probe() {
  const { user, loading } = useUser();
  // What a form would read on its first render with this user.
  const privacy = localStorage.getItem('loore_last_privacy_level') || 'none';
  if (loading) return <span>loading</span>;
  return <span>{user ? `${user.username}:${privacy}` : `nobody:${privacy}`}</span>;
}

const loadAs = async (user) => {
  mockGet.mockResolvedValue({ data: { user: { ...user, timezone } } });
  const view = render(<UserProvider><Probe /></UserProvider>);
  await waitFor(() => expect(screen.getByText(new RegExp(`^${user.username}:`))).toBeInTheDocument());
  return view;
};

const loadSignedOut = async (status = 401) => {
  mockGet.mockRejectedValue({ response: { status } });
  const view = render(<UserProvider><Probe /></UserProvider>);
  await waitFor(() => expect(screen.getByText(/^nobody:/)).toBeInTheDocument());
  return view;
};

const makeChoices = () => {
  localStorage.setItem('loore_last_privacy_level', 'public');
  localStorage.setItem('loore_last_ai_usage', 'train');
  localStorage.setItem('loore_public_reply_ack', 'true');
  localStorage.setItem('loore_agentic_reply', 'false');
  localStorage.setItem('loore_auto_generate', 'false');
  localStorage.setItem('loore:free-month-notice-dismissed', '2026-12-01');
};

const choicesLeft = () => CHOICE_KEYS.filter((key) => localStorage.getItem(key) !== null);

let errorSpy;
beforeEach(() => {
  localStorage.clear();
  mockGet.mockReset();
  mockPatch.mockReset();
  mockPatch.mockResolvedValue({});
  errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => errorSpy.mockRestore());

test('a different account starts without the previous account\'s choices', async () => {
  const first = await loadAs({ id: 5, username: 'ann' });
  makeChoices();
  localStorage.setItem('loore_theme', 'light');
  first.unmount();

  await loadAs({ id: 6, username: 'ben' });
  // Already gone on the first render with the new user.
  expect(screen.getByText('ben:none')).toBeInTheDocument();
  expect(choicesLeft()).toEqual([]);
  expect(localStorage.getItem(OWNER_KEY)).toBe('6');
  expect(localStorage.getItem('loore_theme')).toBe('light');
});

test('the same account keeps its choices over a sign-out and a new sign-in', async () => {
  const first = await loadAs({ id: 5, username: 'ann' });
  makeChoices();
  first.unmount();

  const signedOut = await loadSignedOut();
  expect(choicesLeft()).toEqual(CHOICE_KEYS);
  signedOut.unmount();

  await loadAs({ id: 5, username: 'ann' });
  expect(screen.getByText('ann:public')).toBeInTheDocument();
  expect(choicesLeft()).toEqual(CHOICE_KEYS);
});

test('a different account signing in after a sign-out starts without them too', async () => {
  const first = await loadAs({ id: 5, username: 'ann' });
  makeChoices();
  first.unmount();
  const signedOut = await loadSignedOut();
  signedOut.unmount();

  await loadAs({ id: 6, username: 'ben' });
  expect(screen.getByText('ben:none')).toBeInTheDocument();
  expect(choicesLeft()).toEqual([]);
});

describe('choices stored before their owner was recorded', () => {
  test('stay with the account that is signed in', async () => {
    makeChoices();
    await loadAs({ id: 5, username: 'ann' });
    expect(screen.getByText('ann:public')).toBeInTheDocument();
    expect(choicesLeft()).toEqual(CHOICE_KEYS);
    expect(localStorage.getItem(OWNER_KEY)).toBe('5');
  });

  test('are removed when nobody is signed in', async () => {
    makeChoices();
    await loadSignedOut();
    expect(choicesLeft()).toEqual([]);
  });

  test('are left alone when the server could not be reached', async () => {
    makeChoices();
    await loadSignedOut(503);
    expect(choicesLeft()).toEqual(CHOICE_KEYS);
  });
});
