// After signing in, back to the page where the sign-in started (#704): the
// web's check of a destination, the twin of backend/utils/destination.py.
// Both read destinationCases.json, so the two agree on every row.
import cases from './destinationCases.json';
import {
  MAX_DESTINATION_LENGTH, PENDING_MAX_AGE_MS, forgetPending, rememberArrival,
  returnUrlFrom, safeDestination, signInPath, signInPathFrom, takePending,
} from './destination';

describe('safeDestination', () => {
  test.each(cases.accepted)('keeps %j', (value) => {
    expect(safeDestination(value)).toBe(value);
  });

  test.each(cases.dropped)('drops %j', (value) => {
    expect(safeDestination(value)).toBeNull();
  });

  test('the length bound', () => {
    const longest = `/${'a'.repeat(MAX_DESTINATION_LENGTH - 1)}`;
    expect(safeDestination(longest)).toBe(longest);
    expect(safeDestination(`${longest}a`)).toBeNull();
  });

  test.each([null, undefined, 5, ['/node/1'], { a: 1 }])('drops a non-string %j', (value) => {
    expect(safeDestination(value)).toBeNull();
  });
});

describe('the way to the sign-in page', () => {
  test('carries the page, its query and its fragment', () => {
    expect(signInPath('/@ana/a-first-entry?x=1#reply-7'))
      .toBe('/login?returnUrl=%2F%40ana%2Fa-first-entry%3Fx%3D1%23reply-7');
  });

  test('drops a destination that is not a page inside Loore', () => {
    expect(signInPath('//evil.example')).toBe('/login');
    expect(signInPath('/%2F%2Fevil.example')).toBe('/login');
  });

  test("the nav's link: back to the page, from the pages that introduce Loore to the start", () => {
    expect(signInPathFrom({ pathname: '/@ana', search: '', hash: '' }))
      .toBe('/login?returnUrl=%2F%40ana');
    expect(signInPathFrom({ pathname: '/node/5', search: '?awaitLlm=6', hash: '#r' }))
      .toBe('/login?returnUrl=%2Fnode%2F5%3FawaitLlm%3D6%23r');
    expect(signInPathFrom({ pathname: '/landing', search: '', hash: '' }))
      .toBe('/login?returnUrl=%2F');
    expect(signInPathFrom({ pathname: '/login', search: '?returnUrl=%2Fx', hash: '' }))
      .toBe('/login?returnUrl=%2F');
  });

  test("the sign-in page reads returnUrl, or the server's `next`, and drops the rest", () => {
    expect(returnUrlFrom(new URLSearchParams('returnUrl=%2Fnode%2F5'))).toBe('/node/5');
    expect(returnUrlFrom(new URLSearchParams('next=%2F%40ana'))).toBe('/@ana');
    expect(returnUrlFrom(new URLSearchParams('returnUrl=%2F%2Fevil.example'))).toBe('/dashboard');
    expect(returnUrlFrom(new URLSearchParams('returnUrl=https%3Aevil.example&next=%2Fnode%2F5')))
      .toBe('/node/5');
    expect(returnUrlFrom(new URLSearchParams(''))).toBe('/dashboard');
  });
});

describe('the arrival kept across a step that leaves the page', () => {
  beforeEach(() => forgetPending());

  test('an earlier page load\'s arrival is opened; one of this load is where the person is', () => {
    rememberArrival('/node/5#r', { load: 'A', now: 1000 });
    expect(takePending({ load: 'B', now: 2000 })).toEqual({ path: '/node/5#r', earlierLoad: true });
    expect(takePending({ load: 'B', now: 2000 })).toBeNull(); // taken once

    rememberArrival('/node/5', { load: 'B', now: 1000 });
    expect(takePending({ load: 'B', now: 2000 })).toEqual({ path: '/node/5', earlierLoad: false });
  });

  test('it waits an hour, no longer', () => {
    rememberArrival('/node/5', { load: 'A', now: 0 });
    expect(takePending({ load: 'B', now: PENDING_MAX_AGE_MS })).not.toBeNull();
    rememberArrival('/node/5', { load: 'A', now: 0 });
    expect(takePending({ load: 'B', now: PENDING_MAX_AGE_MS + 1 })).toBeNull();
  });

  test('the start, step and Account pages never replace it (a checkout comes back to /account)', () => {
    rememberArrival('/node/5', { load: 'A', now: 0 });
    ['/', '/account?subscription=success#subscription', '/welcome', '/alpha-thank-you', '/login']
      .forEach((page) => rememberArrival(page, { load: 'B', now: 10 }));
    expect(takePending({ load: 'C', now: 20 })).toEqual({ path: '/node/5', earlierLoad: true });
  });

  test('a newer arrival on another entry replaces it', () => {
    rememberArrival('/node/5', { load: 'A', now: 0 });
    rememberArrival('/@ana/a-first-entry', { load: 'B', now: 10 });
    expect(takePending({ load: 'C', now: 20 }).path).toBe('/@ana/a-first-entry');
  });

  test('a hostile value is never kept, nor read back', () => {
    rememberArrival('//evil.example', { load: 'A', now: 0 });
    expect(takePending({ load: 'B', now: 1 })).toBeNull();
    window.sessionStorage.setItem('loore:pendingDestination',
      JSON.stringify({ path: '/%2F%2Fevil.example', load: 'A', at: 0 }));
    expect(takePending({ load: 'B', now: 1 })).toBeNull();
  });
});
