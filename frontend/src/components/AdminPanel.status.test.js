// The Users tab's Status cell and "new" marker (#697). Payments on: an
// account the admin blocked says so, and an account made in the last week
// is marked new (no email announces a sign-up then); payments off: Active
// or Inactive, as before.
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn(), put: jest.fn() },
}));
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({}) }));

import { NEW_ACCOUNT_DAYS, isNewAccount, statusLabel } from './AdminPanel';

test('payments on: Active, Blocked, Inactive', () => {
  expect(statusLabel({ approved: true, blocked: false }, true)).toBe('Active');
  expect(statusLabel({ approved: false, blocked: true }, true)).toBe('Blocked');
  expect(statusLabel({ approved: false, blocked: false }, true)).toBe('Inactive');
});

test('control: payments off, as before', () => {
  expect(statusLabel({ approved: true }, false)).toBe('Active');
  expect(statusLabel({ approved: false }, false)).toBe('Inactive');
});

test('new: made in the last week', () => {
  const now = Date.parse('2026-11-13T12:00:00Z');
  const day = 86400 * 1000;
  expect(NEW_ACCOUNT_DAYS).toBe(7);
  expect(isNewAccount({ created_at: new Date(now - 2 * day).toISOString() }, now)).toBe(true);
  expect(isNewAccount({ created_at: new Date(now - 8 * day).toISOString() }, now)).toBe(false);
  expect(isNewAccount({ created_at: null }, now)).toBe(false);
});
