// The Activity tab's artifacts cell (A.112): the server sends the
// artifacts a user created as one entry with counts only, and the cell
// shows it as "user-created x<views> (<artifacts> artifacts)". Kinds Loore
// defines keep their names.
const mockGet = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: (...args) => mockGet(...args), post: jest.fn(), put: jest.fn() },
}));
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({}) }));

import React from 'react';
import { render, screen } from '@testing-library/react';
import { AdminActivity, artifactViewLabel } from './AdminPanel';

const row = (artifactViews) => ({
  id: 7, username: 'reader', seeded: 'ca', prefill_consent: null,
  activated_at: '2026-11-01T10:00:00', accepted_terms_at: '2026-11-01T10:00:00',
  last_seen_at: '2026-11-19T10:00:00', last_seen_path: '/api/artifacts/user-created/viewed',
  strip: [{ d: '2026-11-19', w: 1, a: 0, v: 0, pre: false }],
  active_days: 1, days_since_activation: 18, writes: 1, asks: 0,
  voice_nodes: 0, voice_minutes: 0, imports: 0,
  artifact_views: artifactViews,
  profile_versions_since_activation: 0, latest_profile_at: null,
  user_spend_usd: 0, day1_return: false, day7_return: false,
});

beforeEach(() => mockGet.mockReset());

test('shows the user-created artifacts as one anonymized count', async () => {
  mockGet.mockResolvedValue({ data: { days: ['2026-11-19'], users: [row([
    { kind: 'user-created', user_created: true, count: 5, artifacts: 2, last: '2026-11-19T09:00:00' },
    { kind: 'intentions', count: 3, last: '2026-11-18T09:00:00' },
  ])] } });
  render(<AdminActivity />);
  // The cohort filter starts at "seeded"; the row is seeded from CA.
  expect(await screen.findByText('user-created x5 (2 artifacts), intentions x3')).toBeInTheDocument();
});

test('one artifact is singular', () => {
  expect(artifactViewLabel({ kind: 'user-created', user_created: true, count: 1, artifacts: 1 }))
    .toBe('user-created x1 (1 artifact)');
  expect(artifactViewLabel({ kind: 'user-created', user_created: true, count: 4, artifacts: 1 }))
    .toBe('user-created x4 (1 artifact)');
});

test('control: a kind Loore defines keeps its name', () => {
  expect(artifactViewLabel({ kind: 'memory', count: 2 })).toBe('memory x2');
});
