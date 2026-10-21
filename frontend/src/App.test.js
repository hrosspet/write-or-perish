import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import App from './App';

// Only the routing table is under test: stub every page and shell piece.
jest.mock('./components/ProtectedRoute', () => ({ children }) => children);
jest.mock('./pages/ProfilePage', () => () => <div>profile-page</div>);
jest.mock('./pages/ArtifactsPage', () => () => <div>artifacts-page</div>);
jest.mock('./contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, approved: true, username: 'u' }, loading: false }),
}));
jest.mock('./contexts/AudioContext', () => ({
  AudioProvider: ({ children }) => children,
  useAudio: () => ({}),
}));
jest.mock('./hooks/useSessionUpdates', () => ({
  useSessionUpdates: () => [[], () => {}],
}));
jest.mock('./hooks/useNodePrefetch', () => () => ({ pending: null, openNode: () => {}, cancel: () => {} }));
jest.mock('./api', () => ({
  __esModule: true,
  default: { get: jest.fn(() => Promise.resolve({ data: {} })), post: jest.fn() },
}));
jest.mock('./components/MarkdownBody', () => () => null);
jest.mock('./components/NavBar',() => () => null);
jest.mock('./components/SpendCapBanner', () => () => null);
jest.mock('./components/DataDeletionBanner', () => () => null);
jest.mock('./components/TermsModal', () => () => null);
jest.mock('./components/UpdatesModal', () => () => null);
jest.mock('./components/SearchModal', () => () => null);
jest.mock('./components/NodeFormModal', () => () => null);
jest.mock('./components/ProfileGenerationWatcher', () => () => null);

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}

const renderAt = (path) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Where />
    </MemoryRouter>
  );

test('/artifacts/profile redirects to the profile page', () => {
  renderAt('/artifacts/profile');
  expect(screen.getByTestId('where').textContent).toBe('/profile');
  expect(screen.getByText('profile-page')).toBeTruthy();
});

test('/profile still renders the profile page', () => {
  renderAt('/profile');
  expect(screen.getByText('profile-page')).toBeTruthy();
});

test('other artifact kinds still use the artifacts page', () => {
  renderAt('/artifacts/memory');
  expect(screen.getByTestId('where').textContent).toBe('/artifacts/memory');
  expect(screen.getByText('artifacts-page')).toBeTruthy();
});
