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
      <Link to={linkUrl} style={{ color: "#aba9a9", textDecoration: "none" }}>
        {username}
      </Link>
      <span>|</span>
      <span>{formattedDateTime}</span>
      {childrenCount > 0 && (
        <>
          <span>|</span>
          <FaRegCommentDots />
          <span>{childrenCount}</span>
        </>
      )}
    </div>
  );
};

const footerStyle = {
  fontSize: "0.8em",
  color: "#aba9a9",
  marginTop: "8px",
  display: "flex",
  alignItems: "center",
  gap: "5px"
};

export default NodeFooter;