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

// The editor's Save from a stale page must not drop changes it didn't make
// (#476). It sends the revision the editor was opened on; on a 409 the
// editor keeps the user's text and offers "Save mine anyway" or "Show the
// newest list".
describe('editor Save after the list changed elsewhere', () => {
  const opened = todo(1, '## Today\n- [ ] call mom', 'r1');
  const merged = todo(2, '## Today\n- [ ] call mom\n- [ ] buy milk', 'r2');
  const mine = '## Today\n- [ ] call mom tonight';

  // The editor's textarea (the first textbox; the kept text comes after it).
  const editor = () => screen.getAllByRole('textbox')[0];

  const openEditorAndType = async (text) => {
    api.get.mockResolvedValue({ data: { todo: opened } });
    render(<TodoPage />);
    await screen.findByText('call mom');
    fireEvent.click(screen.getByText(/^v1/));
    expect(editor()).toHaveValue(opened.content);
    fireEvent.change(editor(), { target: { value: text } });
  };

  test('a refused Save keeps the text and shows what changed', async () => {
    await openEditorAndType(mine);
    api.put.mockRejectedValueOnce(conflict(merged));

    fireEvent.click(screen.getByText('Save'));

    expect(await screen.findByRole('alert')).toHaveTextContent('changed after you opened the editor');
    expect(api.put).toHaveBeenCalledWith('/todo', {
      content: mine, generated_by: 'user', base_revision: 'r1',
    });
    // Nothing typed is lost, and the merge's new line is named.
    expect(editor()).toHaveValue(mine);
    expect(screen.getByRole('alert')).toHaveTextContent('+ - [ ] buy milk');
    // The header shows the newest version.
    expect(screen.getByText(/^v2/)).toBeInTheDocument();
    expect(mockAddToast).not.toHaveBeenCalled();
  });

  test('"Save mine anyway" saves the text over the newest version', async () => {
    await openEditorAndType(mine);
    api.put
      .mockRejectedValueOnce(conflict(merged))
      .mockResolvedValueOnce({ data: { todo: todo(3, mine, 'r3') } });
    fireEvent.click(screen.getByText('Save'));
    fireEvent.click(await screen.findByText('Save mine anyway'));

    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(2));
    expect(api.put.mock.calls[1][1]).toEqual({
      content: mine, generated_by: 'user', base_revision: 'r2',
    });
    expect(await screen.findByText('call mom tonight')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  test('"Show the newest list" loads it and keeps the text to copy from', async () => {
    await openEditorAndType(mine);
    api.put.mockRejectedValueOnce(conflict(merged));
    fireEvent.click(screen.getByText('Save'));
    fireEvent.click(await screen.findByText('Show the newest list'));

    expect(editor()).toHaveValue(merged.content);
    expect(screen.getByLabelText('Your text, not saved')).toHaveValue(mine);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    // The next Save is checked against the newest version.
    api.put.mockResolvedValueOnce({ data: { todo: todo(3, merged.content, 'r3') } });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(2));
    expect(api.put.mock.calls[1][1].base_revision).toBe('r2');
  });

  test('a second "Show the newest list" keeps both texts until each is discarded', async () => {
    const newer = todo(3, '## Today\n- [x] call mom\n- [ ] buy milk', 'r3');
    const second = '## Today\n- [ ] call mom\n- [ ] buy oat milk';
    await openEditorAndType(mine);
    api.put
      .mockRejectedValueOnce(conflict(merged))
      .mockRejectedValueOnce(conflict(newer))
      .mockResolvedValueOnce({ data: { todo: todo(4, newer.content, 'r4') } });
    fireEvent.click(screen.getByText('Save'));
    fireEvent.click(await screen.findByText('Show the newest list'));
    fireEvent.change(editor(), { target: { value: second } });
    fireEvent.click(screen.getByText('Save'));
    fireEvent.click(await screen.findByText('Show the newest list'));

    expect(editor()).toHaveValue(newer.content);
    expect(screen.getByLabelText('Your text, not saved (1 of 2)')).toHaveValue(mine);
    expect(screen.getByLabelText('Your text, not saved (2 of 2)')).toHaveValue(second);

    // Saving closes the editor; the kept texts stay until discarded.
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(3));
    expect(await screen.findByText('buy milk')).toBeInTheDocument();
    expect(screen.getByLabelText('Your text, not saved (1 of 2)')).toHaveValue(mine);
    fireEvent.click(screen.getAllByText('Discard')[0]);
    expect(screen.getByLabelText('Your text, not saved')).toHaveValue(second);
  });

  test('another failure keeps the text and says why', async () => {
    await openEditorAndType(mine);
    api.put.mockRejectedValueOnce(Object.assign(new Error('503'), {
      response: { status: 503, data: { error: 'Your todo list is busy saving another change.' } },
    }));

    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(
      "Couldn't save the todo list (Your todo list is busy saving another change.)"));
    expect(editor()).toHaveValue(mine);
  });
});
