// An entry shared with a circle never quotes an entry one of its readers
// cannot read (Peter, 2026-11-12: "If a user wants to publish a private
// node with a quote of another private node, a dialog stating that this
// is not possible should be displayed"). The check before saving sends
// the text; the server's 409 quotes_not_shared opens the dialog, and
// nothing is saved.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: { id: 1, username: 'anna', default_ai_usage: 'chat', circles_enabled: true },
  }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    put: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

import React, { createRef } from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';

const REFUSAL = {
  response: {
    status: 409,
    data: {
      code: 'quotes_not_shared',
      error: "This can't be shared with these people yet: it quotes an entry that "
        + 'not all of them can read. Share the quoted entry with them first, or '
        + 'make it public, then try again.',
      quoted: [{ id: 7, can_open: true, preview: 'my private note', permalink: '/node/7' }],
    },
  },
};
const created = () => mockPost.mock.calls.filter(([url]) => url === '/nodes/');
const previews = () => mockPost.mock.calls.filter(([url]) => url === '/circles/audience-preview');

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('loore_auto_generate', 'false');
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockImplementation((url) => {
    if (url === '/circles') {
      return Promise.resolve({ data: { circles: [
        { id: 1, name: 'Close friends', members: [{ username: 'ben' }], shared_count: 0 },
      ] } });
    }
    return Promise.reject({ response: { status: 404 } });
  });
  // The preview names ben; with a text that quotes entry 7 it refuses.
  mockPost.mockImplementation((url, body) => {
    if (url === '/circles/audience-preview') {
      if ((body.content || '').includes('{quote:7}')) return Promise.reject(REFUSAL);
      return Promise.resolve({ data: { readers: ['ben'] } });
    }
    if (url === '/nodes/') return Promise.resolve({ data: { id: 41, audience_set: true } });
    return Promise.resolve({ data: {} });
  });
});

const renderNew = async (text) => {
  const ref = createRef();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} onSuccess={() => {}} />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: text } });
  fireEvent.click(await screen.findByRole('button', { name: 'Circles' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Close friends' }));
  await screen.findByText('@ben can read this entry and reply to it. Nobody else can.');
  // The sentence has reached the form (its button names the circle).
  await screen.findByRole('button', { name: 'Share with Close friends' });
  return ref;
};

test('a quote the circle cannot read: "Not shared yet" names it, nothing is saved', async () => {
  const ref = await renderNew('see {quote:7}');
  await act(async () => { await ref.current.submit(); });
  expect(await screen.findByRole('heading', { name: 'Not shared yet' })).toBeInTheDocument();
  expect(screen.getByText(/not all of them can read/)).toBeInTheDocument();
  expect(screen.getByText('my private note').closest('a')).toHaveAttribute('href', '/node/7');
  // The check before saving carried the text; the entry was not created.
  expect(previews().at(-1)[1]).toEqual(expect.objectContaining({ content: 'see {quote:7}' }));
  expect(created()).toHaveLength(0);
  expect(screen.getAllByRole('textbox')[0]).toHaveValue('see {quote:7}');
});

test('without such a quote the entry is shared as before', async () => {
  const ref = await renderNew('An entry.');
  await act(async () => { await ref.current.submit(); });
  await waitFor(() => expect(created()).toHaveLength(1));
  expect(screen.queryByRole('heading', { name: 'Not shared yet' })).toBeNull();
});
