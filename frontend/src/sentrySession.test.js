import * as Sentry from '@sentry/react';
import { sentryPrivacyOptions } from './sentryPrivacy';
import { sentSessions } from './sentrySessionHarness';

// #666: the browser SDK's default BrowserSession integration sends a
// session record when the page loads and on each route change; the request
// carries the visitor's IP address. The SDK sets an integration up once
// per page and keeps its state on the window, so this file never
// initialises it with the default integrations: the controls are in
// sentrySession.control.test.js, which jest runs in a window of its own.

describe('Session records with the privacy options', () => {
  it('none is sent on load or on a route change, even with a release', async () => {
    expect(await sentSessions(Sentry, {
      ...sentryPrivacyOptions(), release: 'test@1' })).toBe(0);
  });

  it('every other default integration is kept', () => {
    const defaults = Sentry.getDefaultIntegrations({});
    const names = sentryPrivacyOptions().integrations(defaults).map((i) => i.name);
    expect(names).not.toContain('BrowserSession');
    expect(names).toEqual(expect.arrayContaining(
      defaults.map((i) => i.name).filter((n) => n !== 'BrowserSession')));
  });
});
