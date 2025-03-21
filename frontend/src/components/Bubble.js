import React from 'react';
import { FaRegCommentDots } from 'react-icons/fa';

const Bubble = ({ node, onClick, isHighlighted = false, leftAlign = false }) => {
  // Use full content if available; otherwise use preview.
  const text = node.content || node.preview || "";
  // Format date.
  const datetime = node.created_at ? new Date(node.created_at).toLocaleString() : "";
  // Get children count.
  const childrenCount =
    typeof node.child_count !== "undefined"
      ? node.child_count
      : node.children
      ? node.children.length
      : 0;
  // Get username (author's handle)
  const username = node.username;

  // If leftAlign is true then avoid centering the bubble.
  const bubbleStyle = {
    padding: "15px",
    margin: leftAlign ? "10px 0" : "10px auto",
    background: "#1e1e1e",
    border: isHighlighted ? "2px solid #61dafb" : "1px solid #333",
    borderRadius: "8px",
    cursor: "pointer",
    whiteSpace: "pre-wrap",
    maxWidth: "1000px",
    width: "calc(100% - 20px)" // Responsive width on smaller screens.
  };

  // The footer displays the username, datetime,
  // and (only when childrenCount > 0) the message icon plus count.
  const footerStyle = {
    fontSize: "0.8em",
    color: "#aba9a9",
    marginTop: "8px",
    display: "flex",
    alignItems: "center",
    gap: "5px"
  };

  return (
    <div style={bubbleStyle} onClick={() => onClick(node.id)}>
      <div>
        {text.length > 250 ? text.substring(0, 250) + "..." : text}
      </div>
      <div style={footerStyle}>
        <span>{username}</span>
        <span>|</span>
        <span>{datetime}</span>
        {childrenCount > 0 && (
          <>
            <span>|</span>
            <FaRegCommentDots />
            <span>{childrenCount}</span>
          </>
        )}
      </div>
    </div>
  );
};

export default Bubble;