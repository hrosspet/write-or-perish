import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import PrivacySelector from './PrivacySelector';
import api from '../api';

jest.mock('../api');

let mockUser = null;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: mockUser }),
}));

function renderSelector(props = {}) {
  const onPrivacyChange = jest.fn();
  render(
    <MemoryRouter>
      <PrivacySelector
        privacyLevel="private" aiUsage="chat"
        onPrivacyChange={onPrivacyChange} onAIUsageChange={() => {}}
        {...props}
      />
    </MemoryRouter>,
  );
  return { onPrivacyChange };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 1, username: 'anna', circles_enabled: true };
});

test('with Circles off it is the dropdown as before, Circles not selectable', () => {
  mockUser = { id: 1, username: 'anna', circles_enabled: false };
  renderSelector();
  expect(screen.getByText('Privacy Level')).toBeInTheDocument();
  expect(screen.queryByText('Who can read this')).toBeNull();
  const option = screen.getByText(/Circles · Shared with specific groups \(coming soon\)/);
  expect(option).toBeDisabled();
  expect(api.get).not.toHaveBeenCalled();
});

test('with Circles on: Only me / Circles / Everyone, stored as private / circles / public', () => {
  const { onPrivacyChange } = renderSelector();
  expect(screen.getByText('Who can read this')).toBeInTheDocument();
  expect(screen.queryByText('Privacy Level')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Circles' }));
  expect(onPrivacyChange).toHaveBeenCalledWith('circles');
  fireEvent.click(screen.getByRole('button', { name: 'Everyone' }));
  expect(onPrivacyChange).toHaveBeenCalledWith('public');
  fireEvent.click(screen.getByRole('button', { name: 'Only me' }));
  expect(onPrivacyChange).toHaveBeenCalledWith('private');
});

test('Circles opens the picker and names the readers before saving', async () => {
  api.get.mockResolvedValue({ data: { circles: [
    { id: 1, name: 'Close friends', members: [], shared_count: 0 },
  ] } });
  api.post.mockResolvedValue({ data: { readers: ['ben', 'chidi'] } });
  const onResolved = jest.fn();
  renderSelector({
    privacyLevel: 'circles',
    audience: { circleIds: [1], namesText: '' },
    onAudienceChange: () => {},
    onAudienceResolved: onResolved,
  });
  expect(await screen.findByRole('button', { name: 'Close friends' }))
    .toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByLabelText('Or name people for this entry only')).toBeInTheDocument();
  expect(await screen.findByText(
    'ben and chidi can read this entry and reply to it. Nobody else can.',
  )).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledWith('/circles/audience-preview',
    { circle_ids: [1], usernames: [] });
  expect(screen.getByText('You can change this later, or take the entry back to only you.'))
    .toBeInTheDocument();
  await waitFor(() => expect(onResolved).toHaveBeenLastCalledWith(
    expect.objectContaining({ readers: ['ben', 'chidi'], error: '' })));
});

test('a name nobody has is said plainly', async () => {
  api.get.mockResolvedValue({ data: { circles: [] } });
  api.post.mockRejectedValue({ response: { data: {
    error: 'No one on Loore has that name: @nobody' } } });
  renderSelector({
    privacyLevel: 'circles',
    audience: { circleIds: [], namesText: '@nobody' },
    onAudienceChange: () => {},
  });
  expect(await screen.findByText('No one on Loore has that name: @nobody'))
    .toBeInTheDocument();
});

test('a reply in a shared thread shows the people, not a choice', () => {
  renderSelector({
    privacyLevel: 'circles', lockPrivacy: true,
    threadAudience: ['anna', 'chidi'],
  });
  expect(screen.getByText(
    'anna and chidi will see it, with your name. Nobody else can.',
  )).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Only me' })).toBeNull();
});

test('imports keep two choices', () => {
  renderSelector({ allowCircles: false });
  expect(screen.getByRole('button', { name: 'Only me' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Everyone' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Circles' })).toBeNull();
});
