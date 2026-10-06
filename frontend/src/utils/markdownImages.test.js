import { classifyImageSource } from './markdownImages';

// jsdom serves the tests from http://localhost, which plays Loore's origin.

describe('classifyImageSource (#441)', () => {
  const savedBackend = process.env.REACT_APP_BACKEND_URL;
  afterEach(() => {
    if (savedBackend === undefined) delete process.env.REACT_APP_BACKEND_URL;
    else process.env.REACT_APP_BACKEND_URL = savedBackend;
  });

  test("Loore's own media loads, relative or absolute", () => {
    expect(classifyImageSource('/media/user/1/node/2/pic.png')).toEqual({
      kind: 'own', url: 'http://localhost/media/user/1/node/2/pic.png',
    });
    expect(classifyImageSource('http://localhost/media/a/b.png?v=3').kind).toBe('own');
  });

  test('media on the configured backend origin loads', () => {
    expect(classifyImageSource('https://loore.org/media/a.png').kind).toBe('remote');
    process.env.REACT_APP_BACKEND_URL = 'https://loore.org';
    expect(classifyImageSource('https://loore.org/media/a.png').kind).toBe('own');
  });

  test('an outside image waits for a click and names its host', () => {
    expect(classifyImageSource('https://evil.example/p.png?q=private%20text')).toEqual({
      kind: 'remote', url: 'https://evil.example/p.png?q=private%20text', host: 'evil.example',
    });
    expect(classifyImageSource('//evil.example/p.png')).toMatchObject({ kind: 'remote', host: 'evil.example' });
    expect(classifyImageSource('http://evil.example:8080/p.png')).toMatchObject({ kind: 'remote', host: 'evil.example:8080' });
  });

  test('own-origin paths outside /media/ do not load by themselves', () => {
    for (const src of ['/auth/logout', '/media/../auth/logout', '/media/%2e%2e/auth/logout',
      '/media/..%2fauth/logout', '/api/nodes/1', '/media/']) {
      expect(classifyImageSource(src)).toMatchObject({ kind: 'remote', host: 'localhost' });
    }
  });

  test('sources that are not web URLs render as alt text only', () => {
    for (const src of [undefined, null, '', 'javascript:alert(1)', 'data:image/png;base64,AAAA', 'file:///etc/hosts']) {
      expect(classifyImageSource(src)).toEqual({ kind: 'none' });
    }
  });
});
