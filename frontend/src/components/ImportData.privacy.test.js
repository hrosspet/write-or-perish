// Imports never offer Public (#677): the import dialogs' privacy choice
// lists Private and Circles, with one line saying imported entries start
// private. The shared selector still offers all three levels elsewhere.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args), get: jest.fn() },
}));
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: null }),
}));

import React from 'react';
import JSZip from 'jszip';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ImportData from './ImportData';
import PrivacySelector from './PrivacySelector';

const NOTE = 'Imported entries start private. You can make any of them public afterwards.';

const mdAnalyzed = {
  files: [{ name: 'a.md', filename_without_ext: 'a', content: 'Hello.', size: 6,
    modified_at: '2021-03-04T05:06:07', token_count: 2, source_format: 'markdown' }],
  total_files: 1, total_tokens: 2, total_size: 6, errors: [],
};
const chatAnalyzed = {
  import_token: 'tok-1', total_conversations: 1, total_messages: 2,
  total_tokens: 10, total_size: 40, notices: [],
};

const privacyOptions = () => Array.from(
  screen.getByDisplayValue(/Private · Only I can see this/).querySelectorAll('option'),
).map((o) => o.value);

beforeEach(() => mockPost.mockReset());

test('the Markdown import dialog leaves Public out and shows the note', async () => {
  mockPost.mockResolvedValue({ data: mdAnalyzed });
  render(<MemoryRouter><ImportData inline /></MemoryRouter>);
  fireEvent.change(screen.getByLabelText(/Import Markdown, Word \(\.docx\) or PDF/), {
    target: { files: [new File(['# a'], 'a.md', { type: 'text/markdown' })] },
  });
  await screen.findByText('Confirm Import', { selector: 'h3' });
  expect(privacyOptions()).toEqual(['private', 'circles']);
  expect(screen.getByText(NOTE)).toBeTruthy();
  expect(screen.queryByText(/Public · Anyone can see this/)).toBeNull();
});

test('the ChatGPT import dialog leaves Public out and shows the note', async () => {
  mockPost.mockResolvedValue({ data: chatAnalyzed });
  const zip = new JSZip();
  zip.file('conversations.json', '[{"id":"a"}]');
  const bytes = await zip.generateAsync({ type: 'uint8array' });
  render(<MemoryRouter><ImportData inline /></MemoryRouter>);
  fireEvent.change(screen.getByLabelText(/Import ChatGPT/), {
    target: { files: [new File([bytes], 'export.zip', { type: 'application/zip' })] },
  });
  await screen.findByText('Confirm ChatGPT Import');
  expect(privacyOptions()).toEqual(['private', 'circles']);
  expect(screen.getByText(NOTE)).toBeTruthy();
});

test('control: the shared selector still offers Public where nothing is left out', () => {
  render(<PrivacySelector privacyLevel="private" onPrivacyChange={() => {}}
    onAIUsageChange={() => {}} />);
  const select = screen.getByDisplayValue(/Private · Only I can see this/);
  expect(Array.from(select.querySelectorAll('option')).map((o) => o.value))
    .toEqual(['private', 'circles', 'public']);
});
