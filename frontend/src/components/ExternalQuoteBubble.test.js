// MarkdownBody pulls in react-markdown (ESM, untransformed by CRA jest);
// the quote text is not what is under test here.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../api', () => ({ post: jest.fn(), delete: jest.fn() }));
jest.mock('../contexts/UserContext', () => ({ useUser: jest.fn() }));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import ExternalQuoteBubble from './ExternalQuoteBubble';

const quote = (overrides) => ({
  id: 42,
  content: 'Singing was the original psychedelic.',
  source: 'twitter_bookmark',
  author_handle: 'TVachaW',
  url: 'https://x.com/TVachaW/status/1',
  posted_at: '2025-12-24T10:00:00Z',
  user_id: 7,
  read_at: null,
  ...overrides,
});

beforeEach(() => {
  api.post.mockReset();
  api.delete.mockReset();
  useUser.mockReturnValue({ user: { id: 7 } });
  window.open = jest.fn();
});

test('the owner gets a Mark as read toggle that does not open the post', async () => {
  api.post.mockResolvedValue({ data: { id: 42, read_at: '2026-09-13T08:00:00Z' } });
  render(<ExternalQuoteBubble quote={quote()} />);

  fireEvent.click(screen.getByRole('button', { name: 'Mark as read' }));
  expect(api.post).toHaveBeenCalledWith('/external/items/42/read');
  expect(window.open).not.toHaveBeenCalled();

  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
  expect(screen.getByText('Read')).toBeInTheDocument();
});

test('a read reference offers Mark as unread and clears the mark', async () => {
  api.delete.mockResolvedValue({ data: { id: 42, read_at: null } });
  render(<ExternalQuoteBubble quote={quote({ read_at: '2026-09-12T20:00:00Z' })} />);

  expect(screen.getByText('Read')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Mark as unread' }));
  expect(api.delete).toHaveBeenCalledWith('/external/items/42/read');

  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as read' })).toBeInTheDocument());
  expect(screen.queryByText('Read')).not.toBeInTheDocument();
});

test('someone else viewing the node sees the quote without the toggle', () => {
  useUser.mockReturnValue({ user: { id: 99 } });
  render(<ExternalQuoteBubble quote={quote()} />);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
  expect(screen.getByText('Singing was the original psychedelic.')).toBeInTheDocument();
});

test('clicking the quote body still opens the original post', () => {
  render(<ExternalQuoteBubble quote={quote()} />);
  fireEvent.click(screen.getByText('Singing was the original psychedelic.'));
  expect(window.open).toHaveBeenCalledWith('https://x.com/TVachaW/status/1', '_blank', 'noopener,noreferrer');
});
