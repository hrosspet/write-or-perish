// The rebuild question after a re-import set entries to None (#625): two
// equal answers, "Rebuild" sends the server's token back, "Keep" sends
// nothing.
const mockPost = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { post: (...args) => mockPost(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import ProfileRebuildQuestion from './ProfileRebuildQuestion';

beforeEach(() => mockPost.mockReset());

const question = { token: 'signed-token', entries: 4 };

test('it says the entries may be in the profile, not necessarily', () => {
  render(<ProfileRebuildQuestion question={question} />);
  expect(screen.getByText('Rebuild your profile?')).toBeInTheDocument();
  expect(screen.getByText(/set 4 entries that were already in Loore to None/)).toBeInTheDocument();
  expect(screen.getByText(/may still include what they say/)).toBeInTheDocument();
  expect(screen.getByText(/It may also not/)).toBeInTheDocument();
  const yes = screen.getByRole('button', { name: 'Rebuild my profile' });
  const no = screen.getByRole('button', { name: 'Keep my profile as it is' });
  expect(yes.getAttribute('style')).toBe(no.getAttribute('style'));
});

test('one entry is named in the singular', () => {
  render(<ProfileRebuildQuestion question={{ token: 't', entries: 1 }} />);
  expect(screen.getByText(/set 1 entry that was already in Loore to None/)).toBeInTheDocument();
});

test('a yes sends the token back and says so', async () => {
  mockPost.mockResolvedValue({ data: { profile_update_task_id: null } });
  render(<ProfileRebuildQuestion question={question} />);
  fireEvent.click(screen.getByRole('button', { name: 'Rebuild my profile' }));
  expect(mockPost).toHaveBeenCalledWith('/import/profile-rebuild', { token: 'signed-token' });
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(
    'Your profile will be rebuilt without these entries.'));
});

test('a no sends nothing', () => {
  render(<ProfileRebuildQuestion question={question} />);
  fireEvent.click(screen.getByRole('button', { name: 'Keep my profile as it is' }));
  expect(mockPost).not.toHaveBeenCalled();
  expect(screen.getByRole('status')).toHaveTextContent('Your profile stays as it is.');
});

test('a refused answer shows the server\'s reason and can be tried again', async () => {
  mockPost.mockRejectedValue({ response: { data: { error: 'This question has expired. Your profile stays as it is.' } } });
  render(<ProfileRebuildQuestion question={question} />);
  fireEvent.click(screen.getByRole('button', { name: 'Rebuild my profile' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('This question has expired.');
  expect(screen.getByRole('button', { name: 'Rebuild my profile' })).toBeEnabled();
});
