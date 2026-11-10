// #389: a recording left behind in the box's draft location is offered in
// the voice page's banner; nothing is restored or deleted until the user
// chooses.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'u', preferred_model: 'x' } }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
const mockGet = jest.fn();
const mockPost = jest.fn();
const mockDelete = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    delete: (...a) => mockDelete(...a),
  },
}));

import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';

const draftResponse = {
  id: 7,
  content: '',
  session_id: 'sess-1',
  has_stored_chunks: true,
  updated_at: '2026-10-01T10:00:00',
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockDelete.mockReset();
  mockPost.mockResolvedValue({ data: {} });
  mockDelete.mockResolvedValue({ data: {} });
  mockGet.mockImplementation((url) => {
    if (url.startsWith('/drafts/streaming/')) {
      return Promise.resolve({
        data: { streaming_status: 'completed', content: 'recovered words' },
      });
    }
    if (url.startsWith('/drafts/')) return Promise.resolve({ data: draftResponse });
    return Promise.resolve({ data: {} });
  });
});

const renderForm = () => render(<MemoryRouter><NodeForm /></MemoryRouter>);

test('asks before recovering and touches nothing until a choice', async () => {
  renderForm();
  expect(await screen.findByText('Unfinished Voice recording')).toBeTruthy();
  expect(screen.getByText('Recover')).toBeTruthy();
  expect(screen.getByText('Discard')).toBeTruthy();
  expect(mockPost).not.toHaveBeenCalled();
  expect(mockDelete).not.toHaveBeenCalled();
});

test('Recover transcribes the recording into the box', async () => {
  renderForm();
  fireEvent.click(await screen.findByText('Recover'));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith(
    '/drafts/streaming/sess-1/transcribe-remaining'));
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('recovered words'),
    { timeout: 5000 });
  expect(screen.queryByText('Unfinished Voice recording')).toBeNull();
  expect(mockDelete).not.toHaveBeenCalled();
});

test('Discard deletes the session through the discard path only', async () => {
  renderForm();
  fireEvent.click(await screen.findByText('Discard'));
  await waitFor(() => expect(mockDelete).toHaveBeenCalledWith(
    '/drafts/streaming/sess-1/discard'));
  expect(mockDelete).toHaveBeenCalledTimes(1);
  expect(mockPost).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.queryByText('Unfinished Voice recording')).toBeNull());
  expect(screen.getByRole('textbox').value).toBe('');
});

const renderWithSubmit = (onSubmitOverride) =>
  render(<MemoryRouter><NodeForm onSubmitOverride={onSubmitOverride} /></MemoryRouter>);

test('the banner buttons do not submit the form', async () => {
  const onSubmitOverride = jest.fn();
  renderWithSubmit(onSubmitOverride);
  fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'half-written thought' } });
  fireEvent.click(await screen.findByText('Discard'));
  await waitFor(() => expect(mockDelete).toHaveBeenCalled());
  expect(onSubmitOverride).not.toHaveBeenCalled();
  expect(screen.queryByText(/Content is required/)).toBeNull();
});

test('Recover keeps text typed while the banner was showing', async () => {
  const onSubmitOverride = jest.fn();
  renderWithSubmit(onSubmitOverride);
  fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'my typed paragraph' } });
  fireEvent.click(await screen.findByText('Recover'));
  await waitFor(() => expect(screen.getByRole('textbox').value)
    .toBe('my typed paragraph\n\nrecovered words'), { timeout: 5000 });
  expect(onSubmitOverride).not.toHaveBeenCalled();
});

test('Discard shows the typed draft the recording was hiding', async () => {
  let draftCalls = 0;
  mockGet.mockImplementation((url) => {
    if (url.startsWith('/drafts/')) {
      draftCalls += 1;
      return Promise.resolve({
        data: draftCalls === 1
          ? draftResponse
          : { id: 3, content: 'older typed words', updated_at: '2026-10-01T09:00:00' },
      });
    }
    return Promise.resolve({ data: {} });
  });
  renderForm();
  fireEvent.click(await screen.findByText('Discard'));
  await waitFor(() => expect(screen.getByRole('textbox').value).toBe('older typed words'));
});

test('the box is read-only while the recovered audio is transcribing', async () => {
  renderForm();
  fireEvent.click(await screen.findByText('Recover'));
  expect(await screen.findByText('Transcribing recovered audio...')).toBeTruthy();
  expect(screen.getByRole('textbox').readOnly).toBe(true);
  await waitFor(() => expect(screen.getByRole('textbox').readOnly).toBe(false),
    { timeout: 5000 });
});
