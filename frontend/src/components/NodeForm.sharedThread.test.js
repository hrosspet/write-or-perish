// Peter, 2026-11-12: "autogenerate is off in public threads and circles
// and can't be turned on", and a public node never quotes one that isn't
// public. A public (or circles) entry shows Auto-generate off and
// disabled and asks for no reply; a private one keeps the stored
// preference. A save the server refuses with quotes_not_public opens a
// dialog naming the quoted entries, and the form keeps its text.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({
    user: {
      id: 1, username: 'u', default_ai_usage: 'chat',
      default_privacy_level: 'private',
    },
  }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('./StreamingMicButton', () => ({ __esModule: true, default: () => null }));
const mockGet = jest.fn();
const mockPost = jest.fn();
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    put: (...a) => mockPut(...a),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

import React, { createRef } from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeForm from './NodeForm';
import { isSharedPrivacy, quotesNotPublicError } from '../utils/sharedThread';

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('loore_agentic_reply', 'true');
  localStorage.setItem('loore_auto_generate', 'true');
  mockGet.mockReset();
  mockPost.mockReset();
  mockPut.mockReset();
  mockGet.mockRejectedValue({ response: { status: 404 } });
  mockPost.mockImplementation((url) => {
    if (url === '/textmode/start') {
      return Promise.resolve({ data: { conversation_id: 10, user_node_id: 20, tip_id: 20 } });
    }
    return Promise.resolve({ data: {} });
  });
});

const renderForm = (props = {}) => {
  const ref = createRef();
  const onSuccess = jest.fn();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} parentId={null} allowAgenticPrompt onSuccess={onSuccess} {...props} />
    </MemoryRouter>,
  );
  return { ref, onSuccess };
};

const autoGenerateButton = () => screen.getByText('Auto-generate').closest('button');

test('the shared test: anything but private', () => {
  expect(isSharedPrivacy('public')).toBe(true);
  expect(isSharedPrivacy('circles')).toBe(true);
  expect(isSharedPrivacy('private')).toBe(false);
  expect(isSharedPrivacy(undefined)).toBe(false);
});

test('a private entry keeps the stored Auto-generate', async () => {
  const { ref } = renderForm();
  expect(autoGenerateButton()).not.toBeDisabled();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hello' } });
  await act(async () => { await ref.current.submit(); });
  expect(mockPost.mock.calls[0][1]).toEqual(
    expect.objectContaining({ privacy_level: 'private', auto_generate: true }));
});

test('a public entry shows Auto-generate off, disabled, and asks for no reply', async () => {
  localStorage.setItem('loore_last_privacy_level', 'public');
  const { ref } = renderForm();
  const button = autoGenerateButton();
  expect(button).toBeDisabled();
  expect(button).toHaveAttribute('aria-pressed', 'false');
  expect(screen.getByText(/Auto-generate is off in public threads/)).toBeInTheDocument();
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hello' } });
  await act(async () => { await ref.current.submit(); });
  expect(mockPost.mock.calls[0][1]).toEqual(
    expect.objectContaining({ privacy_level: 'public', auto_generate: false }));
  // The stored preference for private entries is untouched.
  expect(localStorage.getItem('loore_auto_generate')).toBe('true');
});

test('a refused publish opens the dialog naming the quoted entry', async () => {
  mockPut.mockRejectedValue({
    response: {
      status: 409,
      data: {
        code: 'quotes_not_public',
        error: "This can't be made public yet: it quotes an entry that isn't public.",
        quoted: [
          { id: 7, can_open: true, preview: 'my private draft', permalink: '/node/7' },
          { id: 9, can_open: false },
        ],
      },
    },
  });
  const { ref, onSuccess } = renderForm({
    editMode: true, nodeId: 5, initialContent: 'see {quote:7}',
    initialPrivacyLevel: 'private', initialAiUsage: 'chat',
  });
  await act(async () => { await ref.current.submit(); });

  expect(screen.getByRole('dialog')).toBeInTheDocument();
  expect(screen.getByText("This can't be made public yet: it quotes an entry that isn't public."))
    .toBeInTheDocument();
  expect(screen.getByText('my private draft').closest('a')).toHaveAttribute('href', '/node/7');
  expect(screen.getByText(/An entry you can't open \(#9\)/)).toBeInTheDocument();
  expect(onSuccess).not.toHaveBeenCalled();
  expect(screen.getByRole('textbox')).toHaveValue('see {quote:7}');
});

test('quotesNotPublicError reads only that refusal', () => {
  expect(quotesNotPublicError({ response: { status: 409, data: { code: 'other' } } })).toBeNull();
  expect(quotesNotPublicError({ response: { status: 409, data: { code: 'quotes_not_public', error: 'x', quoted: [{ id: 1 }] } } }))
    .toEqual({ message: 'x', quoted: [{ id: 1 }] });
});
