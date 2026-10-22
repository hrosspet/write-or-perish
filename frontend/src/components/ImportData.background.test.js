// ChatGPT and Claude imports run in the background (2026-10-20: a large
// ChatGPT import ran past the request timeout inside the confirm request,
// the dialog came back, and the user pressed Confirm five times). The
// analyze answer carries a token, not the conversations; confirm sends the
// token, gets a task id and polls it, showing "N of M messages". Errors show
// inside the confirm dialog, and a second press starts nothing.
const mockPost = jest.fn();
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: (...args) => mockPost(...args),
    get: (...args) => mockGet(...args),
  },
}));

import React from 'react';
import JSZip from 'jszip';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ImportData from './ImportData';

async function zipFile(entries) {
  const zip = new JSZip();
  Object.entries(entries).forEach(([name, text]) => zip.file(name, text));
  const bytes = await zip.generateAsync({ type: 'uint8array' });
  return new File([bytes], 'export.zip', { type: 'application/zip' });
}

const analyzed = {
  import_token: 'tok-1', total_conversations: 2, total_messages: 6,
  total_tokens: 100, total_size: 400, notices: [],
};

const result = {
  message: 'Import successful', created: 6, skipped: 0, restored: 0,
  updated: 0, thread_count: 2, nodes_created: 6, profile_update_task_id: null,
};

async function openConfirm(label = /Import ChatGPT/, confirm) {
  mockPost.mockImplementation((url) => {
    if (url.endsWith('/analyze')) return Promise.resolve({ data: analyzed });
    return confirm(url);
  });
  const file = await zipFile({ 'conversations.json': '[{"id":"a"}]' });
  render(<ImportData inline />);
  fireEvent.change(screen.getByLabelText(label), { target: { files: [file] } });
  return screen.findByRole('button', { name: 'Confirm Import' });
}

beforeEach(() => {
  mockPost.mockReset();
  mockGet.mockReset();
});

test('the analyze upload gets a long timeout and reports upload progress', async () => {
  mockPost.mockReturnValue(new Promise(() => {}));
  const file = await zipFile({ 'conversations.json': '[{"id":"a"}]' });
  render(<ImportData inline />);
  fireEvent.change(screen.getByLabelText(/Import ChatGPT/), { target: { files: [file] } });
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  const config = mockPost.mock.calls[0][2];
  expect(config.timeout).toBe(10 * 60 * 1000);
  expect(typeof config.onUploadProgress).toBe('function');
});

test('confirm sends the token, polls the job, shows "N of M messages", then the result', async () => {
  const button = await openConfirm(/Import ChatGPT/, () =>
    Promise.resolve({ data: { task_id: 'job-1', status: 'queued', total: 6 } }));
  mockGet
    .mockResolvedValueOnce({ data: { status: 'running', done: 3, total: 6 } })
    .mockResolvedValue({ data: { status: 'completed', done: 6, total: 6, result } });

  fireEvent.click(button);
  fireEvent.click(button); // a second press before the re-render

  await waitFor(() => expect(mockGet).toHaveBeenCalled());
  const confirms = mockPost.mock.calls.filter(([url]) => url === '/import/chatgpt/confirm');
  expect(confirms).toHaveLength(1);
  expect(confirms[0][1]).toEqual({
    import_token: 'tok-1', privacy_level: 'private', ai_usage: 'none',
  });
  expect(mockGet.mock.calls[0][0]).toBe('/import/status/job-1');
  expect(await screen.findByText(/3 of 6 messages/)).toBeTruthy();
  expect(screen.getByRole('button', { name: /Importing/ }).disabled).toBe(true);
  expect(await screen.findByText('Import Finished', {}, { timeout: 4000 })).toBeTruthy();
  // The result counts messages and says so, next to the dialog's conversations.
  expect(screen.getByText('Messages imported')).toBeTruthy();
  expect(screen.getByText(/These numbers count messages\. Your archive has 2 conversations with 6 messages\./)).toBeTruthy();
});

test('a re-import result labels the skipped count as messages', async () => {
  const button = await openConfirm(/Import Claude/, () =>
    Promise.resolve({ data: { task_id: 'job-4', status: 'queued', total: 6 } }));
  mockGet.mockResolvedValue({ data: { status: 'completed', done: 6, total: 6,
    result: { ...result, created: 0, skipped: 6 } } });
  fireEvent.click(button);
  expect(await screen.findByText('Import Finished', {}, { timeout: 4000 })).toBeTruthy();
  expect(screen.getByText('Messages skipped')).toBeTruthy();
  expect(screen.getByText(/Your archive has 2 conversations/)).toBeTruthy();
});

test('a failed confirm shows its error inside the dialog and Confirm works again', async () => {
  const button = await openConfirm(/Import ChatGPT/, () =>
    Promise.reject({ response: { status: 409, data: {
      error: 'Another import is still running. Wait until it finishes, then try again.',
      code: 'import_running', task_id: 'job-0',
    } } }));
  fireEvent.click(button);
  const alert = await screen.findByRole('alert');
  expect(alert.textContent).toMatch(/still running/);
  // Inside the dialog, next to its Confirm button.
  const dialogCard = screen.getByText('Confirm ChatGPT Import').closest('div[style]').parentElement;
  expect(dialogCard.contains(alert)).toBe(true);
  expect(screen.getByRole('button', { name: 'Confirm Import' }).disabled).toBe(false);
});

test('a failed job says why inside the dialog', async () => {
  const button = await openConfirm(/Import Claude/, () =>
    Promise.resolve({ data: { task_id: 'job-2', status: 'queued', total: 6 } }));
  mockGet.mockResolvedValue({ data: { status: 'failed', error: 'Import data expired — please upload the export again.' } });
  fireEvent.click(button);
  const alert = await screen.findByRole('alert');
  expect(alert.textContent).toBe('Import data expired — please upload the export again.');
  const confirms = mockPost.mock.calls.filter(([url]) => url === '/import/claude/confirm');
  expect(confirms[0][1].import_token).toBe('tok-1');
  expect(confirms[0][1].conversations).toBeUndefined();
});

test('a status poll that fails once does not lose the import', async () => {
  const button = await openConfirm(/Import ChatGPT/, () =>
    Promise.resolve({ data: { task_id: 'job-3', status: 'queued', total: 6 } }));
  mockGet
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue({ data: { status: 'completed', done: 6, total: 6, result } });
  fireEvent.click(button);
  expect(await screen.findByText('Import Finished', {}, { timeout: 4000 })).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
});
