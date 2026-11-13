import {
  isRecordingLimitCode, untranscribedMessage,
} from './recordingLimits';

describe('recordingLimits', () => {
  test('audio kept past the length limit has a message; a failure has none', () => {
    expect(untranscribedMessage('recording_too_long')).toMatch(/longer than six hours/);
    expect(untranscribedMessage(undefined)).toBeNull();
    expect(untranscribedMessage('init_parse_failed')).toBeNull();
    // The usage limit no longer stops a recording's transcription (#341).
    expect(untranscribedMessage('monthly_spend_limit_reached')).toBeNull();
  });

  test('the refusals no retry can help', () => {
    expect(isRecordingLimitCode('recording_too_large')).toBe(true);
    expect(isRecordingLimitCode('chunk_index_out_of_range')).toBe(true);
    expect(isRecordingLimitCode('chunk_too_large')).toBe(true);
    expect(isRecordingLimitCode('session_not_found')).toBe(false);
    expect(isRecordingLimitCode(undefined)).toBe(false);
  });
});
