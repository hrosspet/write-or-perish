import { spendCapResetDate, spendCapToastMessage, isSpendCapError } from './spendCap';

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
