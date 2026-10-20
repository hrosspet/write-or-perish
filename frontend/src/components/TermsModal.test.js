// The terms say how account deletion works (#269; text by Peter,
// 2026-10-09). No new terms version: nobody has to accept again.
import React from 'react';
import { render, screen } from '@testing-library/react';
import TermsModal from './TermsModal';

jest.mock('../api', () => ({ __esModule: true, default: { post: jest.fn() } }));

test('the terms describe account deletion instead of saying there is none', () => {
  render(<TermsModal onAccepted={() => {}} />);
  expect(screen.queryByText(/no account deletion feature/)).toBeNull();
  expect(screen.getByText(
    'You can delete your account on the Account page. If you change your mind, you can '
    + 'still recover your account within 30 days by signing in; after that it is deleted forever, '
    + 'with everything in it. Copies in our backups are erased within another 30 days. Loore keeps a record of what your AI use '
    + 'cost, without your name.')).toBeTruthy();
});
