// The first cohort: the interview's opening failed while its thread page
// waited for it. The page does not fall back to the interview prompt
// (shown whole); the person goes back to /cohort, which says what happened
// and offers Try again. Any other failed reply still goes to its parent.
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
    put: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'mara', craft_mode: false, is_admin: false, glean_enabled: false } }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: () => {} }),
}));
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: () => ({ text: '', done: false }),
}));
jest.mock('./NodeForm', () => () => null);
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./NodeFormModal', () => () => null);
jest.mock('./ModelSelector', () => () => null);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
jest.mock('./QuotedContent', () => ({ content }) => <div>{content}</div>);

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname}</div>;
}

const root = (promptKey) => ({
  id: 1, username: 'mara', user_id: 1, node_type: 'user', content: 'The prompt',
  is_system_prompt: true, prompt_key: promptKey, ai_usage: 'chat',
  privacy_level: 'private', child_count: 1,
});

function setUp(promptKey) {
  const opening = {
    id: 2, node_type: 'llm', llm_model: 'claude-opus-5.5', llm_task_status: 'processing',
    user: { id: 9, username: 'claude-opus-5.5' }, user_id: 9, parent_user_id: 1,
    content: '', ai_usage: 'chat', privacy_level: 'private',
    ancestors: [root(promptKey)], children: [], child_count: 0,
    in_read_thread: false, read_reply_above: false, glean_thread: false, thread_length: null,
  };
  mockGet.mockReset();
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/2/llm-status') {
      return Promise.resolve({ data: { node_id: 2, status: 'failed', error: 'It did not work.' } });
    }
    if (url === '/nodes/2') return Promise.resolve({ data: opening });
    if (url === '/nodes/1') return Promise.resolve({ data: { ...root(promptKey), ancestors: [], children: [], user: { id: 1, username: 'mara' } } });
    return Promise.resolve({ data: {} });
  });
  jest.spyOn(console, 'error').mockImplementation(() => {});
  render(
    <MemoryRouter initialEntries={['/node/2?awaitLlm=2']}>
      <Routes>
        <Route path="/node/:id" element={<><NodeDetailWrapper /><Where /></>} />
        <Route path="/cohort" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  console.error.mockRestore();
});

test('a failed interview opening goes back to /cohort, not to the prompt', async () => {
  setUp('cohort_interview');
  expect(await screen.findByText('/cohort')).toBeInTheDocument();
});

test('a failed reply under any other prompt still goes to its parent', async () => {
  setUp('textmode');
  expect(await screen.findByText('/node/1')).toBeInTheDocument();
});
