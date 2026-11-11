// AI replies in a shared thread (Circles): any member presses the normal
// LLM Response on any node of the thread, the request is the normal one,
// and the people who will see the reply are named first.
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
  test('has LLM Response on the author\'s entry, and no "Ask my AI"', async () => {
    routes['/nodes/10'] = sharedEntry(BEN);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Ask my AI' })).toBeNull();
    expect(screen.getByText(/No AI reply is generated here unless you press LLM Response\./))
      .toBeInTheDocument();
    // Voice Mode and auto-generate stay the author's.
    expect(screen.queryByText('Voice Mode')).toBeNull();
    expect(screen.queryByText('Auto-generate')).toBeNull();
  });

  test('the people who will see the reply are named, then the normal request is sent', async () => {
    routes['/nodes/10'] = sharedEntry(BEN);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');

    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    // This thread's prompt reads none of ben's artifacts: one sentence.
    expect(screen.getByText('@anna and @chidi will see it, marked with your name.'))
      .toBeInTheDocument();
    expect(screen.queryByText(/Your AI reads/)).toBeNull();
    expect(mockPost).not.toHaveBeenCalled();

    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [url, body] = mockPost.mock.calls[0];
    expect(url).toBe('/nodes/10/llm');
    // The requester's own model, and nothing that marks the request as
    // different from one in a private thread.
    expect(body).toEqual({ model: 'ben-model', source_mode: 'textmode' });
    expect(body).not.toHaveProperty('circle_ask');
    expect(body).not.toHaveProperty('privacy_level');
  });

  test('the dialog says which of the requester\'s artifacts the reply reads', async () => {
    routes['/nodes/10'] = sharedEntry(BEN, {
      audience: audienceFor(BEN, { ai_reads: ['profile', 'memory', 'to-do list'] }),
    });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.getByText(
      '@anna and @chidi will see it, marked with your name. '
      + 'Your AI reads your profile, memory and to-do list for this reply '
      + 'and could share private information from them.',
    )).toBeInTheDocument();
  });

  test('Cancel sends nothing', async () => {
    routes['/nodes/10'] = sharedEntry(BEN);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    await press('Cancel');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('"Don\'t ask again" for typed replies does not skip the AI dialog; its own tick does', async () => {
    localStorage.setItem('loore_circle_reply_ack_2_10', 'true');
    routes['/nodes/10'] = sharedEntry(BEN);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Don't ask again in this thread"));
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(localStorage.getItem('loore_circle_ai_reply_ack_2_10')).toBe('1');
  });

  test('with the AI tick stored, the request goes out without the dialog', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'true');
    routes['/nodes/10'] = sharedEntry(BEN);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
  });

  test('has LLM Response on another reader\'s reply too', async () => {
    routes['/nodes/11'] = sharedEntry(BEN, {
      id: 11, user: { id: 3, username: 'chidi' }, user_id: 3,
      content: 'A reply by chidi.',
      ancestors: [{ id: 10, node_type: 'user', user_id: 1, username: 'anna', content: 'A shared entry.' }],
    });
    renderAt('/node/11');
    await screen.findByText('A reply by chidi.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled();
  });

  test('the entry\'s AI usage None: disabled, with the author named', async () => {
    routes['/nodes/10'] = sharedEntry(BEN, {
      audience: audienceFor(BEN, { entry_ai_usage: 'none' }),
    });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeDisabled();
    expect(screen.getByText(
      "@anna set this entry's AI usage to None, so no AI can read it.",
    )).toBeInTheDocument();
  });

  test('the node\'s own AI usage None: disabled, with the reason', async () => {
    routes['/nodes/10'] = sharedEntry(BEN, { ai_usage: 'none' });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeDisabled();
    expect(screen.getByText("This entry's AI usage is None, so no AI can read it."))
      .toBeInTheDocument();
  });

  test('the reader\'s own AI usage None: disabled, with the reason', async () => {
    mockUser = { ...BEN, default_ai_usage: 'none' };
    routes['/nodes/10'] = sharedEntry(BEN);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeDisabled();
    expect(screen.getByText('Your own AI usage is set to None in Account settings.'))
      .toBeInTheDocument();
  });

  test('without Craft mode there is no "AI reply:" choice', async () => {
    localStorage.setItem('loore_circle_ai_reply_privacy_2_10', 'private');
    routes['/nodes/10'] = sharedEntry(BEN);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.queryByRole('group', { name: 'AI reply' })).toBeNull();
    // A stored choice that is not on the page does not make the reply private.
    await press('LLM Response');
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).not.toHaveProperty('privacy_level');
  });
});

describe('what the reply reads is agreed to exactly (ai_reads_key)', () => {
  const READS = ['profile', 'memory'];
  const withKey = (key = 'key-1', reads = READS) => sharedEntry(BEN, {
    audience: audienceFor(BEN, { ai_reads: reads, ai_reads_key: key }),
  });
  const changedAnswer = { response: { status: 409, data: {
    code: 'circle_ai_reads_changed', error: 'What your AI reads here has changed.',
    ai_reads: ['profile', 'whole archive of writing'], ai_reads_key: 'key-2',
  } } };

  test('the key of the list that was shown is sent, and "Don\'t ask again" stores that key', async () => {
    routes['/nodes/10'] = withKey();
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.getByText(/Your AI reads your profile and memory for this reply/))
      .toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Don't ask again in this thread"));
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0]).toEqual(['/nodes/10/llm', {
      model: 'ben-model', source_mode: 'textmode', ai_reads_ack: 'key-1',
    }]);
    expect(localStorage.getItem('loore_circle_ai_reply_ack_2_10')).toBe('key-1');
  });

  test('a stored key equal to the current one: no dialog, and the key is sent', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'key-1');
    routes['/nodes/10'] = withKey();
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1].ai_reads_ack).toBe('key-1');
  });

  test('a stored key that differs from the current one shows the dialog again', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'key-0');
    routes['/nodes/10'] = withKey();
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1].ai_reads_ack).toBe('key-1');
    // Not ticked this time: the old tick is left as it was.
    expect(localStorage.getItem('loore_circle_ai_reply_ack_2_10')).toBe('key-0');
  });

  test('an older tick (no key) does not cover a list that has a key', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'true');
    routes['/nodes/10'] = withKey();
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('without a key an older tick still skips the dialog, and no ack is sent', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'true');
    routes['/nodes/10'] = withKey('', []);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).not.toHaveProperty('ai_reads_ack');
  });

  test('the server says the list changed: the dialog opens again with the new list, and the retry carries the new key', async () => {
    // The stored tick is for the list the page was loaded with.
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'key-1');
    routes['/nodes/10'] = withKey();
    mockPost.mockRejectedValueOnce(changedAnswer);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');

    expect(await screen.findByRole('heading', { name: 'Generate an AI reply?' }))
      .toBeInTheDocument();
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost.mock.calls[0][1].ai_reads_ack).toBe('key-1');
    expect(screen.getByText(
      '@anna and @chidi will see it, marked with your name. '
      + 'Your AI reads your profile and whole archive of writing for this reply '
      + 'and could share private information from them.',
    )).toBeInTheDocument();

    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(mockPost.mock.calls[1]).toEqual(['/nodes/10/llm', {
      model: 'ben-model', source_mode: 'textmode', ai_reads_ack: 'key-2',
    }]);
  });

  test('a refusal that names the key just sent is not retried', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'key-1');
    routes['/nodes/10'] = withKey();
    mockPost.mockRejectedValue({ response: { status: 409, data: {
      code: 'circle_ai_reads_changed', error: 'What your AI reads here has changed.',
      ai_reads: ['profile', 'memory'], ai_reads_key: 'key-1',
    } } });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    await waitFor(() => expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled());
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
  });

  test('a refusal that names no key is not retried', async () => {
    routes['/nodes/10'] = withKey('', []);
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', '1');
    mockPost.mockRejectedValue({ response: { status: 409, data: {
      code: 'circle_ai_reads_changed', error: 'What your AI reads here has changed.',
    } } });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    await waitFor(() => expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled());
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  test('Cancel after the list changed sends nothing more', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'key-1');
    routes['/nodes/10'] = withKey();
    mockPost.mockRejectedValueOnce(changedAnswer);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    await screen.findByRole('heading', { name: 'Generate an AI reply?' });
    await press('Cancel');
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'LLM Response' })).toBeEnabled();
  });

  test('"Only me" without a key: no dialog and no ack', async () => {
    mockUser = { ...BEN, craft_mode: true };
    routes['/nodes/10'] = withKey('', ['profile']);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('Only me');
    expect(screen.queryByText(/Your AI reads/)).toBeNull();
    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0]).toEqual(['/nodes/10/llm', {
      model: 'ben-model', source_mode: 'textmode', privacy_level: 'private',
    }]);
  });

  test('"Only me" with a key: the dialog in its private wording, and the ack goes with privacy_level "private"', async () => {
    mockUser = { ...BEN, craft_mode: true };
    routes['/nodes/10'] = withKey();
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('Only me');
    // The note under the choice is worded for a private reply.
    expect(screen.getByText(
      'Your AI reads your profile and memory for this reply, as the prompt shared in this thread asks.',
    )).toBeInTheDocument();
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
    // Agreed to in the private dialog: the ack and the stored value say so.
    expect(mockPost.mock.calls[0]).toEqual(['/nodes/10/llm', {
      model: 'ben-model', source_mode: 'textmode', privacy_level: 'private',
      ai_reads_ack: 'private:key-1',
    }]);
    expect(localStorage.getItem('loore_circle_ai_reply_ack_2_10')).toBe('private:key-1');
  });

  test('a private agreement stored for the current key: no private dialog, "private:<key>" is sent', async () => {
    mockUser = { ...BEN, craft_mode: true };
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-1');
    localStorage.setItem('loore_circle_ai_reply_privacy_2_10', 'private');
    routes['/nodes/10'] = withKey();
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).toEqual({
      model: 'ben-model', source_mode: 'textmode', privacy_level: 'private',
      ai_reads_ack: 'private:key-1',
    });
  });

  test('a private agreement does not skip the shared dialog; the bare key goes out only after it is confirmed', async () => {
    mockUser = { ...BEN, craft_mode: true };
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-1');
    routes['/nodes/10'] = withKey();
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('@anna and @chidi will see it, marked with your name.');
    expect(dialog).toHaveTextContent('could share private information from them.');
    expect(mockPost).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Don't ask again in this thread"));
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0]).toEqual(['/nodes/10/llm', {
      model: 'ben-model', source_mode: 'textmode', ai_reads_ack: 'key-1',
    }]);
    // Ticked in the shared dialog: the bare key replaces the private one.
    expect(localStorage.getItem('loore_circle_ai_reply_ack_2_10')).toBe('key-1');
  });

  test('without a key, a private agreement left from an earlier key does not skip the shared dialog', async () => {
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'private:key-0');
    routes['/nodes/10'] = withKey('', []);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    expect(mockPost).not.toHaveBeenCalled();
  });

  test('a shared agreement skips the private dialog too, and the bare key is sent', async () => {
    mockUser = { ...BEN, craft_mode: true };
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'key-1');
    localStorage.setItem('loore_circle_ai_reply_privacy_2_10', 'private');
    routes['/nodes/10'] = withKey();
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).toEqual({
      model: 'ben-model', source_mode: 'textmode', privacy_level: 'private',
      ai_reads_ack: 'key-1',
    });
  });

  test('"Only me": the server says the list changed, the private dialog opens again and the retry carries the new key', async () => {
    mockUser = { ...BEN, craft_mode: true };
    localStorage.setItem('loore_circle_ai_reply_ack_2_10', 'key-1');
    localStorage.setItem('loore_circle_ai_reply_privacy_2_10', 'private');
    routes['/nodes/10'] = withKey();
    mockPost.mockRejectedValueOnce(changedAnswer);
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(
      'Only you will see it. Your AI reads your profile and whole archive of writing '
      + 'for this reply, as the prompt shared in this thread asks.');
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(mockPost.mock.calls[1]).toEqual(['/nodes/10/llm', {
      model: 'ben-model', source_mode: 'textmode', privacy_level: 'private',
      ai_reads_ack: 'private:key-2',
    }]);
  });
});

describe('Craft mode: who reads the AI reply', () => {
  beforeEach(() => {
    mockUser = { ...BEN, craft_mode: true };
    routes['/nodes/10'] = sharedEntry(BEN);
  });

  test('the default is the thread', async () => {
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.getByRole('group', { name: 'AI reply' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Shared with this thread' }))
      .toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Only me' }))
      .toHaveAttribute('aria-pressed', 'false');
    // No artifacts are read: no note under the choice.
    expect(screen.queryByText(/Your AI reads/)).toBeNull();
  });

  test('under the choice, a note says which artifacts the reply reads', async () => {
    routes['/nodes/10'] = sharedEntry(BEN, {
      audience: audienceFor(BEN, { ai_reads: ['profile', 'memory'] }),
    });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    expect(screen.getByText(
      'Your AI reads your profile and memory for this reply '
      + 'and could share private information from them.',
    )).toBeInTheDocument();
  });

  test('"Only me" sends privacy_level "private", without the dialog, and is remembered', async () => {
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('Only me');
    expect(screen.getByRole('button', { name: 'Only me' }))
      .toHaveAttribute('aria-pressed', 'true');
    expect(localStorage.getItem('loore_circle_ai_reply_privacy_2_10')).toBe('private');

    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0]).toEqual(['/nodes/10/llm', {
      model: 'ben-model', source_mode: 'textmode', privacy_level: 'private',
    }]);
  });

  test('back to "Shared with this thread": no privacy_level, and the dialog again', async () => {
    localStorage.setItem('loore_circle_ai_reply_privacy_2_10', 'private');
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('Shared with this thread');
    await press('LLM Response');
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0][1]).toEqual({ model: 'ben-model', source_mode: 'textmode' });
  });
});

describe('the author in her own shared thread', () => {
  beforeEach(() => {
    mockUser = { ...ANNA, craft_mode: true };
    localStorage.setItem('loore_auto_generate', 'false');
  });

  test('has LLM Response on a reader\'s reply, and is asked before generating', async () => {
    routes['/nodes/11'] = sharedEntry(ANNA, {
      id: 11, user: { id: 2, username: 'ben' }, user_id: 2,
      content: 'A reply by ben.',
      ancestors: [{ id: 10, node_type: 'user', user_id: 1, username: 'anna', content: 'A shared entry.' }],
    });
    renderAt('/node/11');
    await screen.findByText('A reply by ben.');
    await press('LLM Response');
    expect(screen.getByText('@ben and @chidi will see it, marked with your name.'))
      .toBeInTheDocument();
    await press('Generate');
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(mockPost.mock.calls[0]).toEqual(['/nodes/11/llm', {
      model: 'anna-model', source_mode: 'textmode',
    }]);
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

  test('an automatic reply is asked about first too; Cancel keeps the entry and asks for no reply', async () => {
    localStorage.setItem('loore_auto_generate', 'true');
    routes['/nodes/10'] = sharedEntry(ANNA);
    routes['/nodes/12'] = sharedEntry(ANNA, { id: 12, content: 'Her next entry.' });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    let sent;
    await act(async () => { sent = mockFormProps.onSuccess({ id: 12 }); });
    expect(await screen.findByRole('heading', { name: 'Generate an AI reply?' }))
      .toBeInTheDocument();
    await press('Cancel');
    await act(async () => { await sent; });
    expect(mockPost).not.toHaveBeenCalled();
    expect(await screen.findByText('Her next entry.')).toBeInTheDocument();
  });

  test('with nobody else reading the thread she is not asked', async () => {
    routes['/nodes/10'] = sharedEntry(ANNA, {
      audience: audienceFor(ANNA, { readers: [] }),
    });
    renderAt('/node/10');
    await screen.findByText('A shared entry.');
    await press('LLM Response');
    expect(screen.queryByRole('heading', { name: 'Generate an AI reply?' })).toBeNull();
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
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
