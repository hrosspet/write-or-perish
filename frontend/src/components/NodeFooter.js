import React from 'react';
import { Link } from 'react-router-dom';
import { FaRegCommentDots } from 'react-icons/fa';
import { useUser } from '../contexts/UserContext';

const NodeFooter = ({ username, createdAt, childrenCount }) => {
  const { user } = useUser();
  // If the username passed to NodeFooter is the same as the logged-in user's username,
  // use "/dashboard" for the link. Otherwise, use "/dashboard/username" for the public dashboard.
  const linkUrl = user && user.username === username ? '/dashboard' : `/dashboard/${username}`;
  const formattedDateTime = createdAt ? new Date(createdAt).toLocaleString() : "";

  return (
    <div style={footerStyle}>
      <Link to={linkUrl} style={{ color: "var(--text-muted)", textDecoration: "none", transition: "color 0.3s ease" }}>
        {username}
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
