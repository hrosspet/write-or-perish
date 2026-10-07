import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api';

// Opening a node from its thread (an ancestor, a reply, a quote): the node is
// fetched while the current page stays, and its page opens with the data in
// hand, so there is no "Loading node..." page in between (NodeDetail is keyed
// by the node id, so each node mounts afresh). A slow answer opens the page
// anyway, and the page waits for the same request (it is not sent twice); a
// failed one opens the page, which shows its error. While it waits, `pending`
// is true (the page shows a spinner); a click on another node meanwhile
// replaces the first, so a misclick can be corrected before its page opens.
// The iOS app does the same (NodePrefetch.swift).

// Longest a click waits for the node before its page opens (heuristic).
export const MAX_WAIT_MS = 2000;
// How long a fetched node may be handed to its page (heuristic).
export const FRESH_FOR_MS = 10000;

// id -> { request, data, at }: `request` is the GET's promise; `data` is set
// when the node arrived before the page opened.
const fetched = new Map();

const freshEntry = (id) => {
  const entry = fetched.get(String(id));
  return entry && Date.now() - entry.at < FRESH_FOR_MS ? entry : null;
};

// The fetched node for a page that is mounting, if it arrived and is fresh.
// Pure, so it can seed a state initializer (StrictMode calls those twice).
export function peekPrefetchedNode(id) {
  const entry = freshEntry(id);
  return entry ? entry.data : null;
}

// The GET's promise for a page that is mounting (settled, or still in flight
// after a slow answer), or null. Handed out once: a later visit (the back
// button) fetches afresh.
export function takePrefetchedNode(id) {
  const entry = freshEntry(id);
  fetched.delete(String(id));
  return entry ? entry.request : null;
}

export default function useNodePrefetch() {
  const [pending, setPending] = useState(false);
  const pendingIdRef = useRef(null);
  const cancelRef = useRef(null);

  const cancel = useCallback(() => {
    if (cancelRef.current) cancelRef.current();
    cancelRef.current = null;
    pendingIdRef.current = null;
  }, []);

  // Leaving the page another way (a link, the back button) drops the click.
  useEffect(() => cancel, [cancel]);

  // Fetches `nodeId` (waiting at most MAX_WAIT_MS), then calls `open`. A
  // click on another node cancels this one; the same node again (a double
  // click) leaves it running.
  const openNode = useCallback((nodeId, open) => {
    if (pendingIdRef.current === String(nodeId)) return;
    cancel();
    pendingIdRef.current = String(nodeId);
    setPending(true);
    const controller = new AbortController();
    let done = false;
    let timer = null;
    const request = api.get(`/nodes/${nodeId}`, { signal: controller.signal });
    const finish = (data) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      cancelRef.current = null;
      pendingIdRef.current = null;
      fetched.set(String(nodeId), { request, data, at: Date.now() });
      setPending(false);
      open();
    };
    cancelRef.current = () => {
      done = true;
      clearTimeout(timer);
      controller.abort();
    };
    timer = setTimeout(() => finish(null), MAX_WAIT_MS);
    request.then((response) => finish(response.data), () => finish(null));
  }, [cancel]);

  return { pending, openNode };
}
