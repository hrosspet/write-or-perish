// #391: until the user has written an entry in Loore, the homepage and the
// Text screen ask the welcome page's question instead of "What's on your
// mind?". (Voice is covered in VoicePage.test.js.)
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn(), removeToast: jest.fn() }),
}));
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(), post: jest.fn() },
}));
let mockNodeFormProps;
jest.mock('../components/NodeForm', () => ({
  __esModule: true,
  default: (props) => {
    mockNodeFormProps = props;
    return null;
  },
}));
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import HomePage from './HomePage';
import WritePage from './WritePage';
import { WELCOME_QUESTION, EVERYDAY_QUESTION } from '../utils/entryPrompt';

const markHasOwnEntries = jest.fn();

const renderPage = (Page, user) => {
  mockUserCtx = { user: { username: 'newbie', ...user }, markHasOwnEntries };
  return render(<MemoryRouter><Page /></MemoryRouter>);
};

beforeAll(() => {
  // HomePage fades its heading in through IntersectionObserver.
  window.IntersectionObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

beforeEach(() => {
  markHasOwnEntries.mockReset();
  mockNavigate.mockReset();
  mockNodeFormProps = null;
});

describe.each([
  ['homepage', HomePage],
  ['Text screen', WritePage],
])('the %s', (_name, Page) => {
  test('asks a newcomer the welcome question', () => {
    renderPage(Page, { has_own_entries: false });
    expect(screen.getByRole('heading', { level: 1 }))
      .toHaveTextContent(WELCOME_QUESTION);
    expect(screen.queryByText(EVERYDAY_QUESTION)).not.toBeInTheDocument();
  });

  test('asks "What\'s on your mind?" once they have written', () => {
    renderPage(Page, { has_own_entries: true });
    expect(screen.getByRole('heading', { level: 1 }))
      .toHaveTextContent(EVERYDAY_QUESTION);
    expect(screen.queryByText(WELCOME_QUESTION)).not.toBeInTheDocument();
  });
});

test('an entry saved on the Text screen ends the welcome question', () => {
  renderPage(WritePage, { has_own_entries: false });
  mockNodeFormProps.onSuccess({ user_node_id: 11, llm_node_id: 12 });
  expect(markHasOwnEntries).toHaveBeenCalledTimes(1);
  expect(mockNavigate).toHaveBeenCalledWith('/node/11?awaitLlm=12');
});
