import {
  isRecordingLimitCode, noticeUntranscribed, untranscribedMessage,
} from './recordingLimits';
import { isSpendBlocked } from './spendCap';

describe('recordingLimits', () => {
  test('a code of audio kept without its text has a message; a failure has none', () => {
    expect(untranscribedMessage('monthly_spend_limit_reached')).toMatch(/monthly usage limit/);
    expect(untranscribedMessage('recording_too_long')).toMatch(/longer than six hours/);
    expect(untranscribedMessage(undefined)).toBeNull();
    expect(untranscribedMessage('init_parse_failed')).toBeNull();
  });

  test('the usage limit also marks the account capped', () => {
    expect(noticeUntranscribed('recording_too_long')).toMatch(/six hours/);
    expect(isSpendBlocked()).toBe(false);
    expect(noticeUntranscribed('monthly_spend_limit_reached')).toMatch(/usage limit/);
    expect(isSpendBlocked()).toBe(true);
  });

  test('the refusals no retry can help', () => {
    expect(isRecordingLimitCode('recording_too_large')).toBe(true);
    expect(isRecordingLimitCode('chunk_index_out_of_range')).toBe(true);
    expect(isRecordingLimitCode('chunk_too_large')).toBe(true);
    expect(isRecordingLimitCode('session_not_found')).toBe(false);
    expect(isRecordingLimitCode(undefined)).toBe(false);
  });
});
