import { betaOpensSentence, BETA_OPENS_ON } from './beta';

test('the opening day is one constant', () => {
  expect(BETA_OPENS_ON).toBe('2026-12-21');
});

test('shown until the opening day starts, gone from then on', () => {
  expect(betaOpensSentence(new Date(2026, 11, 20, 23, 59))).toBe('The Beta opens on 21 December.');
  expect(betaOpensSentence(new Date(2026, 11, 21, 0, 0))).toBeNull();
  expect(betaOpensSentence(new Date(2027, 0, 5))).toBeNull();
});

test('the date comes from the constant argument', () => {
  expect(betaOpensSentence(new Date(2026, 0, 1), '2026-03-05')).toBe('The Beta opens on 5 March.');
});
