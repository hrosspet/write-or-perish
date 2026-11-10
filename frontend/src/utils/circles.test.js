import {
  countWord, joinNames, splitNames, readersSentence, shareButtonLabel,
  replyAudienceNames, sharedBannerText, replyAckKey,
} from './circles';

test('names are joined the way the mockups write them', () => {
  expect(joinNames([])).toBe('');
  expect(joinNames(['anna'])).toBe('anna');
  expect(joinNames(['anna', 'ben'])).toBe('anna and ben');
  expect(joinNames(['anna', 'ben', 'chidi'])).toBe('anna, ben and chidi');
});

test('typed names: commas or spaces, @ optional, no repeats', () => {
  expect(splitNames('@dana, @emeka')).toEqual(['dana', 'emeka']);
  expect(splitNames(' dana  Dana,@ben ')).toEqual(['dana', 'ben']);
  expect(splitNames('')).toEqual([]);
});

test('the sentence before saving', () => {
  expect(readersSentence(['anna', 'ben', 'chidi'])).toBe(
    'anna, ben and chidi can read this entry and reply to it. Nobody else can.');
  expect(readersSentence([])).toBe('');
});

test('the save button names one circle, else counts people', () => {
  const circles = [{ id: 1, name: 'Close friends' }, { id: 2, name: 'Family' }];
  expect(shareButtonLabel({
    circles, circleIds: [1], names: [], readers: ['ben', 'chidi'],
  })).toBe('Share with Close friends');
  expect(shareButtonLabel({
    circles, circleIds: [1], names: ['dana'], readers: ['ben', 'chidi', 'dana'],
  })).toBe('Share with 3 people');
  expect(shareButtonLabel({
    circles, circleIds: [1, 2], names: [], readers: ['ben'],
  })).toBe('Share with 1 person');
  expect(shareButtonLabel({
    circles, circleIds: [], names: [], readers: [],
  })).toBe('Share');
});

test('a reply is seen by the author and the readers, not named to its writer', () => {
  const audience = { author: 'anna', readers: ['ben', 'chidi'] };
  expect(replyAudienceNames(audience, 'ben')).toEqual(['anna', 'chidi']);
  expect(replyAudienceNames(audience, 'anna')).toEqual(['ben', 'chidi']);
  expect(replyAudienceNames(null, 'ben')).toEqual([]);
});

test('the banner over a shared entry', () => {
  expect(sharedBannerText({ author: 'anna', readers: ['ben', 'chidi', 'dana'] })).toBe(
    'anna shared this with you and two other people. Only they can read it, '
    + 'and nobody but anna can make it public.');
  expect(sharedBannerText({ author: 'anna', readers: ['ben', 'chidi'] })).toBe(
    'anna shared this with you and one other person. Only they can read it, '
    + 'and nobody but anna can make it public.');
  expect(sharedBannerText({ author: 'anna', readers: ['ben'] })).toBe(
    'anna shared this with you. Only you and anna can read it, '
    + 'and nobody but anna can make it public.');
});

test('numbers are words up to twelve', () => {
  expect(countWord(2)).toBe('two');
  expect(countWord(12)).toBe('twelve');
  expect(countWord(13)).toBe('13');
});

test('"Don\'t ask again" is kept per thread and per user', () => {
  expect(replyAckKey(31, 5)).toBe('loore_circle_reply_ack_5_31');
  expect(replyAckKey(31, 6)).not.toBe(replyAckKey(31, 5));
});
