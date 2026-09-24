jest.mock('../api', () => ({ post: jest.fn() }));
jest.mock('../contexts/ToastContext', () => ({ useToast: () => ({ addToast: jest.fn() }) }));

import React from 'react';
import { render, screen } from '@testing-library/react';
import ReferenceFeedback from './ReferenceFeedback';

// The tap target itself is CSS (index.css, .ref-feedback-glyph), which
// jsdom does not apply. What the CSS depends on is checked here: the
// icon size reaches --ref-feedback-icon, which sets the target's width.
test('icons default to 16 px and hand their size to the CSS', () => {
  render(<ReferenceFeedback itemId={5} feedback={null} />);
  const good = screen.getByRole('button', { name: 'Good quote' });
  expect(good.querySelector('svg').getAttribute('width')).toBe('16');
  expect(good.parentElement.style.getPropertyValue('--ref-feedback-icon')).toBe('16px');
});

test('a caller-set size reaches both the icon and the CSS', () => {
  render(<ReferenceFeedback itemId={5} feedback="bad" size={18} />);
  const bad = screen.getByRole('button', { name: 'Bad quote' });
  expect(bad.querySelector('svg').getAttribute('width')).toBe('18');
  expect(bad.parentElement.style.getPropertyValue('--ref-feedback-icon')).toBe('18px');
  expect(bad).toHaveAttribute('aria-pressed', 'true');
});
