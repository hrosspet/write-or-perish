// #374: iOS can kill a suspended page's EventSource without an error
// event. A stream that opts in (reconnectOnReturn) gets a fresh connection
// when the user returns to the page; the TTS stream's replayed chunks
// reach the page once each.
import { renderHook, act } from '@testing-library/react';
import { useSSE, useTTSStreamSSE, useLlmTextStream } from './useSSE';

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

test("a reply's text stream reconnects on return and takes the fresh snapshot", () => {
  const { result } = renderHook(() => useLlmTextStream(7, { enabled: true, initialText: 'Hel' }));
  expect(MockEventSource.instances).toHaveLength(1);
  expect(result.current.text).toBe('Hel');

  leaveAndReturn();
  expect(MockEventSource.instances).toHaveLength(2);
  act(() => MockEventSource.instances[1].emit('snapshot', { text: 'Hello, world' }));
  expect(result.current.text).toBe('Hello, world');
});
