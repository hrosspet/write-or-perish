import React from 'react';
import { Link } from 'react-router-dom';
import { FaRegCommentDots } from 'react-icons/fa';
import { useUser } from '../contexts/UserContext';
import { formatDateTime } from '../utils/date';

const NodeFooter = ({ username, createdAt, childrenCount, humanOwnerUsername, llmModel, onReplyClick, publicPage = false, children }) => {
  const { user } = useUser();

  // LLM nodes: private threads show just the model — whose thread it is
  // needs no announcing in your own diary. PUBLIC nodes attribute the
  // human who generated it ("model · via human"), matching the Commons
  // and the funnel.
  const displayUsername = llmModel
    ? (publicPage && humanOwnerUsername
        ? `${llmModel} · via ${humanOwnerUsername}` : llmModel)
    : username;

  // Link goes to human owner's dashboard for LLM nodes. On PUBLIC posts
  // (#228) the handle links to the author's public page instead — the
  // profile isn't public, and a visitor-facing surface shouldn't point at
  // a login wall.
  const linkUsername = humanOwnerUsername || username;
  const linkUrl = publicPage
    ? `/@${linkUsername}`
    : (user && user.username === linkUsername ? '/dashboard' : `/dashboard/${linkUsername}`);
  const formattedDateTime = formatDateTime(createdAt);

  const replyIcon = (
    <>
      <FaRegCommentDots />
      {childrenCount > 0 && <span>{childrenCount}</span>}
    </>
  );

  return (
    <div style={footerStyle}>
      <Link to={linkUrl} style={{ color: "var(--text-muted)", textDecoration: "none", transition: "color 0.3s ease" }}>
        {displayUsername}
      </Link>
      <span style={{ color: "var(--border)" }}>&middot;</span>
      <span>{formattedDateTime}</span>
      <span style={{ color: "var(--border)" }}>&middot;</span>
      {onReplyClick ? (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onReplyClick(); }}
          title="Reply"
          style={{
            background: 'none', border: 'none', padding: 0, cursor: 'pointer',
            color: 'inherit', font: 'inherit',
            display: 'flex', alignItems: 'center', gap: '4px',
          }}
        >
          {replyIcon}
        </button>
      ) : (
        replyIcon
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
