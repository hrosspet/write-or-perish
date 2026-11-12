// AI replies in a shared thread (Circles): any member presses the normal
// LLM Response under their own nodes of the thread, as in a public thread
// (Peter, 2026-11-12: "public rule"), and the request is the normal one.
// The reply is created private (Peter, 2026-11-12): only its requester
// reads it until they share it, so the dialog before it names nobody and
// comes only when the server sends a key (a prompt someone else wrote,
// edited or from scratch, reads the requester's data).
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
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: () => ({ text: '', done: false }),
}));

let mockFormProps;
let mockFooterProps;
let mockContentProps;
jest.mock('./NodeForm', () => (props) => { mockFormProps = props; return null; });
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./NodeFormModal', () => () => null);
jest.mock('./ModelSelector', () => () => null);
jest.mock('./SpeakerIcon', () => () => null);
jest.mock('./DownloadAudioIcon', () => () => null);
jest.mock('./SemanticNeighbors', () => () => null);
jest.mock('./FeedPicks', () => () => null);
jest.mock('./NodeFooter', () => (props) => {
  mockFooterProps = props;
  return <div>{props.children}</div>;
});
jest.mock('./QuotedContent', () => (props) => {
  mockContentProps = props;
  return <div>{props.content}</div>;
});

import React from 'react';
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';

// anna wrote the entry and shared it with ben and chidi.
const ANNA = { id: 1, username: 'anna', craft_mode: false, preferred_model: 'anna-model', default_ai_usage: 'chat' };
const BEN = { id: 2, username: 'ben', craft_mode: false, preferred_model: 'ben-model', default_ai_usage: 'chat' };

const audienceFor = (me, overrides = {}) => ({
  entry_id: 10, author: 'anna', readers: ['ben', 'chidi'],
  is_author: me.username === 'anna', entry_ai_usage: 'chat', ...overrides,
});

const sharedEntry = (me, overrides = {}) => ({
  id: 10, node_type: 'user', llm_model: null,
  user: { id: 1, username: 'anna' }, user_id: 1,
  content: 'A shared entry.', ai_usage: 'chat', privacy_level: 'circles',
  ancestors: [], children: [], child_count: 0,
  in_read_thread: false, read_reply_above: false, glean_thread: false,
  audience: audienceFor(me),
  ...overrides,
});

// ben's own reply in anna's shared thread: where he asks for AI replies.
const bensReply = (overrides = {}) => sharedEntry(BEN, {
  id: 11, user: { id: 2, username: 'ben' }, user_id: 2,
  content: 'A reply by ben.',
  ancestors: [{ id: 10, node_type: 'user', user_id: 1, username: 'anna', content: 'A shared entry.' }],
  ...overrides,
});

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

let routes;
beforeEach(() => {
  mockUser = BEN;
  mockFormProps = null;
  mockFooterProps = null;
  mockContentProps = null;
  routes = {};
  mockGet.mockReset();
  mockPost.mockReset();
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
    <Routes>
      <Route path="/node/:id" element={<NodeDetailWrapper />} />
    </Routes>
  </MemoryRouter>,
);

const press = async (name) => {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }));
  });
};

describe('a reader in someone else\'s shared thread', () => {
  test('has no LLM Response on the author\'s entry: he replies first, as in a public thread', async () => {
    routes['/nodes/10'] = sharedEntry(BEN);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.queryByRole('button', { name: 'LLM Response' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Ask my AI' })).toBeNull();
    expect(screen.getByText(
      /To ask your AI, write your reply first and press LLM Response under it\. Only you see the AI reply until you share it\./,
    )).toBeInTheDocument();
    // Voice Mode and auto-generate stay the author's.
    expect(screen.queryByText('Voice Mode')).toBeNull();
    expect(screen.queryByText('Auto-generate')).toBeNull();
  });

  test('has LLM Response on his own reply in the thread, and no "Ask my AI"', async () => {
    routes['/nodes/11'] = bensReply();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Ask my AI' })).toBeNull();
    expect(screen.getByText(
      /An AI reply is generated here only when you press LLM Response, and only you see it until you share it\./,
    )).toBeInTheDocument();
  });

  test('the reply is the requester\'s alone: nobody is named, and the normal request is sent', async () => {
    routes['/nodes/11'] = bensReply();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    // Every AI reply is created private (Peter, 2026-11-12): with no
    // prompt of someone else's reading ben's data there is nothing to ask.
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [url, body] = mockPost.mock.calls[0];
    expect(url).toBe('/nodes/11/llm');
    // The requester's own model, and nothing that marks the request as
    // different from one in a private thread.
    expect(body).toEqual({ model: 'ben-model', source_mode: 'textmode' });
    expect(body).not.toHaveProperty('circle_ask');
    expect(body).not.toHaveProperty('privacy_level');
  });

  test('has no LLM Response on another reader\'s reply either', async () => {
    mockUser = { ...BEN, craft_mode: true };
    routes['/nodes/12'] = sharedEntry(BEN, {
      id: 12, user: { id: 3, username: 'chidi' }, user_id: 3,
      content: 'A reply by chidi.',
      ancestors: [{ id: 10, node_type: 'user', user_id: 1, username: 'anna', content: 'A shared entry.' }],
    });
    renderAt('/node/12');
    await screen.findByText('A reply by chidi.');
    expect(screen.queryByRole('button', { name: 'LLM Response' })).toBeNull();
  });

  test('the entry\'s AI usage None: disabled, with the author named', async () => {
    routes['/nodes/11'] = bensReply({
      audience: audienceFor(BEN, { entry_ai_usage: 'none' }),
    });
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeDisabled();
    expect(screen.getByText(
      "@anna set this entry's AI usage to None, so no AI can read it.",
    )).toBeInTheDocument();
  });

  test('the node\'s own AI usage None: disabled, with the reason', async () => {
    routes['/nodes/11'] = bensReply({ ai_usage: 'none' });
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeDisabled();
    expect(screen.getByText("This entry's AI usage is None, so no AI can read it."))
      .toBeInTheDocument();
  });

  test('the reader\'s own AI usage None: disabled, with the reason', async () => {
    mockUser = { ...BEN, default_ai_usage: 'none' };
    routes['/nodes/11'] = bensReply();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeDisabled();
    expect(screen.getByText('Your own AI usage is set to None in Account settings.'))
      .toBeInTheDocument();
  });

  test('Craft mode offers no "AI reply:" choice: the reply is private until shared', async () => {
    mockUser = { ...BEN, craft_mode: true };
    // A choice stored by an earlier version changes nothing.
    localStorage.setItem('loore_circle_ai_reply_privacy_2_10', 'shared');
    routes['/nodes/11'] = bensReply();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    expect(screen.queryByRole('group', { name: 'AI reply' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Shared with this thread' })).toBeNull();
    await press('LLM Response');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).toEqual({ model: 'ben-model', source_mode: 'textmode' });
  });
});

describe('what the reply reads is agreed to exactly (ai_reads_key)', () => {
  const READS = ['profile', 'memory'];
  // On ben's own reply: the only place he asks (the public rule).
  const withKey = (key = 'key-1', reads = READS) => bensReply({
    audience: audienceFor(BEN, { ai_reads: reads, ai_reads_key: key }),
  });
  const changedAnswer = { response: { status: 409, data: {
    code: 'circle_ai_reads_changed', error: 'What your AI reads here has changed.',
    ai_reads: ['profile', 'whole archive of writing'], ai_reads_key: 'key-2',
  } } };

  test('the dialog names nobody and says what the prompt reads; the agreement and "Don\'t ask again" carry the key', async () => {
    routes['/nodes/11'] = withKey();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    const dialog = screen.getByRole('dialog');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      'Only you will see it. Your AI reads your profile and memory for this reply, '
      + 'as the prompt shared in this thread asks.');
    expect(dialog).not.toHaveTextContent('@anna');
    expect(dialog).not.toHaveTextContent('could share private information');
    expect(mockPost).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Don't ask again in this thread"));
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0]).toEqual(['/nodes/11/llm', {
      model: 'ben-model', source_mode: 'textmode', ai_reads_ack: 'private:key-1',
    }]);
    expect(localStorage.getItem('loore_circle_ai_reply_ack_2_10')).toBe('private:key-1');
  });

  test('Cancel sends nothing', async () => {
    routes['/nodes/11'] = withKey();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    await press('Cancel');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('"Don\'t ask again" for typed replies does not skip the AI dialog', async () => {
    localStorage.setItem('loore_circle_reply_ack_2_10', 'true');
    routes['/nodes/11'] = withKey();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
  });

  test('an agreement stored for the current key: no dialog, and it is sent', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-1');
    routes['/nodes/11'] = withKey();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1].ai_reads_ack).toBe('private:key-1');
  });

  test('a bare key stored by an earlier version counts for the same list', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'key-1');
    routes['/nodes/11'] = withKey();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1].ai_reads_ack).toBe('key-1');
  });

  test('a stored key that differs from the current one shows the dialog again', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-0');
    routes['/nodes/11'] = withKey();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1].ai_reads_ack).toBe('private:key-1');
    // Not ticked this time: the old tick is left as it was.
    expect(localStorage.getItem('loore_circle_ai_reply_ack_2_10')).toBe('private:key-0');
  });

  test('an older tick (no key) does not cover a list that has a key', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'true');
    routes['/nodes/11'] = withKey();
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('without a key there is no dialog and no ack, whatever is stored', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-0');
    routes['/nodes/11'] = withKey('', []);
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).not.toHaveProperty('ai_reads_ack');
  });

  test('the server says the list changed: the dialog opens again with the new list, and the retry carries the new key', async () => {
    // The stored tick is for the list the page was loaded with.
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-1');
    routes['/nodes/11'] = withKey();
    mockPost.mockRejectedValueOnce(changedAnswer);
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');

    const dialog = await screen.findByRole('dialog');
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost.mock.calls[0][1].ai_reads_ack).toBe('private:key-1');
    expect(dialog).toHaveTextContent(
      'Only you will see it. Your AI reads your profile and whole archive of writing '
      + 'for this reply, as the prompt shared in this thread asks.');

    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(mockPost.mock.calls[1]).toEqual(['/nodes/11/llm', {
      model: 'ben-model', source_mode: 'textmode', ai_reads_ack: 'private:key-2',
    }]);
  });

  test('a refusal that names the key just sent is not retried', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-1');
    routes['/nodes/11'] = withKey();
    mockPost.mockRejectedValue({ response: { status: 409, data: {
      code: 'circle_ai_reads_changed', error: 'What your AI reads here has changed.',
      ai_reads: ['profile', 'memory'], ai_reads_key: 'key-1',
    } } });
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    await waitFor(() => expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled());
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
  });

  test('a refusal that names no key is not retried', async () => {
    routes['/nodes/11'] = withKey('', []);
    mockPost.mockRejectedValue({ response: { status: 409, data: {
      code: 'circle_ai_reads_changed', error: 'What your AI reads here has changed.',
    } } });
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    await waitFor(() => expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled());
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  test('Cancel after the list changed sends nothing more', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-1');
    routes['/nodes/11'] = withKey();
    mockPost.mockRejectedValueOnce(changedAnswer);
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    await screen.findByRole('heading', { name: 'Generate an AI reply?' });
    await press('Cancel');
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled();
  });
});

describe('the author in her own shared thread', () => {
  beforeEach(() => {
    mockUser = { ...ANNA };
    localStorage.setItem('loore_auto_generate', 'true');
  });

  test('has LLM Response on her entry without Craft mode, as in a public thread, and is not asked', async () => {
    routes['/nodes/10'] = sharedEntry(ANNA);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0]).toEqual(['/nodes/10/llm', {
      model: 'anna-model', source_mode: 'textmode',
    }]);
  });

  test('has no LLM Response under a reader\'s reply in her thread (the public rule)', async () => {
    mockUser = { ...ANNA, craft_mode: true };
    routes['/nodes/11'] = sharedEntry(ANNA, {
      id: 11, user: { id: 2, username: 'ben' }, user_id: 2,
      content: 'A reply by ben.',
      ancestors: [{ id: 10, node_type: 'user', user_id: 1, username: 'anna', content: 'A shared entry.' }],
    });
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    expect(screen.queryByRole('button', { name: 'LLM Response' })).toBeNull();
  });

  test('auto-generate is off and can\'t be turned on in her shared thread', async () => {
    mockUser = { ...ANNA, craft_mode: true };
    routes['/nodes/10'] = sharedEntry(ANNA);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    const toggle = screen.getByRole('button', { name: /Auto-generate/ });
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    expect(toggle).toHaveAttribute('title', 'Auto-generate is off in public and shared threads.');
    // The stored preference for private threads is left as it was.
    expect(localStorage.getItem('loore_auto_generate')).toBe('true');
  });

  test('an entry written under her shared entry gets no automatic reply', async () => {
    routes['/nodes/10'] = sharedEntry(ANNA);
    routes['/nodes/12'] = sharedEntry(ANNA, { id: 12, content: 'Her next entry.' });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    let sent;
    await act(async () => {
      sent = mockFormProps.onSuccess({ id: 12, privacy_level: 'circles' });
    });
    await act(async () => { await sent; });
    expect(mockPost).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    expect(await screen.findByText('Her next entry.')).toBeInTheDocument();
  });

  test('her entry\'s AI usage None: disabled, in her own words', async () => {
    routes['/nodes/10'] = sharedEntry(ANNA, {
      ai_usage: 'none', audience: audienceFor(ANNA, { entry_ai_usage: 'none' }),
    });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeDisabled();
    expect(screen.getByText("This entry's AI usage is None, so no AI can read it."))
      .toBeInTheDocument();
  });
});

describe('"Only you see this reply" in a shared thread', () => {
  const privateAiReply = (overrides = {}) => ({
    id: 20, node_type: 'llm', llm_model: 'claude-fable-5.2',
    user: { id: 9, username: 'claude-fable-5.2' }, user_id: 9,
    parent_user_id: 1, human_owner_username: 'ben',
    llm_label: 'Claude Fable 5.2 via @ben', llm_task_status: 'completed',
    content: 'My private AI reply.', ai_usage: 'chat', privacy_level: 'private',
    ancestors: [{ id: 10, node_type: 'user', user_id: 1, username: 'anna',
                  content: 'A shared entry.', privacy_level: 'circles' }],
    children: [], child_count: 0,
    in_read_thread: false, read_reply_above: false, glean_thread: false,
    reply_only_private: true,
    thread_ai_reads: { entry_id: 10, ai_reads: [], ai_reads_key: '' },
    ...overrides,
  });

  test('ben\'s own private AI reply under anna\'s shared entry carries the note', async () => {
    routes['/nodes/20'] = privateAiReply();
    renderAt('/node/20');
    await screen.findByText('My private AI reply.');
    expect(screen.getByText(/Only you see this reply/)).toBeInTheDocument();
  });

  test('once shared with the thread the note goes', async () => {
    routes['/nodes/20'] = privateAiReply({
      privacy_level: 'circles', audience: audienceFor(BEN),
      reply_only_private: undefined, thread_ai_reads: undefined,
    });
    renderAt('/node/20');
    await screen.findByText('My private AI reply.');
    expect(screen.queryByText(/Only you see this reply/)).toBeNull();
  });

  test('under the entry, the note sits on ben\'s reply in the list of replies', async () => {
    routes['/nodes/10'] = sharedEntry(BEN, {
      children: [{
        id: 20, node_type: 'llm', llm_model: 'claude-fable-5.2', user_id: 9,
        username: 'claude-fable-5.2', parent_user_id: 1, human_owner_username: 'ben',
        privacy_level: 'private', content: 'My private AI reply.', ai_usage: 'chat',
        llm_task_status: 'completed', children: [], child_count: 0,
      }],
      child_count: 1,
    });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(await screen.findByText(/Only you see this reply/)).toBeInTheDocument();
  });
});

describe('a shared prompt read by someone who does not own it', () => {
  const prompt = (me, contextArtifacts) => sharedEntry(me, {
    id: 5, is_system_prompt: true, prompt_title: 'Reflect', prompt_version_number: 2,
    content: 'Read {user_profile} and {user_memory}, then reflect.',
    context_artifacts: contextArtifacts,
    children: [],
  });

  test('the reader gets what the server sent (their own artifacts), bare placeholders for the rest, no version line, no edit controls', async () => {
    // For a reader the server sends the reader's own latest artifacts,
    // without version numbers; ben has a profile and no memory.
    const bens = { profile: { id: 3, content: "ben's profile" } };
    routes['/nodes/5'] = prompt(BEN, bens);
    renderAt('/node/5');
    await screen.findByText('Read {user_profile} and {user_memory}, then reflect.');
    expect(mockContentProps.contextArtifacts).toEqual(bens);
    expect(mockContentProps.barePlaceholders).toBe(true);
    expect(screen.getByText('Reflect v2')).toBeInTheDocument();
    expect(screen.queryByText(/Profile v/)).toBeNull();
    expect(screen.queryByText('Edit')).toBeNull();
    expect(screen.queryByText('Delete')).toBeNull();
  });

  test('the owner gets her pinned versions and the version line', async () => {
    mockUser = ANNA;
    const annas = { profile: { id: 1, version_number: 7, content: "anna's profile" } };
    routes['/nodes/5'] = prompt(ANNA, annas);
    renderAt('/node/5');
    await screen.findByText('Read {user_profile} and {user_memory}, then reflect.');
    expect(mockContentProps.contextArtifacts).toEqual(annas);
    expect(mockContentProps.barePlaceholders).toBe(false);
    expect(screen.getByText(/Profile v7/)).toBeInTheDocument();
  });
});

describe('an AI reply in a shared thread', () => {
  const aiReply = (me, requester) => sharedEntry(me, {
    id: 20, node_type: 'llm', llm_model: 'claude-fable-5.2',
    user: { id: 9, username: 'claude-fable-5.2' }, user_id: 9,
    // The node it answers is anna's entry.
    parent_user_id: 1,
    human_owner_username: requester,
    llm_label: `Claude Fable 5.2 via @${requester}`,
    llm_task_status: 'completed',
    content: 'An AI reply.',
    ancestors: [{ id: 10, node_type: 'user', user_id: 1, username: 'anna', content: 'A shared entry.' }],
  });

  test('the footer gets the label and the requester', async () => {
    routes['/nodes/20'] = aiReply(BEN, 'ben');
    renderAt('/node/20');
    await screen.findByText('An AI reply.');
    expect(mockFooterProps.llmLabel).toBe('Claude Fable 5.2 via @ben');
    expect(mockFooterProps.humanOwnerUsername).toBe('ben');
  });

  test('it is its requester\'s to edit, not the author\'s whose entry it answers', async () => {
    // The pin button's title says whether the page takes the viewer for
    // the node's owner.
    routes['/nodes/20'] = aiReply(BEN, 'ben');
    const { unmount } = renderAt('/node/20');
    await screen.findByText('An AI reply.');
    expect(screen.queryByTitle('Only the owner can pin')).toBeNull();
    unmount();

    mockUser = ANNA;
    routes['/nodes/20'] = aiReply(ANNA, 'ben');
    renderAt('/node/20');
    await screen.findByText('An AI reply.');
    expect(screen.getByTitle('Only the owner can pin')).toBeInTheDocument();
  });
});
