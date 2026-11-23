// Nobody is shared with unless the sentence on the screen names them:
// Enter in the names field looks the names up and does not share, and a
// save whose readers differ from the sentence is stopped.
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

const CHANGED = 'Who can read this has changed. Check the names above, then share again.';
const created = () => mockPost.mock.calls.filter(([url]) => url === '/nodes/');

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
  // The circle holds ben; each name typed is a person on Loore.
  mockPost.mockImplementation((url, body) => {
    if (url === '/circles/audience-preview') {
      const readers = [...((body.circle_ids || []).length ? ['ben'] : []),
        ...(body.usernames || [])];
      return Promise.resolve({ data: { readers: [...new Set(readers)].sort() } });
    }
    if (url === '/nodes/') return Promise.resolve({ data: { id: 41, audience_set: true } });
    return Promise.resolve({ data: {} });
  });
});

const renderNew = async () => {
  const ref = createRef();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} onSuccess={() => {}} />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'An entry.' } });
  fireEvent.click(await screen.findByRole('button', { name: 'Circles' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Close friends' }));
  await screen.findByText('@ben can read this entry and reply to it. Nobody else can.');
  return ref;
};

const names = () => screen.getByLabelText('Or name people for this entry only');

test('Enter in the names field looks them up and shares nothing', async () => {
  await renderNew();
  fireEvent.change(names(), { target: { value: '@ben, @dana' } });
  // The sentence still names ben only: "@dana" is not finished.
  expect(screen.getByText('@ben can read this entry and reply to it. Nobody else can.'))
    .toBeInTheDocument();
  await act(async () => {
    fireEvent.keyDown(names(), { key: 'Enter', code: 'Enter' });
    fireEvent.submit(names().closest('form'));
  });
  expect(await screen.findByText(
    '@ben and @dana can read this entry and reply to it. Nobody else can.',
  )).toBeInTheDocument();
  // Submitting the form from the field before the sentence named dana
  // shared nothing.
  expect(created()).toHaveLength(0);
});

test('a save whose readers the sentence did not name is stopped, then goes out once it does', async () => {
  const ref = await renderNew();
  fireEvent.change(names(), { target: { value: '@dana' } });
  await act(async () => { await ref.current.submit(); });
  expect(await screen.findByText(CHANGED)).toBeInTheDocument();
  expect(created()).toHaveLength(0);
  expect(await screen.findByText(
    '@ben and @dana can read this entry and reply to it. Nobody else can.',
  )).toBeInTheDocument();
  // The sentence reached the form (a press of the button comes later).
  expect(await screen.findByRole('button', { name: 'Share with 2 people' }))
    .toBeInTheDocument();
  await act(async () => { await ref.current.submit(); });
  await waitFor(() => expect(created()).toHaveLength(1));
  expect(created()[0][1]).toEqual(expect.objectContaining({
    privacy_level: 'circles', circle_ids: [1], circle_usernames: ['dana'],
  }));
});
