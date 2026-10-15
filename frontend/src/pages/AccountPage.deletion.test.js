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
import DeleteWritingDialog from '../components/DeleteWritingDialog';
import { formatDeletionDate } from '../utils/dataDeletion';

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

test("an admin's purge shows the same restore as her own request", () => {
  // Peter, 2026-10-13: the admin's "Purge data" works the same way.
  renderPage({ data_deletion: {
    status: 'scheduled', grace_days: 30, purge_at: '2026-11-05T12:00:00Z',
    restorable: true, source: 'admin',
  } });
  expect(screen.getByText('Your writing is deleted.')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Restore my writing' })).toBeTruthy();
});

test('a purge the server says is not restorable offers no restore', () => {
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

// "Delete all my writing" again while a deletion waits (Peter, 2026-10-15):
// offered below Restore when she has written or saved more since; one new
// date for everything, shown in the dialog before she confirms; one restore.
describe('asking again while a deletion waits', () => {
  const NOW = Date.parse('2026-10-15T12:00:00Z');
  const waiting = {
    status: 'scheduled', grace_days: 30, purge_at: '2026-11-05T12:00:00Z',
    restorable: true, new_writing: true,
  };
  let nowSpy;
  beforeEach(() => { nowSpy = jest.spyOn(Date, 'now').mockReturnValue(NOW); });
  afterEach(() => nowSpy.mockRestore());

  test('the button comes below Restore and the dialog states the one new date', async () => {
    const newDate = formatDeletionDate(new Date(NOW + 30 * 24 * 3600 * 1000));
    const oldDate = formatDeletionDate(waiting.purge_at);
    mockDelete.mockResolvedValue({ data: {
      ...waiting, purge_at: '2026-11-14T12:00:00Z', new_writing: false,
    } });
    renderPage({ data_deletion: waiting });

    const restore = screen.getByRole('button', { name: 'Restore my writing' });
    const again = screen.getByRole('button', { name: /delete all my writing…/i });
    // eslint-disable-next-line no-bitwise
    expect(restore.compareDocumentPosition(again) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText(/Since your writing was deleted, you have written or saved\s+more\./).textContent)
      .toMatch(/Then everything is deleted forever on one date,\s+30 days after you ask again, and\s+you can restore all of it until then\./);

    fireEvent.click(again);
    expect(screen.getByText(/Loore will also delete everything you have written or recorded here since your writing was deleted/)).toBeTruthy();
    const when = screen.getByText(/The\s+deletion of your earlier writing moves to the same date/).textContent;
    expect(when).toContain(`everything is then deleted forever on ${newDate}, instead of ${oldDate}.`);
    expect(when).toMatch(/Until then you can restore all of it safely, on the Account\s+page\. What you write from now on stays\./);

    const confirm = screen.getByRole('button', { name: /^delete all my writing you can restore all of it until/i });
    expect(confirm.textContent).toContain(`You can restore all of it until ${newDate}.`);
    fireEvent.change(screen.getByLabelText(/type your username/i), { target: { value: 'alice' } });
    fireEvent.click(confirm);
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('/account/data', { data: { confirm: 'alice' } }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText(`What you wrote since is deleted too. You can restore all of it until ${formatDeletionDate('2026-11-14T12:00:00Z')}.`)).toBeTruthy();
  });

  test('nothing written since: no second button', () => {
    renderPage({ data_deletion: { ...waiting, new_writing: false } });
    expect(screen.getByRole('button', { name: 'Restore my writing' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /delete all my writing…/i })).toBeNull();
    expect(screen.queryByText(/Since your writing was deleted/)).toBeNull();
  });

  test("an admin's waiting purge offers it the same way", () => {
    renderPage({ data_deletion: { ...waiting, source: 'admin' } });
    expect(screen.getByRole('button', { name: /delete all my writing…/i })).toBeTruthy();
  });

  test('if the deletion started meanwhile, the dialog says so and the user is read again', async () => {
    mockDelete.mockRejectedValue({ response: { status: 409, data: {
      code: 'already_running',
      error: 'Your earlier writing is being deleted forever now. What you wrote since then was not deleted. You can delete it when this has finished.',
    } } });
    renderPage({ data_deletion: waiting });
    fireEvent.click(screen.getByRole('button', { name: /delete all my writing…/i }));
    fireEvent.change(screen.getByLabelText(/type your username/i), { target: { value: 'alice' } });
    fireEvent.click(screen.getByRole('button', { name: /^delete all my writing you can restore all of it until/i }));
    await waitFor(() => expect(screen.getByText(/What you wrote since then was not deleted/)).toBeTruthy());
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(mockGet).toHaveBeenCalledWith('/dashboard', { params: { profile: 0 } });
  });

  test('the dialog leaves out the old date when it is the same day', () => {
    const same = new Date(NOW + 30 * 24 * 3600 * 1000).toISOString();
    render(
      <DeleteWritingDialog
        open username="alice" graceDays={30} xConnected={false}
        waitingUntil={same} onConfirm={jest.fn()} onClose={jest.fn()}
      />,
    );
    const when = screen.getByText(/deletion of your earlier writing moves/).textContent;
    expect(when).not.toMatch(/instead of/);
    expect(when).toContain(`deleted forever on ${formatDeletionDate(same)}.`);
  });
});
