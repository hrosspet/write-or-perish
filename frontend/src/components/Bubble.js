import React from 'react';
import NodeFooter from './NodeFooter';

const Bubble = ({ node, onClick, isHighlighted = false, leftAlign = false }) => {
  // Use full content if available; otherwise use preview.
  const text = node.content || node.preview || "";

  // Strip leading "# " from title for cleaner display
  const displayText = text.replace(/^#\s+/, '');

  // Extract title (first line) and body (rest)
  const firstNewline = displayText.indexOf('\n');
  const title = firstNewline > 0 ? displayText.substring(0, firstNewline) : displayText;
  const body = firstNewline > 0 ? displayText.substring(firstNewline + 1).trim() : '';

  // Compute children count – use node.child_count if available, else fallback to node.children.length.
  const childrenCount = typeof node.child_count !== "undefined"
    ? node.child_count
    : (node.children ? node.children.length : 0);

  const bubbleStyle = {
    padding: "1.8rem 2rem",
    margin: leftAlign ? "8px 0" : "8px auto",
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    cursor: "pointer",
    maxWidth: "1000px",
    width: "calc(100% - 20px)",
    transition: "border-color 0.3s ease, box-shadow 0.3s ease, transform 0.3s ease",
  };

  return (
    <div
      style={bubbleStyle}
      onClick={() => onClick(node.id)}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = 'var(--border-hover)';
        e.currentTarget.style.boxShadow = '0 4px 24px rgba(0,0,0,0.3)';
        e.currentTarget.style.transform = 'translateY(-1px)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = 'var(--border)';
        e.currentTarget.style.boxShadow = 'none';
        e.currentTarget.style.transform = 'translateY(0)';
      }}
    >
      <div style={{
        fontFamily: "var(--serif)",
        fontSize: "1.2rem",
        color: "var(--text-primary)",
        marginBottom: body ? "8px" : "0",
        fontWeight: 400,
      }}>
        {title.length > 120 ? title.substring(0, 120) + "..." : title}
      </div>
      {body && (
        <div style={{
          fontFamily: "var(--sans)",
          fontSize: "0.95rem",
          fontWeight: 300,
          color: "var(--text-secondary)",
          lineHeight: 1.5,
          overflow: "hidden",
          display: "-webkit-box",
          WebkitLineClamp: 3,
          WebkitBoxOrient: "vertical",
        }}>
          {body.length > 250 ? body.substring(0, 250) + "..." : body}
        </div>
      )}
      {/* Stop propagation in footer so clicks on the Link (author handle) don't trigger the bubble's onClick */}
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
