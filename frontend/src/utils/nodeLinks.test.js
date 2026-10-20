jest.mock('../api', () => ({ get: jest.fn() }));

import api from '../api';
import { parseNodeLink, fetchNodeTitle, cachedNodeTitle, _resetNodeTitleCache } from './nodeLinks';

beforeEach(() => {
  _resetNodeTitleCache();
  api.get.mockReset();
});

describe('parseNodeLink', () => {
  test('recognises Loore hosts, the current origin and app-relative paths', () => {
    expect(parseNodeLink('https://loore.org/node/24621')).toBe(24621);
    expect(parseNodeLink('https://www.loore.org/node/7/')).toBe(7);
    expect(parseNodeLink('https://staging.loore.org/node/3')).toBe(3);
    expect(parseNodeLink(`${window.location.origin}/node/12`)).toBe(12);
    expect(parseNodeLink('/node/5')).toBe(5);
  });

  test('rejects other hosts, other paths and junk', () => {
    expect(parseNodeLink('https://example.com/node/5')).toBeNull();
    expect(parseNodeLink('https://loore.org/about')).toBeNull();
    expect(parseNodeLink('https://loore.org/node/abc')).toBeNull();
    expect(parseNodeLink('https://loore.org/node/5/edit')).toBeNull();
    expect(parseNodeLink('mailto:x@loore.org')).toBeNull();
    expect(parseNodeLink('')).toBeNull();
    expect(parseNodeLink(null)).toBeNull();
  });
});

describe('fetchNodeTitle', () => {
  test('coalesces same-tick lookups into one request and caches the answer', async () => {
    api.get.mockResolvedValue({ data: { titles: { 1: { id: 1, title: 'One' }, 2: null } } });
    const [a, b, c] = await Promise.all([fetchNodeTitle(1), fetchNodeTitle(2), fetchNodeTitle(1)]);
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledWith('/nodes/titles', { params: { ids: '1,2' } });
    expect(a).toEqual({ id: 1, title: 'One' });
    expect(b).toBeNull();
    expect(c).toEqual({ id: 1, title: 'One' });
    expect(cachedNodeTitle(1)).toEqual({ id: 1, title: 'One' });
    await fetchNodeTitle(1);
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  test('a failed lookup resolves undefined and is not cached', async () => {
    api.get.mockRejectedValueOnce(new Error('offline'));
    expect(await fetchNodeTitle(9)).toBeUndefined();
    expect(cachedNodeTitle(9)).toBeUndefined();
    api.get.mockResolvedValueOnce({ data: { titles: { 9: { id: 9, title: 'Nine' } } } });
    expect(await fetchNodeTitle(9)).toEqual({ id: 9, title: 'Nine' });
    expect(api.get).toHaveBeenCalledTimes(2);
  });
});
