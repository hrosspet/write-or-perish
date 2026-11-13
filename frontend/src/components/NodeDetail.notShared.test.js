// An entry the signed-in person may not open (#704): made private since,
// never shared with them, or deleted. The server answers all of these, and
// an id that never existed, with the same 404 (never revealing which ids
// exist), so the page says the same for all of them: plainly that the entry
// is not shared with them, not an error that looks like a broken link, and
// the way on to their own Home page.
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: jest.fn(() => Promise.resolve({ data: {} })),
    put: jest.fn(() => Promise.resolve({ data: {} })),
    delete: jest.fn(() => Promise.resolve({ data: {} })),
  },
}));

jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 2, username: 'bea', approved: true }, loading: false }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: () => {} }),
}));
jest.mock('../hooks/useSSE', () => ({
  useLlmTextStream: () => ({ text: '', done: false }),
}));
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import NodeDetailWrapper from './NodeDetailWrapper';
import PublicThreadPage from '../pages/PublicThreadPage';

const TITLE = "This entry isn't shared with you";
const TEXT = 'It may be private, or it may have been deleted.';

const notFound = () => Promise.reject(Object.assign(new Error('404'), {
  response: { status: 404, data: { error: 'Node not found' } },
}));

beforeEach(() => {
  mockGet.mockReset();
  jest.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  console.error.mockRestore();
});

const renderThread = (path) => render(
  <MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/node/:id" element={<NodeDetailWrapper />} />
      <Route path="/" element={<div>home-page</div>} />
    </Routes>
  </MemoryRouter>,
);

test.each([403, 404])('the thread page says it plainly (%i), with the way Home', async (status) => {
  mockGet.mockImplementation(() => Promise.reject(Object.assign(new Error(String(status)), {
    response: { status, data: { error: 'Node not found' } },
  })));
  renderThread('/node/41');

  expect(await screen.findByText(TITLE)).toBeInTheDocument();
  expect(screen.getByText(TEXT)).toBeInTheDocument();
  expect(screen.queryByText(/doesn't exist/)).toBeNull();
  const home = screen.getByRole('link', { name: /go to your home page/i });
  expect(home).toHaveAttribute('href', '/');
  fireEvent.click(home);
  expect(screen.getByText('home-page')).toBeInTheDocument();
});

test('control: another failure keeps its own message', async () => {
  mockGet.mockImplementation(() => Promise.reject(Object.assign(new Error('500'), {
    response: { status: 500 },
  })));
  renderThread('/node/41');
  expect(await screen.findByText('Error fetching node details.')).toBeInTheDocument();
  expect(screen.queryByText(TITLE)).toBeNull();
});

test('a permalink that does not resolve, signed in: the same words', async () => {
  mockGet.mockImplementation(notFound);
  render(
    <MemoryRouter initialEntries={['/@ana/a-first-entry']}>
      <Routes>
        <Route path="*" element={<PublicThreadPage nodeIdOverride="missing" />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByText(TITLE)).toBeInTheDocument();
  expect(screen.queryByText('Nothing here.')).toBeNull();
  expect(screen.getByRole('link', { name: /go to your home page/i })).toHaveAttribute('href', '/');
});
