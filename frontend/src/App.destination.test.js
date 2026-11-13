// A new account keeps its steps, and the page its sign-in started on is
// opened after them (#704): the terms first (over the page), then whatever
// comes first for the account. Waiting for approval, it cannot open the
// page at all: the waiting screen, and the page is dropped without an
// error. A step that leaves Loore and comes back elsewhere (with payments
// on, the subscribe page's checkout returns to the Account page) is followed
// by the page, once the app itself shows.
let mockUser = null;
jest.mock('./contexts/UserContext', () => ({
  useUser: () => ({
    user: mockUser, loading: false, setUser: () => {}, markHasOwnEntries: () => {},
  }),
}));
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
jest.mock('./api', () => ({
  __esModule: true,
  // Plain functions: the test setup resets jest.fn implementations.
  default: { get: () => new Promise(() => {}), post: () => new Promise(() => {}) },
}));
jest.mock('./components/MarkdownBody', () => () => null);
jest.mock('./components/NodeDetailWrapper', () => () => <div>thread-page</div>);
jest.mock('./pages/AlphaThankYouPage', () => () => <div>waiting-page</div>);
jest.mock('./pages/AccountPage', () => () => <div>account-page</div>);
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

import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
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
});

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
  window.sessionStorage.setItem(KEY, JSON.stringify({
    path: '/@ana/a-first-entry#reply-7', load: 'the-load-before-checkout', at: Date.now(),
  }));
  mockUser = { id: 9, approved: true, terms_up_to_date: true };
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
