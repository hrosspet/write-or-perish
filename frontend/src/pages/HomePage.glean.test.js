// The home page by purpose (#436, Peter 2026-10-09): for a user who
// gleans, a Reflect card and a Glean card, each with Voice and Text, and
// Share, exactly as today, when it is on. A user without Glean keeps the
// Voice and Text cards. The admin-only Read card is gone. The welcome
// question applies to both cards, as to Voice and Text.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import HomePage from './HomePage';
import { WELCOME_QUESTION, EVERYDAY_QUESTION } from '../utils/entryPrompt';

const renderHome = (user) => {
  mockUserCtx = { user: { username: 'ana', has_own_entries: true, ...user } };
  return render(<MemoryRouter><HomePage /></MemoryRouter>);
};

beforeAll(() => {
  window.IntersectionObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

beforeEach(() => {
  mockNavigate.mockReset();
});

test('a user who gleans gets the Reflect and Glean cards, each by voice or text', () => {
  renderHome({ glean_enabled: true });
  const reflect = screen.getByRole('region', { name: 'Reflect' });
  const glean = screen.getByRole('region', { name: 'Glean' });
  expect(within(reflect).getByText('Talk it through with Loore.')).toBeInTheDocument();
  expect(within(glean).getByText('Reflect, and Loore gleans for you.')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: 'Reflect: Voice' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/voice');
  fireEvent.click(screen.getByRole('button', { name: 'Reflect: Text' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/textmode');
  fireEvent.click(screen.getByRole('button', { name: 'Glean: Voice' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/voice?glean=1');
  fireEvent.click(screen.getByRole('button', { name: 'Glean: Text' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/textmode?glean=1');
  expect(screen.queryByText('Share')).toBeNull();
});

test('Share stays exactly as today when it is on: the same card, opened by a click', () => {
  // The card a user without Glean gets ...
  const { container: today } = renderHome({ glean_enabled: false, share_v1_enabled: true });
  const todays = screen.getByText('Give something outward.').closest('div[style]').outerHTML;
  today.remove();
  // ... is the card a user with Glean gets: no purpose-card look, no button.
  renderHome({ glean_enabled: true, share_v1_enabled: true });
  expect(screen.queryByRole('region', { name: 'Share' })).toBeNull();
  expect(screen.queryByRole('button', { name: /Share/ })).toBeNull();
  const glean = screen.getByText('Give something outward.').closest('div[style]');
  expect(glean.outerHTML).toBe(todays);
  fireEvent.click(screen.getByText('Give something outward.'));
  expect(mockNavigate).toHaveBeenLastCalledWith('/share');
});

test('a user without Glean keeps the Voice and Text cards, and no Read card', () => {
  renderHome({ glean_enabled: false, is_admin: true });
  expect(screen.queryByRole('region', { name: 'Glean' })).toBeNull();
  expect(screen.queryByRole('region', { name: 'Reflect' })).toBeNull();
  expect(screen.getByText('Voice')).toBeInTheDocument();
  expect(screen.getByText('Text')).toBeInTheDocument();
  expect(screen.queryByText('Read')).toBeNull();
  expect(screen.queryByText(/Glean/)).toBeNull();
});

test('the welcome question applies to the Glean card too', () => {
  renderHome({ glean_enabled: true, has_own_entries: false });
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(WELCOME_QUESTION);
  expect(screen.queryByText(EVERYDAY_QUESTION)).toBeNull();
  // Both cards lead to the screens that ask it (VoicePage / WritePage use
  // the same entryQuestion rule, EntryQuestion.test.js).
  expect(screen.getByRole('button', { name: 'Glean: Voice' })).toBeInTheDocument();
});
