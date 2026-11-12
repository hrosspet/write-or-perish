// Peter, 2026-11-12: "the announcement should be TTSed as a separate chunk
// and appended to the queue. So that when it's not pending, it's not
// voiced out anymore". A decision in the approval dialog (the thread page
// or the Voice screen) asks again about the reply whose announcement is in
// the player's queue, and takes it out when nothing of that reply waits.
let mockAudio;
jest.mock('../contexts/AudioContext', () => ({ useOptionalAudio: () => mockAudio }));
const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: {
    get: (...args) => mockGet(...args),
    post: (...args) => mockPost(...args),
  },
}));

import { renderHook, act } from '@testing-library/react';
import { useToolApprovals } from './useToolApprovals';

const item = { id: 7, turn_node_id: 40, node_id: 40 };

beforeEach(() => {
  jest.clearAllMocks();
  mockAudio = { announcementNodeId: () => 41, dropAnnouncement: jest.fn() };
  mockPost.mockResolvedValue({ data: { outcome: 'approved', pending: [] } });
});

const decide = async (approve = true) => {
  const { result } = renderHook(() => useToolApprovals());
  await act(async () => { await result.current.decide(item, approve); });
  await act(async () => { await Promise.resolve(); });
};

test.each([true, false])('decided (approve %s): the reply plays alone after', async (approve) => {
  mockGet.mockResolvedValue({ data: { count: 0, url: null, duration: null } });
  await decide(approve);

  expect(mockGet).toHaveBeenCalledWith('/voice/approval-announcement', { params: { node: 41 } });
  expect(mockAudio.dropAnnouncement).toHaveBeenCalledTimes(1);
});

test('another change of that reply still waits: the announcement stays', async () => {
  mockGet.mockResolvedValue({ data: { count: 1, url: '/clip.mp3', duration: 4 } });
  await decide();

  expect(mockAudio.dropAnnouncement).not.toHaveBeenCalled();
});

test('no announcement in the queue: nothing is asked', async () => {
  mockAudio = { announcementNodeId: () => null, dropAnnouncement: jest.fn() };
  await decide();

  expect(mockGet).not.toHaveBeenCalled();
});

test('no player (a page without one): deciding works as before', async () => {
  mockAudio = null;
  await decide();

  expect(mockPost).toHaveBeenCalledWith('/tool-approvals/7/approve');
  expect(mockGet).not.toHaveBeenCalled();
});
