import {
  entryQuestion, isNewcomer, WELCOME_QUESTION, EVERYDAY_QUESTION,
} from './entryPrompt';

test('no entry written in Loore yet: the welcome question', () => {
  expect(isNewcomer({ has_own_entries: false })).toBe(true);
  expect(entryQuestion({ has_own_entries: false })).toBe(WELCOME_QUESTION);
  expect(WELCOME_QUESTION).toBe(
    'What brought you to Loore — and what are you hoping to find here?');
});

test('after the first entry: the everyday question', () => {
  expect(entryQuestion({ has_own_entries: true })).toBe(EVERYDAY_QUESTION);
  expect(EVERYDAY_QUESTION).toBe("What's on your mind?");
});

test('no flag (loading, or a response without it): the everyday question', () => {
  expect(isNewcomer(null)).toBe(false);
  expect(isNewcomer(undefined)).toBe(false);
  expect(entryQuestion({ username: 'alice' })).toBe(EVERYDAY_QUESTION);
});
