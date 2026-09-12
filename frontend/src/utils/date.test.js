import { formatDate, formatDateTime, formatYmd } from './date';

describe('date helpers', () => {
  test('formatYmd zero-pads month and day', () => {
    expect(formatYmd(new Date(2026, 0, 5))).toBe('2026/01/05');
  });

  test('formatDate renders yyyy/mm/dd for non-recent dates', () => {
    expect(formatDate('2024-03-07T12:00:00Z', { relative: false })).toMatch(/^2024\/03\/0[78]$/);
  });

  test('formatDate keeps relative wording for today', () => {
    expect(formatDate(new Date())).toBe('today');
  });

  test('formatDateTime renders yyyy/mm/dd HH:MM without seconds', () => {
    expect(formatDateTime(new Date(2026, 8, 12, 9, 5, 30))).toBe('2026/09/12 09:05');
  });

  test('empty input returns the fallback', () => {
    expect(formatDate(null, { fallback: 'default' })).toBe('default');
    expect(formatDateTime('')).toBe('');
  });
});
