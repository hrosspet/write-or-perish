// The navbar's first link is "Home" (#436, Peter 2026-10-09): the home
// page has a Reflect card of its own now, so the link that leads there
// keeps its destination and gets another word.
jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'ana', approved: true }, setUser: jest.fn() }),
}));
jest.mock('../contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', toggleTheme: jest.fn() }),
}));
jest.mock('../contexts/ToastContext', () => ({
  useToast: () => ({ addToast: jest.fn() }),
}));
jest.mock('../api', () => ({
  __esModule: true,
  default: { get: jest.fn(() => new Promise(() => {})), post: jest.fn(), put: jest.fn() },
}));
jest.mock('./GlobalAudioPlayer', () => () => null);
jest.mock('./CraftModeDialog', () => () => null);

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NavBar from './NavBar';

test('the first link is "Home" and goes to the home page', () => {
  render(<MemoryRouter initialEntries={['/']}><NavBar /></MemoryRouter>);
  const home = screen.getByRole('link', { name: 'Home' });
  expect(home).toHaveAttribute('href', '/');
  expect(screen.queryByRole('link', { name: 'Reflect' })).toBeNull();
});
