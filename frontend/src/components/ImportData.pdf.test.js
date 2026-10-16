// PDF import (Peter, 2026-10-16): the confirm step asks "Whose writing is
// this?" with nothing pre-selected, Confirm stays disabled until the user
// answers, and the answer is sent with the confirm request. An old .doc
// gets its message without being uploaded.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ImportData from './ImportData';
import { OLD_DOC_MESSAGE } from '../utils/docxUpload';

const pdfEntry = {
  name: 'Paper.pdf',
  filename_without_ext: 'Paper',
  content: '## Introduction\n\nText.',
  size: 22,
  modified_at: '2021-03-04T05:06:07',
  token_count: 5,
  source_format: 'pdf',
  pages: 12,
  chapters: 3,
  reference: {
    title: 'A Study of Things', author: 'Jane Doe',
    date: '2020-01-02T03:04:05', truncated: false, char_cap: 100000,
  },
};

const analyzed = {
  files: [pdfEntry], total_files: 1, total_tokens: 5, total_size: 22, errors: [],
};

beforeEach(() => mockPost.mockReset());

async function uploadPdf() {
  mockPost.mockImplementation((url) => {
    if (url === '/import/analyze') return Promise.resolve({ data: analyzed });
    return Promise.resolve({ data: {
      saved_as: 'reference', reference_id: 9, created: 1, skipped: 0, truncated: false,
    } });
  });
  render(<ImportData inline />);
  const input = screen.getByLabelText(/Import Markdown, Word \(\.docx\) or PDF/);
  expect(input.getAttribute('accept')).toBe('.zip,.docx,.doc,.pdf');
  const file = new File(['%PDF-1.4'], 'Paper.pdf', { type: 'application/pdf' });
  fireEvent.change(input, { target: { files: [file] } });
  await waitFor(() => expect(screen.getByText('Whose writing is this?')).toBeTruthy());
}

test('Confirm is disabled until whose writing it is is chosen, and the choice is sent', async () => {
  await uploadPdf();
  expect(mockPost.mock.calls[0][0]).toBe('/import/analyze');

  const mine = screen.getByLabelText(/^Mine: import it as my own entries/);
  const theirs = screen.getByLabelText(/^Someone else's: save it as a Reference/);
  expect(mine.checked).toBe(false);
  expect(theirs.checked).toBe(false);
  const confirm = screen.getByRole('button', { name: 'Confirm Import' });
  expect(confirm.disabled).toBe(true);
  fireEvent.click(confirm);
  expect(mockPost).toHaveBeenCalledTimes(1);   // nothing sent yet

  fireEvent.click(theirs);
  // Entry-only options go away for a Reference; the button says what happens
  expect(screen.queryByText('Import Type')).toBeNull();
  const save = screen.getByRole('button', { name: 'Save as Reference' });
  expect(save.disabled).toBe(false);
  fireEvent.click(save);

  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
  const [url, body] = mockPost.mock.calls[1];
  expect(url).toBe('/import/confirm');
  expect(body.whose_writing).toBe('someone_else');
  expect(body.files).toEqual([pdfEntry]);
  await waitFor(() => expect(screen.getByText('Saved as a Reference')).toBeTruthy());
});

test('choosing "Mine" sends mine and keeps the entry options', async () => {
  await uploadPdf();
  fireEvent.click(screen.getByLabelText(/^Mine: import it as my own entries/));
  expect(screen.getByText('Import Type')).toBeTruthy();
  const confirm = screen.getByRole('button', { name: 'Confirm Import' });
  expect(confirm.disabled).toBe(false);
  fireEvent.click(confirm);
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
  expect(mockPost.mock.calls[1][1].whose_writing).toBe('mine');
});

test('an old .doc gets its message without an upload', async () => {
  render(<ImportData inline />);
  const input = screen.getByLabelText(/Import Markdown, Word \(\.docx\) or PDF/);
  const file = new File(['\xd0\xcf'], 'Old Report.doc');
  fireEvent.change(input, { target: { files: [file] } });
  await waitFor(() => expect(screen.getByText(OLD_DOC_MESSAGE)).toBeTruthy());
  expect(mockPost).not.toHaveBeenCalled();
});
