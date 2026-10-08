import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api';

// Moving from one node's page to another (NodeDetailWrapper): the node is
// fetched while the current page stays, and its page opens with the data in
// hand, so there is no "Loading node..." page in between (NodeDetail is keyed
// by the node id, so each node mounts afresh). A slow answer opens the page
// anyway, and the page waits for the same request (it is not sent twice); a
// failed one opens the page, which shows its error. The iOS app does the
// same (NodePrefetch.swift).

// Longest a move waits for the node before its page opens (heuristic).
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

// A fetch is waiting for this node's page (its data may still be on the way).
export function hasPrefetchedNode(id) {
  return freshEntry(id) != null;
}

// The fetched node for a page that is mounting, if it arrived and is fresh.
// Pure, so it can seed a state initializer (StrictMode calls those twice).
export function peekPrefetchedNode(id) {
  const entry = freshEntry(id);
  return entry ? entry.data : null;
}

// The GET's promise for a page that is mounting (settled, or still in flight
// after a slow answer), or null. Handed out once: a later visit fetches
// afresh.
export function takePrefetchedNode(id) {
  const entry = freshEntry(id);
  fetched.delete(String(id));
  return entry ? entry.request : null;
}

// Fetches `nodeId` for the page about to open, and calls `onReady` once the
// node arrived, failed, or MAX_WAIT_MS passed; the request then waits for
// that page (takePrefetchedNode). Returns a cancel function, which aborts the
// request only before `onReady`.
export function prefetchNode(nodeId, onReady) {
  const controller = new AbortController();
  let done = false;
  let timer = null;
  const request = api.get(`/nodes/${nodeId}`, { signal: controller.signal });
  const finish = (data) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    fetched.set(String(nodeId), { request, data, at: Date.now() });
    onReady();
  };
  timer = setTimeout(() => finish(null), MAX_WAIT_MS);
  request.then((response) => finish(response.data), () => finish(null));
  return () => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    controller.abort();
  };
}

// A click on a node: fetch it first, then `open` (which changes the
// address), so the address and the history only get the node that opens.
// While it waits, `pending` is true; a click on another node meanwhile
// replaces the first, so a misclick can be corrected before its page opens;
// the same node again (a double click) leaves the first request running.
export default function useNodePrefetch() {
  const [pending, setPending] = useState(false);
  const pendingIdRef = useRef(null);
  const cancelRef = useRef(null);

  const cancel = useCallback(() => {
    if (cancelRef.current) cancelRef.current();
    cancelRef.current = null;
    pendingIdRef.current = null;
    setPending(false);
  }, []);

  // Leaving the node pages drops the click.
  useEffect(() => cancel, [cancel]);

  const openNode = useCallback((nodeId, open) => {
    if (pendingIdRef.current === String(nodeId)) return;
    cancel();
    pendingIdRef.current = String(nodeId);
    setPending(true);
    cancelRef.current = prefetchNode(nodeId, () => {
      cancelRef.current = null;
      pendingIdRef.current = null;
      setPending(false);
      open();
    });
  }, [cancel]);

  return { pending, openNode, cancel };
}
