import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api';

// Opening a node from its thread (an ancestor, a reply, a quote): the node is
// fetched while the current page stays, and its page opens with the data in
// hand, so there is no "Loading node..." page in between (NodeDetail is keyed
// by the node id, so each node mounts afresh). A slow or failed answer opens
// the page anyway, which then loads as before. While it waits, `pending` is
// true (the page shows a spinner); another click meanwhile replaces the first,
// so a misclick can be corrected before its page opens. The iOS app does the
// same (NodePrefetch.swift).

// Longest a click waits for the node before its page opens (heuristic).
export const MAX_WAIT_MS = 2000;
// How long a fetched node may be handed to its page (heuristic).
export const FRESH_FOR_MS = 10000;

const fetched = new Map();

// The fetched node for a page that is mounting, if fresh. Pure, so it can
// seed a state initializer (StrictMode calls those twice).
export function peekPrefetchedNode(id) {
  const entry = fetched.get(String(id));
  return entry && Date.now() - entry.at < FRESH_FOR_MS ? entry.data : null;
}

// Hands the fetched node out once: a later visit (the back button) fetches
// afresh.
export function takePrefetchedNode(id) {
  const data = peekPrefetchedNode(id);
  fetched.delete(String(id));
  return data;
}

export default function useNodePrefetch() {
  const [pending, setPending] = useState(false);
  const cancelRef = useRef(null);

  const cancel = useCallback(() => {
    if (cancelRef.current) cancelRef.current();
    cancelRef.current = null;
  }, []);

  // Leaving the page another way (a link, the back button) drops the click.
  useEffect(() => cancel, [cancel]);

  // Fetches `nodeId` (waiting at most MAX_WAIT_MS), then calls `open`. A
  // newer call cancels this one.
  const openNode = useCallback((nodeId, open) => {
    cancel();
    setPending(true);
    const controller = new AbortController();
    let done = false;
    let timer = null;
    const stop = () => {
      done = true;
      clearTimeout(timer);
      controller.abort();
    };
    const finish = (data) => {
      if (done) return;
      stop();
      cancelRef.current = null;
      if (data) fetched.set(String(nodeId), { data, at: Date.now() });
      setPending(false);
      open();
    };
    cancelRef.current = stop;
    timer = setTimeout(() => finish(null), MAX_WAIT_MS);
    api.get(`/nodes/${nodeId}`, { signal: controller.signal })
      .then((response) => finish(response.data), () => finish(null));
  }, [cancel]);

  return { pending, openNode };
}
