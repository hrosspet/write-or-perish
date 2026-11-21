import {
  boundRefusalMessage, finalRefusalMessage, isNotAnnouncedChunk, isRecordingLimitCode,
  isStartRefusal, LONGEST_RECORDING_MESSAGE, RECORDING_LIMIT_MESSAGE, untranscribedMessage,
} from './recordingLimits';

const refusal = (status, data) => ({ response: { status, data } });

describe('recordingLimits', () => {
  test('audio kept past the length limit has a message; a failure has none', () => {
    expect(untranscribedMessage('recording_too_long')).toMatch(/longer than two hours/);
    expect(untranscribedMessage(undefined)).toBeNull();
    expect(untranscribedMessage('init_parse_failed')).toBeNull();
    // The usage limit does not stop a started recording's transcription (#341).
    expect(untranscribedMessage('monthly_spend_limit_reached')).toBeNull();
  });

  test('the refusals no retry can help', () => {
    expect(isRecordingLimitCode('recording_too_large')).toBe(true);
    expect(isRecordingLimitCode('chunk_index_out_of_range')).toBe(true);
    expect(isRecordingLimitCode('chunk_too_large')).toBe(true);
    expect(isRecordingLimitCode('too_many_recordings')).toBe(true);
    expect(isRecordingLimitCode('session_not_found')).toBe(false);
    expect(isRecordingLimitCode(undefined)).toBe(false);
  });

  test("the server's words for a refusal, or nothing for another failure", () => {
    expect(finalRefusalMessage(refusal(402, {
      error: 'monthly_spend_limit_reached', message: 'Limit reached.',
    }))).toBe('Limit reached.');
    expect(finalRefusalMessage(refusal(429, {
      error: 'Too many.', code: 'too_many_recordings',
    }))).toBe('Too many.');
    expect(finalRefusalMessage(refusal(500, { error: 'boom' }))).toBeNull();
    expect(finalRefusalMessage(new Error('Network Error'))).toBeNull();
  });

  test('past what one recording may hold: the server words, only for the bound', () => {
    const words = 'This recording has reached the longest Loore takes, about four hours, '
      + 'and has stopped. Everything before this is saved.';
    expect(boundRefusalMessage(refusal(400, {
      error: words, code: 'chunk_index_out_of_range',
    }))).toBe(words);
    // Without words: the same text at the four-hour bound, a general one otherwise.
    expect(boundRefusalMessage(refusal(400, { code: 'chunk_index_out_of_range' })))
      .toBe(LONGEST_RECORDING_MESSAGE);
    expect(LONGEST_RECORDING_MESSAGE).toBe(words);
    expect(boundRefusalMessage(refusal(413, { code: 'recording_too_large' })))
      .toBe(RECORDING_LIMIT_MESSAGE);
    expect(boundRefusalMessage(refusal(413, { code: 'chunk_too_large' })))
      .toBe(RECORDING_LIMIT_MESSAGE);
    // A recording refused as a new start is not past its bound, nor is a
    // chunk that came after the finalize; keyed on the code, not the words.
    expect(boundRefusalMessage(refusal(429, { error: 'Too many.', code: 'too_many_recordings' })))
      .toBeNull();
    expect(boundRefusalMessage(refusal(400, {
      error: 'This recording was already finished, so this part was not added to it.',
      code: 'chunk_not_announced',
    }))).toBeNull();
    expect(boundRefusalMessage(refusal(400, { error: words }))).toBeNull();
    expect(boundRefusalMessage(refusal(400, { error: 'Missing chunk_index' }))).toBeNull();
    expect(boundRefusalMessage(new Error('Network Error'))).toBeNull();
  });

  test('a chunk after the finalize is told apart from the bound', () => {
    expect(isNotAnnouncedChunk(refusal(400, { code: 'chunk_not_announced' }))).toBe(true);
    expect(isNotAnnouncedChunk(refusal(400, { code: 'chunk_index_out_of_range' }))).toBe(false);
    expect(isNotAnnouncedChunk(new Error('Network Error'))).toBe(false);
    expect(isRecordingLimitCode('chunk_not_announced')).toBe(false);
  });

  test('the start refusals', () => {
    expect(isStartRefusal(refusal(429, { code: 'too_many_recordings' }))).toBe(true);
    expect(isStartRefusal(refusal(402, { error: 'monthly_spend_limit_reached' }))).toBe(false);
  });
});
