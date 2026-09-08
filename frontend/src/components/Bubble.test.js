// Bubble's footer pulls in react-router + the user context, and
// MarkdownBody pulls in react-markdown (ESM, untransformed by CRA jest).
// None of that matters for the title/body logic under test.
jest.mock('./MarkdownBody', () => () => null);
jest.mock('./NodeFooter', () => () => null);
jest.mock('./BubbleKebabMenu', () => () => null);

import React from 'react';
import { render, screen } from '@testing-library/react';
import Bubble, { splitPreview } from './Bubble';

const card = (overrides) => ({
  id: 1,
  preview: '# 2026-09-05 12:12:01 Voice note\nFirst line of the transcript.',
  created_at: '2026-09-05T12:12:01Z',
  username: 'v',
  child_count: 0,
  ...overrides,
});

test('splitPreview strips the heading marker and separates body', () => {
  expect(splitPreview('# Title\nBody line')).toEqual({ title: 'Title', body: 'Body line' });
  expect(splitPreview('Just one line')).toEqual({ title: 'Just one line', body: '' });
  expect(splitPreview('')).toEqual({ title: '', body: '' });
});

test('without a thread name the first line is the title', () => {
  render(<Bubble node={card()} />);
  expect(screen.getByText('2026-09-05 12:12:01 Voice note')).toBeInTheDocument();
  expect(screen.getByText('First line of the transcript.')).toBeInTheDocument();
});

test('a thread name takes the title slot and the first line is skipped', () => {
  render(<Bubble node={card({ thread_name: 'Teplárna plan' })} />);
  expect(screen.getByText('Teplárna plan')).toBeInTheDocument();
  expect(screen.queryByText('2026-09-05 12:12:01 Voice note')).not.toBeInTheDocument();
  expect(screen.getByText('First line of the transcript.')).toBeInTheDocument();
});

test('a named one-line entry keeps its line as the body', () => {
  render(<Bubble node={card({ preview: 'Quick thought about X', thread_name: 'Thoughts on X' })} />);
  expect(screen.getByText('Thoughts on X')).toBeInTheDocument();
  expect(screen.getByText('Quick thought about X')).toBeInTheDocument();
});

test('a blank thread name falls back to the title', () => {
  render(<Bubble node={card({ thread_name: '   ' })} />);
  expect(screen.getByText('2026-09-05 12:12:01 Voice note')).toBeInTheDocument();
});
