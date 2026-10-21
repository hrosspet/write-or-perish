// A long entry is saved as a chain of parts (the first keeps the id, the
// response's tip_id is the last). Its reply belongs under the last part:
// a reply under the first one reads only the first part. The thread page
// asks for the reply, opens the entry and looks for a reply there.
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

let mockUser;
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({ user: mockUser }) }));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: () => {}, removeToast: () => {} }),
}));
const mockTextStream = jest.fn();
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: (...args) => mockTextStream(...args),
}));

// The inline form stands in as its props: a test sends an entry by
// calling its onSuccess with what the server answered.
let mockFormProps;
jest.mock('./NodeForm', () => (props) => { mockFormProps = props; return null; });
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
import { render, screen, act, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

const ME = { id: 1, username: 'peter', craft_mode: true, is_admin: false, glean_enabled: true };

const entry = (id, content, overrides = {}) => ({
  id, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content, ai_usage: 'chat', privacy_level: 'private',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
  ...overrides,
});

const AI_REPLY = {
  id: 90, node_type: 'llm', llm_model: 'claude-opus-5.5', user_id: 3,
  username: 'claude-opus-5.5', content: 'About all of it…', children: [],
};

// Every address the page moved through: a pending reply is followed on
// from the entry to the reply's own page.
let visited;
function LocationProbe() {
  const loc = useLocation();
  const here = loc.pathname + loc.search;
  if (visited.at(-1) !== here) visited.push(here);
  return null;
}

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

let routes;
beforeEach(() => {
  mockUser = ME;
  mockFormProps = null;
  routes = {};
  visited = [];
  mockGet.mockReset();
  mockPost.mockReset();
  mockTextStream.mockReset();
  mockTextStream.mockReturnValue({ text: '', done: false });
  localStorage.clear();
  mockGet.mockImplementation((url) => {
    if (url.endsWith('/llm-status')) return new Promise(() => {});
    if (url in routes) return Promise.resolve({ data: routes[url] });
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  mockPost.mockResolvedValue({ data: { node_id: 80, status: 'pending' } });
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

const renderAt = (path) => render(
  <MemoryRouter initialEntries={[path]}>
    <LocationProbe />
    <Routes>
      <Route path="/node/:id" element={<NodeDetailWrapper />} />
    </Routes>
  </MemoryRouter>,
);

describe('an entry sent inline that was split into parts', () => {
  // The thread the entry is written under; 70 is the first part of the
  // new entry, 72 its last.
  beforeEach(() => {
    routes['/nodes/10'] = entry(10, 'Earlier entry.');
    routes['/nodes/72'] = entry(72, 'Last part.');
  });

  test('with auto-generate on, the reply is asked under the last part and the page opens there', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    renderAt('/node/10');
    await screen.findByText('Earlier entry.');

    await act(async () => {
      await mockFormProps.onSuccess({ id: 70, tip_id: 72, split_into: 3 });
    });

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost.mock.calls[0][0]).toBe('/nodes/72/llm');
    expect(visited).toContain('/node/72?awaitLlm=80');
  });

  test('with auto-generate off, the page opens on the last part', async () => {
    localStorage.setItem('loore_auto_generate', 'false');
    renderAt('/node/10');
    await screen.findByText('Earlier entry.');

    await act(async () => {
      await mockFormProps.onSuccess({ id: 70, tip_id: 72, split_into: 3 });
    });

    expect(mockPost).not.toHaveBeenCalled();
    expect(visited.at(-1)).toBe('/node/72');
  });

  test('an entry that was not split is answered under itself', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    routes['/nodes/70'] = entry(70, 'Short entry.');
    renderAt('/node/10');
    await screen.findByText('Earlier entry.');

    await act(async () => {
      await mockFormProps.onSuccess({ id: 70, tip_id: 70, split_into: 1 });
    });

    expect(mockPost.mock.calls[0][0]).toBe('/nodes/70/llm');
    expect(visited).toContain('/node/70?awaitLlm=80');
  });
});

describe('the first part of a split entry in a read thread', () => {
  const llmButton = () => Array.from(document.querySelectorAll('[data-action-group] button'))
    .find((b) => b.textContent === 'LLM Response') || null;

  // The first part (60) with the rest of the entry under it; the last
  // part (62) holds what *tipChildren* says.
  const firstPart = (tipChildren) => entry(60, 'First part.', {
    in_read_thread: true, read_reply_above: true, glean_thread: true,
    split_tip_id: 62,
    child_count: 1,
    children: [{
      id: 61, node_type: 'user', user_id: 1, username: 'peter',
      content: 'Middle part.', child_count: 1,
      children: [{
        id: 62, node_type: 'user', user_id: 1, username: 'peter',
        content: 'Last part.', child_count: tipChildren.length,
        children: tipChildren,
      }],
    }],
  });

  test('with no reply under the last part, auto-generate on still offers LLM Response', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    routes['/nodes/60'] = firstPart([]);

    renderAt('/node/60');
    await screen.findByText('First part.');
    await waitFor(() => expect(llmButton()).toBeInTheDocument());
    expect(llmButton()).toBeEnabled();
  });

  test('with a reply under the last part, auto-generate on hides it', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    routes['/nodes/60'] = firstPart([AI_REPLY]);

    renderAt('/node/60');
    await screen.findByText('First part.');
    expect(llmButton()).toBeNull();
  });
});
