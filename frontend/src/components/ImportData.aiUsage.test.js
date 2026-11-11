// The import dialog's AI usage starts at the account's AI usage setting
// (#593, Peter 2026-11-10: "it should simply follow the account's
// setting"), for every importer that shares the dialog. The value shown is
// the value sent, also when the user never touched it, and a re-import
// sends it for the entries already in Loore like a value picked by hand.
// Privacy still starts at Private.
const mockPost = jest.fn();
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    get: (...args) => mockGet(...args),
  },
}));

let mockUser = null;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: mockUser }),
}));

import React from 'react';
import JSZip from 'jszip';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ImportData from './ImportData';

const AI_LABELS = {
  none: '🚫 None · No AI access',
  chat: '💬 Chat · AI can use for responses',
  train: '🧠 Train · AI can use for training',
};

const mdAnalyzed = {
  files: [{ name: 'a.md', filename_without_ext: 'a', content: 'Hello.', size: 6,
    modified_at: '2021-03-04T05:06:07', token_count: 2, source_format: 'markdown' }],
  total_files: 1, total_tokens: 2, total_size: 6, errors: [],
};

const chatAnalyzed = {
  import_token: 'tok-1', total_conversations: 1, total_messages: 2,
  total_tokens: 10, total_size: 40, notices: [],
};

const twitterAnalyzed = {
  import_token: 'tw-1', total_tweets: 3, original_count: 3, reply_count: 0,
  total_tokens: 9, original_tokens: 9, skipped_retweets: 0,
};

const finished = {
  message: 'Import successful', created: 1, skipped: 0, restored: 0,
  updated: 0, thread_count: 1, nodes_created: 1,
};

async function zipFile(entries) {
  const zip = new JSZip();
  Object.entries(entries).forEach(([name, text]) => zip.file(name, text));
  const bytes = await zip.generateAsync({ type: 'uint8array' });
  return new File([bytes], 'export.zip', { type: 'application/zip' });
}

function renderImport(props = {}) {
  render(<MemoryRouter><ImportData inline {...props} /></MemoryRouter>);
}

async function openMarkdown() {
  fireEvent.change(screen.getByLabelText(/Import Markdown, Word \(\.docx\) or PDF/), {
    target: { files: [new File(['# a'], 'a.md', { type: 'text/markdown' })] },
  });
  await screen.findByText('Confirm Import', { selector: 'h3' });
}

async function openConversations(label, title) {
  const file = await zipFile({ 'conversations.json': '[{"id":"a"}]' });
  fireEvent.change(screen.getByLabelText(label), { target: { files: [file] } });
  await screen.findByText(title);
}

const aiSelect = () => screen.getByDisplayValue(/· (No AI access|AI can use)/);
const confirmBody = (path) => mockPost.mock.calls.find(([url]) => url === path)[1];

beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
  mockUser = null;
});

test.each(['none', 'chat', 'train'])(
  'the Markdown dialog opens at the account setting (%s) and sends it untouched',
  async (setting) => {
    mockUser = { id: 1, username: 'u', default_ai_usage: setting };
    mockPost.mockImplementation((url) => (url === '/import/analyze'
      ? Promise.resolve({ data: mdAnalyzed })
      : new Promise(() => {})));
    renderImport();
    await openMarkdown();
    expect(aiSelect().value).toBe(setting);
    expect(screen.getByDisplayValue(AI_LABELS[setting])).toBeTruthy();
    expect(screen.getByDisplayValue(/Private · Only I can see this/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(confirmBody('/import/confirm')).toMatchObject({
      privacy_level: 'private', ai_usage: setting,
    });
  });

test('an untouched Chat import hands the profile update the server started to the page', async () => {
  mockUser = { id: 1, username: 'u', default_ai_usage: 'chat' };
  mockPost.mockImplementation((url) => (url === '/import/analyze'
    ? Promise.resolve({ data: mdAnalyzed })
    : Promise.resolve({ data: { ...finished, profile_update_task_id: 'profile-7' } })));
  const onProfileUpdateStarted = jest.fn();
  renderImport({ onProfileUpdateStarted });
  await openMarkdown();
  fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }));
  expect(await screen.findByText('Import Finished')).toBeTruthy();
  expect(confirmBody('/import/confirm').ai_usage).toBe('chat');
  expect(onProfileUpdateStarted).toHaveBeenCalledWith('profile-7');
});

test.each([
  [/Import ChatGPT/, 'Confirm ChatGPT Import', '/import/chatgpt/analyze', '/import/chatgpt/confirm'],
  [/Import Claude/, 'Confirm Claude Import', '/import/claude/analyze', '/import/claude/confirm'],
])('%s: the dialog opens at the account setting and sends it', async (label, title, analyze, confirm) => {
  mockUser = { id: 1, username: 'u', default_ai_usage: 'train' };
  mockPost.mockImplementation((url) => (url === analyze
    ? Promise.resolve({ data: chatAnalyzed })
    : new Promise(() => {})));
  renderImport();
  await openConversations(label, title);
  expect(aiSelect().value).toBe('train');
  fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
  expect(confirmBody(confirm)).toEqual({
    import_token: 'tok-1', privacy_level: 'private', ai_usage: 'train',
  });
});

test('a Twitter re-import sends the shown value, and the result says the entries already in Loore now match', async () => {
  mockUser = { id: 1, username: 'u', default_ai_usage: 'chat' };
  mockPost.mockImplementation((url) => (url === '/import/twitter/analyze'
    ? Promise.resolve({ data: twitterAnalyzed })
    : Promise.resolve({ data: { task_id: 'job-1', status: 'queued', total: 3 } })));
  mockGet.mockResolvedValue({ data: { status: 'completed', done: 3, total: 3,
    result: { ...finished, created: 0, updated: 3 } } });
  renderImport();
  fireEvent.change(screen.getByLabelText(/Import Tweets/), {
    target: { files: [new File(['x'], 'twitter.zip', { type: 'application/zip' })] },
  });
  await screen.findByText('Confirm Twitter Import');
  expect(aiSelect().value).toBe('chat');
  fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }));
  expect(await screen.findByText('Import Finished', {}, { timeout: 4000 })).toBeTruthy();
  expect(confirmBody('/import/twitter/confirm').ai_usage).toBe('chat');
  expect(screen.getByText(/their privacy and\s+AI-usage settings now match this import/)).toBeTruthy();
});

test('a pick holds for its dialog; the next dialog opens at the account setting again', async () => {
  mockUser = { id: 1, username: 'u', default_ai_usage: 'chat' };
  mockPost.mockImplementation((url) => (url === '/import/analyze'
    ? Promise.resolve({ data: mdAnalyzed })
    : new Promise(() => {})));
  renderImport();
  await openMarkdown();
  fireEvent.change(aiSelect(), { target: { value: 'none' } });
  expect(aiSelect().value).toBe('none');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await waitFor(() => expect(screen.queryByText('Confirm Import', { selector: 'h3' })).toBeNull());

  await openMarkdown();
  expect(aiSelect().value).toBe('chat');
  fireEvent.change(aiSelect(), { target: { value: 'train' } });
  fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(3));
  expect(confirmBody('/import/confirm').ai_usage).toBe('train');
});

test('a PDF still asks whose writing it is, with nothing preselected', async () => {
  mockUser = { id: 1, username: 'u', default_ai_usage: 'chat' };
  const pdf = { ...mdAnalyzed.files[0], name: 'P.pdf', source_format: 'pdf', pages: 1, chapters: 1,
    reference: { title: 'P', author: null, date: null, truncated: false, char_cap: 100000 } };
  mockPost.mockImplementation((url) => (url === '/import/analyze'
    ? Promise.resolve({ data: { ...mdAnalyzed, files: [pdf] } })
    : new Promise(() => {})));
  renderImport();
  fireEvent.change(screen.getByLabelText(/Import Markdown, Word \(\.docx\) or PDF/), {
    target: { files: [new File(['%PDF-1.4'], 'P.pdf', { type: 'application/pdf' })] },
  });
  await screen.findByText('Whose writing is this?');
  expect(screen.getByLabelText(/^Mine: import it as my own entries/).checked).toBe(false);
  expect(screen.getByLabelText(/^Someone else's: save it as a Reference/).checked).toBe(false);
  expect(screen.getByRole('button', { name: 'Confirm Import' }).disabled).toBe(true);
});
