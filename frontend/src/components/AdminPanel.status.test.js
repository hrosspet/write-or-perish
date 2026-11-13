// The Users tab's Status cell (#697). Payments on: an account the admin
// blocked says so; payments off: Active or Inactive, as before.
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn(), put: jest.fn() },
}));
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({}) }));

import { statusLabel } from './AdminPanel';

test('payments on: Active, Blocked, Inactive', () => {
  expect(statusLabel({ approved: true, blocked: false }, true)).toBe('Active');
  expect(statusLabel({ approved: false, blocked: true }, true)).toBe('Blocked');
  expect(statusLabel({ approved: false, blocked: false }, true)).toBe('Inactive');
});

test('control: payments off, as before', () => {
  expect(statusLabel({ approved: true }, false)).toBe('Active');
  expect(statusLabel({ approved: false }, false)).toBe('Inactive');
});
