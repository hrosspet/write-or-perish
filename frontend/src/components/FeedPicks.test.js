// MarkdownBody pulls in react-markdown (ESM, untransformed by CRA jest);
// the tweet text is not what is under test here.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../api', () => ({ get: jest.fn(), post: jest.fn(), delete: jest.fn() }));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import api from '../api';
import FeedPicks from './FeedPicks';

const pick = (item) => ({
  rank: 1, relevance: 40, recommended: true, why: 'fits your question', picked_by: null,
  item: {
    id: 5, author_handle: 'bob_b', content: 'second tweet',
    url: 'https://x.com/bob_b/status/222', posted_at: '2026-09-13T10:00:00Z',
    read_at: null, feedback: null, ...item,
  },
});

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
});

test('Open on X marks the pick read; Mark as unread stays to undo it', async () => {
  api.get.mockResolvedValue({ data: { picks: [pick()] } });
  api.post.mockResolvedValue({ data: { id: 5, read_at: '2026-09-21T13:00:00Z' } });
  render(<FeedPicks nodeId={9} />);

  fireEvent.click(await screen.findByRole('link', { name: 'Open on X' }));
  expect(api.post).toHaveBeenCalledWith('/external/items/5/read', { node_id: 9, via: 'open' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
  expect(screen.getByText('All read.')).toBeInTheDocument();
});

test('opening an already-read pick logs the open and keeps the mark', async () => {
  api.get.mockResolvedValue({ data: { picks: [pick({ read_at: '2026-09-12T20:00:00Z' })] } });
  api.post.mockResolvedValue({ data: { id: 5, read_at: '2026-09-12T20:00:00Z' } });
  render(<FeedPicks nodeId={9} />);

  fireEvent.click(await screen.findByRole('link', { name: 'Open on X' }));
  expect(api.post).toHaveBeenCalledWith('/external/items/5/read', { node_id: 9, via: 'open' });
  await waitFor(() => expect(screen.getByText('All read.')).toBeInTheDocument());
});

test('rating a pick marks it read too', async () => {
  api.get.mockResolvedValue({ data: { picks: [pick()] } });
  api.post.mockResolvedValue({ data: { id: 5, feedback: 'good', feedback_at: '2026-09-21T13:00:00Z', read_at: '2026-09-21T13:00:00Z' } });
  render(<FeedPicks nodeId={9} />);

  fireEvent.click(await screen.findByRole('button', { name: 'Good quote' }));
  expect(api.post).toHaveBeenCalledWith('/external/items/5/feedback', { feedback: 'good', node_id: 9 });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
  expect(screen.getByText('All read.')).toBeInTheDocument();
});

test('a verdict given here is no longer labelled as another reply\'s', async () => {
  api.get.mockResolvedValue({ data: { picks: [pick({ feedback: 'good', feedback_shared: true, read_at: '2026-09-21T13:00:00Z' })] } });
  api.post.mockResolvedValue({ data: { id: 5, feedback: 'bad', read_at: '2026-09-21T13:00:00Z' } });
  render(<FeedPicks nodeId={9} />);

  const good = await screen.findByRole('button', { name: 'Good quote' });
  expect(good).toHaveAttribute('title', 'Good quote (your rating from another reply)');
  fireEvent.click(screen.getByRole('button', { name: 'Bad quote' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Bad quote' })).toHaveAttribute('aria-pressed', 'true'));
  expect(screen.getByRole('button', { name: 'Bad quote' })).toHaveAttribute('title', 'Bad quote');
});
