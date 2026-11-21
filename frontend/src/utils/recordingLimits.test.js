import {
  boundRefusalMessage, finalRefusalMessage, isRecordingLimitCode, isStartRefusal,
  RECORDING_LIMIT_MESSAGE, untranscribedMessage,
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
    expect(boundRefusalMessage(refusal(400, {
      error: 'This recording has more parts than Loore accepts.', code: 'chunk_index_out_of_range',
    }))).toBe('This recording has more parts than Loore accepts.');
    expect(boundRefusalMessage(refusal(413, { code: 'recording_too_large' })))
      .toBe(RECORDING_LIMIT_MESSAGE);
    expect(boundRefusalMessage(refusal(413, { code: 'chunk_too_large' })))
      .toBe(RECORDING_LIMIT_MESSAGE);
    // A recording refused as a new start is not past its bound.
    expect(boundRefusalMessage(refusal(429, { error: 'Too many.', code: 'too_many_recordings' })))
      .toBeNull();
    expect(boundRefusalMessage(refusal(400, { error: 'Missing chunk_index' }))).toBeNull();
    expect(boundRefusalMessage(new Error('Network Error'))).toBeNull();
  });

  test('the start refusals', () => {
    expect(isStartRefusal(refusal(429, { code: 'too_many_recordings' }))).toBe(true);
    expect(isStartRefusal(refusal(402, { error: 'monthly_spend_limit_reached' }))).toBe(false);
  });
});
