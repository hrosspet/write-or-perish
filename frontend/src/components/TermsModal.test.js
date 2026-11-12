// The acceptance dialog shows frontend/src/legal/terms.json, the same text
// as the public /terms page and the iPhone app (#440).
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import TermsModal from './TermsModal';
import api from '../api';
import terms from '../legal/terms.json';

jest.mock('../api', () => ({ __esModule: true, default: { post: jest.fn() } }));

const renderModal = (onAccepted = () => {}) => render(
  <MemoryRouter><TermsModal onAccepted={onAccepted} /></MemoryRouter>,
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

test('"I Agree" accepts the terms', async () => {
  api.post.mockResolvedValueOnce({ data: {} });
  const onAccepted = jest.fn();
  renderModal(onAccepted);
  fireEvent.click(screen.getByRole('button', { name: 'I Agree' }));
  await waitFor(() => expect(onAccepted).toHaveBeenCalled());
  expect(api.post).toHaveBeenCalledWith('/terms/accept');
});
