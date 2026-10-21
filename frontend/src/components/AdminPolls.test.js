// Admin polls (#411): an optional "Only these users" field sends the poll
// to named users instead of everyone; the list shows who a poll goes to.
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
  },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { AdminPolls } from './AdminPanel';

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPost.mockResolvedValue({ data: {} });
});

test('shows the audience of a one-user poll only', async () => {
  mockGet.mockImplementation((url) => Promise.resolve({ data:
    url === '/admin/polls'
      ? { default_model_id: 'm1', polls: [
          { id: 1, question: 'For everyone', model_id: 'm1',
            data_source: 'derived', audience: null,
            sent_count: 0, declined_count: 0 },
          { id: 2, question: 'For one', model_id: 'm1',
            data_source: 'derived', audience: ['alice'],
            sent_count: 0, declined_count: 0 },
        ] }
      : { models: [] } }));
  render(<AdminPolls />);
  expect(await screen.findByText(/only alice/)).toBeInTheDocument();
  expect(screen.getAllByText(/only /)).toHaveLength(1);
});

test('sends the typed usernames with the poll, and none when empty', async () => {
  mockGet.mockImplementation((url) => Promise.resolve({ data:
    url === '/admin/polls'
      ? { default_model_id: 'm1', polls: [] }
      : { models: [{ id: 'm1', name: 'M1', kind: 'chat' }] } }));
  render(<AdminPolls />);
  const question = await screen.findByPlaceholderText(/Ask the community/);
  fireEvent.change(question, { target: { value: 'Q?' } });
  fireEvent.change(screen.getByLabelText('Only these users'),
    { target: { value: '@alice, bob' } });
  await waitFor(() => expect(
    screen.getByTitle('Model that drafts answers').value).toBe('m1'));
  fireEvent.click(screen.getByText('Create poll'));
  await waitFor(() => expect(mockPost).toHaveBeenCalled());
  expect(mockPost.mock.calls[0][1].audience_usernames)
    .toEqual(['alice', 'bob']);

  fireEvent.change(question, { target: { value: 'Q2?' } });
  fireEvent.click(screen.getByText('Create poll'));
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
  expect(mockPost.mock.calls[1][1].audience_usernames).toEqual([]);
});

test('a filled audience field with no usable name is refused, not sent to everyone', async () => {
  mockGet.mockImplementation((url) => Promise.resolve({ data:
    url === '/admin/polls'
      ? { default_model_id: 'm1', polls: [] }
      : { models: [{ id: 'm1', name: 'M1', kind: 'chat' }] } }));
  render(<AdminPolls />);
  fireEvent.change(await screen.findByPlaceholderText(/Ask the community/),
    { target: { value: 'Q?' } });
  fireEvent.change(screen.getByLabelText('Only these users'),
    { target: { value: ' @ , ' } });
  await waitFor(() => expect(
    screen.getByTitle('Model that drafts answers').value).toBe('m1'));
  fireEvent.click(screen.getByText('Create poll'));
  expect(await screen.findByText(/No usernames/)).toBeInTheDocument();
  expect(mockPost).not.toHaveBeenCalled();
});
