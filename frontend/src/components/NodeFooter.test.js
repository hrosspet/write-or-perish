import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NodeFooter from './NodeFooter';

jest.mock('../contexts/UserContext', () => ({
  useUser: () => ({ user: { id: 1, username: 'anna' } }),
}));
jest.mock('./CircleReaders', () => () => null);

const renderFooter = (props) => render(
  <MemoryRouter>
    <NodeFooter username="claude-fable-5.2" createdAt="2026-11-11T10:00:00Z"
      childrenCount={0} {...props} />
  </MemoryRouter>,
);

test('an AI reply in a shared thread shows the label in place of the model id', () => {
  renderFooter({
    llmModel: 'claude-fable-5.2',
    llmLabel: 'Claude Fable 5.2 via @ben',
    humanOwnerUsername: 'ben',
  });
  const link = screen.getByRole('link', { name: 'Claude Fable 5.2 via @ben' });
  expect(link).toHaveAttribute('href', '/dashboard/ben');
  expect(screen.queryByText('claude-fable-5.2')).toBeNull();
});

test('the label of my own AI reply links to my own page', () => {
  renderFooter({
    llmModel: 'claude-fable-5.2',
    llmLabel: 'Claude Fable 5.2 via @anna',
    humanOwnerUsername: 'anna',
  });
  expect(screen.getByRole('link', { name: 'Claude Fable 5.2 via @anna' }))
    .toHaveAttribute('href', '/dashboard');
});

test('without a label an AI reply shows its model, as before', () => {
  renderFooter({ llmModel: 'claude-fable-5.2', humanOwnerUsername: 'anna' });
  expect(screen.getByRole('link', { name: 'claude-fable-5.2' })).toBeInTheDocument();
});
