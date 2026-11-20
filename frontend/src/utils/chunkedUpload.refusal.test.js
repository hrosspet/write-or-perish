// A chunked upload refused for good (e.g. 410: it stopped arriving more than
// a day ago) is not retried, and the form gets the server's words.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import { uploadFileInChunks, isFinalRefusal } from './chunkedUpload';

const answer = (status, data) => {
  const err = new Error(`Request failed with status code ${status}`);
  err.response = { status, data };
  return err;
};

beforeEach(() => {
  mockPost.mockReset();
  jest.spyOn(console, 'warn').mockImplementation(() => {});
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.warn.mockRestore();
  console.error.mockRestore();
});

test("a piece refused for good is not retried and keeps the server's words", async () => {
  const words = 'This upload stopped arriving more than a day ago. Please upload the file again.';
  mockPost.mockImplementation((url) => {
    if (url === '/nodes/upload/init') return Promise.resolve({ data: { node_id: 9 } });
    if (url === '/nodes/upload/chunk') {
      return Promise.reject(answer(410, { error: words, code: 'upload_expired' }));
    }
    return Promise.resolve({ data: {} });
  });
  const file = new Blob([new Uint8Array(10)]);
  file.name = 'talk.m4a';

  let caught;
  try {
    await uploadFileInChunks(file, {}, null, 4);
  } catch (err) {
    caught = err;
  }

  expect(caught.response.data.error).toBe(words);
  const pieces = mockPost.mock.calls.filter(([u]) => u === '/nodes/upload/chunk');
  expect(pieces).toHaveLength(1);
  expect(mockPost.mock.calls.map(([u]) => u)).toContain('/nodes/upload/cleanup');
});

test('which answers no retry can change', () => {
  expect(isFinalRefusal(answer(410, {}))).toBe(true);
  expect(isFinalRefusal(answer(413, {}))).toBe(true);
  expect(isFinalRefusal(answer(400, {}))).toBe(true);
  expect(isFinalRefusal(answer(429, {}))).toBe(false);
  expect(isFinalRefusal(answer(408, {}))).toBe(false);
  expect(isFinalRefusal(answer(502, {}))).toBe(false);
  expect(isFinalRefusal(new Error('Network Error'))).toBe(false);
});
