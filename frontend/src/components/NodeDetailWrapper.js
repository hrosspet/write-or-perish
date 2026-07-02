import React from 'react';
import { useParams } from 'react-router-dom';
import NodeDetail from './NodeDetail';

// nodeIdOverride: set by the permalink route (/u/:username/:slug), which
// resolves the slug itself — the pretty URL stays in the address bar.
const NodeDetailWrapper = ({ nodeIdOverride }) => {
  const { id } = useParams();
  const effective = nodeIdOverride || id;
  return <NodeDetail key={effective} nodeIdOverride={nodeIdOverride} />;
};

export default NodeDetailWrapper;
