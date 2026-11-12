// A prompt shared in a circle is shown to a reader with the READER's own
// artifacts in its placeholders (the server sends only those). Where the
// reader has no such artifact the placeholder is shown as written.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./InlineQuoteBubble', () => () => null);
jest.mock('../api', () => ({ post: jest.fn(), delete: jest.fn() }));
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({ user: { id: 2 } }) }));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));

import React from 'react';
import { render as rtlRender, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QuotedContent from './QuotedContent';

const render = (ui) => rtlRender(<MemoryRouter>{ui}</MemoryRouter>);

const PROMPT = 'Read {user_profile} and then use {user_memory} to answer.';

test('a reader sees their own profile embedded and a bare placeholder for a missing artifact', () => {
  render(<QuotedContent
    content={PROMPT}
    quotes={{}} externalQuotes={{}}
    contextArtifacts={{ profile: { id: 3, content: "ben's own profile text" } }}
    barePlaceholders
  />);
  // The profile is a section of its own, without a version number.
  const header = screen.getByText('User Profile');
  fireEvent.click(header);
  expect(screen.getByText("ben's own profile text")).toBeInTheDocument();
  // The memory the reader does not have: the placeholder as written,
  // inside its sentence.
  expect(screen.getByText('and then use {user_memory} to answer.')).toBeInTheDocument();
  expect(screen.queryByText(/not available/)).toBeNull();
});

test('a reader with none of the artifacts sees the prompt as written', () => {
  render(<QuotedContent
    content={PROMPT} quotes={{}} externalQuotes={{}}
    contextArtifacts={{}} barePlaceholders
  />);
  expect(screen.getByText(PROMPT)).toBeInTheDocument();
  expect(screen.queryByText(/not available/)).toBeNull();
});

test('the owner keeps the "not available" mark for a missing artifact', () => {
  render(<QuotedContent
    content={PROMPT} quotes={{}} externalQuotes={{}}
    contextArtifacts={{ profile: { id: 1, version_number: 7, content: 'p' } }}
  />);
  expect(screen.getByText('User Profile v7')).toBeInTheDocument();
  expect(screen.getByText('[Memory not available]')).toBeInTheDocument();
});
