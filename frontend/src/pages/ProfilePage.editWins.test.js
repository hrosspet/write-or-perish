// A version saved by a running build must not replace the text the user is
// editing (#420). The editor keeps the user's text and a notice appears;
// Cancel then shows the newer version.
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import api from '../api';
import ProfilePage from './ProfilePage';

jest.mock('../components/MarkdownBody', () => ({ children }) =>
  require('react').createElement('span', null, children));
jest.mock('../components/VersionHistoryDrawer', () => () => null);
jest.mock('../components/ArtifactsNav', () => () => null);
jest.mock('../components/SpeakerIcon', () => () => null);
jest.mock('../components/RegenerateTtsDialog', () => () => null);
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({ user: {} }) }));
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), put: jest.fn(), post: jest.fn() },
}));

const prof = (id, content) => ({
  id, content, created_at: '2026-10-09T10:00:00Z', generated_by: 'x', tokens_used: 1,
});

let latest;
beforeEach(() => {
  jest.clearAllMocks();
  latest = prof(1, 'old text');
  api.get.mockImplementation((url) => Promise.resolve(
    url === '/dashboard'
      ? { data: { latest_profile: latest } }
      : { data: { versions: [{ id: latest.id }] } }));
});

const chunkLands = () => act(async () => {
  latest = prof(2, 'built text');
  window.dispatchEvent(new CustomEvent('loore_profile_progress', {
    detail: { running: true, latestProfileId: 2, progress: 10 },
  }));
});

const startEditing = async () => {
  render(<ProfilePage />);
  fireEvent.click(await screen.findByText(/^v/));
  const box = await screen.findByRole('textbox');
  fireEvent.change(box, { target: { value: 'my edit' } });
  return box;
};

test('a chunk landing mid-edit keeps the editor text and shows a notice', async () => {
  const box = await startEditing();
  await chunkLands();
  expect(await screen.findByRole('status')).toHaveTextContent(/newer version/i);
  expect(box.value).toBe('my edit');
});

test('cancel then shows the newer version', async () => {
  await startEditing();
  await chunkLands();
  await screen.findByRole('status');
  fireEvent.click(screen.getByText('Cancel'));
  expect(await screen.findByText('built text')).toBeInTheDocument();
});

test('save sends the edit against the version it started from', async () => {
  api.put.mockResolvedValue({ data: {} });
  await startEditing();
  await chunkLands();
  await screen.findByRole('status');
  fireEvent.click(screen.getByText('Save'));
  await waitFor(() => expect(api.put).toHaveBeenCalledWith('/profile/1', { content: 'my edit' }));
});

test('an untouched editor still follows a landing chunk', async () => {
  render(<ProfilePage />);
  fireEvent.click(await screen.findByText(/^v/));
  const box = await screen.findByRole('textbox');
  await chunkLands();
  await waitFor(() => expect(box.value).toBe('built text'));
});
