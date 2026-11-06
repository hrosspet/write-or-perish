// Profile and Todo have the same ⋮ menu as an artifact, with Edit only
// (#603). Peter, 2026-11-06: "profile and todo not being artifacts is only
// our internal thing, they should behave the same as Predictions which are
// built in, so can't be deleted, but are editable."
import { render, screen, fireEvent } from '@testing-library/react';
import api from '../api';
import ProfilePage from './ProfilePage';
import TodoPage from './TodoPage';

jest.mock('../components/MarkdownBody', () => ({ children }) =>
  require('react').createElement('div', null, children));
jest.mock('../components/VersionHistoryDrawer', () => () => null);
jest.mock('../components/ArtifactsNav', () => () => null);
jest.mock('../components/SpeakerIcon', () => () => null);
jest.mock('../components/RegenerateTtsDialog', () => () => null);
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({ user: { id: 1 } }) }));
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: jest.fn(), post: jest.fn(), put: jest.fn(), patch: jest.fn(), delete: jest.fn(),
  },
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));

const PROFILE = {
  id: 7, content: 'Ana writes about slow evenings.', created_at: '2026-10-28T10:00:00Z',
  generated_by: 'claude-opus-5.5', tokens_used: 1200, privacy_level: 'private',
  ai_usage: 'chat', has_tts: false,
};
const TODO = {
  id: 3, content: '## Today\n- [ ] call the landlord', revision: 'r1', version_number: 2,
  generated_by: 'user', created_at: '2026-10-28T10:00:00Z',
};

beforeEach(() => {
  jest.clearAllMocks();
  api.get.mockImplementation((url) => {
    if (url === '/dashboard') return Promise.resolve({ data: { latest_profile: PROFILE } });
    if (url === '/profile/versions') return Promise.resolve({ data: { versions: [{ id: 7 }] } });
    if (url === '/todo') return Promise.resolve({ data: { todo: TODO } });
    if (url.startsWith('/todo')) return Promise.resolve({ data: { versions: [] } });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
});

test.each([
  ['Profile', ProfilePage, PROFILE.content],
  ['Todo', TodoPage, TODO.content],
])('%s has Edit and no Delete in its ⋮ menu, and Edit opens the form', async (_, Page, content) => {
  render(<Page />);
  fireEvent.click(await screen.findByRole('button', { name: 'More actions' }));
  expect(screen.getByText('Edit')).toBeInTheDocument();
  expect(screen.queryByText('Delete')).not.toBeInTheDocument();

  fireEvent.click(screen.getByText('Edit'));
  expect((await screen.findByRole('textbox')).value).toBe(content);
  // While the form is open the menu is gone.
  expect(screen.queryByRole('button', { name: 'More actions' })).not.toBeInTheDocument();
  expect(api.delete).not.toHaveBeenCalled();
});
