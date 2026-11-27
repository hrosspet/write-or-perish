// #799: the artifacts page's edit form saves only over the text it was
// opened on. Its Save sends that text as base_content ("" for an artifact
// with no version yet); when the artifact changed since (the AI wrote a
// version, a box was ticked in another tab) the server refuses with 409
// content_changed, and the form stays open with the person's text and says
// what to do.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../api';
import ArtifactsPage from './ArtifactsPage';
import { NEWER_ARTIFACT_EDIT_MESSAGE } from '../utils/markdown';

jest.mock('../components/MarkdownBody', () => ({ children }) => <div>{children}</div>);
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
  generated_by: 'user', created_at: '2026-11-24T10:00:00Z',
  privacy_level: 'private', ai_usage: 'chat', deletable: true, ...extra,
});

const changed = () => Object.assign(new Error('409'), {
  response: { status: 409, data: { error: 'newer', code: 'content_changed' } },
});

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
  api.put.mockResolvedValue({ data: { artifact: devMap(`${LIST}\n- [ ] c`) } });
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

const renderPage = (kind = 'dev-map') => render(
  <MemoryRouter initialEntries={[`/artifacts/${kind}`]}>
    <Routes>
      <Route path="/artifacts/:kind" element={<ArtifactsPage />} />
    </Routes>
  </MemoryRouter>,
);

const openEditor = async () => {
  fireEvent.click(await screen.findByText('v1'));
  return screen.getByPlaceholderText('Artifact content (markdown)...');
};

test('Save sends the text the form was opened on as its base', async () => {
  renderPage();
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: `${LIST}\n- [ ] c` } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(api.put).toHaveBeenCalledWith('/artifacts/dev-map', {
    content: `${LIST}\n- [ ] c`, description: 'Ideas', generated_by: 'user', base_content: LIST,
  }));
  expect(screen.queryByText(NEWER_ARTIFACT_EDIT_MESSAGE)).toBeNull();
});

test('a Save refused for a newer version keeps the form open with the text', async () => {
  api.put.mockRejectedValueOnce(changed());
  renderPage();
  const editor = await openEditor();
  fireEvent.change(editor, { target: { value: 'my rewrite' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  expect(await screen.findByText(NEWER_ARTIFACT_EDIT_MESSAGE)).toBeInTheDocument();
  expect(screen.getByPlaceholderText('Artifact content (markdown)...')).toHaveValue('my rewrite');
  expect(screen.getByRole('button', { name: 'Copy my text' })).toBeInTheDocument();
  // The page does not reload the artifact by itself.
  expect(api.get.mock.calls.filter(([url]) => url === '/artifacts')).toHaveLength(1);
});

test('writing an empty artifact sends an empty base', async () => {
  api.get.mockImplementation((url) => {
    if (url === '/artifacts') return Promise.resolve({ data: { artifacts: [], deleted_kinds: [] } });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  renderPage('memory');
  fireEvent.click(await screen.findByRole('button', { name: /^Write / }));
  fireEvent.change(screen.getByPlaceholderText('Facts the AI should remember about you...'),
    { target: { value: '- lives in Prague' } });
  fireEvent.change(screen.getByPlaceholderText('One-line description — what this artifact is for'),
    { target: { value: 'Facts' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
  expect(api.put.mock.calls[0][1].base_content).toBe('');
});
