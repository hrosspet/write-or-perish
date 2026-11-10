import {
  spendCapResetDate, spendCapToastMessage, isSpendCapError,
  markSpendBlocked, spendCapBannerMessage, resetSpendCapForTests,
  SPEND_CAP_DEFAULT_MESSAGE,
} from './spendCap';

afterEach(() => resetSpendCapForTests());

// #594: a carry over a whole month's limit holds the cap past the start of
// next month; the server sends the date (user.spend_cap_lifts_on, a 402's
// lifts_on).
const NOV_20 = new Date(Date.UTC(2026, 10, 20));

test('a later lift date from the server names that date', () => {
  markSpendBlocked('2027-01-01');
  expect(spendCapResetDate(NOV_20)).toMatch(/January/);
  expect(spendCapToastMessage('record', NOV_20)).toMatch(/until it resets on January 1\./);
  expect(spendCapBannerMessage(NOV_20)).toBe(
    "You've reached your monthly usage limit for the free alpha. It resets on January 1.");
});

test('the usual lift date keeps the usual text', () => {
  markSpendBlocked('2026-12-01');
  expect(spendCapResetDate(NOV_20)).toMatch(/December/);
  expect(spendCapBannerMessage(NOV_20)).toBe(SPEND_CAP_DEFAULT_MESSAGE);
});

test('without a date from the server the cap resets next month', () => {
  markSpendBlocked();
  expect(spendCapResetDate(NOV_20)).toMatch(/December/);
  expect(spendCapBannerMessage(NOV_20)).toBe(SPEND_CAP_DEFAULT_MESSAGE);
});

test('a lift date already past is ignored', () => {
  markSpendBlocked('2026-12-01');
  expect(spendCapResetDate(new Date(Date.UTC(2027, 0, 5)))).toMatch(/February/);
});

test("a 402's lift date reaches the toasts", () => {
  window.dispatchEvent(new CustomEvent('loore:spend-capped', {
    detail: { message: 'x', liftsOn: '2027-01-01' },
  }));
  expect(spendCapResetDate(NOV_20)).toMatch(/January/);
});

// The cap resets at the start of the server's (UTC) month.
test('reset date is the first of the next UTC month', () => {
  const lateSeptUtc = new Date(Date.UTC(2026, 8, 30, 23, 30));
  expect(spendCapResetDate(lateSeptUtc)).toMatch(/October/);
  expect(spendCapResetDate(lateSeptUtc)).toMatch(/\b1\b/);
  const december = new Date(Date.UTC(2026, 11, 15));
  expect(spendCapResetDate(december)).toMatch(/January/);
});

test('toast names the refused action', () => {
  const now = new Date(Date.UTC(2026, 8, 23));
  expect(spendCapToastMessage('record', now)).toMatch(/start a new recording/);
  expect(spendCapToastMessage('upload', now)).toMatch(/upload audio/);
});

test('recognizes only the spend-cap 402', () => {
  const res = (status, error) => ({ response: { status, data: { error } } });
  expect(isSpendCapError(res(402, 'monthly_spend_limit_reached'))).toBe(true);
  expect(isSpendCapError(res(402, 'something_else'))).toBe(false);
  expect(isSpendCapError(res(400, 'monthly_spend_limit_reached'))).toBe(false);
  expect(isSpendCapError(new Error('Network Error'))).toBe(false);
});
