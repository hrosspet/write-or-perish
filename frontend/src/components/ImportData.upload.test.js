// The import upload sends an in-memory copy of the picked file, never the
// original (a file from a cloud app on Android can fail when read again
// during upload). A file the browser cannot read is not uploaded.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ImportData from './ImportData';
import { FILE_READ_MESSAGE } from '../utils/docxUpload';

const bytesOf = (f) =>
  new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(new Uint8Array(r.result));
    r.onerror = () => rej(r.error);
    r.readAsArrayBuffer(f);
  });

beforeEach(() => mockPost.mockReset());

function pick(file) {
  render(<ImportData inline />);
  const input = screen.getByLabelText(/Import Markdown, Word \(\.docx\) or PDF/);
  fireEvent.change(input, { target: { files: [file] } });
}

test('a docx without media is uploaded as an in-memory copy, not the original', async () => {
  mockPost.mockReturnValue(new Promise(() => {}));
  const file = new File(['not really a zip'], 'Notes.docx', { type: 'application/vnd.x' });
  pick(file);
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  const sent = mockPost.mock.calls[0][1].get('zip_file');
  expect(sent).not.toBe(file);
  expect(sent.name).toBe('Notes.docx');
  expect(sent.type).toBe('application/vnd.x');
  expect(await bytesOf(sent)).toEqual(await bytesOf(file));
});

test('a file that cannot be read shows the cloud-file message and makes no request', async () => {
  const file = new File(['x'], 'Paper.pdf', { type: 'application/pdf' });
  file.arrayBuffer = () => Promise.reject(new DOMException('gone', 'NotReadableError'));
  pick(file);
  await waitFor(() => expect(screen.getByText(FILE_READ_MESSAGE)).toBeTruthy());
  expect(mockPost).not.toHaveBeenCalled();
});
