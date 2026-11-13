// Test helper for sentrySession*.test.js (#666): how many session items
// reach the transport for a page load and one route change, with Sentry
// initialised with *options*.
export async function sentSessions(Sentry, options) {
  const sent = [];
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
    window.history.pushState({}, '', `/another-page-${Math.random()}`);
    await Sentry.flush(1000);
  } finally {
    await Sentry.close();
    window.history.pushState({}, '', '/');
  }
  return sent.flatMap(([, items]) => items)
    .filter(([header]) => header.type === 'session').length;
}
