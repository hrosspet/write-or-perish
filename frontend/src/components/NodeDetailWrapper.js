import React from 'react';
import { useParams } from 'react-router-dom';
import NodeDetail from './NodeDetail';

const NodeDetailWrapper = () => {
  const { id } = useParams();
  return <NodeDetail key={id} />;
};

export default NodeDetailWrapper;
