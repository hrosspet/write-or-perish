// Save to shares on a node's :::share blocks, and what the card shows
// after the node is edited: a block keeps "Saved" while its text and type
// stay the same (wherever the edit moved it), and gets its Save button
// back when its text changed. The server re-reads the saved blocks on
// every edit and sends them in the node's tool meta.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('../api', () => ({ __esModule: true, default: { get: jest.fn(), post: jest.fn(), put: jest.fn() } }));

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ProposalInline, { shareBlockKeys } from './ProposalInline';
import api from '../api';

const block = (type, text) => [`:::share ${type}`, text, ':::'].join('\n');
const card = (content, meta) => (
  <MemoryRouter>
    <ProposalInline content={content} nodeId={7} toolCallsMeta={meta} shareOnly />
  </MemoryRouter>
);

beforeEach(() => api.post.mockReset());

test('the owner saves a block of their own entry with one click', async () => {
  api.post.mockResolvedValue({ data: { status: 'completed', share: { id: 1 }, saved_indexes: [0] } });
  render(card(`My note.\n\n${block('need', 'A reading partner.')}`, null));
  fireEvent.click(screen.getByText('Save to shares'));
  await waitFor(() => expect(screen.getByText('Saved as a private draft')).toBeInTheDocument());
  expect(api.post).toHaveBeenCalledWith('/share/save-proposal', { node_id: 7, share_index: 0 });
});

test('an edit that changes a saved block brings its Save button back', async () => {
  api.post.mockResolvedValue({ data: { status: 'completed', share: { id: 1 }, saved_indexes: [0] } });
  const { rerender } = render(card(block('need', 'A reading partner.'), null));
  fireEvent.click(screen.getByText('Save to shares'));
  await waitFor(() => expect(screen.getByText('Saved as a private draft')).toBeInTheDocument());

  rerender(card(block('need', 'Two reading partners.'),
    [{ name: 'propose_share', apply_status: 'pending_approval', saved_indexes: [] }]));
  expect(screen.getByText('Save to shares')).toBeInTheDocument();
  expect(screen.queryByText('Saved as a private draft')).toBeNull();
});

test('a saved block stays saved when an edit moves it', async () => {
  api.post.mockResolvedValue({ data: { status: 'partial', share: { id: 1 }, saved_indexes: [1] } });
  const first = block('insight', 'First.');
  const second = block('need', 'Second.');
  const { rerender } = render(card(`${first}\n\n${second}`, null));
  fireEvent.click(screen.getAllByText('Save to shares')[1]);
  await waitFor(() => expect(screen.getByText('Saved as a private draft')).toBeInTheDocument());

  // The first block is edited out: the saved one is now the only block.
  rerender(card(second,
    [{ name: 'propose_share', apply_status: 'completed', saved_indexes: [0] }]));
  expect(screen.getByText('Saved as a private draft')).toBeInTheDocument();
  expect(screen.queryByText('Save to shares')).toBeNull();
});

test('the saved state is read afresh from the node meta', () => {
  const content = block('need', 'A reading partner.');
  const { rerender } = render(card(content,
    [{ name: 'propose_share', apply_status: 'partial', saved_indexes: [0] }]));
  expect(screen.getByText('Saved as a private draft')).toBeInTheDocument();
  rerender(card(content,
    [{ name: 'propose_share', apply_status: 'pending_approval', saved_indexes: [] }]));
  expect(screen.getByText('Save to shares')).toBeInTheDocument();
});

test('block keys follow type and text, and tell copies apart', () => {
  const keys = shareBlockKeys([
    { type: 'need', content: 'A' }, { type: 'need', content: 'A' },
    { type: 'insight', content: 'A' },
  ]);
  expect(new Set(keys).size).toBe(3);
  expect(shareBlockKeys([{ type: 'need', content: 'A' }])[0]).toBe(keys[0]);
});
