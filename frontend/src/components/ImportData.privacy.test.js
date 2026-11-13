// Imports are always private (#677, Peter 2026-11-13): the import dialogs
// have no privacy choice, one line says so, and they send "private". The
// shared selector still offers all three levels elsewhere.
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
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ImportData from './ImportData';
import PrivacySelector from './PrivacySelector';

const NOTE = 'Imported entries are private. You can share or publish the ones you choose afterwards.';

const mdAnalyzed = {
  files: [{ name: 'a.md', filename_without_ext: 'a', content: 'Hello.', size: 6,
    modified_at: '2021-03-04T05:06:07', token_count: 2, source_format: 'markdown' }],
  total_files: 1, total_tokens: 2, total_size: 6, errors: [],
};
const chatAnalyzed = {
  import_token: 'tok-1', total_conversations: 1, total_messages: 2,
  total_tokens: 10, total_size: 40, notices: [],
};

const noPrivacyChoice = () => {
  expect(screen.queryByText('Privacy Level')).toBeNull();
  expect(screen.queryByDisplayValue(/Private · Only I can see this/)).toBeNull();
  expect(screen.getByText(NOTE)).toBeTruthy();
  // The AI usage choice stays.
  expect(screen.getByDisplayValue(/· (No AI access|AI can use)/)).toBeTruthy();
};

beforeEach(() => mockPost.mockReset());

test('the Markdown import dialog has no privacy choice and shows the note', async () => {
  mockPost.mockResolvedValue({ data: mdAnalyzed });
  render(<MemoryRouter><ImportData inline /></MemoryRouter>);
  fireEvent.change(screen.getByLabelText(/Import Markdown, Word \(\.docx\) or PDF/), {
    target: { files: [new File(['# a'], 'a.md', { type: 'text/markdown' })] },
  });
  await screen.findByText('Confirm Import', { selector: 'h3' });
  noPrivacyChoice();
});

test('the ChatGPT import dialog has no privacy choice and shows the note', async () => {
  mockPost.mockResolvedValue({ data: chatAnalyzed });
  const zip = new JSZip();
  zip.file('conversations.json', '[{"id":"a"}]');
  const bytes = await zip.generateAsync({ type: 'uint8array' });
  render(<MemoryRouter><ImportData inline /></MemoryRouter>);
  fireEvent.change(screen.getByLabelText(/Import ChatGPT/), {
    target: { files: [new File([bytes], 'export.zip', { type: 'application/zip' })] },
  });
  await screen.findByText('Confirm ChatGPT Import');
  noPrivacyChoice();
  fireEvent.click(screen.getByRole('button', { name: 'Confirm Import' }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
  expect(mockPost.mock.calls[1][1].privacy_level).toBe('private');
});

test('control: the shared selector still offers Public where nothing is left out', () => {
  render(<PrivacySelector privacyLevel="private" onPrivacyChange={() => {}}
    onAIUsageChange={() => {}} />);
  const select = screen.getByDisplayValue(/Private · Only I can see this/);
  expect(Array.from(select.querySelectorAll('option')).map((o) => o.value))
    .toEqual(['private', 'circles', 'public']);
});
