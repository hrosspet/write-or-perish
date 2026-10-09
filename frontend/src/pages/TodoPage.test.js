// A tick on a stale Todo page must not overwrite a newer list (#430). The page
// sends the revision of the list it edited; on a 409 it applies the same tick
// to the newest list the server returned and saves that.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import api from '../api';
import TodoPage from './TodoPage';

jest.mock('../components/MarkdownBody', () => ({ children }) =>
  require('react').createElement('span', null, children));
jest.mock('../components/VersionHistoryDrawer', () => () => null);
jest.mock('../components/ArtifactsNav', () => () => null);
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), patch: jest.fn(), put: jest.fn(), post: jest.fn() },
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));

const todo = (id, content, revision, versionNumber = id) => ({
  id, content, revision, version_number: versionNumber,
  generated_by: 'user', created_at: '2026-10-09T10:00:00Z',
});

const conflict = (newest) => Object.assign(new Error('409'), {
  response: { status: 409, data: { error: 'changed', code: 'todo_changed', todo: newest } },
});

// The round checkbox before the item's label.
const checkboxOf = (label) => screen.getByText(label).parentElement.parentElement.firstChild;

beforeEach(() => {
  jest.clearAllMocks();
});

test('a tick on a stale page is applied to the newest list, not over it', async () => {
  api.get.mockResolvedValue({ data: { todo: todo(1, '## Today\n- [ ] call mom', 'r1') } });
  // A todo merge saved a new version after the page loaded.
  const merged = todo(2, '## Today\n- [ ] call mom\n- [ ] buy milk', 'r2');
  api.patch
    .mockRejectedValueOnce(conflict(merged))
    .mockResolvedValueOnce({ data: { todo: todo(2, '## Today\n- [x] call mom\n- [ ] buy milk', 'r3', 2) } });

  render(<TodoPage />);
  await screen.findByText('call mom');
  fireEvent.click(checkboxOf('call mom'));

  await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
  expect(api.patch.mock.calls[0][1]).toEqual({
    content: '## Today\n- [x] call mom', base_revision: 'r1',
  });
  expect(api.patch.mock.calls[1][1]).toEqual({
    content: '## Today\n- [x] call mom\n- [ ] buy milk', base_revision: 'r2',
  });
  // The merged item stays on the page.
  expect(await screen.findByText('buy milk')).toBeInTheDocument();
  expect(mockAddToast).not.toHaveBeenCalled();
});

test('a tick whose item the newest list no longer has is not guessed', async () => {
  api.get.mockResolvedValue({ data: { todo: todo(1, '## Today\n- [ ] call mom', 'r1') } });
  // The merge reworded the item.
  api.patch.mockRejectedValueOnce(conflict(todo(2, '## Today\n- [ ] call mum', 'r2')));

  render(<TodoPage />);
  await screen.findByText('call mom');
  fireEvent.click(checkboxOf('call mom'));

  // The newest list is shown and nothing more is saved.
  expect(await screen.findByText('call mum')).toBeInTheDocument();
  expect(screen.queryByText('call mom')).not.toBeInTheDocument();
  expect(api.patch).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledWith(
    expect.stringContaining('changed since this page loaded'));
});

test('quick ticks are saved in order, each on the list the previous save returned', async () => {
  api.get.mockResolvedValue({ data: { todo: todo(1, '## Today\n- [ ] a\n- [ ] b', 'r1') } });
  api.patch
    .mockResolvedValueOnce({ data: { todo: todo(1, '## Today\n- [x] a\n- [ ] b', 'r2', 1) } })
    .mockResolvedValueOnce({ data: { todo: todo(1, '## Today\n- [x] a\n- [x] b', 'r3', 1) } });

  render(<TodoPage />);
  await screen.findByText('a');
  fireEvent.click(checkboxOf('a'));
  fireEvent.click(checkboxOf('b'));

  await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
  expect(api.patch.mock.calls[0][1]).toEqual({
    content: '## Today\n- [x] a\n- [ ] b', base_revision: 'r1',
  });
  expect(api.patch.mock.calls[1][1]).toEqual({
    content: '## Today\n- [x] a\n- [x] b', base_revision: 'r2',
  });
  expect(mockAddToast).not.toHaveBeenCalled();
});
