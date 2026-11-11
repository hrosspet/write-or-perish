import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import {
  AddCircleMemberDialog, CircleReplyDialog, accentButton, plainButton,
} from './CircleDialogs';

describe('adding a person to a circle', () => {
  const renderAdd = (props = {}) => {
    const onChoice = jest.fn();
    const onClose = jest.fn();
    render(
      <AddCircleMemberDialog
        open name="ben" circleName="Close friends" sharedCount={3}
        onChoice={onChoice} onClose={onClose} {...props}
      />,
    );
    return { onChoice, onClose };
  };

  test('the text says new entries first, then the earlier ones', () => {
    renderAdd();
    expect(screen.getByRole('heading', { name: 'Add @ben to Close friends?' }))
      .toBeInTheDocument();
    expect(screen.getByText(
      '@ben will read every entry you share with Close friends from now on. '
      + 'The 3 entries you have already shared with it stay closed to @ben '
      + 'unless you choose "Also show earlier entries".',
    )).toBeInTheDocument();
    expect(screen.getByText(/@ben gets a note in Loore the first time/)).toBeInTheDocument();
  });

  test('one earlier entry is written in the singular', () => {
    renderAdd({ sharedCount: 1 });
    expect(screen.getByText(
      '@ben will read every entry you share with Close friends from now on. '
      + 'The entry you have already shared with it stays closed to @ben '
      + 'unless you choose "Also show earlier entries".',
    )).toBeInTheDocument();
  });

  test('the emphasized button adds the person for new entries only', () => {
    const { onChoice } = renderAdd();
    const add = screen.getByRole('button', { name: 'Add @ben' });
    // jsdom drops var() colours from inline styles, so the accent button
    // is told from the plain one by its weight and padding.
    expect(add.style.fontWeight).toBe(String(accentButton.fontWeight));
    expect(add.style.padding).toBe('0px 24px');
    expect(accentButton.padding).toBe('0 24px');
    expect(accentButton.fontWeight).not.toBe(plainButton.fontWeight);
    fireEvent.click(add);
    expect(onChoice).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith(false);
  });

  test('the plain button also opens the earlier entries', () => {
    const { onChoice } = renderAdd();
    const earlier = screen.getByRole('button', { name: 'Also show earlier entries' });
    expect(earlier.style.fontWeight).toBe(String(plainButton.fontWeight));
    expect(earlier.style.padding).toBe('0px 20px');
    expect(plainButton.padding).toBe('0 20px');
    fireEvent.click(earlier);
    expect(onChoice).toHaveBeenCalledTimes(1);
    expect(onChoice).toHaveBeenCalledWith(true);
  });

  test('with nothing shared yet there are no earlier entries to offer', () => {
    const { onChoice } = renderAdd({ sharedCount: 0 });
    expect(screen.getByText(
      '@ben will read every entry you share with Close friends from now on. '
      + 'You have not shared anything with it yet.',
    )).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Also show earlier entries' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Add @ben' }));
    expect(onChoice).toHaveBeenCalledWith(false);
  });

  test('Cancel adds nobody', () => {
    const { onChoice, onClose } = renderAdd();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
    expect(onChoice).not.toHaveBeenCalled();
  });
});

describe('the dialog before a reply in a shared thread', () => {
  test('a typed reply', () => {
    const onConfirm = jest.fn();
    render(
      <CircleReplyDialog open names={['anna', 'chidi']}
        onClose={() => {}} onConfirm={onConfirm} />,
    );
    expect(screen.getByRole('heading', { name: 'Send this reply?' })).toBeInTheDocument();
    expect(screen.getByText('@anna and @chidi will see it, with your name.'))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Send reply' }));
    expect(onConfirm).toHaveBeenCalledWith(false);
  });

  test('an AI reply', () => {
    const onConfirm = jest.fn();
    const onClose = jest.fn();
    render(
      <CircleReplyDialog open ai names={['anna', 'chidi']}
        onClose={onClose} onConfirm={onConfirm} />,
    );
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' }))
      .toBeInTheDocument();
    expect(screen.getByText('@anna and @chidi will see it, marked with your name.'))
      .toBeInTheDocument();
    expect(screen.queryByText(/Your AI reads/)).toBeNull();
    fireEvent.click(screen.getByLabelText("Don't ask again in this thread"));
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
    expect(onConfirm).toHaveBeenCalledWith(true);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
  });

  test('an AI reply whose prompt reads the requester\'s artifacts says which', () => {
    render(
      <CircleReplyDialog open ai names={['anna', 'chidi']}
        aiReads={['profile', 'memory', 'to-do list']}
        onClose={() => {}} onConfirm={() => {}} />,
    );
    expect(screen.getByText(
      '@anna and @chidi will see it, marked with your name. '
      + 'Your AI reads your profile, memory and to-do list for this reply '
      + 'and could share private information from them.',
    )).toBeInTheDocument();
  });

  test('an empty list of artifacts adds no sentence', () => {
    render(
      <CircleReplyDialog open ai names={['anna']} aiReads={[]}
        onClose={() => {}} onConfirm={() => {}} />,
    );
    expect(screen.getByText('@anna will see it, marked with your name.'))
      .toBeInTheDocument();
    expect(screen.queryByText(/Your AI reads/)).toBeNull();
  });

  test('an AI reply for its requester only names nobody and says what the shared prompt reads', () => {
    const onConfirm = jest.fn();
    render(
      <CircleReplyDialog open ai onlyMe names={['anna', 'chidi']}
        aiReads={['profile', 'memory', 'to-do list']}
        onClose={() => {}} onConfirm={onConfirm} />,
    );
    expect(screen.getByRole('heading', { name: 'Generate an AI reply?' })).toBeInTheDocument();
    expect(screen.getByText(
      'Only you will see it. Your AI reads your profile, memory and to-do list '
      + 'for this reply, as the prompt shared in this thread asks.',
    )).toBeInTheDocument();
    expect(screen.queryByText(/@anna/)).toBeNull();
    expect(screen.queryByText(/could share private information/)).toBeNull();
    fireEvent.click(screen.getByLabelText("Don't ask again in this thread"));
    fireEvent.click(screen.getByRole('button', { name: 'Generate' }));
    expect(onConfirm).toHaveBeenCalledWith(true);
  });

  test('a typed reply never carries the artifacts sentence', () => {
    render(
      <CircleReplyDialog open names={['anna']} aiReads={['profile']}
        onClose={() => {}} onConfirm={() => {}} />,
    );
    expect(screen.getByText('@anna will see it, with your name.')).toBeInTheDocument();
    expect(screen.queryByText(/Your AI reads/)).toBeNull();
  });
});
