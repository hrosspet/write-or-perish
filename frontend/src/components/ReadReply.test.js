jest.mock('../api', () => ({ post: jest.fn() }));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import api from '../api';
import { ReadWindowLine, ReadReplyTail } from './ReadReply';

beforeEach(() => {
  api.post.mockReset();
});

test('the window line says which day was read and what was left out', () => {
  render(<ReadWindowLine window={{
    window_start: '2026-09-18T07:02:00Z', window_end: '2026-09-19T07:02:00Z',
    tweets: 4812, accounts: 410, excluded: 37,
  }} />);
  const line = screen.getByText(/Tweets from/);
  expect(line.textContent).toMatch(/^Tweets from 2026\/09\/1[89] \d\d:\d\d to 2026\/09\/(19|20) \d\d:\d\d \(your time\): 4,812 by 410 accounts\. 37 you had already seen were left out\.$/);
});

test('nothing left out, nothing said about it; no window, no line', () => {
  const { container, rerender } = render(<ReadWindowLine window={{
    window_start: '2026-09-18T07:02:00Z', window_end: '2026-09-19T07:02:00Z',
    tweets: 12, accounts: 3, excluded: 0,
  }} />);
  expect(container.textContent).not.toMatch(/left out/);
  rerender(<ReadWindowLine window={null} />);
  expect(container.textContent).toBe('');
});

test('the tail marks the whole list read and reports back', async () => {
  api.post.mockResolvedValue({ data: { read_at: { 5: '2026-09-19T08:00:00Z', 6: '2026-09-19T08:00:00Z' } } });
  const onMarkedAll = jest.fn();
  render(<ReadReplyTail nodeId={77} unread={2} total={6} onMarkedAll={onMarkedAll} onReadAgain={() => {}} />);
  expect(screen.getByText('2 of 6 unread')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Mark all as read' }));
  expect(api.post).toHaveBeenCalledWith('/nodes/77/feed-picks/read');
  await waitFor(() => expect(onMarkedAll).toHaveBeenCalledWith({ 5: '2026-09-19T08:00:00Z', 6: '2026-09-19T08:00:00Z' }));
});

test('an all-read list says so and the read-again action is its own button', () => {
  const onReadAgain = jest.fn();
  render(<ReadReplyTail nodeId={77} unread={0} total={6} onReadAgain={onReadAgain} />);
  expect(screen.getByText('All read.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Mark all as read' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Read again with my marks' }));
  expect(onReadAgain).toHaveBeenCalled();
});

test('while the read runs the button waits; before the quotes load the state is silent', () => {
  const { rerender } = render(<ReadReplyTail nodeId={77} unread={3} total={3} busy onReadAgain={() => {}} />);
  expect(screen.getByRole('button', { name: 'Reading…' })).toBeDisabled();
  rerender(<ReadReplyTail nodeId={77} unread={0} total={3} loaded={false} onReadAgain={() => {}} />);
  expect(screen.queryByText('All read.')).toBeNull();
});

test('the hint says what a second read gets, and asks for marks while there are none', () => {
  const { container, rerender } = render(
    <ReadReplyTail nodeId={77} unread={3} total={3} marked={0} onReadAgain={() => {}} />,
  );
  expect(container.textContent).toMatch(/Nothing marked yet/);
  expect(container.textContent).toMatch(/Text-mode conversation/);
  rerender(<ReadReplyTail nodeId={77} unread={3} total={3} marked={1} onReadAgain={() => {}} />);
  expect(container.textContent).toMatch(/The second read gets your marks/);
  expect(container.textContent).not.toMatch(/Nothing marked yet/);
  // Unknown marks (quotes still loading) are not "none".
  rerender(<ReadReplyTail nodeId={77} unread={3} total={3} marked={0} loaded={false} onReadAgain={() => {}} />);
  expect(container.textContent).not.toMatch(/Nothing marked yet/);
});
