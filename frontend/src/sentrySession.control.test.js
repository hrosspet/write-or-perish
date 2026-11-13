import * as Sentry from '@sentry/react';
import { sentSessions } from './sentrySessionHarness';

// The controls for sentrySession.test.js (#666), in a window of their own:
// the SDK's default integrations, as before the privacy options left
// BrowserSession out. In this order: the first init sets the integration
// up, and the second still has its route-change handler.

describe('Session records with the default integrations', () => {
  it('without a release, as the build is today, the SDK sends none', async () => {
    expect(await sentSessions(Sentry, { sendDefaultPii: false })).toBe(0);
  });

  it('with a release set, they are sent', async () => {
    expect(await sentSessions(Sentry, {
      sendDefaultPii: false, release: 'test@1' })).toBeGreaterThan(0);
  });
});
