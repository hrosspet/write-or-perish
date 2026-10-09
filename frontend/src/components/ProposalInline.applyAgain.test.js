// A failed todo merge leaves its proposal applicable (#434): the card shows
// "Apply again" next to the error when the server marked it retryable.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../api';
import ProposalInline from './ProposalInline';

jest.mock('./MarkdownBody', () => () => null);
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn(), put: jest.fn() },
}));

const PROPOSAL = '### New Tasks\n- buy milk';

const renderCard = (todoEntry) => render(
  <MemoryRouter>
    <ProposalInline
      content={PROPOSAL}
      nodeId={7}
      toolCallsMeta={[{ name: 'propose_todo', status: 'success', ...todoEntry }]}
    />
  </MemoryRouter>,
);

beforeEach(() => {
  jest.clearAllMocks();
});

test('a failed, retryable merge shows the error and "Apply again"', async () => {
  api.post.mockResolvedValue({ data: { status: 'started' } });
  renderCard({ apply_status: 'failed', apply_error: 'The todo update was cut off, so nothing was changed.', retryable: true });

  expect(screen.getByText('The todo update was cut off, so nothing was changed.')).toBeInTheDocument();
  fireEvent.click(screen.getByText('Apply again'));

  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/todo/apply-draft', { llm_node_id: 7 }));
  expect(await screen.findByText('Todo update started…')).toBeInTheDocument();
  expect(screen.queryByText('Apply again')).not.toBeInTheDocument();
});

test('a failed merge whose proposal is no longer pending shows the error only', () => {
  // Failed before this change, or a newer proposal is pending.
  renderCard({ apply_status: 'failed', apply_error: 'Empty merge result' });

  expect(screen.getByText('Empty merge result')).toBeInTheDocument();
  expect(screen.queryByText('Apply again')).not.toBeInTheDocument();
});

test('applying a proposal another tab is already applying follows that merge', async () => {
  api.post.mockRejectedValue({
    response: { status: 409, data: { error: 'These todo changes are already being applied.', code: 'todo_merge_started' } },
  });
  renderCard({ apply_status: 'failed', apply_error: 'Empty merge result', retryable: true });

  fireEvent.click(screen.getByText('Apply again'));

  expect(await screen.findByText('Todo update started…')).toBeInTheDocument();
  expect(screen.queryByText('These todo changes are already being applied.')).not.toBeInTheDocument();
});
