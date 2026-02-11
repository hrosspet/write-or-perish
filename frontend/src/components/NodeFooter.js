import React from 'react';
import { Link } from 'react-router-dom';
import { FaRegCommentDots } from 'react-icons/fa';
import { useUser } from '../contexts/UserContext';

const NodeFooter = ({ username, createdAt, childrenCount, humanOwnerUsername, llmModel, children }) => {
  const { user } = useUser();

  // "via" display: show "humanOwner via model" for LLM nodes
  const displayUsername = humanOwnerUsername && llmModel
    ? `${humanOwnerUsername} via ${llmModel}`
    : username;

  // Link goes to human owner's dashboard for LLM nodes
  const linkUsername = humanOwnerUsername || username;
  const linkUrl = user && user.username === linkUsername ? '/dashboard' : `/dashboard/${linkUsername}`;
  const formattedDateTime = createdAt ? new Date(createdAt).toLocaleString() : "";

  return (
    <div style={footerStyle}>
      <Link to={linkUrl} style={{ color: "var(--text-muted)", textDecoration: "none", transition: "color 0.3s ease" }}>
        {displayUsername}
      </Link>
      <span style={{ color: "var(--border)" }}>&middot;</span>
      <span>{formattedDateTime}</span>
      {childrenCount > 0 && (
        <>
          <span style={{ color: "var(--border)" }}>&middot;</span>
          <FaRegCommentDots />
          <span>{childrenCount}</span>
        </>
      )}
      {children && (
        <>
          <span style={{ color: "var(--border)" }}>&middot;</span>
          <span style={{ display: "flex", alignItems: "center", gap: "4px" }}>
            {children}
          </span>
        </>
      )}
    </div>
  );
};

const footerStyle = {
  fontSize: "0.75rem",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  color: "var(--text-muted)",
  marginTop: "12px",
  display: "flex",
  alignItems: "center",
  gap: "6px"
};

export default NodeFooter;
