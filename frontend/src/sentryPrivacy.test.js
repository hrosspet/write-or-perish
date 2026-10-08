import * as Sentry from '@sentry/react';
import { scrubBreadcrumb, scrubEvent, sentryPrivacyOptions, withoutQuery } from './sentryPrivacy';

describe('scrubBreadcrumb', () => {
  it('drops the search words from a search call', () => {
    const crumb = scrubBreadcrumb({
      category: 'xhr',
      type: 'http',
      data: { method: 'GET', url: '/api/search?q=my+private+words&mode=archive', status_code: 500 },
    });
    expect(crumb.data).toEqual({ method: 'GET', url: '/api/search', status_code: 500 });
  });

  it('drops the query and fragment of an absolute fetch address', () => {
    const crumb = scrubBreadcrumb({
      category: 'fetch',
      type: 'http',
      data: {
        method: 'GET',
        url: 'https://example.org/api/search/semantic?q=private#top',
        'http.query': '?q=private',
        'http.fragment': '#top',
      },
    });
    expect(crumb.data).toEqual({ method: 'GET', url: 'https://example.org/api/search/semantic' });
  });

  it('drops the token from a navigation away from the confirm-email page', () => {
    const crumb = scrubBreadcrumb({
      category: 'navigation',
      data: {
        from: '/confirm-email?token=abc123',
        to: '/login?returnUrl=%2Fconfirm-email%3Ftoken%3Dabc123',
      },
    });
    expect(crumb.data).toEqual({ from: '/confirm-email', to: '/login' });
  });

  it('drops a fragment', () => {
    const crumb = scrubBreadcrumb({ category: 'navigation', data: { from: '/log', to: '/node/42#reply-7' } });
    expect(crumb.data.to).toBe('/node/42');
  });

  it('leaves an address without a query unchanged', () => {
    const crumb = scrubBreadcrumb({
      category: 'xhr',
      data: { method: 'POST', url: '/api/nodes/42/llm', status_code: 200 },
    });
    expect(crumb.data).toEqual({ method: 'POST', url: '/api/nodes/42/llm', status_code: 200 });
  });

  it('drops console breadcrumbs', () => {
    expect(scrubBreadcrumb({
      category: 'console',
      level: 'log',
      message: '[VoiceSession] TTS trigger: [object Object]',
      data: { arguments: ['[VoiceSession] TTS trigger:', { contentPreview: 'The opening of a reply' }] },
    })).toBeNull();
  });

  it('describes a clicked element by tag, id and classes, without its labels', () => {
    document.body.innerHTML = `
      <div class="chapters">
        <button id="chapter-2" type="button" title="The opening of my reply"
                aria-label="Play: The opening of my reply"><span>2</span></button>
      </div>`;
    const button = document.getElementById('chapter-2');
    const crumb = scrubBreadcrumb(
      { category: 'ui.click', message: 'div.chapters > button#chapter-2[title="The opening of my reply"]' },
      { event: { target: button }, name: 'click' },
    );
    expect(crumb.message).toBe('body > div.chapters > button#chapter-2');
  });

  it('keeps no element description it cannot rebuild', () => {
    const crumb = scrubBreadcrumb({ category: 'ui.input', message: 'input[aria-label="New email address (current: a@b.c)"]' });
    expect(crumb.message).toBe('<unknown>');
  });
});

describe('scrubEvent', () => {
  it('drops the token from the page address, Referer and transaction', () => {
    const event = scrubEvent({
      request: {
        url: 'https://example.org/confirm-email?token=abc123',
        query_string: 'token=abc123',
        data: '{"token":"abc123"}',
        headers: {
          Referer: 'https://example.org/log?q=private#section',
          'User-Agent': 'Mozilla/5.0',
        },
      },
      transaction: '/confirm-email?token=abc123',
    });
    expect(event.request).toEqual({
      url: 'https://example.org/confirm-email',
      headers: { Referer: 'https://example.org/log', 'User-Agent': 'Mozilla/5.0' },
    });
    expect(event.transaction).toBe('/confirm-email');
  });

  it('drops the page query from stack frames of inlined scripts', () => {
    const event = scrubEvent({
      exception: {
        values: [{
          type: 'ChunkLoadError',
          stacktrace: {
            frames: [
              { filename: 'https://example.org/confirm-email?token=abc123', lineno: 1 },
              { filename: 'https://example.org/static/js/main.abc.js', lineno: 2 },
            ],
          },
        }],
      },
    });
    expect(event.exception.values[0].stacktrace.frames.map((f) => f.filename)).toEqual([
      'https://example.org/confirm-email',
      'https://example.org/static/js/main.abc.js',
    ]);
  });

  it('leaves an event without queries unchanged', () => {
    const event = { request: { url: 'https://example.org/node/42', headers: {} }, message: 'boom' };
    expect(scrubEvent(JSON.parse(JSON.stringify(event)))).toEqual(event);
  });
});

describe('withoutQuery', () => {
  it('keeps the path', () => {
    expect(withoutQuery('/api/search/semantic?q=x#y')).toBe('/api/search/semantic');
    expect(withoutQuery('/node/42')).toBe('/node/42');
  });
});

// The options wired into the real browser SDK: what reaches the transport
// when an error follows a search, a console line and a click, on a page
// opened from a link with a token.
async function sendErrorAfterActivity(options) {
  const sent = [];
  // Breadcrumbs live on the global scope, not the client: start clean.
  Sentry.getIsolationScope().clearBreadcrumbs();
  Sentry.getCurrentScope().clearBreadcrumbs();
  Sentry.init({
    dsn: 'https://public@example.org/1',
    ...options,
    transport: () => ({
      send: (envelope) => {
        sent.push(envelope);
        return Promise.resolve({});
      },
      flush: () => Promise.resolve(true),
    }),
  });
  try {
    // The search box's call, made the way axios makes it (an XHR). Nothing
    // listens on the port, so it fails, which still leaves its breadcrumb.
    await new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.addEventListener('loadend', resolve);
      xhr.open('GET', 'http://127.0.0.1:9/api/search?q=private');
      xhr.send();
    });
    console.log('[VoiceSession] TTS trigger:', { contentPreview: 'The opening of a reply' });
    document.body.innerHTML = '<button id="chapter-1" title="The opening of a reply">1</button>';
    document.getElementById('chapter-1').click();
    Sentry.captureException(new Error('boom'));
    await Sentry.flush(1000);
  } finally {
    await Sentry.close();
  }
  const events = sent
    .flatMap(([, items]) => items)
    .filter(([header]) => header.type === 'event')
    .map(([, payload]) => payload);
  expect(events).toHaveLength(1);
  return events[0];
}

describe('Sentry in the browser', () => {
  beforeAll(() => {
    Object.defineProperty(document, 'referrer', {
      value: 'https://example.org/log?q=private',
      configurable: true,
    });
    window.history.pushState({}, '', '/confirm-email?token=abc123#x');
    // Quiet output; Sentry wraps these spies, so it still sees each call
    // (jsdom reports the refused connection through console.error).
    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterAll(() => {
    jest.restoreAllMocks();
    delete document.referrer;
    window.history.pushState({}, '', '/');
  });

  it('control: with sendDefaultPii off alone, the SDK sends all of it', async () => {
    const event = await sendErrorAfterActivity({ sendDefaultPii: false });
    expect(event.request.url).toBe('http://localhost/confirm-email?token=abc123#x');
    expect(event.request.headers.Referer).toBe('https://example.org/log?q=private');
    const crumbs = event.breadcrumbs;
    expect(crumbs.find((b) => b.category === 'xhr').data.url).toBe('http://127.0.0.1:9/api/search?q=private');
    const logged = crumbs.find((b) => b.category === 'console' && b.level === 'log');
    expect(logged.data.arguments[1].contentPreview).toBe('The opening of a reply');
    expect(crumbs.find((b) => b.category === 'ui.click').message).toContain('The opening of a reply');
  });

  it('with the privacy options it sends no query, console output or element label', async () => {
    const event = await sendErrorAfterActivity(sentryPrivacyOptions());
    expect(event.request.url).toBe('http://localhost/confirm-email');
    expect(event.request.headers.Referer).toBe('https://example.org/log');
    const crumbs = event.breadcrumbs;
    expect(crumbs.find((b) => b.category === 'xhr').data.url).toBe('http://127.0.0.1:9/api/search');
    expect(crumbs.find((b) => b.category === 'ui.click').message).toBe('body > button#chapter-1');
    expect(crumbs.some((b) => b.category === 'console')).toBe(false);
    expect(JSON.stringify(event)).not.toMatch(/private|abc123|opening of a reply/);
  });
});
