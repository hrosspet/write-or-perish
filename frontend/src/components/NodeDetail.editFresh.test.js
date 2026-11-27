// #799 review 3: Edit opens on the entry as it is now, not on the page's copy.
// After a refused Save the page still shows the older text; a form opened on
// it would type on that older text, into the older edit's draft row. The page
// reads the entry first and opens the form on what the server has.
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(() => Promise.resolve({ data: {} })),
    put: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'peter', craft_mode: true, approved: true } }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: () => {} }),
}));
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: () => ({ text: '', done: false }),
}));
jest.mock('./NodeForm', () => () => null);
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
// The edit overlay shows the text its form opens on.
jest.mock('./NodeFormModal', () => ({ nodeFormProps }) => (
  <div data-testid="edit-form">{nodeFormProps.initialContent}</div>
));
jest.mock('./ModelSelector', () => () => null);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
jest.mock('./QuotedContent', () => ({ content }) => <div data-testid="page-text">{content}</div>);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

const entry = (content) => ({
  id: 41, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content, ai_usage: 'chat', privacy_level: 'private',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

test('Edit reads the entry first and opens the form on its current text', async () => {
  let reads = 0;
  mockGet.mockImplementation((url) => {
    if (url === '/nodes/41') {
      reads += 1;
      // The page loaded v1; the phone saved v2 meanwhile.
      return Promise.resolve({ data: entry(reads === 1 ? 'version one' : 'version two') });
    }
    if (url.endsWith('/llm-status')) return new Promise(() => {});
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  render(
    <MemoryRouter initialEntries={['/node/41']}>
      <Routes>
        <Route path="/node/:id" element={<NodeDetailWrapper />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByText('version one')).toBeInTheDocument();

  fireEvent.click(screen.getAllByLabelText('More actions')[0]);
  fireEvent.click(await screen.findByText('Edit'));

  expect((await screen.findByTestId('edit-form')).textContent).toBe('version two');
  expect(reads).toBe(2);
  // The page shows the current text too.
  await waitFor(() => expect(screen.getByTestId('page-text').textContent).toBe('version two'));
});
