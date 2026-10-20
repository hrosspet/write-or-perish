// MarkdownBody pulls in react-markdown (ESM, untransformed by CRA jest).
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../api', () => ({ post: jest.fn(), delete: jest.fn() }));
jest.mock('../contexts/UserContext', () => ({ useUser: jest.fn() }));
const mockToast = jest.fn();
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: mockToast }) }));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import PickVerdict from './PickVerdict';
import ExternalQuoteBubble from './ExternalQuoteBubble';

const URL = '/nodes/5/feed-picks/42/verdict';
const answer = (overrides) => ({
  data: { node_id: 5, item_id: 42, intro_verdict: null, feedback: null, feedback_shared: false, read_at: null, ...overrides },
});
const btn = (name) => screen.getByRole('button', { name });
const queryBtn = (name) => screen.queryByRole('button', { name });

beforeEach(() => {
  api.post.mockReset();
  mockToast.mockReset();
  useUser.mockReturnValue({ user: { id: 7 } });
});

test('at rest a pick has one pair, Good pick / Bad pick', () => {
  render(<PickVerdict nodeId={5} itemId={42} />);
  expect(btn('Good pick')).toHaveAttribute('aria-pressed', 'false');
  expect(btn('Bad pick')).toBeInTheDocument();
  expect(screen.queryByText('The intro')).toBeNull();
  expect(screen.queryByText('The tweet')).toBeNull();
});

test('a tap on the pair rates both parts in one request and opens it, focus on the intro', async () => {
  api.post.mockResolvedValue(answer({ intro_verdict: 'good', feedback: 'good', read_at: '2026-10-14T08:00:00Z' }));
  const onChange = jest.fn();
  render(<PickVerdict nodeId={5} itemId={42} onChange={onChange} />);
  fireEvent.click(btn('Good pick'));
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith(URL, { intro: 'good', tweet: 'good' });

  await waitFor(() => expect(btn('Good intro')).toHaveAttribute('aria-pressed', 'true'));
  expect(btn('Good tweet')).toHaveAttribute('aria-pressed', 'true');
  expect(btn('Bad intro')).toHaveAttribute('aria-pressed', 'false');
  expect(btn('Bad tweet')).toHaveAttribute('aria-pressed', 'false');
  expect(screen.getByText('The intro')).toBeInTheDocument();
  expect(screen.getByText('The tweet')).toBeInTheDocument();
  expect(queryBtn('Good pick')).toBeNull();
  expect(document.activeElement).toBe(btn('Good intro'));
  expect(onChange).toHaveBeenCalledWith(
    expect.objectContaining({ intro_verdict: 'good', feedback: 'good' }), { intro: 'good', tweet: 'good' });
});

test('a lit glyph clears its part, the other changes it, and each part sends only itself', async () => {
  render(<PickVerdict nodeId={5} itemId={42} intro="good" tweet="good" />);
  api.post.mockResolvedValueOnce(answer({ intro_verdict: null, feedback: 'good' }));
  fireEvent.click(btn('Good intro'));
  expect(api.post).toHaveBeenLastCalledWith(URL, { intro: null });
  await waitFor(() => expect(btn('Good intro')).toHaveAttribute('aria-pressed', 'false'));
  // Still open: the tweet keeps its verdict.
  expect(btn('Good tweet')).toHaveAttribute('aria-pressed', 'true');

  api.post.mockResolvedValueOnce(answer({ intro_verdict: null, feedback: 'bad' }));
  fireEvent.click(btn('Bad tweet'));
  expect(api.post).toHaveBeenLastCalledWith(URL, { tweet: 'bad' });
  await waitFor(() => expect(btn('Bad tweet')).toHaveAttribute('aria-pressed', 'true'));
  expect(btn('Good tweet')).toHaveAttribute('aria-pressed', 'false');
});

test('with both parts cleared the pair closes again, focus on the pick', async () => {
  render(<PickVerdict nodeId={5} itemId={42} tweet="bad" />);
  // A pick with a verdict opens on load.
  expect(btn('Bad tweet')).toHaveAttribute('aria-pressed', 'true');
  expect(btn('Good intro')).toHaveAttribute('aria-pressed', 'false');
  api.post.mockResolvedValue(answer());
  fireEvent.click(btn('Bad tweet'));
  expect(api.post).toHaveBeenCalledWith(URL, { tweet: null });
  await waitFor(() => expect(btn('Bad pick')).toBeInTheDocument());
  expect(queryBtn('Bad tweet')).toBeNull();
  expect(document.activeElement).toBe(btn('Bad pick'));
});

test('an intro verdict alone opens the pick on load', () => {
  render(<PickVerdict nodeId={5} itemId={42} intro="bad" />);
  expect(btn('Bad intro')).toHaveAttribute('aria-pressed', 'true');
  expect(btn('Good tweet')).toHaveAttribute('aria-pressed', 'false');
});

test("the tweet's verdict from a parallel Read says so", () => {
  render(<PickVerdict nodeId={5} itemId={42} tweet="good" tweetShared />);
  expect(screen.getByRole('button', { name: 'Good tweet (your rating from another reply)' }))
    .toHaveAttribute('aria-pressed', 'true');
});

test('the verdict the model knew is named beside an unrated tweet', () => {
  render(<PickVerdict nodeId={5} itemId={42} ratedBefore={{ feedback: 'good', at: '2026-10-12T10:00:00Z' }} />);
  expect(screen.getByText(/You rated this tweet good on/)).toBeInTheDocument();
});

test('a failed save says so and changes nothing', async () => {
  api.post.mockRejectedValue(new Error('500'));
  render(<PickVerdict nodeId={5} itemId={42} />);
  fireEvent.click(btn('Good pick'));
  await waitFor(() => expect(mockToast).toHaveBeenCalledWith('Could not save your verdict.', 4000));
  expect(btn('Good pick')).toBeInTheDocument();
});

test('a second tap while one is saving is ignored', async () => {
  let resolve;
  api.post.mockReturnValue(new Promise((r) => { resolve = r; }));
  render(<PickVerdict nodeId={5} itemId={42} />);
  fireEvent.click(btn('Good pick'));
  fireEvent.click(btn('Bad pick'));
  expect(api.post).toHaveBeenCalledTimes(1);
  resolve(answer({ intro_verdict: 'good', feedback: 'good' }));
  await waitFor(() => expect(btn('Good intro')).toBeInTheDocument());
});

// ── In the gleaning's tweet card ─────────────────────────────────────────

const pickQuote = (overrides) => ({
  id: 42,
  content: 'Six months of daily writing.',
  source: 'read_pick',
  author_handle: 'ineswrites',
  url: 'https://x.com/ineswrites/status/1',
  user_id: 7,
  read_at: null,
  feedback: null,
  recommendation_id: 9,
  recommendation_kind: 'read',
  intro_verdict: null,
  ...overrides,
});

const inRouter = (ui) => render(<MemoryRouter>{ui}</MemoryRouter>);

test("a gleaning's card has no verdict in its footer; the pick's pair is under it", () => {
  const { container } = inRouter(
    <ExternalQuoteBubble quote={pickQuote()} nodeId={5} showRecommendationFeedback />);
  const card = container.querySelector('.ext-quote-card');
  expect(card.querySelector('.ref-feedback')).toBeNull();
  expect(card.textContent).toContain('Twitter ↗');
  expect(card.textContent).toContain('Mark as read');
  expect(queryBtn('Good quote')).toBeNull();
  const foot = container.querySelector('.pick-foot');
  expect(card.contains(foot)).toBe(false);
  expect(card.nextElementSibling).toBe(foot);
  expect(btn('Good pick')).toBeInTheDocument();
});

test('a quote outside a gleaning keeps its one verdict in the card', () => {
  const { container } = inRouter(
    <ExternalQuoteBubble quote={pickQuote({ recommendation_kind: 'quote' })} nodeId={5} showRecommendationFeedback />);
  expect(container.querySelector('.pick-foot')).toBeNull();
  expect(btn('Good quote')).toBeInTheDocument();
});

test("the pick's verdicts tell the page, and the tweet's marks it read", async () => {
  api.post.mockResolvedValue(answer({ intro_verdict: 'bad', feedback: 'bad', read_at: '2026-10-14T08:00:00Z' }));
  const onFeedbackChange = jest.fn();
  const onIntroVerdictChange = jest.fn();
  const onReadChange = jest.fn();
  inRouter(
    <ExternalQuoteBubble
      quote={pickQuote()}
      nodeId={5}
      showRecommendationFeedback
      onFeedbackChange={onFeedbackChange}
      onIntroVerdictChange={onIntroVerdictChange}
      onReadChange={onReadChange}
    />);
  fireEvent.click(btn('Bad pick'));
  await waitFor(() => expect(btn('Mark as unread')).toBeInTheDocument());
  expect(onFeedbackChange).toHaveBeenCalledWith(42, 'bad');
  expect(onIntroVerdictChange).toHaveBeenCalledWith(42, 'bad');
  expect(onReadChange).toHaveBeenCalledWith(42, '2026-10-14T08:00:00Z');
});
