// A new account keeps its steps, and the page its sign-in started on is
// opened after them (#704): the terms first (over the page), then whatever
// comes first for the account. Waiting for approval, it cannot open the
// page at all: the waiting screen, and the page is dropped without an
// error. A step that leaves Loore and comes back elsewhere (with payments
// on, the subscribe page's checkout returns to the Account page) is followed
// by the page, once the app itself shows.

// The signed-in account, as UserContext keeps it: setUser re-renders
// whoever reads it (the subscription section lets an account in that way).
let mockUser = null;
const mockUserReaders = new Set();
jest.mock('./contexts/UserContext', () => {
  const { useSyncExternalStore } = jest.requireActual('react');
  const subscribe = (reader) => {
    mockUserReaders.add(reader);
    return () => mockUserReaders.delete(reader);
  };
  const setUser = (next) => {
    mockUser = typeof next === 'function' ? next(mockUser) : next;
    mockUserReaders.forEach((reader) => reader());
  };
  return {
    useUser: () => ({
      user: useSyncExternalStore(subscribe, () => mockUser),
      loading: false, setUser, markHasOwnEntries: () => {},
    }),
  };
});
jest.mock('./contexts/AudioContext', () => ({
  AudioProvider: ({ children }) => children,
  useAudio: () => ({}),
}));
jest.mock('./hooks/useSessionUpdates', () => ({
  ...jest.requireActual('./hooks/useSessionUpdates'),
  useSessionUpdates: () => [null, () => {}],
}));
jest.mock('./hooks/useBothProvidersQuestion', () => ({
  useBothProvidersQuestion: () => [false, () => {}],
}));
jest.mock('./hooks/useNodePrefetch', () => () => ({ pending: null, openNode: () => {}, cancel: () => {} }));
// What Stripe's state looks like to /billing/sync, back from a checkout;
// null: no answer yet. Every other request is still waiting.
let mockSyncAnswer = null;
jest.mock('./api', () => ({
  __esModule: true,
  // Plain functions: the test setup resets jest.fn implementations.
  default: {
    get: () => new Promise(() => {}),
    post: (path) => (path === '/billing/sync' && mockSyncAnswer
      ? Promise.resolve({ data: mockSyncAnswer })
      : new Promise(() => {})),
  },
}));
jest.mock('./components/MarkdownBody', () => () => null);
jest.mock('./components/NodeDetailWrapper', () => () => <div>thread-page</div>);
jest.mock('./pages/AlphaThankYouPage', () => () => <div>waiting-page</div>);
// The subscribe page in miniature, around the real subscription section:
// back from a checkout, the section drops the checkout's query and lets the
// account in once /billing/sync answers.
jest.mock('./pages/SubscribePage', () => function SubscribePage() {
  const { useUser } = jest.requireMock('./contexts/UserContext');
  const SubscriptionSection = jest.requireActual('./components/SubscriptionSection').default;
  const { user, setUser } = useUser();
  return <div>subscribe-page<SubscriptionSection user={user} setUser={setUser} /></div>;
});
// The Account page is the real one: with payments on, its subscription
// section drops a checkout's query when the page opens.
jest.mock('./pages/HomePage', () => () => <div>home-page</div>);
jest.mock('./components/TermsModal', () => ({ onAccepted }) => (
  <button type="button" onClick={onAccepted}>accept-terms</button>
));
jest.mock('./components/NavBar', () => () => null);
jest.mock('./components/SpendCapBanner', () => () => null);
jest.mock('./components/DataDeletionBanner', () => () => null);
jest.mock('./components/UpdatesModal', () => () => null);
jest.mock('./components/SearchModal', () => () => null);
jest.mock('./components/NodeFormModal', () => () => null);
jest.mock('./components/ProfileGenerationWatcher', () => () => null);

import React, { useEffect, useRef } from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import App from './App';
import { PAGE_LOAD } from './utils/destination';

const KEY = 'loore:pendingDestination';

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname + loc.search + loc.hash}</div>;
}

const renderAt = (path) => render(
  <MemoryRouter initialEntries={[path]}>
    <App />
    <Where />
  </MemoryRouter>,
);
const where = () => screen.getByTestId('where').textContent;
const kept = () => JSON.parse(window.sessionStorage.getItem(KEY) || 'null');

beforeEach(() => {
  window.sessionStorage.clear();
  mockSyncAnswer = null;
  // The Account page scrolls to its #subscription; jsdom has no layout.
  Element.prototype.scrollIntoView = () => {};
});

// A page kept by the load before the checkout (Stripe's page in between).
const keepFromEarlierLoad = (path) => window.sessionStorage.setItem(KEY, JSON.stringify({
  path, load: 'the-load-before-checkout', at: Date.now(),
}));
const CHECKOUT_RETURN = '/account?subscription=success&session_id=cs_test_1#subscription';

test('a new account waiting for approval: the terms, then the waiting screen; the entry is dropped', () => {
  mockUser = { id: 9, approved: false, terms_up_to_date: false };
  renderAt('/@ana/a-first-entry#reply-7');

  expect(where()).toBe('/alpha-thank-you');
  expect(screen.getByText('waiting-page')).toBeInTheDocument();
  expect(screen.getByText('accept-terms')).toBeInTheDocument();
  expect(screen.queryByText('thread-page')).toBeNull();
  expect(kept()).toBeNull();

  fireEvent.click(screen.getByText('accept-terms'));
  expect(where()).toBe('/alpha-thank-you');
  expect(screen.getByText('waiting-page')).toBeInTheDocument();
});

test('the same for /node/<id>: no error page for a waiting account', () => {
  mockUser = { id: 9, approved: false, terms_up_to_date: true };
  renderAt('/node/5');
  expect(where()).toBe('/alpha-thank-you');
  expect(screen.queryByText('thread-page')).toBeNull();
});

test('control: an account let in, new terms: the terms over the entry, then the entry', () => {
  mockUser = { id: 9, approved: true, terms_up_to_date: false };
  renderAt('/node/5#reply-2');
  expect(screen.getByText('thread-page')).toBeInTheDocument();
  fireEvent.click(screen.getByText('accept-terms'));
  expect(where()).toBe('/node/5#reply-2');
  expect(screen.getByText('thread-page')).toBeInTheDocument();
  expect(kept()).toBeNull(); // arrived and there: nothing waits
});

test('a step that left Loore: back on the Account page, the entry opens', () => {
  keepFromEarlierLoad('/@ana/a-first-entry#reply-7');
  mockUser = {
    id: 9, approved: true, terms_up_to_date: true,
    payments_enabled: true, subscription: { state: 'active' },
  };
  renderAt('/account?subscription=success#subscription');

  expect(where()).toBe('/@ana/a-first-entry#reply-7');
  expect(kept()).toBeNull();
});

test('control: a kept entry an hour old is not opened', () => {
  window.sessionStorage.setItem(KEY, JSON.stringify({
    path: '/node/5', load: 'the-load-before-checkout', at: Date.now() - 61 * 60 * 1000,
  }));
  mockUser = { id: 9, approved: true, terms_up_to_date: true };
  renderAt('/account');
  expect(where()).toBe('/account');
});

test('waiting for approval: a kept entry is dropped, not opened', () => {
  window.sessionStorage.setItem(KEY, JSON.stringify({
    path: '/node/5', load: 'an-earlier-load', at: Date.now(),
  }));
  mockUser = { id: 9, approved: false, terms_up_to_date: true };
  renderAt('/alpha-thank-you');
  expect(where()).toBe('/alpha-thank-you');
  expect(kept()).toBeNull();
});

test('signed out: a kept entry is dropped', () => {
  window.sessionStorage.setItem(KEY, JSON.stringify({
    path: '/node/5', load: 'an-earlier-load', at: Date.now(),
  }));
  mockUser = null;
  renderAt('/node/6');
  expect(where()).toBe('/node/6');
  expect(kept()).toBeNull();
});

// With payments on, an account without a subscription sees only the
// subscribe page, which App renders in place of the app (the payments
// train). Its shape, in miniature: the arrival is kept above the early
// return, and taken only where the app itself renders.
describe('a step page shown in place of the app', () => {
  // eslint-disable-next-line global-require
  const { default: PendingDestination, useRememberArrival } = require('./components/PendingDestination');

  function Shell() {
    useRememberArrival();
    if (mockUser && mockUser.held) return <div>step-page</div>;
    return <PendingDestination />;
  }

  test('the page it arrived on is kept for after the step', () => {
    mockUser = { id: 9, approved: true, held: true };
    render(<MemoryRouter initialEntries={['/node/5?x=1#r']}><Shell /><Where /></MemoryRouter>);
    expect(screen.getByText('step-page')).toBeInTheDocument();
    expect(kept()).toEqual({ path: '/node/5?x=1#r', load: PAGE_LOAD, at: expect.any(Number) });
  });

  test('control: a step done without leaving the page: the person is there already', () => {
    mockUser = { id: 9, approved: true, held: true };
    const view = render(<MemoryRouter initialEntries={['/node/5']}><Shell /><Where /></MemoryRouter>);
    mockUser = { id: 9, approved: true, held: false };
    act(() => {
      view.rerender(<MemoryRouter initialEntries={['/node/5']}><Shell /><Where /></MemoryRouter>);
    });
    expect(where()).toBe('/node/5');
    expect(kept()).toBeNull();
  });
});

// A page that sends the person on as it opens (the Account page drops a
// Connect X answer's ?x_login= that way) runs its effect after
// <PendingDestination />'s, so its navigation replaces the one to the kept
// page: the kept page is forgotten only once the person is on it.
describe('the kept page is forgotten once the person is there', () => {
  // eslint-disable-next-line global-require
  const { default: PendingDestination } = require('./components/PendingDestination');

  // Sends the person on at its first `times` addresses.
  function SendsOn({ times }) {
    const navigate = useNavigate();
    const { key } = useLocation();
    const sent = useRef(0);
    useEffect(() => {
      if (sent.current >= times) return;
      sent.current += 1;
      navigate(`/elsewhere-${sent.current}`, { replace: true });
    }, [key, navigate, times]);
    return null;
  }

  const renderWith = (times) => render(
    <MemoryRouter initialEntries={['/account']}>
      <PendingDestination />
      <SendsOn times={times} />
      <Where />
    </MemoryRouter>,
  );

  test('replaced by another navigation of the same moment: opened again', () => {
    keepFromEarlierLoad('/node/5#reply-2');
    mockUser = { id: 9, approved: true };
    renderWith(1);
    expect(where()).toBe('/node/5#reply-2');
    expect(kept()).toBeNull();
  });

  test('replaced again: the second try forgets it, so nothing can loop', () => {
    keepFromEarlierLoad('/node/5#reply-2');
    mockUser = { id: 9, approved: true };
    renderWith(2);
    expect(where()).toBe('/elsewhere-2');
    expect(kept()).toBeNull();
  });

  test('control: nothing else navigates: opened at once', () => {
    keepFromEarlierLoad('/node/5#reply-2');
    mockUser = { id: 9, approved: true };
    renderWith(0);
    expect(where()).toBe('/node/5#reply-2');
    expect(kept()).toBeNull();
  });
});

// The real App, payments on (#700): a new account sees the terms, then the
// subscribe page, at the entry's address; the entry opens only after the
// checkout's return, never in place of a step.
describe('payments on: terms, then the subscribe page, then the entry', () => {
  const onNoSub = (over = {}) => ({
    id: 9, approved: true, payments_enabled: true,
    subscription: { state: 'none' }, terms_up_to_date: false, ...over,
  });

  test('the steps show at the entry address, and it is kept', () => {
    mockUser = onNoSub();
    renderAt('/@ana/a-first-entry?x=1#reply-7');
    expect(screen.getByText('accept-terms')).toBeInTheDocument();
    expect(screen.getByText('subscribe-page')).toBeInTheDocument();
    expect(screen.queryByText('thread-page')).toBeNull();
    expect(kept()).toEqual({
      path: '/@ana/a-first-entry?x=1#reply-7', load: PAGE_LOAD, at: expect.any(Number),
    });
    fireEvent.click(screen.getByText('accept-terms'));
    expect(screen.getByText('subscribe-page')).toBeInTheDocument();
    expect(screen.queryByText('thread-page')).toBeNull();
  });

  test('the checkout returns to the Account page: the entry opens', () => {
    keepFromEarlierLoad('/node/5#reply-2');
    mockUser = onNoSub({ terms_up_to_date: true, subscription: { state: 'active' } });
    renderAt('/account?subscription=success');
    expect(where()).toBe('/node/5#reply-2');
    expect(kept()).toBeNull();
  });

  // The two orders of Stripe's webhook and the return page. The Account
  // page's subscription section drops the checkout's query when it opens,
  // a navigation of its own: the entry opens after it, not under it.
  test('let in before the return page loads: the query is dropped, then the entry opens', () => {
    keepFromEarlierLoad('/node/5#reply-2');
    mockUser = onNoSub({ terms_up_to_date: true, subscription: { state: 'trialing' } });
    renderAt(CHECKOUT_RETURN);
    expect(where()).toBe('/node/5#reply-2');
    expect(screen.getByText('thread-page')).toBeInTheDocument();
    expect(kept()).toBeNull();
  });

  test('let in by the sync after the return page loads: then the entry opens', async () => {
    keepFromEarlierLoad('/node/5#reply-2');
    mockUser = onNoSub({ terms_up_to_date: true });
    mockSyncAnswer = { subscription: { state: 'trialing', status: 'trialing' } };
    renderAt(CHECKOUT_RETURN);
    // Not let in yet: the subscribe page, which drops the checkout's query.
    expect(screen.getByText('subscribe-page')).toBeInTheDocument();
    expect(where()).toBe('/account#subscription');
    expect(kept()).not.toBeNull();

    await act(async () => {}); // /billing/sync answers: let in
    expect(where()).toBe('/node/5#reply-2');
    expect(screen.getByText('thread-page')).toBeInTheDocument();
    expect(kept()).toBeNull();
  });

  test('back from a cancelled checkout, the same', () => {
    keepFromEarlierLoad('/node/5#reply-2');
    mockUser = onNoSub({ terms_up_to_date: true, subscription: { state: 'active' } });
    renderAt('/account?subscription=cancelled#subscription');
    expect(where()).toBe('/node/5#reply-2');
    expect(kept()).toBeNull();
  });

  test('control: payments off, a new account: the terms over the entry, no subscribe page', () => {
    mockUser = { id: 9, approved: true, terms_up_to_date: false };
    renderAt('/node/5');
    expect(screen.getByText('thread-page')).toBeInTheDocument();
    expect(screen.queryByText('subscribe-page')).toBeNull();
  });
});
