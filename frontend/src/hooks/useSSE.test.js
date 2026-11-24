// #374: iOS can kill a suspended page's EventSource without an error
// event. A stream that opts in (reconnectOnReturn) gets a fresh connection
// when the user returns to the page; the TTS stream's replayed chunks
// reach the page once each.
import { renderHook, act } from '@testing-library/react';
import {
  useSSE, useTTSStreamSSE, useLlmTextStream, reconnectWait, RECONNECT_MAX_DELAY_MS,
} from './useSSE';

class MockEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = MockEventSource.OPEN;
    this.listeners = {};
    this.closed = false;
    MockEventSource.instances.push(this);
  }

  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }

  close() {
    this.closed = true;
    this.readyState = MockEventSource.CLOSED;
  }

  emit(type, data) {
    (this.listeners[type] || []).forEach((fn) => fn({ data: JSON.stringify(data) }));
  }
}
MockEventSource.CONNECTING = 0;
MockEventSource.OPEN = 1;
MockEventSource.CLOSED = 2;

let visibility = 'visible';
beforeAll(() => {
  global.EventSource = MockEventSource;
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => visibility,
  });
});

beforeEach(() => {
  MockEventSource.instances = [];
  visibility = 'visible';
  jest.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  console.log.mockRestore();
});

const leaveAndReturn = () => {
  act(() => {
    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
  });
  act(() => {
    visibility = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
  });
};

const open = () => MockEventSource.instances.filter((es) => !es.closed);

test('a stream with reconnectOnReturn is replaced when the user returns', () => {
  renderHook(() => useSSE('/api/sse/x', { enabled: true, reconnectOnReturn: true }));
  expect(MockEventSource.instances).toHaveLength(1);

  leaveAndReturn();
  expect(MockEventSource.instances).toHaveLength(2);
  expect(MockEventSource.instances[0].closed).toBe(true);
  expect(open()).toHaveLength(1);

  act(() => { window.dispatchEvent(new Event('online')); });
  expect(MockEventSource.instances).toHaveLength(3);
  expect(open()).toHaveLength(1);
});

test('without the option, or when disabled, a return leaves the stream alone', () => {
  renderHook(() => useSSE('/api/sse/x', { enabled: true }));
  renderHook(() => useSSE('/api/sse/y', { enabled: false, reconnectOnReturn: true }));
  expect(MockEventSource.instances).toHaveLength(1);

  leaveAndReturn();
  expect(MockEventSource.instances).toHaveLength(1);
  expect(MockEventSource.instances[0].closed).toBe(false);
});

test('the TTS stream replayed after a return delivers each chunk once', () => {
  const onChunkReady = jest.fn();
  renderHook(() => useTTSStreamSSE(42, {
    enabled: true, reconnectOnReturn: true, onChunkReady,
  }));
  const first = MockEventSource.instances[0];
  expect(first.url).toBe('/api/sse/nodes/42/tts-stream');
  act(() => first.emit('chunk_ready', { chunk_index: 0, audio_url: '/a/0.mp3', duration: 2 }));

  leaveAndReturn();
  const second = MockEventSource.instances[1];
  expect(second.url).toBe('/api/sse/nodes/42/tts-stream');
  // The fresh connection sends every chunk made so far, then the new one.
  act(() => second.emit('chunk_ready', { chunk_index: 0, audio_url: '/a/0.mp3', duration: 2 }));
  act(() => second.emit('chunk_ready', { chunk_index: 1, audio_url: '/a/1.mp3', duration: 2 }));

  expect(onChunkReady.mock.calls.map(([data]) => data.chunk_index)).toEqual([0, 1]);
});

// The server's `error` event (generation failed) reaches onError; a dropped
// connection, which fires `error` without data, does not.
test("the TTS stream passes the server's error event to onError", () => {
  jest.spyOn(console, 'warn').mockImplementation(() => {});
  const onError = jest.fn();
  const { rerender } = renderHook(({ enabled }) => useTTSStreamSSE(9, {
    enabled, entityType: 'profile', onError,
  }), { initialProps: { enabled: true } });
  const es = MockEventSource.instances[0];
  expect(es.url).toBe('/api/sse/profiles/9/tts-stream');

  act(() => es.listeners.error.forEach((fn) => fn({})));
  expect(onError).not.toHaveBeenCalled();
  act(() => es.emit('error', { message: 'TTS generation failed' }));
  expect(onError).toHaveBeenCalledWith({ message: 'TTS generation failed' });

  // The caller then turns the stream off: closed, and no new connection.
  rerender({ enabled: false });
  expect(es.closed).toBe(true);
  expect(MockEventSource.instances).toHaveLength(1);
  console.warn.mockRestore();
});

test("a reply's text stream reconnects on return and takes the fresh snapshot", () => {
  const { result } = renderHook(() => useLlmTextStream(7, { enabled: true, initialText: 'Hel' }));
  expect(MockEventSource.instances).toHaveLength(1);
  expect(result.current.text).toBe('Hel');

  leaveAndReturn();
  expect(MockEventSource.instances).toHaveLength(2);
  act(() => MockEventSource.instances[1].emit('snapshot', { text: 'Hello, world' }));
  expect(result.current.text).toBe('Hello, world');
});

// A stream the server refuses (429 when the account holds the most open
// streams; 400, 404) is asked for again after a delay that doubles with each
// refusal in a row, up to RECONNECT_MAX_DELAY_MS, and starts over once a
// connection opens.
describe('reconnecting after refusals', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    jest.useRealTimers();
    console.error.mockRestore();
  });

  // Handlers that stay the same between renders, as the callers memoize them.
  const HANDLERS = {};
  const refuse = (es) => act(() => {
    es.readyState = MockEventSource.CLOSED;
    es.onerror(new Event('error'));
  });
  const last = () => MockEventSource.instances[MockEventSource.instances.length - 1];
  // How long until the next connection is made after a refusal.
  const waitBeforeNext = () => {
    const before = MockEventSource.instances.length;
    let waited = 0;
    while (MockEventSource.instances.length === before && waited < 120000) {
      act(() => { jest.advanceTimersByTime(500); });
      waited += 500;
    }
    return waited;
  };

  test('the delay doubles with each refusal, up to the most', () => {
    renderHook(() => useSSE('/api/sse/x', { enabled: true, eventHandlers: HANDLERS }));
    const waits = [];
    for (let i = 0; i < 7; i += 1) {
      refuse(last());
      waits.push(waitBeforeNext());
    }
    expect(waits).toEqual([3000, 6000, 12000, 24000, 48000,
      RECONNECT_MAX_DELAY_MS, RECONNECT_MAX_DELAY_MS]);
  });

  test('a connection that opens starts over at the first delay', () => {
    renderHook(() => useSSE('/api/sse/x', { enabled: true, eventHandlers: HANDLERS }));
    refuse(last());
    waitBeforeNext();
    refuse(last());
    expect(waitBeforeNext()).toBe(6000);
    act(() => { last().onopen(); });
    refuse(last());
    expect(waitBeforeNext()).toBe(3000);
  });

  test('reconnectWait', () => {
    expect(reconnectWait(3000, 1)).toBe(3000);
    expect(reconnectWait(3000, 3)).toBe(12000);
    expect(reconnectWait(3000, 30)).toBe(RECONNECT_MAX_DELAY_MS);
  });
});
