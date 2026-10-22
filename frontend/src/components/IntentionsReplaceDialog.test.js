// Admin "Infer intentions" when the user's own version is current (#412):
// the dialog says that the version exists, how it came about and when,
// never its text; keeping it is the first choice, and replacing it needs
// a click on its own button (no checkbox).
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import IntentionsReplaceDialog from './IntentionsReplaceDialog';

const ask = {
  userId: 7, username: 'alice', mode: 'batch', cancelPending: false,
  versionId: 42, changedBy: 'user', changedAt: '2026-10-12T09:30:00Z',
};

test('renders nothing without an ask', () => {
  const { container } = render(
    <IntentionsReplaceDialog ask={null} onConfirm={() => {}} onClose={() => {}} />);
  expect(container.innerHTML).toBe('');
});

test('says who changed the intentions and when, and replaces only on its own button', () => {
  const onConfirm = jest.fn();
  const onClose = jest.fn();
  render(<IntentionsReplaceDialog ask={ask} onConfirm={onConfirm} onClose={onClose} />);
  expect(screen.getByText('Replace their intentions?')).toBeTruthy();
  expect(screen.getByText(/@alice edited their intentions on /)).toBeTruthy();
  expect(screen.getByText(/A billed batch call/)).toBeTruthy();
  expect(screen.queryByRole('checkbox')).toBeNull();
  const buttons = screen.getAllByRole('button');
  expect(buttons[0].textContent).toMatch(/Keep their version/);

  fireEvent.click(buttons[0]);
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(onConfirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: /Replace their version/ }));
  expect(onConfirm).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(onClose).toHaveBeenCalledTimes(2);
});

test('names a conversation update and a restored version', () => {
  const { rerender } = render(
    <IntentionsReplaceDialog ask={{ ...ask, changedBy: 'conversation', mode: 'sync' }}
                             onConfirm={() => {}} onClose={() => {}} />);
  expect(screen.getByText(/had their intentions updated in a conversation/)).toBeTruthy();
  expect(screen.getByText(/A billed call at full price/)).toBeTruthy();
  rerender(<IntentionsReplaceDialog ask={{ ...ask, changedBy: 'revert' }}
                                    onConfirm={() => {}} onClose={() => {}} />);
  expect(screen.getByText(/restored an earlier version of their intentions/)).toBeTruthy();
});
