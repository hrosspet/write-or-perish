// ChatGPT exports are split into conversations-000.json, conversations-001.json,
// ...; each shard is uploaded as its own part, in order. Other JSON files in
// the zip are not uploaded. A Claude export with one conversations.json still
// sends one part.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import React from 'react';
import JSZip from 'jszip';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ImportData from './ImportData';

const textOf = (f) =>
  new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = () => rej(r.error);
    r.readAsText(f);
  });

async function zipFile(entries) {
  const zip = new JSZip();
  Object.entries(entries).forEach(([name, text]) => zip.file(name, text));
  const bytes = await zip.generateAsync({ type: 'uint8array' });
  return new File([bytes], 'export.zip', { type: 'application/zip' });
}

function pick(label, file) {
  render(<ImportData inline />);
  fireEvent.change(screen.getByLabelText(label), { target: { files: [file] } });
}

beforeEach(() => mockPost.mockReset());

test('a sharded ChatGPT export uploads the shards in order and nothing else', async () => {
  mockPost.mockReturnValue(new Promise(() => {}));
  const file = await zipFile({
    'sectioned_conversations.json': '[]',
    'shared_conversations.json': '[{"id":"share"}]',
    'conversations-001.json': '[{"id":"b"}]',
    'conversations-000.json': '[{"id":"a"}]',
  });
  pick(/Import ChatGPT/, file);
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  expect(mockPost.mock.calls[0][0]).toBe('/import/chatgpt/analyze');
  const parts = mockPost.mock.calls[0][1].getAll('conversations_file');
  expect(parts.map((p) => p.name)).toEqual(['conversations-000.json', 'conversations-001.json']);
  expect(await Promise.all(parts.map(textOf))).toEqual(['[{"id":"a"}]', '[{"id":"b"}]']);
});

test('a Claude export with one conversations.json sends one part', async () => {
  mockPost.mockReturnValue(new Promise(() => {}));
  const file = await zipFile({
    'users.json': '[{"u":1}]',
    'conversations.json': '[{"uuid":"c"}]',
  });
  pick(/Import Claude/, file);
  await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  expect(mockPost.mock.calls[0][0]).toBe('/import/claude/analyze');
  const parts = mockPost.mock.calls[0][1].getAll('conversations_file');
  expect(parts.map((p) => p.name)).toEqual(['conversations.json']);
  expect(await textOf(parts[0])).toBe('[{"uuid":"c"}]');
});
