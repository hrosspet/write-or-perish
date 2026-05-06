import React, { useState, useRef, useEffect } from 'react';
import { FaChevronDown, FaChevronUp } from 'react-icons/fa';
import NodeFooter from './NodeFooter';
import MarkdownBody from './MarkdownBody';
import BubbleKebabMenu from './BubbleKebabMenu';

const tombstoneStyle = {
  fontFamily: 'var(--sans)',
  fontSize: '0.95rem',
  fontStyle: 'italic',
  color: 'var(--text-muted)',
  marginBottom: '0.6rem',
};

const Bubble = ({
  node,
  onClick,
  isHighlighted = false,
  leftAlign = false,
  actions = null,
}) => {
  // Detect voice notes via backend-provided has_original_audio flag
  const isVoiceNote = !!node.has_original_audio;
  const isPinned = !!node.pinned_at;
  const promptKey = node.prompt_key || null;
  const isTombstone = !!node.deleted;
  const isInaccessible = !!node.inaccessible;
  const isPlaceholder = isTombstone || isInaccessible;

  const [expanded, setExpanded] = useState(false);
  const [hovered, setHovered] = useState(false);
  const hoverHideTimerRef = useRef(null);

  const cancelHoverHide = () => {
    if (hoverHideTimerRef.current) {
      clearTimeout(hoverHideTimerRef.current);
      hoverHideTimerRef.current = null;
    }
  };

  // Keep the kebab on screen for a beat after the cursor leaves so a
  // user can still reach it across the gap between bubble and icon.
  const scheduleHoverHide = () => {
    cancelHoverHide();
    hoverHideTimerRef.current = setTimeout(() => {
      setHovered(false);
      hoverHideTimerRef.current = null;
    }, 3000);
  };

  useEffect(() => cancelHoverHide, []);

  // Use full content if available; otherwise use preview.
  const text = node.content || node.preview || "";

  // Strip leading "# " from title for cleaner display
  const displayText = text.replace(/^#\s+/, '');

  // Extract title (first line) and body (rest)
  const firstNewline = displayText.indexOf('\n');
  const title = firstNewline > 0 ? displayText.substring(0, firstNewline) : displayText;
  const body = firstNewline > 0 ? displayText.substring(firstNewline + 1).trim() : '';

  // Only offer expand when there's full content AND the collapsed preview
  // actually hides something: title past 120 chars, body past 250 chars,
  // or body with >2 newline-separated lines (which would be line-clamped).
  const canExpand = !isPlaceholder && !!node.content && (
    title.length > 120 ||
    body.length > 250 ||
    body.split('\n').length > 2
  );

  // Compute children count – use node.child_count if available, else fallback to node.children.length.
  const childrenCount = typeof node.child_count !== "undefined"
    ? node.child_count
    : (node.children ? node.children.length : 0);

  const bubbleStyle = {
    padding: "1.6rem 1.8rem",
    margin: leftAlign ? "8px 0" : "8px auto",
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    cursor: isPlaceholder ? "default" : "pointer",
    maxWidth: "1000px",
    // Right-side reserve covers the always-outside kebab (8px margin +
    // ~26px icon + small buffer). On wide viewports maxWidth caps below
    // 100%-40 so this doesn't change anything; on narrow viewports it's
    // what keeps the kebab on screen down to ~320px portrait.
    width: "calc(100% - 40px)",
    transition: "border-color 0.3s ease, box-shadow 0.3s ease, transform 0.3s ease",
    position: "relative",
  };

  const tagStyle = {
    fontFamily: "var(--sans)",
    fontSize: "0.65rem",
    fontWeight: 500,
    textTransform: "uppercase",
    letterSpacing: "0.08em",
    padding: "3px 8px",
    borderRadius: "4px",
    marginLeft: "8px",
  };

  // Show kebab on hover (mouse), on focus (keyboard), or always on touch
  // devices (no hover). Computed in inline style + the touch-device fallback.
  const kebabAlwaysVisible = (
    typeof window !== 'undefined' &&
    window.matchMedia &&
    window.matchMedia('(hover: none)').matches
  );
  const hasActions = !isPlaceholder && Array.isArray(actions) && actions.length > 0;
  const kebabVisible = hasActions && (kebabAlwaysVisible || hovered);

  const handleCardClick = (e) => {
    if (isPlaceholder) return; // Tombstones and inaccessible are non-navigable.
    if (onClick) onClick(node.id, e);
  };

  return (
    <div
      style={bubbleStyle}
      onClick={handleCardClick}
      onMouseEnter={(e) => {
        cancelHoverHide();
        setHovered(true);
        if (!isPlaceholder) {
          e.currentTarget.style.borderColor = 'var(--border-hover)';
          e.currentTarget.style.boxShadow = '0 4px 24px rgba(0,0,0,0.3)';
          e.currentTarget.style.transform = 'translateY(-1px)';
        }
      }}
      onMouseLeave={(e) => {
        scheduleHoverHide();
        e.currentTarget.style.borderColor = 'var(--border)';
        e.currentTarget.style.boxShadow = 'none';
        e.currentTarget.style.transform = 'translateY(0)';
      }}
    >
      {hasActions && (
        <BubbleKebabMenu
          visible={kebabVisible}
          items={actions}
          onFocus={() => { cancelHoverHide(); setHovered(true); }}
          onBlur={() => scheduleHoverHide()}
        />
      )}
      {isTombstone ? (
        <div style={tombstoneStyle}>[Node deleted]</div>
      ) : isInaccessible ? (
        <div style={tombstoneStyle}>[Node inaccessible]</div>
      ) : expanded ? (
        <div style={{
          fontFamily: "var(--sans)",
          fontSize: "0.95rem",
          fontWeight: 300,
          color: "var(--text-secondary)",
          lineHeight: 1.7,
          marginBottom: "0.6rem",
        }}>
          <MarkdownBody>{node.content}</MarkdownBody>
        </div>
      ) : (
        <>
          <div style={{
            fontFamily: "var(--sans)",
            fontSize: "1rem",
            color: "var(--text-primary)",
            marginBottom: body ? "0.6rem" : "0",
            fontWeight: 400,
          }}>
            {title.length > 120 ? title.substring(0, 120) + "..." : title}
          </div>
          {body && (
            <div style={{
              fontFamily: "var(--sans)",
              fontSize: "0.92rem",
              fontWeight: 300,
              color: "var(--text-secondary)",
              lineHeight: 1.7,
              overflow: "hidden",
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
            }}>
              {body.length > 250 ? body.substring(0, 250) + "..." : body}
            </div>
          )}
        </>
      )}
      {/* Footer row with optional tags + node footer. Inaccessible nodes
          omit the footer entirely — the whole point is to not leak
          username/timestamp to viewers without pre-deletion access. */}
      {!isInaccessible && (
        <div onClick={(e) => e.stopPropagation()} style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}>
          <NodeFooter
            username={node.username}
            createdAt={node.created_at}
            childrenCount={childrenCount}
            humanOwnerUsername={node.human_owner_username}
            llmModel={node.llm_model}
            onReplyClick={
              !isPlaceholder && Array.isArray(actions)
                ? (actions.find((a) => a.label === 'Reply') || {}).action || null
                : null
            }
          />
          {!isPlaceholder && (
            <div style={{ display: "flex", alignItems: "center" }}>
              {isPinned && (
                <span style={{
                  ...tagStyle,
                  color: "var(--accent-dim)",
                  backgroundColor: "var(--accent-subtle)",
                }}>
                  Pinned
                </span>
              )}
              {promptKey ? (
                <span style={{
                  ...tagStyle,
                  color: "var(--accent-dim)",
                  backgroundColor: "var(--accent-subtle)",
                }}>
                  {promptKey.charAt(0).toUpperCase() + promptKey.slice(1)}
                </span>
              ) : isVoiceNote ? (
                <span style={{
                  ...tagStyle,
                  color: "var(--accent-dim)",
                  backgroundColor: "var(--accent-subtle)",
                }}>
                  Voice Note
                </span>
              ) : null}
              {canExpand && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setExpanded((prev) => !prev);
                  }}
                  aria-label={expanded ? "Collapse preview" : "Expand preview"}
                  style={{
                    marginLeft: "8px",
                    padding: "4px 8px",
                    background: "none",
                    border: "none",
                    color: "var(--text-muted)",
                    cursor: "pointer",
                    display: "inline-flex",
                    alignItems: "center",
                    fontSize: "0.85rem",
                    lineHeight: 1,
                  }}
                >
                  {expanded ? <FaChevronUp /> : <FaChevronDown />}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default Bubble;
