import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import ModelSelector, { pickerOptions } from './ModelSelector';
import api from '../api';

jest.mock('../api', () => ({ get: jest.fn() }));

const MODELS = [
  { id: 'gpt-6-astra', name: 'GPT-6 Astra', provider: 'openai', featured: true, read: false },
  { id: 'gpt-6-luna', name: 'GPT-6 Luna', provider: 'openai', featured: false, read: true },
  { id: 'gpt-5.6-luna', name: 'GPT-5.6 Luna', provider: 'openai', featured: false, read: true },
  { id: 'claude-opus-5.5', name: 'Opus 5.5', provider: 'anthropic', featured: true, read: false },
  { id: 'claude-fable-5.1', name: 'Fable 5.1', provider: 'anthropic', featured: false, read: false },
  { id: 'claude-opus-4.6', name: 'Opus 4.6', provider: 'anthropic', featured: true, read: false },
];
const ids = (list) => list.map((m) => m.id);

describe('pickerOptions', () => {
  it('shows the featured models, Anthropic first, then More', () => {
    const o = pickerOptions(MODELS, 'claude-opus-4.6', 'chat', false);
    expect(ids(o.models)).toEqual(['claude-opus-5.5', 'claude-opus-4.6', 'gpt-6-astra']);
    expect(o.more).toBe(true);
  });

  it('puts a selected model that is not featured first, once', () => {
    const o = pickerOptions(MODELS, 'claude-fable-5.1', 'chat', false);
    expect(ids(o.models)).toEqual(
      ['claude-fable-5.1', 'claude-opus-5.5', 'claude-opus-4.6', 'gpt-6-astra']);
  });

  it('expands to every model grouped by provider', () => {
    const o = pickerOptions(MODELS, 'claude-opus-4.6', 'chat', true);
    expect(o.groups.map((g) => g.label)).toEqual(['Anthropic', 'OpenAI']);
    expect(ids(o.groups[1].models)).toEqual(['gpt-6-astra', 'gpt-6-luna', 'gpt-5.6-luna']);
  });

  it('offers only read models for Read, with no More', () => {
    const o = pickerOptions(MODELS, 'gpt-6-luna', 'read', false);
    expect(ids(o.models)).toEqual(['gpt-6-luna', 'gpt-5.6-luna']);
    expect(o.more).toBeUndefined();
  });
});

describe('ModelSelector', () => {
  const mockApi = (suggestion) => {
    api.get.mockImplementation((url) => Promise.resolve({
      data: url === '/nodes/models' ? { models: MODELS } : suggestion,
    }));
  };
  const trigger = (name = /^Model:/) => screen.getByRole('button', { name });
  const optionNames = () => screen.getAllByRole('option').map((o) => o.textContent);

  afterEach(() => jest.clearAllMocks());

  it('asks for the read default and replaces a chat model with it', async () => {
    mockApi({ suggested_model: 'gpt-6-luna', source: 'default' });
    const onChange = jest.fn();
    render(<ModelSelector nodeId={7} purpose="read" selectedModel="claude-opus-4.6"
      onModelChange={onChange} />);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('gpt-6-luna'));
    expect(api.get).toHaveBeenCalledWith('/nodes/7/suggested-model', { params: { purpose: 'read' } });
  });

  it('replaces a selection no picker offers (a deprecated preference)', async () => {
    mockApi({ suggested_model: 'claude-opus-4.6', source: 'default' });
    const onChange = jest.fn();
    render(<ModelSelector nodeId={null} selectedModel="claude-opus-5" onModelChange={onChange} />);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('claude-opus-4.6'));
  });

  it('keeps an offered selection over a non-thread default', async () => {
    mockApi({ suggested_model: 'claude-opus-4.6', source: 'user_preference' });
    const onChange = jest.fn();
    render(<ModelSelector nodeId={3} selectedModel="gpt-6-astra" onModelChange={onChange} />);
    await waitFor(() => expect(trigger(/GPT-6 Astra/)).not.toBeDisabled());
    expect(onChange).not.toHaveBeenCalled();
  });

  it('More models grows the open list in place, and a pick closes it', async () => {
    mockApi({ suggested_model: 'claude-opus-4.6', source: 'default' });
    const onChange = jest.fn();
    render(<ModelSelector nodeId={3} selectedModel="claude-opus-4.6" onModelChange={onChange} />);
    await waitFor(() => expect(trigger(/Opus 4.6/)).not.toBeDisabled());
    fireEvent.click(trigger(/Opus 4.6/));
    expect(optionNames()).toEqual(['Opus 5.5', 'Opus 4.6', 'GPT-6 Astra', 'More models…']);

    fireEvent.click(screen.getByRole('option', { name: 'More models…' }));
    // Still open, now the whole grouped list, and nothing was chosen.
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    expect(screen.getAllByRole('group')).toHaveLength(2);
    expect(optionNames()).toContain('Fable 5.1');
    expect(optionNames()).not.toContain('More models…');
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('option', { name: 'Fable 5.1' }));
    expect(onChange).toHaveBeenCalledWith('claude-fable-5.1');
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('works from the keyboard: open, End, Enter expands, Enter chooses', async () => {
    mockApi({ suggested_model: 'claude-opus-4.6', source: 'default' });
    const onChange = jest.fn();
    render(<ModelSelector nodeId={3} selectedModel="claude-opus-4.6" onModelChange={onChange} />);
    await waitFor(() => expect(trigger(/Opus 4.6/)).not.toBeDisabled());
    fireEvent.keyDown(trigger(/Opus 4.6/), { key: 'ArrowDown' });
    const list = screen.getByRole('listbox');
    fireEvent.keyDown(list, { key: 'End' });
    fireEvent.keyDown(list, { key: 'Enter' });
    expect(screen.getAllByRole('group')).toHaveLength(2);
    // The keyboard lands on the first model the expansion added.
    expect(list.getAttribute('aria-activedescendant')).toMatch(/claude-fable-5\.1$/);
    fireEvent.keyDown(list, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('claude-fable-5.1');
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('closes on a click outside without choosing', async () => {
    mockApi({ suggested_model: 'claude-opus-4.6', source: 'default' });
    const onChange = jest.fn();
    render(<ModelSelector nodeId={3} selectedModel="claude-opus-4.6" onModelChange={onChange} />);
    await waitFor(() => expect(trigger(/Opus 4.6/)).not.toBeDisabled());
    fireEvent.click(trigger(/Opus 4.6/));
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('listbox')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('stays shut while disabled', async () => {
    mockApi({ suggested_model: 'claude-opus-4.6', source: 'default' });
    render(<ModelSelector nodeId={3} selectedModel="claude-opus-4.6" onModelChange={jest.fn()}
      disabled />);
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    fireEvent.click(trigger(/Opus 4.6|loading/));
    expect(screen.queryByRole('listbox')).toBeNull();
  });
});
