// MarkdownBody pulls in react-markdown (ESM, untransformed by CRA jest);
// the quote text is not what is under test here.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../api', () => ({ post: jest.fn(), delete: jest.fn() }));
jest.mock('../contexts/UserContext', () => ({ useUser: jest.fn() }));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));

import React from 'react';
import { render as rtlRender, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation, useParams } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import ExternalQuoteBubble from './ExternalQuoteBubble';

// The reference page the card opens, standing in for the real one: it
// shows the id and the link back it was handed.
function ReferenceStub() {
  const { id } = useParams();
  const location = useLocation();
  const state = location.state || {};
  return <div>{`Reference page ${id}${state.backTo ? ` back to ${state.backTo} (${state.backLabel})` : ''}`}</div>;
}

const render = (ui) => rtlRender(
  <MemoryRouter initialEntries={['/']}>
    <Routes>
      <Route path="/" element={ui} />
      <Route path="/references/:id" element={<ReferenceStub />} />
    </Routes>
  </MemoryRouter>,
);

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

const openOnX = () => screen.getByRole('button', { name: 'Open on X ↗' });

test('the owner gets a Mark as read toggle that does not open the post', async () => {
  api.post.mockResolvedValue({ data: { id: 42, read_at: '2026-09-13T08:00:00Z' } });
  render(<ExternalQuoteBubble quote={quote()} />);

  fireEvent.click(screen.getByRole('button', { name: 'Mark as read' }));
  expect(api.post).toHaveBeenCalledWith('/external/items/42/read', {});
  expect(window.open).not.toHaveBeenCalled();

  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
  // Nor the reference page.
  expect(screen.queryByText(/Reference page/)).toBeNull();
});

test('a read reference offers Mark as unread and clears the mark', async () => {
  api.delete.mockResolvedValue({ data: { id: 42, read_at: null } });
  render(<ExternalQuoteBubble quote={quote({ read_at: '2026-09-12T20:00:00Z' })} />);

  fireEvent.click(screen.getByRole('button', { name: 'Mark as unread' }));
  expect(api.delete).toHaveBeenCalledWith('/external/items/42/read', { data: {} });

  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as read' })).toBeInTheDocument());
});

test('the owner can rate the quote good or bad without opening anything', async () => {
  api.post.mockResolvedValue({ data: { id: 42, feedback: 'good', feedback_at: '2026-09-13T08:00:00Z' } });
  render(<ExternalQuoteBubble quote={quote()} showRecommendationFeedback />);

  const more = screen.getByRole('button', { name: 'Good quote' });
  expect(more).toHaveAttribute('aria-pressed', 'false');
  fireEvent.click(more);
  expect(api.post).toHaveBeenCalledWith('/external/items/42/feedback', { feedback: 'good' });
  expect(window.open).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole('button', { name: 'Good quote' })).toHaveAttribute('aria-pressed', 'true'));
  expect(screen.getByRole('button', { name: 'Bad quote' })).toHaveAttribute('aria-pressed', 'false');
  expect(screen.queryByText(/Reference page/)).toBeNull();

  // Choosing the selected one again clears it.
  api.post.mockResolvedValue({ data: { id: 42, feedback: null, feedback_at: null } });
  fireEvent.click(screen.getByRole('button', { name: 'Good quote' }));
  expect(api.post).toHaveBeenLastCalledWith('/external/items/42/feedback', { feedback: null });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Good quote' })).toHaveAttribute('aria-pressed', 'false'));
});

test('the read toggle is tinted while unread and muted once read', () => {
  const { unmount } = render(<ExternalQuoteBubble quote={quote()} />);
  expect(screen.getByRole('button', { name: 'Mark as read' })).toHaveAttribute('data-read', 'false');
  unmount();
  render(<ExternalQuoteBubble quote={quote({ read_at: '2026-09-12T20:00:00Z' })} />);
  expect(screen.getByRole('button', { name: 'Mark as unread' })).toHaveAttribute('data-read', 'true');
});

test('someone else viewing the node sees the quote and Open on X, without the toggle', () => {
  useUser.mockReturnValue({ user: { id: 99 } });
  render(<ExternalQuoteBubble quote={quote()} />);
  expect(screen.queryByRole('button', { name: /Mark as/ })).not.toBeInTheDocument();
  expect(openOnX()).toBeInTheDocument();
  expect(screen.getByText('Singing was the original psychedelic.')).toBeInTheDocument();
});

test('someone else viewing an LLM reply sees neither the verdict nor the toggle', () => {
  useUser.mockReturnValue({ user: { id: 99 } });
  render(<ExternalQuoteBubble
    quote={quote({ rated_before: { feedback: 'good', at: '2026-09-22T10:00:00Z' } })}
    showRecommendationFeedback
  />);
  expect(screen.queryByRole('button', { name: /quote/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Mark as/ })).not.toBeInTheDocument();
  expect(screen.queryByText(/You rated this/)).not.toBeInTheDocument();
});

test('in an LLM reply the owner gets the verdict and the read toggle', () => {
  render(<ExternalQuoteBubble quote={quote()} nodeId={5} showRecommendationFeedback />);
  expect(screen.getByRole('button', { name: 'Good quote' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Bad quote' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Mark as read' })).toBeInTheDocument();
});

test('in the user\'s own node the owner gets only the read toggle (#363)', async () => {
  // Nothing recommended a reference the user quoted themselves, so
  // there is no recommendation to judge: no verdict, and no reminder
  // of an earlier one.
  const { unmount } = render(<ExternalQuoteBubble
    quote={quote({ feedback: null, rated_before: { feedback: 'bad', at: '2026-09-22T10:00:00Z' } })}
    nodeId={5}
  />);
  expect(screen.queryByRole('button', { name: 'Good quote' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Bad quote' })).not.toBeInTheDocument();
  expect(screen.queryByText(/You rated this/)).not.toBeInTheDocument();
  unmount();

  // A verdict stored on the reference is not shown there either.
  render(<ExternalQuoteBubble quote={quote({ feedback: 'good' })} nodeId={5} />);
  expect(screen.queryByRole('button', { name: 'Good quote' })).not.toBeInTheDocument();

  api.post.mockResolvedValue({ data: { id: 42, read_at: '2026-09-25T08:00:00Z' } });
  fireEvent.click(screen.getByRole('button', { name: 'Mark as read' }));
  expect(api.post).toHaveBeenCalledWith('/external/items/42/read', { node_id: 5 });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
});

// ── The tweet card (#435) ───────────────────────────────────────────────

test('tapping the card opens the reference page in Loore, not the post on X', () => {
  render(<ExternalQuoteBubble quote={quote()} nodeId={5} backLabel="Today's gleanings" />);
  fireEvent.click(screen.getByText('Singing was the original psychedelic.'));
  expect(window.open).not.toHaveBeenCalled();
  expect(api.post).not.toHaveBeenCalled(); // opening the page is not a read
  expect(screen.getByText("Reference page 42 back to /node/5 (Today's gleanings)")).toBeInTheDocument();
});

test('the card opens the reference page from the keyboard too', () => {
  render(<ExternalQuoteBubble quote={quote()} />);
  fireEvent.keyDown(screen.getByLabelText('Open this reference in Loore'), { key: 'Enter' });
  expect(screen.getByText('Reference page 42')).toBeInTheDocument();
});

test('someone else\'s card is not a link: they cannot open the reference page', () => {
  useUser.mockReturnValue({ user: { id: 99 } });
  render(<ExternalQuoteBubble quote={quote()} />);
  fireEvent.click(screen.getByText('Singing was the original psychedelic.'));
  expect(screen.queryByText(/Reference page/)).toBeNull();
  expect(screen.queryByLabelText('Open this reference in Loore')).toBeNull();
});

test('Open on X opens the post, marks it read and does not open the reference page', async () => {
  api.post.mockResolvedValue({ data: { id: 42, read_at: '2026-09-21T13:00:00Z' } });
  const onReadChange = jest.fn();
  render(<ExternalQuoteBubble quote={quote()} onReadChange={onReadChange} />);

  fireEvent.click(openOnX());
  expect(window.open).toHaveBeenCalledWith('https://x.com/TVachaW/status/1', '_blank', 'noopener,noreferrer');
  expect(api.post).toHaveBeenCalledWith('/external/items/42/read', { via: 'open' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
  expect(onReadChange).toHaveBeenCalledWith(42, '2026-09-21T13:00:00Z');
  expect(screen.queryByText(/Reference page/)).toBeNull();
});

test('opening an already-read post logs the open; someone else\'s logs nothing', async () => {
  api.post.mockResolvedValue({ data: { id: 42, read_at: '2026-09-12T20:00:00Z' } });
  const { unmount } = render(<ExternalQuoteBubble quote={quote({ read_at: '2026-09-12T20:00:00Z' })} nodeId={5} />);
  fireEvent.click(openOnX());
  // Every open is a fact for the recommendation record (#352); the
  // server keeps the original read mark.
  expect(api.post).toHaveBeenCalledWith('/external/items/42/read', { node_id: 5, via: 'open' });
  await act(async () => { await Promise.resolve(); });
  unmount();
  api.post.mockReset();

  useUser.mockReturnValue({ user: { id: 99 } });
  render(<ExternalQuoteBubble quote={quote()} />);
  fireEvent.click(openOnX());
  expect(api.post).not.toHaveBeenCalled();
  expect(window.open).toHaveBeenCalledTimes(2);
});

test('the byline: display name, handle and date; no "Saved from" line, no avatar', () => {
  render(<ExternalQuoteBubble quote={quote({ author_name: 'Tara Vacha', source: 'read_pick' })} />);
  expect(screen.getByText('Tara Vacha')).toBeInTheDocument();
  expect(screen.getByText('@TVachaW')).toBeInTheDocument();
  expect(screen.getByText(/· Dec 24, 2025/)).toBeInTheDocument();
  expect(screen.queryByText(/Saved from/)).toBeNull();
  expect(document.querySelector('img')).toBeNull();
});

test('without a display name the byline is the handle alone', () => {
  render(<ExternalQuoteBubble quote={quote({ source: 'read_pick' })} />);
  expect(screen.getByText('@TVachaW')).toBeInTheDocument();
  expect(document.querySelector('.ext-quote-name')).toBeNull();
});

test('the text is cut at 500 characters with "…"', () => {
  const long = 'x'.repeat(520);
  render(<ExternalQuoteBubble quote={quote({ content: long })} />);
  expect(screen.getByText(`${'x'.repeat(500)}…`)).toBeInTheDocument();
  expect(screen.queryByText(long)).toBeNull();
});

test('a good / bad verdict marks the quote read too, and tells the page', async () => {
  api.post.mockResolvedValue({ data: { id: 42, feedback: 'bad', feedback_at: '2026-09-21T13:00:00Z', read_at: '2026-09-21T13:00:00Z' } });
  const onReadChange = jest.fn();
  render(<ExternalQuoteBubble quote={quote()} onReadChange={onReadChange} showRecommendationFeedback />);

  fireEvent.click(screen.getByRole('button', { name: 'Bad quote' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
  expect(onReadChange).toHaveBeenCalledWith(42, '2026-09-21T13:00:00Z');
  expect(window.open).not.toHaveBeenCalled();
});

test('in a reply, marks and verdicts carry the reply id', async () => {
  api.post.mockResolvedValue({ data: { id: 42, feedback: 'good', read_at: '2026-09-24T08:00:00Z' } });
  render(<ExternalQuoteBubble quote={quote()} nodeId={5} showRecommendationFeedback />);
  fireEvent.click(screen.getByRole('button', { name: 'Good quote' }));
  expect(api.post).toHaveBeenCalledWith('/external/items/42/feedback', { feedback: 'good', node_id: 5 });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
  api.delete.mockResolvedValue({ data: { id: 42, read_at: null } });
  fireEvent.click(screen.getByRole('button', { name: 'Mark as unread' }));
  expect(api.delete).toHaveBeenCalledWith('/external/items/42/read', { data: { node_id: 5 } });
});

test('a verdict from a parallel Read shows, and says where it came from', () => {
  render(<ExternalQuoteBubble quote={quote({ feedback: 'good', feedback_shared: true })} nodeId={5} showRecommendationFeedback />);
  const good = screen.getByRole('button', { name: 'Good quote' });
  expect(good).toHaveAttribute('aria-pressed', 'true');
  expect(good).toHaveAttribute('title', 'Good quote (your rating from another reply)');
});

test('quoted again after a verdict: the control is empty and the earlier verdict is named', () => {
  render(<ExternalQuoteBubble
    quote={quote({ feedback: null, rated_before: { feedback: 'good', at: '2026-09-22T10:00:00Z' } })}
    nodeId={5}
    showRecommendationFeedback
  />);
  expect(screen.getByText(/You rated this good on/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Good quote' })).toHaveAttribute('aria-pressed', 'false');
});
