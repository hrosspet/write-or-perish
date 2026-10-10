// "Delete all my writing" on the Account page (#268): a dialog, never a
// pre-ticked option; the typed username is required; the writing goes at
// once and can be restored until the date (Peter, 2026-10-09); "Restore my
// writing" goes to the restore route.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPost = jest.fn();
const mockDelete = jest.fn();
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    delete: (...args) => mockDelete(...args),
    put: jest.fn(),
    get: (...args) => mockGet(...args),
  },
}));
jest.mock('../components/ModelSelector', () => () => null);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountPage from './AccountPage';

const baseUser = {
  username: 'alice', email: 'alice@example.com', twitter_login: false,
  pending_email: null, pending_email_expired: false, plan: 'alpha',
  data_deletion: { status: null, grace_days: 30, x_connected: false },
};

const renderPage = (user) => {
  mockUserCtx = { user: { ...baseUser, ...user }, setUser: jest.fn() };
  return render(<MemoryRouter><AccountPage /></MemoryRouter>);
};

beforeEach(() => {
  mockPost.mockReset();
  mockDelete.mockReset();
  mockGet.mockReset();
  mockGet.mockResolvedValue({ data: {} });
});

test('the delete button opens a dialog; nothing is sent until the username is typed', async () => {
  const scheduled = { status: 'scheduled', grace_days: 30, purge_at: '2026-11-05T12:00:00Z', restorable: true };
  mockDelete.mockResolvedValue({ data: scheduled });
  renderPage();

  expect(screen.getByText(/You can restore your writing safely\s+within 30 days\. After that it is deleted forever\./)).toBeTruthy();
  expect(screen.queryByRole('dialog')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /delete all my writing…/i }));
  expect(screen.getByRole('dialog')).toBeTruthy();
  const when = screen.getByText(/It disappears at once, for you and for everyone else/).textContent;
  expect(when).toMatch(/you can restore your writing safely within\s+30 days, until .+, on the Account page\. After that it\s+is deleted forever\. What you write from now on stays\./);
  expect(screen.queryByText(/Nothing is deleted for/)).toBeNull();
  // No checkbox anywhere in the dialog: the choice is the button itself.
  expect(screen.queryByRole('checkbox')).toBeNull();

  const confirm = screen.getByRole('button', { name: /^delete all my writing you can restore it until/i });
  expect(confirm.disabled).toBe(true);
  fireEvent.click(confirm);
  expect(mockDelete).not.toHaveBeenCalled();

  const input = screen.getByLabelText(/type your username/i);
  fireEvent.change(input, { target: { value: 'bob' } });
  expect(confirm.disabled).toBe(true);
  fireEvent.change(input, { target: { value: 'Alice' } });
  expect(confirm.disabled).toBe(false);
  fireEvent.click(confirm);

  await waitFor(() => expect(mockDelete).toHaveBeenCalledTimes(1));
  expect(mockDelete).toHaveBeenCalledWith('/account/data', { data: { confirm: 'Alice' } });
  await waitFor(() => expect(mockUserCtx.setUser).toHaveBeenCalled());
  const update = mockUserCtx.setUser.mock.calls[0][0];
  expect(update({ username: 'alice' }).data_deletion).toEqual(scheduled);
});

test('"Keep my writing" and Escape close the dialog without a request', () => {
  renderPage();
  fireEvent.click(screen.getByRole('button', { name: /delete all my writing…/i }));
  fireEvent.click(screen.getByRole('button', { name: /keep my writing/i }));
  expect(screen.queryByRole('dialog')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /delete all my writing…/i }));
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(mockDelete).not.toHaveBeenCalled();
});

test('the dialog tells a user with X connected that Loore removes its access there', () => {
  renderPage({ data_deletion: { status: null, grace_days: 30, x_connected: true } });
  fireEvent.click(screen.getByRole('button', { name: /delete all my writing…/i }));
  const note = screen.getByText(/removes\s+its access on X/);
  expect(note.textContent).toMatch(/If X still lists Loore afterwards, remove it\s+yourself: on X/);
  expect(note.textContent).toMatch(/Connected apps, choose Loore/);
});

test('after the purge the page says Loore asked X to remove its access', () => {
  renderPage({ data_deletion: {
    status: 'done', grace_days: 30, finished_at: '2026-11-05T12:00:00Z',
    x_connection_removed: true,
  } });
  const text = screen.getByText(/Your writing was deleted forever on/).textContent;
  expect(text).toMatch(/asked X to\s+remove its access/);
  expect(text).toMatch(/If X still lists Loore, remove it\s+yourself: on X/);
});

test('a deleted writing shows the date and restores through the restore route', async () => {
  mockPost.mockResolvedValue({ data: { status: null, grace_days: 30 } });
  renderPage({ data_deletion: {
    status: 'scheduled', grace_days: 30, purge_at: '2026-11-05T12:00:00Z',
    restorable: true,
  } });
  expect(screen.getByText('Your writing is deleted.')).toBeTruthy();
  expect(screen.getByText(/You can restore it safely until .+\.\s+After that it is deleted forever\. What you write from now on stays\./)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /delete all my writing…/i })).toBeNull();
  expect(screen.queryByText(/cancel the deletion/i)).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: 'Restore my writing' }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/account/data/restore'));
  await waitFor(() => expect(screen.getByText('Your writing is restored.')).toBeTruthy());
  // The whole user is read again (description, has_own_entries).
  expect(mockGet).toHaveBeenCalledWith('/dashboard', { params: { profile: 0 } });
  expect(mockDelete).not.toHaveBeenCalled();
});

test('an admin purge waiting to start offers no restore', () => {
  renderPage({ data_deletion: {
    status: 'scheduled', grace_days: 30, purge_at: '2026-11-05T12:00:00Z',
    restorable: false, source: 'admin',
  } });
  expect(screen.getByText(/will be deleted forever on/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /restore my writing/i })).toBeNull();
});

test('a deletion in progress cannot be started again', () => {
  renderPage({ data_deletion: { status: 'running', grace_days: 30 } });
  expect(screen.getByText(/being deleted forever now/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /delete all my writing…/i })).toBeNull();
  expect(screen.queryByRole('button', { name: /restore my writing/i })).toBeNull();
});
