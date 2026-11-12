import {
  countWord, joinNames, splitNames, readersSentence, shareButtonLabel,
  replyAudienceNames, sharedBannerText, replyAckKey, removeMemberBody,
  atName, aiReplyAckKey, aiReplyOffReason, possessive,
  joinPlain, aiReadsPrivateSentence,
} from './circles';

test('removing a person says what stays open', () => {
  expect(removeMemberBody('ben', 'Close friends')).toBe(
    'From this moment @ben no longer reads your entries through Close friends. '
    + 'An entry you also shared with @ben another way (another circle @ben is in, '
    + 'or by name) stays open to @ben. @ben is not told.');
});

test('joined names carry "@"', () => {
  expect(joinNames([])).toBe('');
  expect(joinNames(['ben'])).toBe('@ben');
  expect(joinNames(['anna', 'ben'])).toBe('@anna and @ben');
  expect(joinNames(['ben', 'chidi', 'dana'])).toBe('@ben, @chidi and @dana');
});

test('a name never gets a second "@"', () => {
  expect(atName('ben')).toBe('@ben');
  expect(atName('@ben')).toBe('@ben');
  expect(atName('')).toBe('');
  expect(atName(null)).toBe('');
  expect(joinNames(['@ben', 'chidi'])).toBe('@ben and @chidi');
  expect(possessive('ben')).toBe("@ben's");
});

test('typed names: commas or spaces, @ optional, no repeats', () => {
  expect(splitNames('@dana, @emeka')).toEqual(['dana', 'emeka']);
  expect(splitNames(' dana  Dana,@ben ')).toEqual(['dana', 'ben']);
  expect(splitNames('')).toEqual([]);
});

test('the sentence before saving', () => {
  expect(readersSentence(['anna', 'ben', 'chidi'])).toBe(
    '@anna, @ben and @chidi can read this entry and reply to it. Nobody else can.');
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
    '@anna shared this with you and two other people. Only they can read it, '
    + 'and nobody but @anna can make it public.');
  expect(sharedBannerText({ author: 'anna', readers: ['ben', 'chidi'] })).toBe(
    '@anna shared this with you and one other person. Only they can read it, '
    + 'and nobody but @anna can make it public.');
  expect(sharedBannerText({ author: 'anna', readers: ['ben'] })).toBe(
    '@anna shared this with you. Only you and @anna can read it, '
    + 'and nobody but @anna can make it public.');
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

test('AI replies have their own "Don\'t ask again"', () => {
  expect(aiReplyAckKey(31, 5)).toBe('loore_circle_ai_reply_ack_5_31');
  expect(aiReplyAckKey(31, 5)).not.toBe(replyAckKey(31, 5));
});

test('what an AI reply reads of its requester\'s artifacts', () => {
  expect(joinPlain(['profile', 'memory', 'to-do list'])).toBe('profile, memory and to-do list');
  expect(joinPlain(['profile'])).toBe('profile');
  expect(aiReadsPrivateSentence(['profile', 'memory'])).toBe(
    'Your AI reads your profile and memory for this reply, as the prompt shared in this thread asks.');
  expect(aiReadsPrivateSentence([])).toBe('');
  expect(aiReadsPrivateSentence(undefined)).toBe('');
});

test('why no AI reply can be generated in a shared thread', () => {
  const open = { author: 'anna', readers: ['ben'], entry_ai_usage: 'chat', is_author: false };
  const me = { default_ai_usage: 'chat' };
  expect(aiReplyOffReason(null, { ai_usage: 'none' }, me)).toBeNull();
  expect(aiReplyOffReason(open, { ai_usage: 'chat' }, me)).toBeNull();
  expect(aiReplyOffReason({ ...open, entry_ai_usage: 'none' }, { ai_usage: 'chat' }, me))
    .toBe("@anna set this entry's AI usage to None, so no AI can read it.");
  expect(aiReplyOffReason(
    { ...open, entry_ai_usage: 'none', is_author: true }, { ai_usage: 'chat' }, me,
  )).toBe("This entry's AI usage is None, so no AI can read it.");
  expect(aiReplyOffReason(open, { ai_usage: 'none' }, me))
    .toBe("This entry's AI usage is None, so no AI can read it.");
  expect(aiReplyOffReason(open, { ai_usage: 'chat' }, { default_ai_usage: 'none' }))
    .toBe('Your own AI usage is set to None in Account settings.');
});
