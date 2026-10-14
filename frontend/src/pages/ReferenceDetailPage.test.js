// MarkdownBody pulls in react-markdown (ESM, untransformed by CRA jest);
// the text itself is what is checked here, not its rendering.
jest.mock('../components/MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../api', () => ({
  get: jest.fn(), post: jest.fn(), delete: jest.fn(), put: jest.fn(),
}));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));
// The speaker needs the audio and user contexts; the page only places it.
jest.mock('../components/SpeakerIcon', () => () => <button type="button">Play audio</button>);
jest.mock('../components/NodeFormModal', () => ({ children }) => <div>{children}</div>);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../api';
import ReferenceDetailPage from './ReferenceDetailPage';

const tweet = {
  id: 42,
  source: 'community_archive',
  external_id: '1844000000000000000',
  author_handle: 'miraokafor',
  author_name: 'Mira Okafor',
  title: null,
  content: 'We keep looking for a better method when what most of us lack is an unhurried evening.',
  url: 'https://x.com/miraokafor/status/1844000000000000000',
  posted_at: '2026-10-12T21:07:00Z',
  fetched_at: '2026-10-13T08:00:00Z',
  read_at: null,
  feedback: null,
  edited_at: null,
  has_tts: false,
  surfaced_count: 1,
  last_surfaced_at: '2026-10-13T08:00:00Z',
};

const page = {
  ...tweet,
  id: 43,
  source: 'web_clip',
  external_id: 'a'.repeat(64),
  author_handle: 'Jane Doe',
  author_name: null,
  title: 'On slow evenings',
  content: '# On slow evenings\n\nThe first paragraph of the essay.',
  url: 'https://www.example.com/essays/slow-evenings',
  surfaced_count: 0,
  last_surfaced_at: null,
};

const video = {
  ...page,
  id: 44,
  author_handle: 'Some Channel',
  title: 'A talk on attention',
  content: '# A talk on attention\n\n0:00 Intro\n4:10 The argument',
  url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=90',
};

const show = (item, state) => {
  api.get.mockResolvedValue({ data: item });
  return render(
    <MemoryRouter initialEntries={[{ pathname: `/references/${item.id}`, state }]}>
      <Routes>
        <Route path="/references/:id" element={<ReferenceDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
};

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset();
  api.delete.mockReset();
  // Opening the page marks the reference read (Peter, 2026-10-14).
  api.post.mockImplementation((url) => Promise.resolve({
    data: { id: Number(url.split('/')[3]), read_at: '2026-10-14T09:00:00Z' },
  }));
  window.open = jest.fn();
});

test('a tweet shows its stored text in the card, with no embed and no toggle', async () => {
  const { container } = show(tweet);
  expect(await screen.findByText(tweet.content)).toBeInTheDocument();

  expect(screen.getByText('Tweet from the Community Archive')).toBeInTheDocument();
  expect(screen.getByText('Mira Okafor')).toBeInTheDocument();
  expect(screen.getByText('@miraokafor').closest('a'))
    .toHaveAttribute('href', 'https://x.com/miraokafor');
  // No X widget: no iframe, no widgets.js, no stored-text toggle.
  expect(container.querySelector('iframe')).toBeNull();
  expect(document.querySelector('script[src*="platform.twitter.com"]')).toBeNull();
  expect(screen.queryByText('Show stored text')).toBeNull();
  // The card has no accent rail, and the source is no longer a chip.
  const card = container.querySelector('.ref-card');
  expect(card.style.borderLeft).toBe('');
  expect(screen.queryByText('Archive tweet')).toBeNull();

  expect(screen.getByRole('link', { name: 'Twitter ↗' })).toHaveAttribute('href', tweet.url);
  expect(screen.getByRole('button', { name: 'Good quote' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument();
  expect(screen.getByText(/^Shown by Loore 1× · last /)).toBeInTheDocument();
});

test('a page shows its source, byline, title once and the text', async () => {
  show(page);
  expect(await screen.findByRole('heading', { name: 'On slow evenings' })).toBeInTheDocument();

  expect(screen.getByText('Page from example.com')).toBeInTheDocument();
  expect(screen.getByText('Jane Doe')).toBeInTheDocument();
  // The heading that repeats the title is dropped from the text.
  const text = screen.getByTestId('reference-text').textContent;
  expect(text).toContain('The first paragraph of the essay.');
  expect(text).not.toContain('On slow evenings');
  expect(screen.getByRole('link', { name: 'Open page ↗' })).toHaveAttribute('href', page.url);
  expect(screen.getByText(/Not yet shown by Loore in a conversation/)).toBeInTheDocument();
});

test('a video keeps its player in the card and its description behind the toggle', async () => {
  const { container } = show(video);
  expect(await screen.findByText('Video from YouTube')).toBeInTheDocument();

  const iframe = container.querySelector('.ref-card iframe');
  expect(iframe.getAttribute('src')).toBe('https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?rel=0&start=90');
  expect(screen.queryByTestId('reference-text')).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: 'Show stored text' }));
  expect(screen.getByTestId('reference-text').textContent).toContain('4:10 The argument');
  expect(screen.getByRole('button', { name: 'Hide stored text' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'YouTube ↗' })).toHaveAttribute('href', video.url);
});

test('opening the page marks the reference read once, against the reply it was opened from', async () => {
  show(tweet, { backTo: '/node/5', nodeId: 5 });
  expect(await screen.findByRole('button', { name: 'Mark as unread' })).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith('/external/items/42/read', { via: 'open', node_id: 5 });
  expect(screen.getByText(/you read it/)).toBeInTheDocument();
});

test('opened from anywhere else, the mark carries no node id', async () => {
  show(tweet);
  expect(await screen.findByRole('button', { name: 'Mark as unread' })).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith('/external/items/42/read', { via: 'open' });
});

test('StrictMode does not log the open twice', async () => {
  api.get.mockResolvedValue({ data: tweet });
  render(
    <React.StrictMode>
      <MemoryRouter initialEntries={['/references/42']}>
        <Routes><Route path="/references/:id" element={<ReferenceDetailPage />} /></Routes>
      </MemoryRouter>
    </React.StrictMode>,
  );
  expect(await screen.findByRole('button', { name: 'Mark as unread' })).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
});

test('a failed open mark still shows the reference, unread', async () => {
  api.post.mockRejectedValue(new Error('boom'));
  show(tweet);
  expect(await screen.findByRole('button', { name: 'Mark as read' })).toBeInTheDocument();
});

test('Mark as unread after the open mark stays unread', async () => {
  api.delete.mockResolvedValue({ data: { id: 42, read_at: null } });
  show(tweet);
  fireEvent.click(await screen.findByRole('button', { name: 'Mark as unread' }));
  expect(await screen.findByRole('button', { name: 'Mark as read' })).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
});

test('the link out logs the open and marks the reference read', async () => {
  show(tweet);
  await screen.findByRole('button', { name: 'Mark as unread' });
  api.post.mockClear();
  fireEvent.click(screen.getByRole('link', { name: 'Twitter ↗' }));

  expect(api.post).toHaveBeenCalledWith('/external/items/42/read', { via: 'open' });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Mark as unread' })).toBeInTheDocument());
  // The line under the card says it; there is no separate "Read" chip.
  expect(screen.getByText(/you read it/)).toBeInTheDocument();
  expect(screen.queryByText('Read')).toBeNull();
});

test('Mark as read and Mark as unread toggle the mark', async () => {
  api.delete.mockResolvedValue({ data: { id: 42, read_at: null } });
  show(tweet);

  fireEvent.click(await screen.findByRole('button', { name: 'Mark as unread' }));
  expect(api.delete).toHaveBeenCalledWith('/external/items/42/read');
  fireEvent.click(await screen.findByRole('button', { name: 'Mark as read' }));
  expect(api.post).toHaveBeenLastCalledWith('/external/items/42/read', {});
  expect(await screen.findByRole('button', { name: 'Mark as unread' })).toBeInTheDocument();
  expect(window.open).not.toHaveBeenCalled();
});

test('the kebab keeps Open source, Edit and Delete', async () => {
  show(page);
  fireEvent.click(await screen.findByRole('button', { name: 'More actions' }));
  api.post.mockClear();
  fireEvent.click(screen.getByText('Open source'));
  expect(window.open).toHaveBeenCalledWith(page.url, '_blank', 'noopener,noreferrer');
  // The menu entry logs no further mark (the page open already did).
  expect(api.post).not.toHaveBeenCalled();
});
