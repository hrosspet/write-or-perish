// "Start from your tweets" in Account settings (#662): the card
// (PrefillConsentCard) says the answer can be changed in Settings. The
// row shows where the question applies, shows the current answer (none
// selected until there is one), saves a change both ways, says when a
// change did not save, and says what each answer does.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    post: jest.fn(),
    delete: jest.fn(),
    put: (...args) => mockPut(...args),
    get: jest.fn(() => new Promise(() => {})),
  },
}));
jest.mock('../components/ModelSelector', () => () => null);

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountPage from './AccountPage';
import PrefillConsentCard from '../components/PrefillConsentCard';

const baseUser = {
  username: 'ana', email: null, twitter_login: true, twitter_handle: 'ana_x',
  pending_email: null, pending_email_expired: false, plan: 'alpha',
  prefill_consent: null, prefilled_handle: null,
};

const renderPage = (user) => {
  mockUserCtx = { user: { ...baseUser, ...user }, setUser: jest.fn() };
  return render(<MemoryRouter><AccountPage /></MemoryRouter>);
};

const row = () => screen.queryByRole('combobox', { name: 'Start from your tweets' });

beforeEach(() => {
  mockPut.mockReset();
});

test('an email account Loore never pre-filled has no such row', () => {
  renderPage({ twitter_login: false, email: 'ana@example.com' });
  expect(row()).toBeNull();
});

test.each([
  ['an X sign-in', {}],
  ['an account Loore pre-filled', { twitter_login: false, prefilled_handle: 'ana_tw' }],
  ['an account that answered', { twitter_login: false, prefill_consent: 'no' }],
])('%s has the row', (_label, user) => {
  renderPage(user);
  expect(row()).not.toBeNull();
});

test('before an answer nothing is selected, and the text says what the wait means', () => {
  renderPage({});
  expect(row()).toHaveValue('');
  expect(within(row()).getByRole('option', { name: 'Not answered yet' })).toBeDisabled();
  expect(screen.getByText(/Until you answer, Loore may still bring them in\./)).toBeInTheDocument();
});

test.each(['yes', 'no'])('the answer %s shows, and cannot be unanswered', (answer) => {
  renderPage({ prefill_consent: answer });
  expect(row()).toHaveValue(answer);
  expect(within(row()).queryByRole('option', { name: 'Not answered yet' })).toBeNull();
  expect(screen.queryByText(/Until you answer/)).toBeNull();
});

test('the text says what each answer does and what stays', () => {
  renderPage({ prefill_consent: 'yes', prefilled_handle: 'ana_tw' });
  const help = screen.getByText(/With Yes, Loore brings in your public tweets as @ana_tw/);
  expect(help).toHaveTextContent(/With No, it brings in no more of them/);
  expect(help).toHaveTextContent(
    /What it already made stays: those tweets, your profile and your intentions, each yours to edit or delete\./);
});

test.each([['yes', 'no'], ['no', 'yes']])('changing %s to %s saves it', async (from, to) => {
  const updated = { ...baseUser, prefill_consent: to };
  mockPut.mockResolvedValue({ data: { user: updated } });
  renderPage({ prefill_consent: from });
  fireEvent.change(row(), { target: { value: to } });
  expect(mockPut).toHaveBeenCalledWith('/dashboard/user', { prefill_consent: to });
  await waitFor(() => expect(mockUserCtx.setUser).toHaveBeenCalledWith(updated));
});

test('a no that did not save says so and the yes stays shown', async () => {
  mockPut.mockRejectedValue(new Error('offline'));
  renderPage({ prefill_consent: 'yes' });
  fireEvent.change(row(), { target: { value: 'no' } });
  expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't save your answer. Please try again.");
  expect(row()).toHaveValue('yes');
  expect(mockUserCtx.setUser).not.toHaveBeenCalled();
});

describe('the card', () => {
  const answerNo = async (user) => {
    mockPut.mockResolvedValue({ data: { user: { ...user, prefill_consent: 'no' } } });
    mockUserCtx = { user, setUser: jest.fn() };
    render(<PrefillConsentCard />);
    expect(screen.getByText('You can change your mind later in Settings.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
    await screen.findByText(/^Noted\./);
  };

  test('a no on an account Loore already pre-filled does not promise a blank page', async () => {
    await answerNo({ ...baseUser, prefilled_handle: 'ana_tw' });
    expect(screen.queryByText(/blank page/)).toBeNull();
    expect(screen.getByText(/What it already made stays, yours to edit or delete\./)).toBeInTheDocument();
  });

  test('a no before any pre-fill keeps its line', async () => {
    await answerNo(baseUser);
    expect(screen.getByText(/You'll start from a blank page/)).toBeInTheDocument();
  });
});
