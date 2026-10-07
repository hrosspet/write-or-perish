// Moving between nodes keeps the current page, with a spinner, until the
// next node is fetched: after a click (the address changes once the node is
// in) and after any other move (the address changes first: a reply that
// continues on a new node, the back button).
jest.mock('../api', () => ({ get: jest.fn() }));
jest.mock('./NodeDetail', () => {
  const React = require('react');
  const { useNavigate, useLocation } = require('react-router-dom');
  const { takePrefetchedNode } = require('../hooks/useNodePrefetch');
  return function MockNodeDetail({ nodeId, openNode }) {
    const navigate = useNavigate();
    const { pathname } = useLocation();
    // As NodeDetail does on mount: the page takes its prefetched request.
    React.useEffect(() => { takePrefetchedNode(nodeId); }, [nodeId]);
    const button = (label, onClick) => React.createElement('button', { onClick }, label);
    return React.createElement('div', null,
      React.createElement('div', { 'data-testid': 'shown' }, nodeId),
      React.createElement('div', { 'data-testid': 'address' }, pathname),
      button('click 2', () => openNode(2, () => navigate('/node/2'))),
      button('click 1', () => openNode(1, () => navigate('/node/1'))),
      button('auto 3', () => navigate('/node/3?awaitLlm=3')),
      button('back', () => navigate(-1)));
  };
});

import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import api from '../api';
import NodeDetailWrapper from './NodeDetailWrapper';
import { MAX_WAIT_MS } from '../hooks/useNodePrefetch';

// Requests that answer when the test says so, and reject when aborted.
let calls;
beforeEach(() => {
  jest.useFakeTimers();
  calls = [];
  api.get.mockReset();
  api.get.mockImplementation((url, { signal }) => new Promise((resolve, reject) => {
    calls.push({ url, resolve, signal });
    signal.addEventListener('abort', () => reject(new Error('canceled')));
  }));
});

afterEach(() => {
  jest.useRealTimers();
});

const renderAt = (path) => render(
  <MemoryRouter initialEntries={[path]}>
    <Routes><Route path="/node/:id" element={<NodeDetailWrapper />} /></Routes>
  </MemoryRouter>,
);

const shown = () => screen.getByTestId('shown').textContent;
const address = () => screen.getByTestId('address').textContent;
const spinner = () => screen.queryByRole('status', { name: 'Loading node' });

test('a move that changes the address first keeps the page until the node is in', async () => {
  renderAt('/node/1');
  fireEvent.click(screen.getByText('auto 3'));
  expect(address()).toBe('/node/3');
  expect(shown()).toBe('1');
  expect(spinner()).not.toBeNull();
  expect(calls.map((c) => c.url)).toEqual(['/nodes/3']);

  await act(async () => { calls[0].resolve({ data: { id: 3 } }); });
  expect(shown()).toBe('3');
  expect(spinner()).toBeNull();
  expect(api.get).toHaveBeenCalledTimes(1);
});

test('a slow node opens after MAX_WAIT_MS', async () => {
  renderAt('/node/1');
  fireEvent.click(screen.getByText('auto 3'));
  await act(async () => { jest.advanceTimersByTime(MAX_WAIT_MS); });
  expect(shown()).toBe('3');
  expect(spinner()).toBeNull();
  expect(calls[0].signal.aborted).toBe(false);
});

test('going back before the node is in stays on the shown page', async () => {
  renderAt('/node/1');
  fireEvent.click(screen.getByText('auto 3'));
  fireEvent.click(screen.getByText('back'));
  expect(address()).toBe('/node/1');
  expect(shown()).toBe('1');
  expect(spinner()).toBeNull();
  expect(calls[0].signal.aborted).toBe(true);

  await act(async () => { jest.advanceTimersByTime(MAX_WAIT_MS); });
  expect(shown()).toBe('1');
});

test('a click fetches first; the address changes once, with the node', async () => {
  renderAt('/node/1');
  fireEvent.click(screen.getByText('click 2'));
  expect(address()).toBe('/node/1');
  expect(shown()).toBe('1');
  expect(spinner()).not.toBeNull();

  await act(async () => { calls[0].resolve({ data: { id: 2 } }); });
  expect(address()).toBe('/node/2');
  expect(shown()).toBe('2');
  expect(spinner()).toBeNull();
  // The page took the click's request: no second fetch.
  expect(api.get).toHaveBeenCalledTimes(1);
});

test('a move by other means drops a click still loading', async () => {
  renderAt('/node/1');
  fireEvent.click(screen.getByText('click 2'));
  fireEvent.click(screen.getByText('auto 3'));
  expect(calls[0].signal.aborted).toBe(true);

  await act(async () => { calls[1].resolve({ data: { id: 3 } }); });
  expect(shown()).toBe('3');
  expect(address()).toBe('/node/3');
});

test('a click on the node already shown just navigates, without a fetch', () => {
  renderAt('/node/1');
  fireEvent.click(screen.getByText('click 1'));
  expect(api.get).not.toHaveBeenCalled();
  expect(spinner()).toBeNull();
  expect(shown()).toBe('1');
});
