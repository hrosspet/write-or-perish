// "Delete all my writing" on the Account page (#268): a dialog, never a
// pre-ticked option; the typed username is required; the date and the
// way to cancel are shown; Cancel goes to the cancel route.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPost = jest.fn();
const mockDelete = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    delete: (...args) => mockDelete(...args),
    put: jest.fn(),
    get: jest.fn(),
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
});

test('the delete button opens a dialog; nothing is sent until the username is typed', async () => {
  const scheduled = { status: 'scheduled', grace_days: 30, purge_at: '2026-11-05T12:00:00Z' };
  mockDelete.mockResolvedValue({ data: scheduled });
  renderPage();

  expect(screen.queryByRole('dialog')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /delete all my writing…/i }));
  expect(screen.getByRole('dialog')).toBeTruthy();
  expect(screen.getByText(/Nothing is deleted for 30 days/)).toBeTruthy();
  // No checkbox anywhere in the dialog: the choice is the button itself.
  expect(screen.queryByRole('checkbox')).toBeNull();

  const confirm = screen.getByRole('button', { name: /delete all my writing on/i });
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
  const text = screen.getByText(/Your writing was deleted on/).textContent;
  expect(text).toMatch(/asked X to\s+remove its access/);
  expect(text).toMatch(/If X still lists Loore, remove it\s+yourself: on X/);
});

test('a scheduled deletion shows its date and cancels through the cancel route', async () => {
  mockPost.mockResolvedValue({ data: { status: null, grace_days: 30 } });
  renderPage({ data_deletion: {
    status: 'scheduled', grace_days: 30, purge_at: '2026-11-05T12:00:00Z',
  } });
  expect(screen.getByText(/All your writing will be deleted on/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /delete all my writing…/i })).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: /cancel the deletion/i }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/account/data/cancel'));
  await waitFor(() => expect(screen.getByText(/Deletion cancelled/)).toBeTruthy());
  expect(mockDelete).not.toHaveBeenCalled();
});

test('a deletion in progress cannot be started again', () => {
  renderPage({ data_deletion: { status: 'running', grace_days: 30 } });
  expect(screen.getByText(/being deleted now/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /delete all my writing…/i })).toBeNull();
  expect(screen.queryByRole('button', { name: /cancel the deletion/i })).toBeNull();
});
