// Two PRs of train #787 on one page: on one's own public entry, the reply
// form's privacy selector is free and a choice made there is never reset
// (#797), and a tick on the entry is saved only over the text the page
// shows, undone when the entry changed elsewhere (#798). A tick, saved or
// undone, changes the entry the page holds; the reply form below keeps the
// person's Private choice, and the reply is sent Private. The ticks send
// only the text and its base, never a level.
const mockGet = jest.fn();
const mockPut = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
    put: (...args) => mockPut(...args),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: {
      id: 1, username: 'peter', craft_mode: true, approved: true,
      default_ai_usage: 'chat', default_privacy_level: 'private',
    },
  }),
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: () => {} }),
}));
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: () => ({ text: '', done: false }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./NodeFormModal', () => () => null);
jest.mock('./ModelSelector', () => () => null);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
// The entry's body stands in as its checklist: a checkbox per task line,
// wired to the callback the page gives the real one.
jest.mock('./QuotedContent', () => ({ content, onCheckboxToggle }) => (
  <div>
    {String(content || '').split('\n').filter((l) => /^- \[[ x]\] /.test(l)).map((line) => {
      const label = line.slice(6);
      const checked = line[3] === 'x';
      return (
        <button key={label} type="button" role="checkbox" aria-checked={checked}
          aria-label={label} onClick={() => onCheckboxToggle(label, checked)} />
      );
    })}
  </div>
));

import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';
import { NEWER_ENTRY_MESSAGE } from '../utils/markdown';

const LIST = '- [ ] deploy\n- [ ] write the note';

// Peter's own public entry.
const entry = () => ({
  id: 41, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content: LIST, ai_usage: 'chat', privacy_level: 'public',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
});

const changed = () => Object.assign(new Error('409'), {
  response: { status: 409, data: { error: 'newer', code: 'content_changed' } },
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  localStorage.clear();
  mockGet.mockReset();
  mockPut.mockReset();
  mockPost.mockReset();
  mockAddToast.mockReset();
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/41') return Promise.resolve({ data: entry() });
    if (String(url).endsWith('/llm-status')) return new Promise(() => {});
    return Promise.reject({ response: { status: 404 } });
  });
  mockPost.mockResolvedValue({ data: { id: 60, privacy_level: 'private' } });
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

const renderEntry = () => render(
  <MemoryRouter initialEntries={['/node/41']}>
    <Routes>
      <Route path="/node/:id" element={<NodeDetailWrapper />} />
    </Routes>
  </MemoryRouter>,
);

// The reply form's privacy selector: the select that offers Circles.
const privacySelect = () => screen.getAllByRole('combobox')
  .find((s) => s.querySelector('option[value="circles"]'));

test('ticks on the entry, saved or undone, leave the reply form\'s Private choice', async () => {
  mockPut
    .mockResolvedValueOnce({ data: {} })
    .mockRejectedValueOnce(changed());
  renderEntry();
  await screen.findByRole('checkbox', { name: 'deploy' });
  await act(async () => { await Promise.resolve(); });

  // Free under one's own public entry, starting at Public (#797).
  expect(screen.queryByText('Public — inherited from the thread.')).toBeNull();
  expect(privacySelect()).toHaveValue('public');
  fireEvent.change(privacySelect(), { target: { value: 'private' } });
  fireEvent.change(screen.getByPlaceholderText("Type what's on your mind…"),
    { target: { value: 'A note for me.' } });

  // A tick that is saved, then one the server refuses (#798).
  fireEvent.click(screen.getByRole('checkbox', { name: 'deploy' }));
  await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
  const once = '- [x] deploy\n- [ ] write the note';
  expect(mockPut).toHaveBeenNthCalledWith(1, '/nodes/41',
    { content: once, base_content: LIST });
  fireEvent.click(screen.getByRole('checkbox', { name: 'write the note' }));
  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(
    NEWER_ENTRY_MESSAGE, expect.any(Number)));
  expect(mockPut).toHaveBeenNthCalledWith(2, '/nodes/41',
    { content: '- [x] deploy\n- [x] write the note', base_content: once });
  expect(screen.getByRole('checkbox', { name: 'write the note' }))
    .toHaveAttribute('aria-checked', 'false');

  // The form kept the choice and the text, and sends them.
  expect(privacySelect()).toHaveValue('private');
  expect(screen.getByPlaceholderText("Type what's on your mind…"))
    .toHaveValue('A note for me.');
  await act(async () => {
    fireEvent.submit(privacySelect().closest('form'));
  });
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/nodes/',
    expect.objectContaining({
      content: 'A note for me.', parent_id: 41, privacy_level: 'private',
    })));
  expect(screen.queryByText('This reply will be public')).toBeNull();
});
