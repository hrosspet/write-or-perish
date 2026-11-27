// A tick (or the row "+") on an entry's checklist is shown at once and saved
// only over the text the page shows (#795): the save sends that text as
// base_content. When the entry changed elsewhere (another tab, the phone) the
// server refuses with 409 content_changed, and the page undoes the tick and
// asks for a reload instead of putting its older text back.
const mockGet = jest.fn();
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(() => Promise.resolve({ data: {} })),
    put: (...args) => mockPut(...args),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'peter', craft_mode: true, approved: true } }),
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: () => {} }),
}));
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: () => ({ text: '', done: false }),
}));
jest.mock('./NodeForm', () => () => null);
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./NodeFormModal', () => () => null);
jest.mock('./ModelSelector', () => () => null);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
// The entry's body stands in as its checklist: a checkbox and a "+" per
// task line, wired to the callbacks the page gives the real one.
jest.mock('./QuotedContent', () => ({ content, onCheckboxToggle, onAddTask }) => (
  <div>
    {String(content || '').split('\n').filter((l) => /^- \[[ x]\] /.test(l)).map((line) => {
      const label = line.slice(6);
      const checked = line[3] === 'x';
      return (
        <span key={label}>
          <button type="button" role="checkbox" aria-checked={checked} aria-label={label}
            onClick={() => onCheckboxToggle(label, checked)} />
          <button type="button" aria-label={`add after ${label}`}
            onClick={() => onAddTask(label, 'book the room')} />
        </span>
      );
    })}
  </div>
));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';
import { NEWER_ENTRY_MESSAGE } from '../utils/markdown';

const STATUS = '- [ ] deploy\n- [ ] write the note';

const entry = (content) => ({
  id: 41, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content, ai_usage: 'chat', privacy_level: 'private',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
});

const changed = () => Object.assign(new Error('409'), {
  response: { status: 409, data: { error: 'newer', code: 'content_changed' } },
});

// A promise the test settles by hand: a save still on its way.
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  mockAddToast.mockReset();
  localStorage.clear();
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/41') return Promise.resolve({ data: entry(STATUS) });
    if (url.endsWith('/llm-status')) return new Promise(() => {});
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
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

const box = (label) => screen.getByRole('checkbox', { name: label });

test('a tick sends the text the page shows as its base', async () => {
  mockPut.mockResolvedValue({ data: {} });
  renderEntry();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'deploy' }));

  expect(box('deploy')).toHaveAttribute('aria-checked', 'true');
  await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
  expect(mockPut).toHaveBeenCalledWith('/nodes/41', {
    content: '- [x] deploy\n- [ ] write the note', base_content: STATUS,
  });
  expect(mockAddToast).not.toHaveBeenCalled();
});

test('a tick after the entry changed in another tab is undone, with a toast', async () => {
  const save = deferred();
  mockPut.mockReturnValueOnce(save.promise);
  renderEntry();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'write the note' }));
  // Shown at once.
  expect(box('write the note')).toHaveAttribute('aria-checked', 'true');

  save.reject(changed());
  await waitFor(() => expect(box('write the note')).toHaveAttribute('aria-checked', 'false'));
  expect(mockAddToast).toHaveBeenCalledWith(NEWER_ENTRY_MESSAGE, 8000);
  // Nothing more is sent; the page does not load the newer text by itself.
  expect(mockPut).toHaveBeenCalledTimes(1);
  expect(mockGet.mock.calls.filter(([url]) => url === '/nodes/41')).toHaveLength(1);
});

test('quick ticks go one after the other, each on the text the last one saved', async () => {
  const first = deferred();
  mockPut.mockReturnValueOnce(first.promise).mockResolvedValueOnce({ data: {} });
  renderEntry();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'deploy' }));
  fireEvent.click(box('write the note'));
  expect(box('deploy')).toHaveAttribute('aria-checked', 'true');
  expect(box('write the note')).toHaveAttribute('aria-checked', 'true');
  // The second waits for the first.
  await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(1));
  await new Promise((r) => setTimeout(r, 20));
  expect(mockPut).toHaveBeenCalledTimes(1);

  first.resolve({ data: {} });
  await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(2));
  expect(mockPut.mock.calls[1][1]).toEqual({
    content: '- [x] deploy\n- [x] write the note',
    base_content: '- [x] deploy\n- [ ] write the note',
  });
  expect(mockAddToast).not.toHaveBeenCalled();
});

test('ticks waiting behind a refused one are undone with it, with one toast', async () => {
  const first = deferred();
  mockPut.mockReturnValueOnce(first.promise);
  renderEntry();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'deploy' }));
  fireEvent.click(box('write the note'));

  first.reject(changed());
  await waitFor(() => expect(box('deploy')).toHaveAttribute('aria-checked', 'false'));
  expect(box('write the note')).toHaveAttribute('aria-checked', 'false');
  expect(mockPut).toHaveBeenCalledTimes(1);
  expect(mockAddToast).toHaveBeenCalledTimes(1);
});

test('the row "+" on a stale page is undone the same way', async () => {
  mockPut.mockRejectedValueOnce(changed());
  renderEntry();
  fireEvent.click(await screen.findByRole('button', { name: 'add after deploy' }));

  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(NEWER_ENTRY_MESSAGE, 8000));
  expect(mockPut).toHaveBeenCalledWith('/nodes/41', {
    content: '- [ ] deploy\n- [ ] book the room\n- [ ] write the note', base_content: STATUS,
  });
  expect(screen.queryByRole('checkbox', { name: 'book the room' })).toBeNull();
});

test('another failure undoes the tick and says why', async () => {
  mockPut.mockRejectedValueOnce(Object.assign(new Error('500'), {
    response: { status: 500, data: { error: 'DB error updating node' } },
  }));
  renderEntry();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'deploy' }));

  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(
    "Couldn't save change — reverted (DB error updating node)"));
  expect(box('deploy')).toHaveAttribute('aria-checked', 'false');
});
