// A reader who hid a person can still be in that person's circles or
// named on their entries, which they cannot open to leave from: the
// list of hidden people offers Leave for them (Peter, 2026-11-23).
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 2, username: 'ben', circles_enabled: true } }),
}));

import React from 'react';
import { render, screen, act, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SharedWithYouPage from './SharedWithYouPage';

let page;
beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  page = { people: [], hidden: ['anna', 'dana'], hidden_leavable: ['anna'], next_before: null };
  mockGet.mockImplementation(() => Promise.resolve({ data: page }));
  mockPost.mockImplementation((url) => {
    if (url === '/circles/leave/preview') {
      return Promise.resolve({ data: { author: 'anna', closes: 2, circles: 1 } });
    }
    if (url === '/circles/leave') {
      page = { ...page, hidden_leavable: [] };
      return Promise.resolve({ data: { left: true, author: 'anna', closes: 2, circles: 1 } });
    }
    return Promise.reject(new Error(`unexpected POST ${url}`));
  });
});

test('Leave is offered for a hidden person who still holds the reader, and leaves her', async () => {
  render(<MemoryRouter><SharedWithYouPage /></MemoryRouter>);
  const hidden = await screen.findByRole('region', { name: 'People you have hidden' });
  // Only anna still holds ben; dana does not.
  expect(within(hidden).getAllByRole('button', { name: 'Leave' })).toHaveLength(1);
  await act(async () => {
    fireEvent.click(within(hidden).getByRole('button', { name: 'Leave' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/circles/leave/preview', { username: 'anna' });
  expect(screen.getByRole('heading', { name: 'Leave everything @anna shares with you?' }))
    .toBeInTheDocument();
  expect(screen.getByText(
    "You leave @anna's circles and the entries @anna named you for, and "
    + 'nobody sees your name among their readers any more, @anna included.',
  )).toBeInTheDocument();
  const dialog = screen.getByRole('dialog');
  await act(async () => {
    fireEvent.click(within(dialog).getByRole('button', { name: 'Leave' }));
  });
  expect(mockPost).toHaveBeenCalledWith('/circles/leave', { username: 'anna' });
  // The page is read again: anna stays hidden, with nothing left to leave.
  expect(screen.queryByRole('dialog')).toBeNull();
  const after = screen.getByRole('region', { name: 'People you have hidden' });
  expect(within(after).queryByRole('button', { name: 'Leave' })).toBeNull();
  expect(within(after).getByText('@anna')).toBeInTheDocument();
});
