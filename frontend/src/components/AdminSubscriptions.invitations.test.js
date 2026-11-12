// Referrals (#439): the admin's Subscriptions tab lists who invited whom
// and the free week each side got.
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(),
    patch: jest.fn(),
  },
}));

import React from 'react';
import { render, screen } from '@testing-library/react';
import AdminSubscriptions from './AdminSubscriptions';

test('who invited whom, and the free weeks', async () => {
  mockGet.mockImplementation((url) => {
    if (url === '/admin/referrals') {
      return Promise.resolve({ data: { referrals: [
        { id: 2, inviter: 'ines', friend: 'fred', friend_approved: true,
          created_at: '2026-11-01T10:00:00Z', rewarded_at: '2026-11-15T10:00:00Z',
          inviter_reward: 'granted', inviter_credit_cents: 833, inviter_credit_currency: 'usd',
          friend_reward: 'granted', friend_credit_cents: 333, friend_credit_currency: 'usd' },
        { id: 1, inviter: null, friend: 'gus', friend_approved: false,
          created_at: '2026-11-02T10:00:00Z', rewarded_at: null,
          inviter_reward: null, friend_reward: null },
      ] } });
    }
    if (url === '/admin/subscriptions') return Promise.resolve({ data: { subscriptions: [] } });
    return Promise.resolve({ data: { codes: [] } });
  });
  render(<AdminSubscriptions />);
  expect(await screen.findByText('Invitations')).toBeInTheDocument();
  expect(await screen.findByText('ines')).toBeInTheDocument();
  expect(screen.getByText('fred')).toBeInTheDocument();
  expect(screen.getByText('$8.33')).toBeInTheDocument();
  expect(screen.getByText('$3.33')).toBeInTheDocument();
  expect(screen.getByText('(deleted)')).toBeInTheDocument();
  expect(screen.getByText('waiting')).toBeInTheDocument();
});
