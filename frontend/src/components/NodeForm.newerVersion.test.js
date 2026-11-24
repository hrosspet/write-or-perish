// #799: the entry's edit form saves only over the text it was opened on.
// Its Save sends that text as base_content; when the entry changed since
// (another tab, the phone, a tick) the server refuses with 409
// content_changed. The form then keeps the person's text, stays open and
// says what to do; nothing is merged. Autosaved drafts carry the same base,
// and a draft the server marks as written on an older version is offered
// below the editor instead of being loaded into it.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: {
      id: 1, username: 'u', default_ai_usage: 'chat',
      default_privacy_level: 'private',
    },
  }),
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
const mockGet = jest.fn();
const mockPut = jest.fn();
const mockPost = jest.fn();
const mockDelete = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    put: (...a) => mockPut(...a),
    delete: (...a) => mockDelete(...a),
  },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';
import { NEWER_ENTRY_EDIT_MESSAGE } from '../utils/markdown';

const OPENED_ON = 'Call the bank on Monday.\r\n- [ ] deploy\n';

const changed = () => Object.assign(new Error('409'), {
  response: { status: 409, data: { error: 'newer', code: 'content_changed' } },
});

beforeEach(() => {
  localStorage.clear();
  jest.clearAllMocks();
  mockGet.mockRejectedValue({ response: { status: 404 } });
  mockPut.mockResolvedValue({ data: { node: {} } });
  mockPost.mockResolvedValue({ data: { id: 31, updated_at: '2026-11-24T10:00:00' } });
  mockDelete.mockResolvedValue({ data: {} });
});

const renderEdit = (props = {}) => {
  const onSuccess = jest.fn();
  render(
    <MemoryRouter>
      <NodeForm editMode nodeId={7} initialContent={OPENED_ON}
        initialPrivacyLevel="private" initialAiUsage="chat"
        onSuccess={onSuccess} {...props} />
    </MemoryRouter>,
  );
  return onSuccess;
};

const textarea = () => screen.getByPlaceholderText("What's present for you right now...");
const save = () => fireEvent.click(screen.getByRole('button', { name: 'Send' }));

test('the first Save of a form opened fresh sends the text it was opened on as its base', async () => {
  const onSuccess = renderEdit();
  await waitFor(() => expect(mockGet).toHaveBeenCalled());
  fireEvent.change(textarea(), { target: { value: 'Call the bank on Tuesday.' } });
  save();

  await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
  const [url, body] = mockPut.mock.calls[0];
  expect(url).toBe('/nodes/7');
  expect(body.content).toBe('Call the bank on Tuesday.');
  // Exactly the page's text, line endings and all.
  expect(body.base_content).toBe(OPENED_ON);
  await waitFor(() => expect(onSuccess).toHaveBeenCalled());
});

test('a Save refused for a newer version keeps the text, stays open and says what to do', async () => {
  mockPut.mockRejectedValueOnce(changed());
  const onSuccess = renderEdit();
  await waitFor(() => expect(mockGet).toHaveBeenCalled());
  fireEvent.change(textarea(), { target: { value: 'my long edit' } });
  save();

  expect(await screen.findByText(NEWER_ENTRY_EDIT_MESSAGE)).toBeInTheDocument();
  expect(textarea()).toHaveValue('my long edit');
  expect(onSuccess).not.toHaveBeenCalled();
  expect(mockPut).toHaveBeenCalledTimes(1);
  // The draft is not deleted: the text stays on the server too.
  expect(mockDelete).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Copy my text' })).toBeInTheDocument();
});

test('autosaves send the same base', async () => {
  renderEdit();
  await waitFor(() => expect(mockGet).toHaveBeenCalled());
  fireEvent.change(textarea(), { target: { value: 'typed' } });
  await waitFor(() => expect(mockPost).toHaveBeenCalled(), { timeout: 3000 });
  expect(mockPost.mock.calls[0][1]).toEqual({
    content: 'typed', node_id: 7, parent_id: null, base_content: OPENED_ON,
  });
});

test('a draft written on an older version is offered below the entry, not loaded', async () => {
  mockGet.mockResolvedValue({ data: {
    id: 12, content: 'my older edit', node_id: 7,
    updated_at: '2026-11-24T09:00:00', older_than_entry: true,
    entry_updated_at: '2026-11-24T09:30:00',
  } });
  renderEdit();

  expect(await screen.findByLabelText('Your unsaved edit')).toHaveValue('my older edit');
  // The editor holds the entry as it is now (a textarea shows "\r\n" as
  // "\n"; the base is sent from the form's state, unchanged).
  expect(textarea()).toHaveValue(OPENED_ON.replace(/\r\n/g, '\n'));

  fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
  await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('/drafts/?node_id=7&draft_id=12'));
  expect(screen.queryByLabelText('Your unsaved edit')).toBeNull();
});

test('a current draft is loaded as before, and the base stays the text the form was opened on', async () => {
  mockGet.mockResolvedValue({ data: {
    id: 12, content: 'my current draft', node_id: 7, updated_at: '2026-11-24T09:00:00',
  } });
  renderEdit();
  await waitFor(() => expect(textarea()).toHaveValue('my current draft'));
  expect(screen.queryByLabelText('Your unsaved edit')).toBeNull();
  save();
  await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
  expect(mockPut.mock.calls[0][1].base_content).toBe(OPENED_ON);
});

// #799 review 1: another device's Save deleted every draft of the entry, this
// form's included. On the refusal the form saves its text again at once (not
// after the typing debounce), with its base, so a reload offers it.
test('a refused Save saves the text as a draft at once, with its base', async () => {
  mockPut.mockRejectedValueOnce(changed());
  renderEdit();
  await waitFor(() => expect(mockGet).toHaveBeenCalled());
  fireEvent.change(textarea(), { target: { value: 'my long edit' } });
  save();

  // Well before the 1 s typing debounce.
  await waitFor(() => expect(mockPost).toHaveBeenCalled(), { timeout: 400 });
  expect(mockPost.mock.calls[0]).toEqual(['/drafts/', {
    content: 'my long edit', node_id: 7, parent_id: null, base_content: OPENED_ON,
  }]);
});

// #799 review 2: a Save deletes only the drafts its form showed: its own row
// and the older edit on offer. Another device's unsaved edit stays.
test('a Save deletes only the drafts the form showed', async () => {
  mockGet.mockResolvedValue({ data: {
    id: 12, content: 'my older edit', node_id: 7,
    updated_at: '2026-11-24T09:00:00', older_than_entry: true,
  } });
  const onSuccess = renderEdit();
  await screen.findByLabelText('Your unsaved edit');
  fireEvent.change(textarea(), { target: { value: 'typed on the current text' } });
  // The autosave lands (row 31).
  await waitFor(() => expect(mockPost).toHaveBeenCalled(), { timeout: 3000 });
  save();

  await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  await waitFor(() => expect(mockDelete).toHaveBeenCalledTimes(1));
  expect(mockDelete).toHaveBeenCalledWith('/drafts/?node_id=7&draft_id=31&draft_id=12');
});

test('a Save with no draft of its own and none on offer deletes no draft', async () => {
  const onSuccess = renderEdit();
  await waitFor(() => expect(mockGet).toHaveBeenCalled());
  save();
  await waitFor(() => expect(onSuccess).toHaveBeenCalled());
  await new Promise((r) => setTimeout(r, 20));
  expect(mockDelete).not.toHaveBeenCalled();
});
