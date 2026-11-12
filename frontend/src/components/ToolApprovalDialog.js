import React, { useEffect } from "react";
import DiffRows from "./DiffRows";
import { collapseUnchanged, refineWordDiffs } from "../utils/diff";

// Peter, 2026-11-12: in a thread whose history holds text the user didn't
// write, the AI's calls that change the user's data directly wait for the
// user. One dialog per reply shows every waiting call with what it would
// change; each is approved or rejected on its own. Closing it decides
// nothing: the calls keep waiting.

const KIND_PHRASES = {
  other_user: "another person's writing",
  saved_reference: "a saved reference",
  glean: "tweets from Glean",
};

const joinNatural = (items) => (items.length <= 1 ? items.join("")
  : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`);

// The dialog's one sentence on why it asks.
export function approvalReason(kinds) {
  const phrases = (kinds || []).map((k) => KIND_PHRASES[k]).filter(Boolean);
  const what = phrases.length ? ` (${joinNatural(phrases)})` : "";
  return `This thread holds text you didn't write${what}, so Loore asks `
    + "before the AI changes your data.";
}

// The waiting calls of one reply each, oldest reply first.
export function groupByTurn(items) {
  const groups = new Map();
  [...(items || [])].sort((a, b) => a.id - b.id).forEach((item) => {
    const key = item.turn_node_id ?? item.node_id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  return [...groups.values()];
}

const overlayStyle = {
  position: "fixed", top: 0, left: 0, right: 0, bottom: 0,
  backgroundColor: "rgba(0,0,0,0.7)", backdropFilter: "blur(8px)",
  WebkitBackdropFilter: "blur(8px)", display: "flex",
  alignItems: "center", justifyContent: "center", zIndex: 1000,
};

const cardStyle = {
  background: "var(--bg-card)", border: "1px solid var(--border)",
  borderRadius: "12px", padding: "clamp(1rem, 5vw, 2rem)", width: "560px",
  maxWidth: "92vw", maxHeight: "88vh", overflowY: "auto",
};

const titleStyle = {
  fontFamily: "var(--serif)", fontSize: "1.4rem", fontWeight: 400,
  color: "var(--text-primary)", margin: 0, marginBottom: "0.75rem",
};

const bodyStyle = {
  fontFamily: "var(--sans)", fontSize: "0.9rem", fontWeight: 300,
  color: "var(--text-secondary)", lineHeight: 1.6, margin: 0,
  marginBottom: "1.25rem",
};

const itemStyle = {
  border: "1px solid var(--border)", borderRadius: "8px",
  padding: "12px 14px", marginBottom: "12px",
};

const itemTitleStyle = {
  fontFamily: "var(--sans)", fontSize: "0.92rem", fontWeight: 400,
  color: "var(--text-primary)", marginBottom: "6px",
};

const noteStyle = {
  fontFamily: "var(--sans)", fontSize: "0.8rem", fontWeight: 300,
  color: "var(--text-muted)", margin: "4px 0",
};

const previewStyle = {
  maxHeight: "min(260px, 30vh)", overflowY: "auto", margin: "6px 0",
  paddingTop: "6px", borderTop: "1px solid var(--border)",
};

const buttonStyle = (primary) => ({
  fontFamily: "var(--sans)", fontSize: "0.85rem",
  fontWeight: primary ? 400 : 300, padding: "8px 16px", borderRadius: "6px",
  cursor: "pointer",
  background: primary ? "var(--accent)" : "none",
  border: primary ? "none" : "1px solid var(--border)",
  color: primary ? "var(--bg-deep)" : "var(--text-secondary)",
});

function ItemBody({ item }) {
  if (item.tool === "update_artifact") {
    const rows = collapseUnchanged(refineWordDiffs(item.lines || []), 2);
    return (
      <>
        {item.base_version ? (
          <p style={noteStyle}>Based on v{item.base_version}</p>
        ) : null}
        {item.stale && (
          <p style={noteStyle}>
            Your {item.artifact_title} changed since
            {item.current_version ? ` (now v${item.current_version})` : ""}.
            Approving would save nothing.
          </p>
        )}
        {item.error && <p style={noteStyle}>{item.error}</p>}
        {/* The write also sets these: approving applies them as shown. */}
        {[['Title', item.title_change], ['Description', item.description_change]]
          .filter(([, change]) => change)
          .map(([label, change]) => (
            <p key={label} style={{ ...noteStyle, color: "var(--text-secondary)" }}>
              {label}: {change.from ? `${change.from} → ${change.to}` : change.to}
            </p>
          ))}
        <div style={previewStyle}><DiffRows rows={rows} /></div>
      </>
    );
  }
  return (
    <>
      {item.stale && <p style={noteStyle}>Already applied or replaced.</p>}
      {item.proposal_text && (
        <div style={{ ...previewStyle, whiteSpace: "pre-wrap", ...noteStyle }}>
          {item.proposal_text}
        </div>
      )}
    </>
  );
}

// On the Voice screen the next turn waits for the decision (Peter,
// 2026-11-12), so the dialog says so and has no "Later".
export const VOICE_WAITS_TEXT = "Voice mode continues once you've approved "
  + "or rejected each change.";

/**
 * *items*: the waiting calls of one reply (the server's pending_approvals
 * entries). *busy* / *errors*: per item id. *onDecide(item, approve)*.
 *
 * *voice*: shown on the Voice screen, where no new turn starts while a
 * change waits. Nothing closes it but deciding (no "Later", no Escape, no
 * click outside); *onOpenThread* leaves the Voice screen for the thread
 * page instead, where the dialog works as everywhere else.
 */
function ToolApprovalDialog({
  open, items, busy, errors, onDecide, onClose, voice = false, onOpenThread,
}) {
  useEffect(() => {
    if (!open || voice) return undefined;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose, voice]);

  if (!open || !items || items.length === 0) return null;
  const kinds = [...new Set(items.flatMap((i) => i.kinds || []))];

  return (
    <div style={overlayStyle} onClick={voice ? undefined : onClose}>
      <div
        style={cardStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tool-approval-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="tool-approval-title" style={titleStyle}>Approve changes?</h2>
        <p style={bodyStyle}>
          {approvalReason(kinds)}
          {voice ? ` ${VOICE_WAITS_TEXT}` : null}
        </p>
        {items.map((item) => (
          <div key={item.id} style={itemStyle} data-testid={`approval-${item.id}`}>
            <div style={itemTitleStyle}>{item.title}</div>
            {/* The decision sits right under the call's title, so it is in
                view whenever the call is, whatever the size of the screen
                and of the change below it. */}
            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "6px" }}>
              <button
                type="button"
                style={buttonStyle(false)}
                disabled={!!busy?.[item.id]}
                onClick={() => onDecide(item, false)}
              >
                Reject
              </button>
              <button
                type="button"
                style={{
                  ...buttonStyle(true),
                  ...((item.stale || busy?.[item.id]) && { opacity: 0.5, cursor: "not-allowed" }),
                }}
                disabled={!!item.stale || !!busy?.[item.id]}
                onClick={() => onDecide(item, true)}
              >
                Approve
              </button>
            </div>
            {errors?.[item.id] && (
              <p style={{ ...noteStyle, color: "var(--accent)" }}>{errors[item.id]}</p>
            )}
            <ItemBody item={item} />
          </div>
        ))}
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "4px" }}>
          {voice ? (
            onOpenThread && (
              <button type="button" style={buttonStyle(false)} onClick={onOpenThread}>
                Open the thread
              </button>
            )
          ) : (
            <button type="button" style={buttonStyle(false)} onClick={onClose}>
              Later
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default ToolApprovalDialog;
