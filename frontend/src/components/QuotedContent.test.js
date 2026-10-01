// MarkdownBody pulls in react-markdown (ESM, untransformed by CRA jest);
// the text around the quotes is not what is under test here.
jest.mock('./MarkdownBody', () => ({ children }) => <div>{children}</div>);
jest.mock('./InlineQuoteBubble', () => () => null);
jest.mock('./InlineArtifactSection', () => () => null);
jest.mock('../api', () => ({ post: jest.fn(), delete: jest.fn() }));
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({ user: { id: 7 } }) }));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));

import React from 'react';
import { render, screen } from '@testing-library/react';
import QuotedContent from './QuotedContent';

const externalQuotes = {
  42: {
    id: 42,
    content: 'Singing was the original psychedelic.',
    source: 'twitter_bookmark',
    author_handle: 'TVachaW',
    url: 'https://x.com/TVachaW/status/1',
    user_id: 7,
    read_at: null,
  },
};

test('an LLM reply passes the verdict down to its reference quotes', () => {
  render(<QuotedContent
    content={'Try this one:\n{quote_ext:42}'}
    externalQuotes={externalQuotes}
    nodeId={5}
    showRecommendationFeedback
  />);
  expect(screen.getByRole('button', { name: 'Good quote' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Mark as read' })).toBeInTheDocument();
});

test('a user-authored node\'s reference quotes get only the read toggle (#363)', () => {
  render(<QuotedContent
    content={'I keep coming back to\n{quote_ext:42}'}
    externalQuotes={externalQuotes}
    nodeId={5}
  />);
  expect(screen.queryByRole('button', { name: 'Good quote' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Mark as read' })).toBeInTheDocument();
});
