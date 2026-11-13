// The acceptance dialog shows frontend/src/legal/terms.json, the same text
// as the public /terms page and the iPhone app (#440).
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import TermsModal from './TermsModal';
import api from '../api';
import terms from '../legal/terms.json';
import { startExportDownload } from '../utils/exportData';

jest.mock('../api', () => ({ __esModule: true, default: { post: jest.fn() } }));
jest.mock('../utils/exportData', () => ({ startExportDownload: jest.fn() }));

let mockUser;
jest.mock('../contexts/UserContext', () => ({ useUser: () => ({ user: mockUser }) }));

beforeEach(() => {
  api.post.mockReset();
  startExportDownload.mockReset();
  mockUser = {
    id: 7, username: 'mia', email: 'mia@example.com', approved: true,
    account_deletion: { grace_days: 30, confirm_by_email: true, link_expires_in: 3600 },
  };
});

const renderModal = (props = {}) => render(
  <MemoryRouter><TermsModal onAccepted={() => {}} {...props} /></MemoryRouter>,
);

test('the dialog shows the terms of terms.json, the version included', () => {
  renderModal();
  const title = terms.blocks.find((b) => b.type === 'title').text;
  expect(screen.getByRole('heading', { level: 1, name: title })).toBeTruthy();
  for (const block of terms.blocks.filter((b) => b.type === 'sectionTitle')) {
    expect(screen.getByRole('heading', { level: 2, name: block.text })).toBeTruthy();
  }
  expect(screen.getByText(/Terms version 3\.0/)).toBeTruthy();
});

test('the account deletion sentence is Peter\'s (#269), and nothing says alpha or no refunds', () => {
  const { container } = renderModal();
  expect(screen.getByText(
    'You can delete your account on the Account page. If you change your mind, you can '
    + 'still recover your account within 30 days by signing in; after that it is deleted forever, '
    + 'with everything in it. Copies in our backups are erased within another 30 days. Loore keeps a record of what your AI use '
    + 'cost, without your name.')).toBeTruthy();
  const text = container.textContent;
  // Alpha testers keep their access; Loore itself is no longer called alpha.
  expect(text).not.toMatch(/alpha (software|release|disclaimer)|this is alpha/i);
  expect(text).toMatch(/Beta/);
  expect(text).not.toMatch(/no refunds/i);
  expect(text).not.toMatch(/no account deletion/i);
});

test('links in the dialog open in a new tab, so the dialog stays', () => {
  renderModal();
  const privacy = screen.getAllByRole('link', { name: 'Privacy Policy' });
  expect(privacy.length).toBeGreaterThan(0);
  for (const link of privacy) {
    expect(link.getAttribute('href')).toMatch(/^\/privacy(#\w+)?$/);
    expect(link.getAttribute('target')).toBe('_blank');
  }
});

test('"I Agree" sends the version it showed, and accepts', async () => {
  api.post.mockResolvedValueOnce({ data: {} });
  const onAccepted = jest.fn();
  renderModal({ onAccepted });
  fireEvent.click(screen.getByRole('button', { name: 'I Agree' }));
  await waitFor(() => expect(onAccepted).toHaveBeenCalled());
  expect(api.post).toHaveBeenCalledWith('/terms/accept', { version: '3.0' });
  expect(terms.version).toBe('3.0');
});

test('when the server has newer Terms, nothing is accepted and the page reloads', async () => {
  api.post.mockRejectedValueOnce({
    response: { status: 409, data: { error: 'terms_version_mismatch', current_version: '3.1' } },
  });
  const onAccepted = jest.fn();
  const onOutdated = jest.fn();
  renderModal({ onAccepted, onOutdated });
  fireEvent.click(screen.getByRole('button', { name: 'I Agree' }));
  await waitFor(() => expect(onOutdated).toHaveBeenCalled());
  expect(onAccepted).not.toHaveBeenCalled();
  expect(screen.getByRole('alert').textContent).toMatch(/Terms have changed/);
});

test('another failure leaves the dialog with a retry message', async () => {
  api.post.mockRejectedValueOnce({ response: { status: 500, data: {} } });
  const onOutdated = jest.fn();
  renderModal({ onOutdated });
  fireEvent.click(screen.getByRole('button', { name: 'I Agree' }));
  await waitFor(() => expect(screen.getByRole('alert').textContent)
    .toBe('Error accepting terms. Please try again.'));
  expect(onOutdated).not.toHaveBeenCalled();
});

// Terms §13: a person who doesn't accept can export and delete (#440).
test('without agreeing: export my data, sign out, delete my account', async () => {
  startExportDownload.mockResolvedValueOnce(true);
  renderModal();
  fireEvent.click(screen.getByRole('button', { name: 'Export my data' }));
  await waitFor(() => expect(screen.getByRole('status').textContent)
    .toBe('Your export is downloading.'));
  expect(api.post).not.toHaveBeenCalled();

  expect(screen.getByRole('link', { name: 'Sign out' }).getAttribute('href'))
    .toMatch(/\/auth\/logout$/);

  fireEvent.click(screen.getByRole('button', { name: 'Delete my account…' }));
  // The deletion dialog opens in <body>, above the Terms dialog.
  const typed = await screen.findByRole('textbox');
  const overlay = document.body.lastChild;
  expect(overlay.contains(typed)).toBe(true);
  expect(Number(overlay.style.zIndex)).toBeGreaterThan(10000);
  fireEvent.change(typed, { target: { value: 'mia' } });
  api.post.mockResolvedValueOnce({ data: { status: 'link_sent' } });
  fireEvent.click(screen.getByRole('button', { name: /Email me the confirmation link/ }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith('/account/delete', { confirm: 'mia' }));
  expect(await screen.findByText(/Check your email: Loore sent a confirmation link to mia@example.com/))
    .toBeTruthy();
  expect(api.post).not.toHaveBeenCalledWith('/terms/accept', expect.anything());
});

test('an export that cannot start says so', async () => {
  startExportDownload.mockResolvedValueOnce(false);
  renderModal();
  fireEvent.click(screen.getByRole('button', { name: 'Export my data' }));
  await waitFor(() => expect(screen.getByRole('status').textContent)
    .toBe("The export couldn't start. Please try again."));
});

test('a waitlisted account, which cannot export, is not offered the export', () => {
  mockUser = { ...mockUser, approved: false };
  renderModal();
  expect(screen.queryByRole('button', { name: 'Export my data' })).toBeNull();
  expect(screen.getByRole('button', { name: 'Delete my account…' })).toBeTruthy();
  expect(screen.getByRole('link', { name: 'Sign out' })).toBeTruthy();
});
