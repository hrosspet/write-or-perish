// The home page by purpose (#436, Peter 2026-10-09): for a user who
// gleans, a Reflect card and a Glean card, and Share, exactly as today,
// when it is on. A user without Glean keeps the Voice and Text cards. The
// admin-only Read card is gone. The welcome question applies to both
// cards, as to Voice and Text.
//
// Since Peter's local test (2026-10-13) the whole card starts it, and
// Voice / Text is a switch on the card that only chooses how: Voice by
// default, remembered per card in this browser.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  ...jest.requireActual('react-router-dom'),
  useNavigate: () => mockNavigate,
}));

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import HomePage from './HomePage';
import { WELCOME_QUESTION, EVERYDAY_QUESTION } from '../utils/entryPrompt';

const renderHome = (user) => {
  mockUserCtx = { user: { username: 'ana', has_own_entries: true, ...user } };
  return render(<MemoryRouter><HomePage /></MemoryRouter>);
};

const switchOf = (title) => screen.getByRole('radiogroup', { name: `${title}: voice or text` });
const optionOf = (title, label) => within(switchOf(title)).getByRole('radio', { name: label });

beforeAll(() => {
  window.IntersectionObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

beforeEach(() => {
  mockNavigate.mockReset();
  localStorage.clear();
});

afterEach(() => {
  jest.restoreAllMocks();
});

test('a user who gleans gets the Reflect and Glean cards, both on Voice by default', () => {
  renderHome({ glean_enabled: true });
  const reflect = screen.getByRole('group', { name: 'Reflect' });
  const glean = screen.getByRole('group', { name: 'Glean' });
  expect(within(reflect).getByText('Talk it through with Loore.')).toBeInTheDocument();
  expect(within(glean).getByText('Reflect, and Loore gleans for you.')).toBeInTheDocument();

  expect(optionOf('Reflect', 'Voice')).toHaveAttribute('aria-checked', 'true');
  expect(optionOf('Reflect', 'Text')).toHaveAttribute('aria-checked', 'false');
  expect(optionOf('Glean', 'Voice')).toHaveAttribute('aria-checked', 'true');

  fireEvent.click(screen.getByRole('button', { name: 'Reflect by voice' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/voice');
  fireEvent.click(screen.getByRole('button', { name: 'Glean by voice' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/voice?glean=1');
  expect(screen.queryByText('Share')).toBeNull();
});

test('the switch only chooses; the card then starts the chosen mode', () => {
  renderHome({ glean_enabled: true });

  fireEvent.click(optionOf('Reflect', 'Text'));
  expect(mockNavigate).not.toHaveBeenCalled();
  expect(optionOf('Reflect', 'Text')).toHaveAttribute('aria-checked', 'true');
  expect(optionOf('Reflect', 'Voice')).toHaveAttribute('aria-checked', 'false');
  // Clicking the chosen half again does nothing either.
  fireEvent.click(optionOf('Reflect', 'Text'));
  expect(mockNavigate).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole('button', { name: 'Reflect in text' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/textmode');

  fireEvent.click(optionOf('Glean', 'Text'));
  fireEvent.click(screen.getByRole('button', { name: 'Glean in text' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/textmode?glean=1');
  expect(mockNavigate).toHaveBeenCalledTimes(2);
});

test('each card remembers its own choice; Reflect and Glean are independent', () => {
  const { unmount } = renderHome({ glean_enabled: true });
  fireEvent.click(optionOf('Glean', 'Text'));
  expect(localStorage.getItem('loore.home.gleanMode')).toBe('text');
  expect(localStorage.getItem('loore.home.reflectMode')).toBeNull();
  // Reflect stays on Voice.
  expect(optionOf('Reflect', 'Voice')).toHaveAttribute('aria-checked', 'true');
  unmount();

  // The next visit opens with Glean on Text and Reflect on Voice.
  renderHome({ glean_enabled: true });
  expect(optionOf('Glean', 'Text')).toHaveAttribute('aria-checked', 'true');
  expect(optionOf('Reflect', 'Voice')).toHaveAttribute('aria-checked', 'true');
  expect(screen.getByRole('button', { name: 'Glean in text' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Reflect by voice' })).toBeInTheDocument();
});

test('storage that refuses (a private window) means Voice, and choosing still works', () => {
  jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
  jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  renderHome({ glean_enabled: true });
  expect(optionOf('Reflect', 'Voice')).toHaveAttribute('aria-checked', 'true');
  fireEvent.click(optionOf('Reflect', 'Text'));
  fireEvent.click(screen.getByRole('button', { name: 'Reflect in text' }));
  expect(mockNavigate).toHaveBeenLastCalledWith('/textmode');
});

test('the switch is a radio group: one tab stop, the arrow keys move the choice', () => {
  renderHome({ glean_enabled: true });
  const voice = optionOf('Reflect', 'Voice');
  const text = optionOf('Reflect', 'Text');
  expect(voice).toHaveAttribute('tabindex', '0');
  expect(text).toHaveAttribute('tabindex', '-1');

  voice.focus();
  fireEvent.keyDown(voice, { key: 'ArrowRight' });
  expect(text).toHaveAttribute('aria-checked', 'true');
  expect(text).toHaveAttribute('tabindex', '0');
  expect(document.activeElement).toBe(text);
  fireEvent.keyDown(text, { key: 'ArrowLeft' });
  expect(voice).toHaveAttribute('aria-checked', 'true');
  expect(document.activeElement).toBe(voice);
  expect(mockNavigate).not.toHaveBeenCalled();
});

test('the card glows on hover, but not while the pointer is on the switch', () => {
  renderHome({ glean_enabled: true });
  const card = screen.getByRole('group', { name: 'Reflect' });
  const start = screen.getByRole('button', { name: 'Reflect by voice' });
  expect(card.style.boxShadow).toBe('none');
  fireEvent.mouseEnter(start);
  expect(card.style.boxShadow).toContain('var(--accent-glow)');
  // Onto the switch: the pointer leaves the start button.
  fireEvent.mouseLeave(start, { relatedTarget: optionOf('Reflect', 'Text') });
  expect(card.style.boxShadow).toBe('none');
});

test('Share stays exactly as today when it is on: the same card, opened by a click', () => {
  // The card a user without Glean gets ...
  const { container: today } = renderHome({ glean_enabled: false, share_v1_enabled: true });
  const todays = screen.getByText('Give something outward.').closest('div[style]').outerHTML;
  today.remove();
  // ... is the card a user with Glean gets: no switch, no card button.
  renderHome({ glean_enabled: true, share_v1_enabled: true });
  expect(screen.queryByRole('group', { name: 'Share' })).toBeNull();
  expect(screen.queryByRole('button', { name: /Share/ })).toBeNull();
  const glean = screen.getByText('Give something outward.').closest('div[style]');
  expect(glean.outerHTML).toBe(todays);
  fireEvent.click(screen.getByText('Give something outward.'));
  expect(mockNavigate).toHaveBeenLastCalledWith('/share');
});

test('a user without Glean keeps the Voice and Text cards, and no Read card', () => {
  renderHome({ glean_enabled: false, is_admin: true });
  expect(screen.queryByRole('group', { name: 'Glean' })).toBeNull();
  expect(screen.queryByRole('group', { name: 'Reflect' })).toBeNull();
  expect(screen.queryByRole('radiogroup')).toBeNull();
  expect(screen.getByText('Voice')).toBeInTheDocument();
  expect(screen.getByText('Text')).toBeInTheDocument();
  expect(screen.queryByText('Read')).toBeNull();
  expect(screen.queryByText(/Glean/)).toBeNull();
});

test('the welcome question applies to the Glean card too', () => {
  renderHome({ glean_enabled: true, has_own_entries: false });
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(WELCOME_QUESTION);
  expect(screen.queryByText(EVERYDAY_QUESTION)).toBeNull();
  // Both cards lead to the screens that ask it (VoicePage / WritePage use
  // the same entryQuestion rule, EntryQuestion.test.js).
  expect(screen.getByRole('button', { name: 'Glean by voice' })).toBeInTheDocument();
});
