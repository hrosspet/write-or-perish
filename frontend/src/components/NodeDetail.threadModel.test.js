// #791: the thread page named the account's model in its reply requests
// unless the LLM Response picker was on screen (it fills in the thread's
// model). So the automatic reply to a typed message, in a thread that ran
// on another model, moved the thread to the account's model, and that
// turn read the whole thread without the prompt cache. The page now names
// a model only when the person picked one in the picker on this page;
// otherwise the server continues on the thread's model.
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

// The inline form, the edit dialog and the LLM Response picker stand in
// as their props: a test sends an entry, saves an edit or picks a model
// by calling them.
let mockFormProps;
let mockModalProps;
let mockChatPicker;
jest.mock('./NodeForm', () => (props) => { mockFormProps = props; return null; });
jest.mock('./NodeFormModal', () => ({ nodeFormProps }) => {
  mockModalProps = nodeFormProps;
  return null;
});
jest.mock('./ModelSelector', () => (props) => {
  if (props.purpose !== 'read') mockChatPicker = props;
  return null;
});
// The entry's ⋯ menu as plain buttons; the thread above it is not shown.
jest.mock('./BubbleKebabMenu', () => ({ items }) => (
  <div>{items.map((i) => <button key={i.label} type="button" onClick={i.action}>{i.label}</button>)}</div>
));
jest.mock('./Bubble', () => () => null);
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => ({ children }) => <div>{children}</div>);
jest.mock('./QuotedContent', () => ({ content }) => <div>{content}</div>);

import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

// The account's model is Opus 5.5; the thread runs on GPT-6 Astra.
const ME = {
  id: 1, username: 'peter', craft_mode: false, is_admin: false,
  preferred_model: 'claude-opus-5.5',
};

const FIRST_ENTRY = {
  id: 10, node_type: 'user', user_id: 1, username: 'peter',
  content: 'The first entry.', ai_usage: 'chat', privacy_level: 'private',
};
const ASTRA_REPLY = {
  id: 11, node_type: 'llm', llm_model: 'gpt-6-astra', user_id: 3,
  parent_user_id: 1, username: 'gpt-6-astra', content: 'An answer from Astra.',
  llm_task_status: 'completed', ai_usage: 'chat', privacy_level: 'private',
};

// The thread's newest AI reply, open on the page.
const astraReplyPage = (overrides = {}) => ({
  ...ASTRA_REPLY,
  user: { id: 3, username: 'gpt-6-astra' },
  ancestors: [FIRST_ENTRY], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
  ...overrides,
});

// An entry of the user's under the Astra reply, already answered.
const answeredEntryPage = () => ({
  id: 12, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'peter' }, user_id: 1,
  content: 'My second entry.', ai_usage: 'chat', privacy_level: 'private',
  ancestors: [FIRST_ENTRY, ASTRA_REPLY],
  children: [{ ...ASTRA_REPLY, id: 13, content: 'Another answer.', children: [] }],
  child_count: 1,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
});

// A Glean reply, finished: what is typed under it goes to Text mode.
const READ_PROMPT = {
  id: 49, user_id: 1, username: 'peter', node_type: 'user',
  content: 'Read the day', is_system_prompt: true, prompt_key: 'read',
  ai_usage: 'chat', privacy_level: 'private', child_count: 1,
};
const gleaningPage = () => ({
  id: 50, node_type: 'llm', llm_model: 'gpt-6-luna',
  user: { id: 2, username: 'gpt-6-luna' }, user_id: 2, parent_user_id: 1,
  content: 'Nothing to pick today.', llm_task_status: 'completed',
  tool_calls_meta: null, ai_usage: 'chat', privacy_level: 'private',
  ancestors: [FIRST_ENTRY, ASTRA_REPLY, READ_PROMPT], children: [], child_count: 0,
  in_read_thread: true, read_reply_above: true, glean_thread: false,
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

let routes;
beforeEach(() => {
  mockUser = ME;
  mockFormProps = null;
  mockModalProps = null;
  mockChatPicker = null;
  routes = {};
  mockGet.mockReset();
  mockPost.mockReset();
  mockTextStream.mockReset();
  mockTextStream.mockReturnValue({ text: '', done: false });
  localStorage.clear();
  localStorage.setItem('loore_auto_generate', 'true');
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

const show = async (page) => {
  routes[`/nodes/${page.id}`] = page;
  render(
    <MemoryRouter initialEntries={[`/node/${page.id}`]}>
      <Routes>
        <Route path="/node/:id" element={<NodeDetailWrapper />} />
        <Route path="/voice" element={<div />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByText(page.content);
};

const llmResponse = () => screen.getByRole('button', { name: 'LLM Response' });
const bodyOf = (url) => mockPost.mock.calls.find(([u]) => u === url)?.[1];

describe('a reply typed in a thread on another model', () => {
  test.each([
    ['without craft mode', false],
    ['in craft mode, where no picker is on screen under an AI reply', true],
  ])('its automatic reply names no model (%s)', async (_, craft) => {
    mockUser = { ...ME, craft_mode: craft };
    await show(astraReplyPage());
    expect(mockChatPicker).toBeNull();

    await act(async () => { await mockFormProps.onSuccess({ id: 95, tip_id: 95 }); });

    expect(bodyOf('/nodes/95/llm')).toEqual({ source_mode: 'textmode', auto_generate: true });
  });

  test('an edit of an answered entry asks its new reply with no model', async () => {
    await show(answeredEntryPage());

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(mockModalProps).not.toBeNull();
    await act(async () => {
      await mockModalProps.onSuccess({
        node: { id: 12, content: 'My second entry, rewritten.' },
      });
    });

    expect(bodyOf('/nodes/12/llm')).toEqual({ source_mode: 'textmode', auto_generate: true });
  });

  test('a reply typed under a Glean reply names no model', async () => {
    mockPost.mockResolvedValue({ data: { user_node_id: 96, llm_node_id: 97 } });
    await show(gleaningPage());

    await act(async () => {
      await mockFormProps.onSubmitOverride({
        content: 'Tell me more.', privacy_level: 'private', ai_usage: 'chat',
      });
    });

    expect(bodyOf('/textmode/from-node/50')).toEqual({
      content: 'Tell me more.', ai_usage: 'chat', auto_generate: true,
    });
  });

  test('a reply recorded under a Glean reply names no model', async () => {
    mockPost.mockResolvedValue({ data: { user_node_id: 96, llm_node_id: 97 } });
    await show(gleaningPage());

    await act(async () => {
      await mockFormProps.onSubmitOverride({
        content: 'Tell me more.', privacy_level: 'private', ai_usage: 'chat',
        streaming_session_id: 'sess-1',
      });
    });

    expect(bodyOf('/drafts/streaming/sess-1/save-as-node')).toEqual({
      content: 'Tell me more.', privacy_level: 'private', ai_usage: 'chat',
      agentic: true, auto_generate: true,
    });
  });

  test('Voice Mode names no model', async () => {
    mockPost.mockResolvedValue({ data: { mode: 'ready', parent_id: 11 } });
    await show(astraReplyPage());

    await act(async () => {
      fireEvent.click(screen.getByTitle('Continue this conversation by voice'));
    });

    expect(bodyOf('/voice/from-node/11')).toEqual({});
  });
});

describe('the LLM Response picker', () => {
  beforeEach(() => {
    mockUser = { ...ME, craft_mode: true };
    localStorage.setItem('loore_auto_generate', 'false');
  });

  test('LLM Response pressed before the picker has the thread\'s model names no model', async () => {
    await show(astraReplyPage());
    expect(mockChatPicker.selectedModel).toBe('claude-opus-5.5');

    await act(async () => { llmResponse().click(); });
    expect(bodyOf('/nodes/11/llm')).toEqual({ source_mode: 'textmode' });
  });

  test('the model the picker fills in by itself is not sent', async () => {
    await show(astraReplyPage());
    act(() => { mockChatPicker.onModelChange('gpt-6-astra'); });
    expect(mockChatPicker.selectedModel).toBe('gpt-6-astra');

    await act(async () => { llmResponse().click(); });
    expect(bodyOf('/nodes/11/llm')).toEqual({ source_mode: 'textmode' });
  });

  test('a model the person picks is sent with LLM Response', async () => {
    await show(astraReplyPage());
    act(() => { mockChatPicker.onModelChange('gpt-6-astra'); });
    act(() => { mockChatPicker.onModelChange('claude-opus-5.5', { picked: true }); });

    await act(async () => { llmResponse().click(); });
    expect(bodyOf('/nodes/11/llm')).toEqual({
      model: 'claude-opus-5.5', source_mode: 'textmode',
    });
  });

  test('a model the person picks is sent with the automatic reply to what they type next', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    // An entry that waits for its reply: the picker is on screen with
    // auto-generate on.
    await show({ ...answeredEntryPage(), children: [], child_count: 0 });
    act(() => { mockChatPicker.onModelChange('claude-sonnet-5', { picked: true }); });

    await act(async () => { await mockFormProps.onSuccess({ id: 95, tip_id: 95 }); });

    expect(bodyOf('/nodes/95/llm')).toEqual({
      model: 'claude-sonnet-5', source_mode: 'textmode', auto_generate: true,
    });
  });

  test('a pick the picker has since replaced by the thread\'s model is not sent', async () => {
    await show(astraReplyPage());
    act(() => { mockChatPicker.onModelChange('claude-opus-5.5', { picked: true }); });
    act(() => { mockChatPicker.onModelChange('gpt-6-astra'); });

    await act(async () => { llmResponse().click(); });
    expect(bodyOf('/nodes/11/llm')).toEqual({ source_mode: 'textmode' });
  });
});
