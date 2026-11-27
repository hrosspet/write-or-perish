// A tick (or the row "+") in an artifact is shown at once and saved only over
// the text the page shows (#795): the PATCH sends that text as base_content.
// When the artifact changed elsewhere (the AI wrote a version, another tab
// ticked a box in place) the server refuses with 409 content_changed, and the
// page undoes the click and asks for a reload.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../api';
import ArtifactsPage from './ArtifactsPage';
import { NEWER_ARTIFACT_MESSAGE } from '../utils/markdown';

// The artifact's body stands in as its checklist: a checkbox and a "+" per
// task line, wired to the callbacks the page gives the real one.
jest.mock('../components/MarkdownBody', () => ({ children, onCheckboxToggle, onAddTask }) => (
  <div>
    {String(children || '').split('\n').filter((l) => /^- \[[ x]\] /.test(l)).map((line) => {
      const label = line.slice(6);
      const checked = line[3] === 'x';
      return (
        <span key={label}>
          <button type="button" role="checkbox" aria-checked={checked} aria-label={label}
            onClick={() => onCheckboxToggle?.(label, checked)} />
          <button type="button" aria-label={`add after ${label}`}
            onClick={() => onAddTask?.(label, 'new item')} />
        </span>
      );
    })}
  </div>
));
jest.mock('../components/IntentionsView', () => () => null);
jest.mock('../components/VersionHistoryDrawer', () => () => null);
jest.mock('../components/ArtifactsNav', () => () => null);
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: jest.fn(), post: jest.fn(), put: jest.fn(), patch: jest.fn(), delete: jest.fn(),
  },
}));
const mockAddToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast }),
}));

const LIST = '- [ ] a\n- [ ] b';
const devMap = (content, extra = {}) => ({
  id: 5, kind: 'dev-map', title: 'Dev Map', content, description: 'Ideas',
  generated_by: 'claude-opus-5.5', created_at: '2026-11-24T10:00:00Z',
  privacy_level: 'private', ai_usage: 'chat', deletable: true, ...extra,
});

const changed = () => Object.assign(new Error('409'), {
  response: { status: 409, data: { error: 'newer', code: 'content_changed' } },
});

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

beforeEach(() => {
  jest.clearAllMocks();
  api.get.mockImplementation((url) => {
    if (url === '/artifacts') {
      return Promise.resolve({ data: { artifacts: [devMap(LIST)], deleted_kinds: [] } });
    }
    if (url.endsWith('/versions')) return Promise.resolve({ data: { versions: [{ id: 5 }] } });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  api.post.mockResolvedValue({ data: {} });
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

const renderPage = () => render(
  <MemoryRouter initialEntries={['/artifacts/dev-map']}>
    <Routes>
      <Route path="/artifacts/:kind" element={<ArtifactsPage />} />
    </Routes>
  </MemoryRouter>,
);

const box = (label) => screen.getByRole('checkbox', { name: label });

test('a tick sends the text the page shows as its base', async () => {
  api.patch.mockResolvedValue({ data: { artifact: devMap('- [x] a\n- [ ] b', { id: 6, generated_by: 'user' }) } });
  renderPage();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'a' }));

  expect(box('a')).toHaveAttribute('aria-checked', 'true');
  await waitFor(() => expect(api.patch).toHaveBeenCalledWith('/artifacts/dev-map', {
    content: '- [x] a\n- [ ] b', base_content: LIST,
  }));
  expect(mockAddToast).not.toHaveBeenCalled();
});

test('a tick after the AI wrote a newer version is undone, with a toast', async () => {
  const save = deferred();
  api.patch.mockReturnValueOnce(save.promise);
  renderPage();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'b' }));
  expect(box('b')).toHaveAttribute('aria-checked', 'true');

  save.reject(changed());
  await waitFor(() => expect(box('b')).toHaveAttribute('aria-checked', 'false'));
  expect(mockAddToast).toHaveBeenCalledWith(NEWER_ARTIFACT_MESSAGE, 8000);
  expect(api.patch).toHaveBeenCalledTimes(1);
  // The page keeps its text: no reload of its own.
  expect(api.get.mock.calls.filter(([url]) => url === '/artifacts')).toHaveLength(1);
});

test('quick ticks go one after the other, each on the text the last one saved', async () => {
  const first = deferred();
  api.patch
    .mockReturnValueOnce(first.promise)
    .mockResolvedValueOnce({ data: { artifact: devMap('- [x] a\n- [x] b', { id: 6, generated_by: 'user' }) } });
  renderPage();
  fireEvent.click(await screen.findByRole('checkbox', { name: 'a' }));
  fireEvent.click(box('b'));
  await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(1));

  first.resolve({ data: { artifact: devMap('- [x] a\n- [ ] b', { id: 6, generated_by: 'user' }) } });
  await waitFor(() => expect(api.patch).toHaveBeenCalledTimes(2));
  expect(api.patch.mock.calls[1][1]).toEqual({
    content: '- [x] a\n- [x] b', base_content: '- [x] a\n- [ ] b',
  });
  expect(box('a')).toHaveAttribute('aria-checked', 'true');
  expect(box('b')).toHaveAttribute('aria-checked', 'true');
  expect(mockAddToast).not.toHaveBeenCalled();
});

test('the row "+" on a stale page is undone the same way', async () => {
  api.patch.mockRejectedValueOnce(changed());
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: 'add after a' }));

  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(NEWER_ARTIFACT_MESSAGE, 8000));
  expect(api.patch).toHaveBeenCalledWith('/artifacts/dev-map', {
    content: '- [ ] a\n- [ ] new item\n- [ ] b', base_content: LIST,
  });
  expect(screen.queryByRole('checkbox', { name: 'new item' })).toBeNull();
});
