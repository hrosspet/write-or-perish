// The Background tasks setting (#599): the user's answer to the
// both-providers question, next to the default model. No option is
// selected until the user answers; changing it saves the answer. A
// changed default model asks the question while it was never answered.
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
let mockPickModel;
jest.mock('../components/ModelSelector', () => (props) => {
  mockPickModel = props.onModelChange;
  return null;
});

import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AccountPage from './AccountPage';

const baseUser = {
  username: 'ana', email: 'ana@example.com', twitter_login: false,
  pending_email: null, pending_email_expired: false, plan: 'alpha',
  profile_provider: 'anthropic',
};

const renderPage = (user) => {
  mockUserCtx = { user: { ...baseUser, ...user }, setUser: jest.fn() };
  return render(<MemoryRouter><AccountPage /></MemoryRouter>);
};

const setting = () => screen.getByRole('combobox', { name: 'Background tasks' });

beforeEach(() => {
  mockPut.mockReset();
  window.sessionStorage.clear();
});

test('before an answer nothing is selected', () => {
  renderPage({ profile_both_providers: null });
  expect(setting()).toHaveValue('');
  expect(screen.getByRole('option', { name: 'Not answered yet' })).toBeDisabled();
  expect(screen.getByRole('option', { name: 'Anthropic only' })).toBeInTheDocument();
});

test.each([[true, 'both'], [false, 'one']])(
  'the answer %s shows as %s', (answer, value) => {
    renderPage({ profile_both_providers: answer });
    expect(setting()).toHaveValue(value);
    expect(screen.queryByRole('option', { name: 'Not answered yet' })).toBeNull();
  });

test('an OpenAI user is offered OpenAI only', () => {
  renderPage({ profile_both_providers: false, profile_provider: 'openai' });
  expect(screen.getByRole('option', { name: 'OpenAI only' })).toBeInTheDocument();
});

test('changing it saves the answer', async () => {
  const updated = { ...baseUser, profile_both_providers: false };
  mockPut.mockResolvedValue({ data: { user: updated } });
  renderPage({ profile_both_providers: true });
  fireEvent.change(setting(), { target: { value: 'one' } });
  expect(mockPut).toHaveBeenCalledWith('/dashboard/user', { profile_both_providers: false });
  await waitFor(() => expect(mockUserCtx.setUser).toHaveBeenCalledWith(updated));
});

test('a changed default model asks while never answered', async () => {
  mockPut.mockResolvedValue({ data: { user: {
    ...baseUser, preferred_model: 'gpt-6-astra', profile_provider: 'openai',
    profile_both_providers: null,
    ask_both_providers: { model: true, visit: true, import: true },
  } } });
  renderPage({ profile_both_providers: null });
  expect(screen.queryByRole('dialog')).toBeNull();
  await act(async () => { await mockPickModel('gpt-6-astra', { picked: true }); });
  expect(screen.getByRole('dialog')).toBeInTheDocument();
});

test('the default the picker fills in when the page opens does not ask', async () => {
  mockPut.mockResolvedValue({ data: { user: {
    ...baseUser, preferred_model: 'claude-opus-5.5', profile_both_providers: null,
    ask_both_providers: { model: true, visit: false, import: true },
  } } });
  renderPage({ profile_both_providers: null });
  await act(async () => { await mockPickModel('claude-opus-5.5'); });
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('a changed default model does not ask after an answer', async () => {
  mockPut.mockResolvedValue({ data: { user: {
    ...baseUser, profile_both_providers: false,
    ask_both_providers: { model: false, visit: false, import: false },
  } } });
  renderPage({ profile_both_providers: false });
  await act(async () => { await mockPickModel('gpt-6-astra', { picked: true }); });
  expect(screen.queryByRole('dialog')).toBeNull();
});
