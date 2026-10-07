import React, { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import NodeDetail from './NodeDetail';
import NodeOpeningSpinner from './NodeOpeningSpinner';
import useNodePrefetch, { hasPrefetchedNode, prefetchNode } from '../hooks/useNodePrefetch';

// nodeIdOverride: set by the permalink route (/u/:username/:slug), which
// resolves the slug itself — the pretty URL stays in the address bar.
//
// Moving to another node keeps the current page, with a spinner, until the
// node is fetched; its page then opens with the data in hand, so there is no
// "Loading node..." page in between. A click on a node fetches it before the
// address changes (openNode). Every other move changes the address first (a
// reply that finished or continues on a new node, a sent entry, a delete, the
// back and forward buttons); the page then stays on the node it shows until
// the new one is in.
const NodeDetailWrapper = ({ nodeIdOverride }) => {
  const { id } = useParams();
  const target = String(nodeIdOverride || id);
  const [shown, setShown] = useState(target);
  const { pending: clickPending, openNode, cancel: cancelClick } = useNodePrefetch();

  // Fetched already (by the click that changed the address): open it now.
  if (target !== shown && hasPrefetchedNode(target)) setShown(target);

  // The address moved: a click still loading is dropped, and the page stays
  // on the shown node while the new one is fetched.
  useEffect(() => { cancelClick(); }, [target, cancelClick]);
  useEffect(() => {
    if (target === shown) return undefined;
    return prefetchNode(target, () => setShown(target));
  }, [target, shown]);

  // A click on the node already shown (a quote of itself) just navigates:
  // its data would otherwise wait unused and be shown on a later visit. A
  // click on the node the address is already moving to waits for that move.
  const open = useCallback((nodeId, go) => {
    const clicked = String(nodeId);
    if (clicked === target && target !== shown) return;
    if (clicked === shown) go();
    else openNode(nodeId, go);
  }, [shown, target, openNode]);

  const moving = clickPending || target !== shown;
  return (
    <>
      <NodeDetail key={shown} nodeId={shown} openNode={open} moving={moving} />
      {moving && <NodeOpeningSpinner />}
    </>
  );
};

export default NodeDetailWrapper;
