// Referrals (#439): the invite link on the Account page, the code kept
// from /invite/<code> to the sign-in, and nothing shown about the inviter.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(),
    put: jest.fn(),
    delete: jest.fn(),
  },
}));
jest.mock('./ModelSelector', () => () => null);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import InviteSection from './InviteSection';
import InvitePage from '../pages/InvitePage';
import AccountPage from '../pages/AccountPage';
import {
  INVITE_MAX_AGE_DAYS, forgetInvite, pendingInvite, rememberInvite,
} from '../utils/invite';

const CODE = 'Abc_def-123456XY';

beforeEach(() => {
  mockGet.mockReset();
  window.localStorage.clear();
});

test('the link is shown when asked for, and copied', async () => {
  const writeText = jest.fn().mockResolvedValue();
  Object.assign(navigator, { clipboard: { writeText } });
  mockGet.mockResolvedValue({ data: { url: `https://loore.org/invite/${CODE}` } });
  render(<InviteSection />);
  expect(screen.getByText(/you both get a free week/)).toBeInTheDocument();
  // Nothing is made until asked for.
  expect(mockGet).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Show my invite link'));
  const input = await screen.findByLabelText('Your invite link');
  expect(mockGet).toHaveBeenCalledWith('/referrals/link');
  expect(input.value).toBe(`https://loore.org/invite/${CODE}`);
  fireEvent.click(screen.getByText('Copy'));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(`https://loore.org/invite/${CODE}`));
  expect(await screen.findByText('Copied')).toBeInTheDocument();
});

test('no counts and no names', () => {
  render(<InviteSection />);
  const text = document.body.textContent;
  expect(text).not.toMatch(/\d+ (friends|people|invit)/i);
  expect(text).not.toMatch(/invited \d/i);
});

const baseUser = {
  username: 'ana', email: 'ana@example.com', twitter_login: false,
  pending_email: null, pending_email_expired: false, plan: 'free',
};

test('the Account page shows the section only while referrals are on', () => {
  mockGet.mockReturnValue(new Promise(() => {}));
  mockUserCtx = { user: { ...baseUser }, setUser: jest.fn() };
  const { unmount } = render(<MemoryRouter><AccountPage /></MemoryRouter>);
  expect(screen.queryByText('Invite a friend')).toBeNull();
  unmount();
  mockUserCtx = {
    user: { ...baseUser, payments_enabled: true, referrals_enabled: true, subscription: { status: 'none' } },
    setUser: jest.fn(),
  };
  render(<MemoryRouter><AccountPage /></MemoryRouter>);
  expect(screen.getByText('Invite a friend')).toBeInTheDocument();
});

const renderInvitePage = (user) => {
  mockUserCtx = { user, loading: false };
  render(
    <MemoryRouter initialEntries={[`/invite/${CODE}`]}>
      <Routes>
        <Route path="/invite/:code" element={<InvitePage />} />
        <Route path="/landing" element={<div>landing</div>} />
        <Route path="/" element={<div>home</div>} />
      </Routes>
    </MemoryRouter>,
  );
};

test('/invite/<code> keeps the code and goes to the landing page', async () => {
  renderInvitePage(null);
  expect(await screen.findByText('landing')).toBeInTheDocument();
  expect(pendingInvite()).toBe(CODE);
});

test('someone signed in is not brought by a link', async () => {
  renderInvitePage({ ...baseUser });
  expect(await screen.findByText('home')).toBeInTheDocument();
  expect(pendingInvite()).toBeNull();
});

test('a kept code: malformed codes are refused, old ones expire', () => {
  expect(rememberInvite('../x')).toBe(false);
  expect(pendingInvite()).toBeNull();
  const then = Date.now() - (INVITE_MAX_AGE_DAYS + 1) * 86400000;
  expect(rememberInvite(CODE, then)).toBe(true);
  expect(pendingInvite()).toBeNull();
  rememberInvite(CODE);
  expect(pendingInvite()).toBe(CODE);
  forgetInvite();
  expect(pendingInvite()).toBeNull();
});
