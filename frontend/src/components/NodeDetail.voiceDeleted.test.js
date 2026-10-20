// #480: Voice Mode on a node deleted after the page opened. The server
// answers 410 and creates nothing; the page says so in a toast and keeps
// the thread on screen, as the LLM Response button does.
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

const mockAddToast = jest.fn();
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'peter', craft_mode: true } }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: mockAddToast, removeToast: () => {} }),
}));
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: () => ({ text: '', done: false }),
}));

const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

// Children with their own fetching (and the markdown renderer), stubbed.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./NodeForm', () => () => null);
jest.mock('./NodeFormModal', () => () => null);
jest.mock('./ModelSelector', () => () => null);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
jest.mock('./QuotedContent', () => ({ content }) => <div>{content}</div>);

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

const ENTRY = {
  id: 70, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content: 'An entry deleted on another device.',
  ai_usage: 'chat', privacy_level: 'private',
  ancestors: [], children: [], child_count: 0,
};

beforeAll(() => {
  // The page scrolls to the focal node; jsdom has no layout.
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockAddToast.mockReset();
  mockNavigate.mockReset();
  localStorage.clear();
  mockGet.mockImplementation((url) => (url === '/nodes/70'
    ? Promise.resolve({ data: ENTRY })
    : Promise.reject(new Error(`unexpected GET ${url}`))));
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

test('a 410 from Voice Mode is a toast; the thread stays on screen', async () => {
  mockPost.mockRejectedValue({
    response: { status: 410, data: { error: 'Parent node has been deleted' } },
  });
  render(
    <MemoryRouter initialEntries={['/node/70']}>
      <Routes>
        <Route path="/node/:id" element={<NodeDetailWrapper />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByText(ENTRY.content);

  fireEvent.click(screen.getByTitle('Continue this conversation by voice'));

  await waitFor(() => expect(mockAddToast).toHaveBeenCalledWith(
    'Parent node has been deleted', 8000));
  expect(mockPost).toHaveBeenCalledWith('/voice/from-node/70', expect.anything());
  expect(mockNavigate).not.toHaveBeenCalled();
  // Not replaced by the error text, and Voice Mode can be pressed again.
  expect(screen.getByText(ENTRY.content)).toBeInTheDocument();
  expect(screen.getByText('Voice Mode')).toBeInTheDocument();
});
