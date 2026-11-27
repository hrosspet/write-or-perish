// The first cohort's group shares, outside /cohort: the audience panel's
// sentence for a group circle (one sentence, no "Nobody else can"), the
// new-entry form that starts shared with the group, and the note about a
// group share (no "Don't show").
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../../api', () => ({
  __esModule: true,
  default: {
    get: (...a) => mockGet(...a),
    post: (...a) => mockPost(...a),
    put: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));
jest.mock('../../contexts/UserContext', () => ({
  useUser: () => ({
    user: { id: 2, username: 'ben', default_ai_usage: 'chat', circles_enabled: true },
  }),
}));
jest.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('../StreamingMicButton', () => ({ __esModule: true, default: () => null }));
jest.mock('../MarkdownBody', () => ({ children }) => <div>{children}</div>);

import React, { createRef } from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import CircleAudiencePicker from '../CircleAudiencePicker';
import NodeForm from '../NodeForm';
import UpdatesModal from '../UpdatesModal';
import { groupReadersSentence, readersSentence } from '../../utils/circles';

const CIRCLES = [
  { id: 33, name: 'First Eight', members: [{ username: 'mara' }, { username: 'peterf' }], shared_count: 1, cohort: { name: 'First Eight', slug: 'first-eight' } },
  { id: 4, name: 'Close friends', members: [{ username: 'dana' }], shared_count: 0 },
];

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('loore_auto_generate', 'false');
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockImplementation((url) => (url === '/circles'
    ? Promise.resolve({ data: { circles: CIRCLES } })
    : Promise.reject({ response: { status: 404 } })));
  mockPost.mockImplementation((url, body) => {
    if (url === '/circles/audience-preview') {
      return Promise.resolve({ data: { readers: (body.circle_ids || []).includes(33) ? ['mara', 'peterf'] : ['dana'] } });
    }
    if (url === '/nodes/') return Promise.resolve({ data: { id: 41, audience_set: true } });
    return Promise.resolve({ data: {} });
  });
});

test('the group sentence names who reads it and the people who join later, without "Nobody else can"', () => {
  expect(groupReadersSentence(['mara', 'peterf'], ['First Eight'])).toBe(
    '@mara and @peterf can read this entry and reply to it, and so will people who join First Eight later.');
  expect(readersSentence(['dana'])).toBe('@dana can read this entry and reply to it. Nobody else can.');
  expect(groupReadersSentence([], ['First Eight'])).toBe('');
});

test('the audience panel with a group circle chosen says one thing', async () => {
  render(
    <MemoryRouter>
      <CircleAudiencePicker circleIds={[33]} namesText="" onChange={() => {}} onResolved={() => {}} />
    </MemoryRouter>,
  );
  expect(await screen.findByText(
    '@mara and @peterf can read this entry and reply to it, and so will people who join First Eight later.',
  )).toBeInTheDocument();
  expect(screen.queryByText(/Nobody else can/)).toBeNull();
});

test('an ordinary circle keeps "Nobody else can"', async () => {
  render(
    <MemoryRouter>
      <CircleAudiencePicker circleIds={[4]} namesText="" onChange={() => {}} onResolved={() => {}} />
    </MemoryRouter>,
  );
  expect(await screen.findByText('@dana can read this entry and reply to it. Nobody else can.')).toBeInTheDocument();
});

test('a new entry can start shared with the group (Write to the group), and is sent so', async () => {
  const ref = createRef();
  render(
    <MemoryRouter>
      <NodeForm ref={ref} parentId={null} initialPrivacyLevel="circles"
        initialCircleIds={[33]} onSuccess={() => {}} />
    </MemoryRouter>,
  );
  expect(await screen.findByRole('button', { name: 'First Eight', pressed: true })).toBeInTheDocument();
  fireEvent.change(screen.getAllByRole('textbox')[0], { target: { value: 'For the group' } });
  // The readers are named before anything is shared.
  expect(await screen.findByText(
    '@mara and @peterf can read this entry and reply to it, and so will people who join First Eight later.',
  )).toBeInTheDocument();
  expect(await screen.findByRole('button', { name: 'Share with First Eight' })).toBeInTheDocument();
  await act(async () => { await ref.current.submit(); });
  const created = mockPost.mock.calls.filter(([u]) => u === '/nodes/');
  expect(created).toHaveLength(1);
  expect(created[0][1]).toMatchObject({ privacy_level: 'circles', circle_ids: [33], content: 'For the group' });
});

const note = (extra) => ({
  id: 9, type: 'circle_share', title: '@mara shared an entry with you.', body: 'Hello',
  link: '/node/16', created_at: '2026-11-27T10:00:00Z', node_id: 16, actor_username: 'mara', ...extra,
});

test('a note about a group share names the group and offers no "Don\'t show"', () => {
  render(
    <MemoryRouter>
      <UpdatesModal onClose={() => {}} data={{ changelog: [], polls: [], notifications: [
        note({ title: '@mara shared an entry with First Eight.', group: 'First Eight' })] }} />
    </MemoryRouter>,
  );
  expect(screen.getByText('@mara shared an entry with First Eight.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Don't show/ })).toBeNull();
  expect(screen.getByRole('button', { name: 'Read it' })).toBeInTheDocument();
});

test('a note about any other share keeps "Don\'t show"', () => {
  render(
    <MemoryRouter>
      <UpdatesModal onClose={() => {}} data={{ changelog: [], polls: [], notifications: [note({ group: null })] }} />
    </MemoryRouter>,
  );
  expect(screen.getByRole('button', { name: "Don't show @mara's entries" })).toBeInTheDocument();
});
