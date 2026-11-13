// Payments (#438): from an accepted init to its finalize, a chunked upload
// is a recording in progress, so the app does not leave for the subscribe
// page while it is sent (UserContext waits for it).
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import { uploadFileInChunks } from './chunkedUpload';
import { isRecordingActive } from './recordingActivity';

const file = { name: 'talk.m4a', size: 12, slice: () => 'bytes' };

beforeEach(() => mockPost.mockReset());

test('in progress from the accepted init to the finalize', async () => {
  const seen = [];
  mockPost.mockImplementation(async (url) => {
    seen.push([url, isRecordingActive()]);
    if (url === '/nodes/upload/init') return { data: { node_id: 7 } };
    return { data: { id: 7 } };
  });

  const result = await uploadFileInChunks(file, {}, null, 5);

  expect(result).toEqual({ id: 7 });
  expect(seen).toEqual([
    ['/nodes/upload/init', false],     // a refused start is not held
    ['/nodes/upload/chunk', true],
    ['/nodes/upload/chunk', true],
    ['/nodes/upload/chunk', true],
    ['/nodes/upload/finalize', true],
  ]);
  expect(isRecordingActive()).toBe(false);
});

test('a failed upload is no longer in progress', async () => {
  mockPost.mockImplementation(async (url) => {
    if (url === '/nodes/upload/init') return { data: { node_id: 7 } };
    if (url === '/nodes/upload/finalize') throw new Error('down');
    return { data: {} };
  });
  await expect(uploadFileInChunks(file, {}, null, 12)).rejects.toThrow('down');
  expect(isRecordingActive()).toBe(false);
});
