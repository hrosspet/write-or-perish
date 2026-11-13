// Referrals (#439): one personal invite a month on the Account page
// (Peter, 2026-11-13), the code kept from /invite/<code> to the sign-in,
// and nothing shown about the inviter.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockGet = jest.fn();
const mockPost = jest.fn();
const mockDelete = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
    put: jest.fn(),
    delete: (...args) => mockDelete(...args),
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
  mockPost.mockReset();
  mockDelete.mockReset();
  window.localStorage.clear();
});

const CREDIT = { cents: 750, currency: 'usd' };
const NONE = { invite: null, can_invite: true, next_invite_on: null, credit: CREDIT };
const OPEN = {
  invite: {
    url: `https://loore.org/invite/${CODE}`, kind: 'x', to: 'ben_x',
    expires_at: '2026-12-13T12:00:00Z',
  },
  can_invite: false, next_invite_on: null, credit: CREDIT,
};
const USED = { invite: null, can_invite: false, next_invite_on: '2026-12-01', credit: CREDIT };

test('one invite a month, made for one person', async () => {
  mockGet.mockResolvedValue({ data: NONE });
  mockPost.mockResolvedValue({ data: OPEN });
  render(<InviteSection />);
  expect(await screen.findByText(/you both get \$7\.50 off your next payment/)).toBeInTheDocument();
  expect(screen.getByText(/invite one person a month/)).toBeInTheDocument();
  const field = screen.getByLabelText("Your friend's X handle or email address");
  fireEvent.change(field, { target: { value: ' @Ben_X ' } });
  fireEvent.click(screen.getByText('Make invite'));
  const link = await screen.findByLabelText('Your invite link');
  expect(mockPost).toHaveBeenCalledWith('/referrals/invite', { to: '@Ben_X' });
  expect(link.value).toBe(`https://loore.org/invite/${CODE}`);
  expect(screen.getByText(/Your invite for @ben_x works until/)).toBeInTheDocument();
  expect(screen.getByText(/works only for them, when they sign up with that X account/)).toBeInTheDocument();
});

test('the open invite: copy and withdraw', async () => {
  const writeText = jest.fn().mockResolvedValue();
  Object.assign(navigator, { clipboard: { writeText } });
  mockGet.mockResolvedValue({ data: OPEN });
  mockDelete.mockResolvedValue({ data: NONE });
  render(<InviteSection />);
  fireEvent.click(await screen.findByText('Copy'));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(`https://loore.org/invite/${CODE}`));
  expect(await screen.findByText('Copied')).toBeInTheDocument();
  fireEvent.click(screen.getByText('Withdraw'));
  expect(await screen.findByText('Make invite')).toBeInTheDocument();
  expect(mockDelete).toHaveBeenCalledWith('/referrals/invite');
  expect(screen.queryByLabelText('Your invite link')).toBeNull();
});

test("this month's invite used: when the next one comes, no counter", async () => {
  mockGet.mockResolvedValue({ data: USED });
  render(<InviteSection />);
  expect(await screen.findByText(
    /You've used this month's invite\. A new one is available on (December 1|1 December)\./,
  )).toBeInTheDocument();
  expect(screen.queryByText('Make invite')).toBeNull();
});

test('a refusal says why', async () => {
  mockGet.mockResolvedValue({ data: NONE });
  mockPost.mockRejectedValue({ response: { data: { error: "That's your own address or handle. Invite someone else." } } });
  render(<InviteSection />);
  fireEvent.change(await screen.findByLabelText("Your friend's X handle or email address"),
    { target: { value: 'ana@example.com' } });
  fireEvent.click(screen.getByText('Make invite'));
  expect(await screen.findByText(/That's your own address/)).toBeInTheDocument();
});

test('no counts and no names', async () => {
  mockGet.mockResolvedValue({ data: USED });
  render(<InviteSection />);
  await screen.findByText(/You've used this month's invite/);
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
