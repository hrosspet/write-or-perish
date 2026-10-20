const mockGet = jest.fn();
const mockPatch = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    patch: (...args) => mockPatch(...args),
  },
}));

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { UserProvider, useUser } from './UserContext';

function Username() {
  const { user } = useUser();
  return <span>{user ? user.username : 'nobody'}</span>;
}

beforeEach(() => {
  mockGet.mockReset();
  mockPatch.mockReset();
  mockPatch.mockResolvedValue({});
});

// #481: the app load reads only `user`, so it asks the server to leave out
// the profile, whose decryption it would otherwise pay on every load.
test('the app load asks for the user without the profile', async () => {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  mockGet.mockResolvedValue({ data: { user: { id: 5, username: 'seowriter', timezone } } });
  render(<UserProvider><Username /></UserProvider>);

  await waitFor(() => expect(screen.getByText('seowriter')).toBeInTheDocument());
  expect(mockGet).toHaveBeenCalledTimes(1);
  expect(mockGet).toHaveBeenCalledWith('/dashboard', { params: { profile: 0 } });
});
