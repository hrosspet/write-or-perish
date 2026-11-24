// A tick on a todo proposal's row moves it between New Tasks and Completed in
// the reply's text. Like a tick in an entry (#795), it is shown at once and
// saved only over the text the card shows (base_content); when the reply
// changed elsewhere the server refuses and the card undoes the move.
import React, { useState } from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../api';
import ProposalInline from './ProposalInline';
import { NEWER_ENTRY_MESSAGE } from '../utils/markdown';

jest.mock('./MarkdownBody', () => () => null);
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn(), put: jest.fn() },
}));

const PROPOSAL = '### New Tasks\n- buy milk\n- call the bank';
const onError = jest.fn();

function Card() {
  const [content, setContent] = useState(PROPOSAL);
  return (
    <MemoryRouter>
      <div data-testid="text">{content}</div>
      <ProposalInline
        content={content}
        nodeId={7}
        toolCallsMeta={[{ name: 'propose_todo', status: 'success' }]}
        onContentChange={setContent}
        onError={onError}
      />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

const markDone = (label) => {
  const row = screen.getByText(label).parentElement;
  fireEvent.click(row.querySelector('[title="Mark as done"]'));
};

test('a tick sends the text the card shows as its base', async () => {
  api.put.mockResolvedValue({ data: {} });
  render(<Card />);
  markDone('buy milk');

  await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
  const [url, body] = api.put.mock.calls[0];
  expect(url).toBe('/nodes/7');
  expect(body.base_content).toBe(PROPOSAL);
  expect(body.content).toContain('buy milk');
  expect(body.content).not.toBe(PROPOSAL);
  expect(onError).not.toHaveBeenCalled();
});

test('a tick on a reply that changed elsewhere is undone, with a toast', async () => {
  api.put.mockRejectedValueOnce(Object.assign(new Error('409'), {
    response: { status: 409, data: { error: 'newer', code: 'content_changed' } },
  }));
  render(<Card />);
  markDone('buy milk');

  await waitFor(() => expect(onError).toHaveBeenCalledWith(NEWER_ENTRY_MESSAGE, 8000));
  expect(screen.getByTestId('text').textContent).toBe(PROPOSAL);
  expect(api.put).toHaveBeenCalledTimes(1);
});
