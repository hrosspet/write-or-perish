// The Glean switch in Account settings (#435, Peter 2026-10-09): shown
// inside the rollout gate (glean_available), set to the user's effective
// value (on by default with Community Archive or X data, which the server
// works out), and saving it stores the user's explicit choice.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: jest.fn(),
    delete: jest.fn(),
    put: (...args) => mockPut(...args),
    get: jest.fn(() => new Promise(() => {})),
  },
}));
jest.mock('../components/ModelSelector', () => () => null);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountPage from './AccountPage';

const baseUser = {
  username: 'ana', email: 'ana@example.com', twitter_login: false,
  pending_email: null, pending_email_expired: false, plan: 'alpha',
};

const renderPage = (user) => {
  mockUserCtx = { user: { ...baseUser, ...user }, setUser: jest.fn() };
  return render(<MemoryRouter><AccountPage /></MemoryRouter>);
};

const gleanSelect = () => {
  const label = screen.queryByText('Glean', { selector: 'div' });
  return label ? label.parentElement.querySelector('select') : null;
};

beforeEach(() => {
  mockPut.mockReset();
});

test('outside the rollout gate there is no Glean switch', () => {
  renderPage({ glean_available: false, glean_enabled: false });
  expect(gleanSelect()).toBeNull();
});

test('inside the gate the switch shows the effective value', () => {
  renderPage({ glean_available: true, glean_enabled: true });
  expect(gleanSelect()).toHaveValue('on');
});

test('turning it off saves the explicit choice', async () => {
  const updated = { ...baseUser, glean_available: true, glean_enabled: false };
  mockPut.mockResolvedValue({ data: { user: updated } });
  renderPage({ glean_available: true, glean_enabled: true });
  fireEvent.change(gleanSelect(), { target: { value: 'off' } });
  expect(mockPut).toHaveBeenCalledWith('/dashboard/user', { glean_enabled: false });
  await waitFor(() => expect(mockUserCtx.setUser).toHaveBeenCalledWith(updated));
});
