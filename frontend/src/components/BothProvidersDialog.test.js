// The both-providers question (#599): two equal answers, nothing chosen
// in advance, the user's provider named on the "keep" answer, and a close
// without an answer that records nothing.
let mockUserCtx;
jest.mock('../contexts/UserContext', () => ({
  useUser: () => mockUserCtx,
}));
const mockPut = jest.fn();
jest.mock('../api', () => ({
  __esModule: true,
  default: { put: (...args) => mockPut(...args) },
}));

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import BothProvidersDialog, { BothProvidersQuestion } from './BothProvidersDialog';

const renderWith = (ui, user = { profile_provider: 'anthropic' }) => {
  mockUserCtx = { user, setUser: jest.fn() };
  return render(<MemoryRouter>{ui}</MemoryRouter>);
};

beforeEach(() => mockPut.mockReset());

test('the two answers look the same and name the user\'s provider', () => {
  renderWith(<BothProvidersDialog onClose={jest.fn()} />);
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  const both = screen.getByRole('button', { name: 'Use both' });
  const keep = screen.getByRole('button', { name: 'Keep to Anthropic' });
  expect(both.getAttribute('style')).toBe(keep.getAttribute('style'));
  expect(screen.getByText(/do you want to stick to Anthropic\?/)).toBeInTheDocument();
  expect(screen.getByText(/With Anthropic only, all of it stays with Anthropic/)).toBeInTheDocument();
  // The user can tell that their writing itself goes to both companies.
  expect(screen.getByText(/your writing can be read by OpenAI's and Anthropic's models\./)).toBeInTheDocument();
});

test('an OpenAI user keeps to OpenAI', () => {
  renderWith(<BothProvidersQuestion />, { profile_provider: 'openai' });
  expect(screen.getByRole('button', { name: 'Keep to OpenAI' })).toBeInTheDocument();
});

test.each([[true, 'Use both', 'Saved: Anthropic and OpenAI.'],
  [false, 'Keep to Anthropic', 'Saved: Anthropic only.']])(
  'answering %s saves it and says so', async (value, label, saved) => {
    const updated = { profile_provider: 'anthropic', profile_both_providers: value };
    mockPut.mockResolvedValue({ data: { user: updated } });
    const onClose = jest.fn();
    renderWith(<BothProvidersDialog onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: label }));
    expect(mockPut).toHaveBeenCalledWith('/dashboard/user', { profile_both_providers: value });
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(saved));
    expect(mockUserCtx.setUser).toHaveBeenCalledWith(updated);
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenLastCalledWith(true);
  });

test('closing without an answer records nothing', () => {
  const onClose = jest.fn();
  renderWith(<BothProvidersDialog onClose={onClose} />);
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(onClose).toHaveBeenLastCalledWith(false);
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  expect(onClose).toHaveBeenLastCalledWith(false);
  expect(mockPut).not.toHaveBeenCalled();
});

test('a failed save says so and keeps the question', async () => {
  mockPut.mockRejectedValue(new Error('offline'));
  renderWith(<BothProvidersQuestion />);
  fireEvent.click(screen.getByRole('button', { name: 'Use both' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent("Couldn't save"));
  expect(screen.getByRole('button', { name: 'Use both' })).toBeEnabled();
});
