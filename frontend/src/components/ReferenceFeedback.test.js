jest.mock('../api', () => ({ post: jest.fn() }));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));

import React from 'react';
import { render, screen } from '@testing-library/react';
import ReferenceFeedback from './ReferenceFeedback';

// The 40×40 tap target itself is CSS (index.css, .ref-feedback-glyph),
// which jsdom does not apply. What the CSS depends on is checked here:
// the icon size reaches --ref-feedback-icon, which sets the pair's
// negative side margins.
test('icons default to 20 px and hand their size to the CSS', () => {
  render(<ReferenceFeedback itemId={5} feedback={null} />);
  const good = screen.getByRole('button', { name: 'Good quote' });
  expect(good.querySelector('svg').getAttribute('width')).toBe('20');
  expect(good.parentElement.style.getPropertyValue('--ref-feedback-icon')).toBe('20px');
});

test('a caller-set size reaches both the icon and the CSS', () => {
  render(<ReferenceFeedback itemId={5} feedback="bad" size={24} />);
  const bad = screen.getByRole('button', { name: 'Bad quote' });
  expect(bad.querySelector('svg').getAttribute('width')).toBe('24');
  expect(bad.parentElement.style.getPropertyValue('--ref-feedback-icon')).toBe('24px');
  expect(bad).toHaveAttribute('aria-pressed', 'true');
});
