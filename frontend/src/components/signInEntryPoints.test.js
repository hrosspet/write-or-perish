// Every way a signed-out person is sent to sign in carries where they were
// (#704): the protected pages, the nav's sign-in link, a public thread's "Sign in to respond" (also under its /@user/slug
// address), and a thread that is not public.
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(),
    put: jest.fn(),
    head: jest.fn(),
  },
}));
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: null, loading: false, setUser: jest.fn() }),
}));
jest.mock('../contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', toggleTheme: jest.fn() }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('./GlobalAudioPlayer', () => () => null);
jest.mock('./CraftModeDialog', () => () => null);
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import ProtectedRoute from './ProtectedRoute';
import NavBar from './NavBar';
import PublicThreadPage from '../pages/PublicThreadPage';

function Where() {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname + loc.search + loc.hash}</div>;
}

const returnUrl = () => new URLSearchParams(
  screen.getByTestId('where').textContent.split('?').slice(1).join('?'),
).get('returnUrl');

afterEach(() => {
  mockGet.mockReset();
});

test('a protected page keeps its query and its fragment', () => {
  render(
    <MemoryRouter initialEntries={['/account?tab=1#x']}>
      <Routes>
        <Route path="/account" element={<ProtectedRoute><div>account</div></ProtectedRoute>} />
        <Route path="/login" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(returnUrl()).toBe('/account?tab=1#x');
});

test.each([
  ['/@ana', '/@ana'],
  ['/@ana/a-first-entry#reply-7', '/@ana/a-first-entry#reply-7'],
  ['/node/5?awaitLlm=6', '/node/5?awaitLlm=6'],
])("the nav's sign-in link on %s comes back to it", (page, back) => {
  render(<MemoryRouter initialEntries={[page]}><NavBar /></MemoryRouter>);
  const link = screen.getByRole('link', { name: 'Login' });
  const target = new URL(link.getAttribute('href'), 'http://x.test');
  expect(target.pathname).toBe('/login');
  expect(target.searchParams.get('returnUrl')).toBe(back);
});

test("control: the nav's sign-in link on the landing page goes to the start", () => {
  render(<MemoryRouter initialEntries={['/landing']}><NavBar /></MemoryRouter>);
  expect(screen.getByRole('link', { name: 'Login' })).toHaveAttribute('href', '/login?returnUrl=%2F');
});

const PUBLIC_THREAD = {
  focus_id: 5,
  thread: {
    id: 5, node_type: 'user', username: 'ana', content: 'A shared recording.',
    created_at: '2026-11-01T10:00:00Z', children: [],
  },
};

test('"Sign in to respond" under the /@user/slug address returns to it, fragment included', async () => {
  mockGet.mockResolvedValue({ data: PUBLIC_THREAD });
  render(
    <MemoryRouter initialEntries={[{ pathname: '/@ana/a-first-entry', hash: '#reply-7' }]}>
      <Routes>
        <Route path="/login" element={<Where />} />
        <Route path="*" element={<PublicThreadPage nodeIdOverride="5" />} />
      </Routes>
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole('button', { name: 'Sign in to respond' }));
  expect(returnUrl()).toBe('/@ana/a-first-entry#reply-7');
});

test('a thread that is not public sends a signed-out visitor to sign in, on the way back to it', async () => {
  mockGet.mockRejectedValue(Object.assign(new Error('404'), { response: { status: 404 } }));
  render(
    <MemoryRouter initialEntries={[{ pathname: '/node/41', hash: '#reply-2' }]}>
      <Routes>
        <Route path="/login" element={<Where />} />
        <Route path="/node/:id" element={<PublicThreadPage />} />
      </Routes>
    </MemoryRouter>,
  );
  await screen.findByTestId('where');
  expect(returnUrl()).toBe('/node/41#reply-2');
});
