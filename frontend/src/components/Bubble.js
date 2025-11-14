import React from 'react';
import NodeFooter from './NodeFooter';

const Bubble = ({ node, onClick, isHighlighted = false, leftAlign = false }) => {
  // Use full content if available; otherwise use preview.
  const text = node.content || node.preview || "";

  // Compute children count – use node.child_count if available, else fallback to node.children.length.
  const childrenCount = typeof node.child_count !== "undefined"
    ? node.child_count
    : (node.children ? node.children.length : 0);

  const bubbleStyle = {
    padding: "15px",
    margin: leftAlign ? "10px 0" : "10px auto",
    background: "#1e1e1e",
    border: isHighlighted ? "2px solid #61dafb" : "1px solid #333",
    borderRadius: "8px",
    cursor: "pointer",
    whiteSpace: "pre-wrap",
    maxWidth: "1000px",
    width: "calc(100% - 20px)"
  };

  return (
    <div style={bubbleStyle} onClick={() => onClick(node.id)}>
      <div>
        {text.length > 250 ? text.substring(0, 250) + "..." : text}
      </div>
      {/* Stop propagation in footer so clicks on the Link (author handle) don’t trigger the bubble’s onClick */}
      <div onClick={(e) => e.stopPropagation()}>
        <NodeFooter
          username={node.username}
          createdAt={node.created_at}
          childrenCount={childrenCount}
        />
      </div>
    </div>
  );
};

export default Bubble;