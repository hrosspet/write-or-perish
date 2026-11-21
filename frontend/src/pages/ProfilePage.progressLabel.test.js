// The Profile page's label while a build runs, from the app-wide watcher's
// events. A pre-fill load's final merge that goes on after the person was
// let in (#731) shows as a build's final step, without a percentage.
import { render, screen, act } from '@testing-library/react';
import api from '../api';
import ProfilePage from './ProfilePage';

jest.mock('../components/MarkdownBody', () => ({ children }) =>
  require('react').createElement('span', null, children));
jest.mock('../components/VersionHistoryDrawer', () => () => null);
jest.mock('../components/ArtifactsNav', () => () => null);
jest.mock('../components/SpeakerIcon', () => () => null);
jest.mock('../components/RegenerateTtsDialog', () => () => null);
let mockUser = {};
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({ user: mockUser }) }));
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), put: jest.fn(), post: jest.fn() },
}));

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = {};
  api.get.mockImplementation((url) => Promise.resolve(
    url === '/dashboard'
      ? { data: { latest_profile: {
        id: 1, content: 'loaded profile', created_at: '2026-11-21T10:00:00Z',
        generated_by: 'x', tokens_used: 1,
      } } }
      : { data: { versions: [{ id: 1 }] } }));
});

const progress = (detail) => act(async () => {
  window.dispatchEvent(new CustomEvent('loore_profile_progress', {
    detail: { running: true, latestProfileId: 1, ...detail },
  }));
});

test('a late merge shows its step without a percentage', async () => {
  mockUser = { profile_merge_pending: true };
  render(<ProfilePage />);
  await screen.findByText('loaded profile');
  // The payload's flag shows the label before the first answer.
  expect(screen.getByText('Starting generation...')).toBeInTheDocument();
  await progress({ source: 'merge', message: 'Integrating profile versions', progress: 95 });
  expect(screen.getByText('Integrating profile versions')).toBeInTheDocument();
  expect(screen.queryByText(/95%/)).toBeNull();
});

test('the sync task keeps its percentage', async () => {
  render(<ProfilePage />);
  await screen.findByText('loaded profile');
  await progress({ source: 'sync', message: 'Generating profile', progress: 40 });
  expect(screen.getByText('Generating profile · 40%')).toBeInTheDocument();
});
