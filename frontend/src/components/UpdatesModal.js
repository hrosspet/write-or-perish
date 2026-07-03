import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api";
import MarkdownBody from "./MarkdownBody";

/**
 * The dev-update channel surface (#207): shows unread changelog sections,
 * targeted notifications, and pending admin polls when the user opens
 * Loore. Appears only when something is unread; closing it (or "Later")
 * shows the remaining items again next visit, "Got it" dismisses an item
 * for good. Poll answers follow the two-phase opt-in: an LLM draft is
 * requested explicitly, and nothing leaves the account until "Send".
 */

const overlayStyle = {
  position: "fixed",
  top: 0, left: 0, right: 0, bottom: 0,
  backgroundColor: "rgba(0,0,0,0.7)",
  backdropFilter: "blur(8px)",
  WebkitBackdropFilter: "blur(8px)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1000,
};

const cardStyle = {
  background: "var(--bg-card)",
  border: "1px solid var(--border)",
  borderRadius: "12px",
  padding: "2rem",
  width: "560px",
  maxWidth: "92vw",
  maxHeight: "82vh",
  overflowY: "auto",
};

const headerStyle = {
  fontFamily: "var(--serif)",
  fontSize: "1.5rem",
  fontWeight: 400,
  color: "var(--text-primary)",
  margin: 0,
  marginBottom: "0.25rem",
};

const itemStyle = {
  borderTop: "1px solid var(--border)",
  paddingTop: "1.25rem",
  marginTop: "1.25rem",
};

const itemTitleStyle = {
  fontFamily: "var(--serif)",
  fontSize: "1.15rem",
  fontWeight: 400,
  color: "var(--text-primary)",
  margin: 0,
};

const dateStyle = {
  fontFamily: "var(--sans)",
  fontSize: "0.75rem",
  fontWeight: 300,
  color: "var(--text-muted)",
  letterSpacing: "0.04em",
};

const buttonRowStyle = {
  display: "flex",
  gap: "10px",
  justifyContent: "flex-end",
  marginTop: "0.75rem",
};

const ghostButtonStyle = {
  fontFamily: "var(--sans)", fontSize: "0.85rem", fontWeight: 300,
  padding: "8px 16px", borderRadius: "6px", cursor: "pointer",
  background: "none", border: "1px solid var(--border)",
  color: "var(--text-secondary)",
};

const accentButtonStyle = {
  fontFamily: "var(--sans)", fontSize: "0.85rem", fontWeight: 400,
  padding: "8px 16px", borderRadius: "6px", cursor: "pointer",
  background: "var(--accent)", border: "none",
  color: "var(--bg-deep)",
};

const mutedNoteStyle = {
  fontFamily: "var(--sans)", fontSize: "0.78rem", fontWeight: 300,
  color: "var(--text-muted)", lineHeight: 1.5,
};

const textareaStyle = {
  width: "100%", minHeight: "110px", boxSizing: "border-box",
  fontFamily: "var(--sans)", fontSize: "0.9rem", fontWeight: 300,
  lineHeight: 1.6, color: "var(--text-primary)",
  background: "var(--bg-surface)", border: "1px solid var(--border)",
  borderRadius: "8px", padding: "10px 12px", resize: "vertical",
  marginTop: "0.6rem",
};

function ChangelogItem({ section, onDone }) {
  const mark = (action) => {
    api.post(`/updates/changelog/${section.id}/${action}`).catch(() => {});
    onDone();
  };
  return (
    <div style={itemStyle}>
      {section.date && <div style={dateStyle}>{section.date}</div>}
      <h3 style={itemTitleStyle}>{section.title}</h3>
      <MarkdownBody style={{ fontSize: "0.9rem" }}>
        {section.body}
      </MarkdownBody>
      <div style={buttonRowStyle}>
        <button style={ghostButtonStyle} onClick={() => mark("skip")}>
          Later
        </button>
        <button style={accentButtonStyle} onClick={() => mark("read")}>
          Got it
        </button>
      </div>
    </div>
  );
}

function NotificationItem({ notification, onDone }) {
  const navigate = useNavigate();
  const mark = (action) => {
    api.post(`/updates/notifications/${notification.id}/${action}`)
      .catch(() => {});
    onDone();
  };
  return (
    <div style={itemStyle}>
      <h3 style={itemTitleStyle}>{notification.title}</h3>
      {notification.body && (
        <p style={{
          fontFamily: "var(--sans)", fontSize: "0.9rem", fontWeight: 300,
          color: "var(--text-secondary)", lineHeight: 1.6,
          margin: "0.5rem 0 0",
        }}>
          {notification.body}
        </p>
      )}
      <div style={buttonRowStyle}>
        <button style={ghostButtonStyle} onClick={() => mark("skip")}>
          Later
        </button>
        {notification.link && (
          <button
            style={ghostButtonStyle}
            onClick={() => { mark("read"); navigate(notification.link); }}
          >
            Take a look
          </button>
        )}
        <button style={accentButtonStyle} onClick={() => mark("read")}>
          Got it
        </button>
      </div>
    </div>
  );
}

function PollItem({ poll, onDone }) {
  const [response, setResponse] = useState(poll.response);
  const [text, setText] = useState(poll.response?.content || "");
  const [writing, setWriting] = useState(
    !!(poll.response && poll.response.content));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const pollTimer = useRef(null);

  const drafting = response?.status === "drafting";

  // While the LLM draft is generating, poll for its completion.
  useEffect(() => {
    if (!drafting) return undefined;
    pollTimer.current = setInterval(async () => {
      try {
        const res = await api.get(`/updates/polls/${poll.id}`);
        const r = res.data.response;
        if (r && r.status !== "drafting") {
          setResponse(r);
          if (r.status === "draft") {
            setText(r.content || "");
            setWriting(true);
          } else if (r.status === "draft_failed") {
            setError(
              "Drafting didn't work this time — you can still write " +
              "your own answer.");
            setWriting(true);
          }
        }
      } catch (e) { /* keep polling */ }
    }, 2500);
    return () => clearInterval(pollTimer.current);
  }, [drafting, poll.id]);

  const requestDraft = async () => {
    setError(null);
    setBusy(true);
    try {
      const res = await api.post(`/updates/polls/${poll.id}/draft`);
      setResponse(res.data.response);
    } catch (e) {
      setError(e.response?.data?.error ||
        "Couldn't start the draft — you can still write your own answer.");
      setWriting(true);
    } finally {
      setBusy(false);
    }
  };

  const send = async () => {
    if (!text.trim()) return;
    setError(null);
    setBusy(true);
    try {
      await api.put(`/updates/polls/${poll.id}/response`,
        { content: text });
      await api.post(`/updates/polls/${poll.id}/send`);
      onDone();
    } catch (e) {
      setError(e.response?.data?.error || "Sending failed — try again?");
    } finally {
      setBusy(false);
    }
  };

  const decline = () => {
    api.post(`/updates/polls/${poll.id}/decline`).catch(() => {});
    onDone();
  };

  return (
    <div style={itemStyle}>
      <div style={dateStyle}>A QUESTION FROM THE DEVELOPER</div>
      <h3 style={itemTitleStyle}>{poll.question}</h3>
      <p style={{ ...mutedNoteStyle, margin: "0.5rem 0 0" }}>
        Answering is optional. Loore can draft a reply from your profile
        for you to edit — and nothing is sent until you press Send.
      </p>

      {drafting && (
        <p style={{ ...mutedNoteStyle, margin: "0.75rem 0 0",
          color: "var(--accent)" }}>
          Loore is drafting an answer from your profile…
        </p>
      )}

      {error && (
        <p style={{ ...mutedNoteStyle, margin: "0.75rem 0 0",
          color: "var(--error)" }}>
          {error}
        </p>
      )}

      {writing && !drafting && (
        <div>
          {response?.generated_by && response?.content === text && (
            <p style={{ ...mutedNoteStyle, margin: "0.6rem 0 0" }}>
              Drafted by AI from your profile — please review and edit
              before sending.
            </p>
          )}
          <textarea
            style={textareaStyle}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Your answer…"
          />
        </div>
      )}

      <div style={buttonRowStyle}>
        <button style={ghostButtonStyle} onClick={decline} disabled={busy}>
          No thanks
        </button>
        <button style={ghostButtonStyle} onClick={onDone} disabled={busy}>
          Later
        </button>
        {!writing && !drafting && (
          <>
            <button
              style={ghostButtonStyle}
              onClick={() => setWriting(true)}
              disabled={busy}
            >
              Write my own
            </button>
            <button
              style={accentButtonStyle}
              onClick={requestDraft}
              disabled={busy}
            >
              Draft with AI
            </button>
          </>
        )}
        {writing && !drafting && (
          <button
            style={{
              ...accentButtonStyle,
              opacity: text.trim() && !busy ? 1 : 0.5,
              cursor: text.trim() && !busy ? "pointer" : "default",
            }}
            onClick={send}
            disabled={!text.trim() || busy}
          >
            Send to developer
          </button>
        )}
      </div>
    </div>
  );
}

function UpdatesModal({ data, onClose }) {
  const [changelog, setChangelog] = useState(data.changelog || []);
  const [notifications, setNotifications] = useState(
    data.notifications || []);
  const [polls, setPolls] = useState(data.polls || []);

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const remaining =
    changelog.length + notifications.length + polls.length;
  useEffect(() => {
    if (remaining === 0) onClose();
  }, [remaining, onClose]);

  if (remaining === 0) return null;

  const removeFrom = (setter) => (id, key) =>
    setter((items) => items.filter((item) => item[key] !== id));

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={cardStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={headerStyle}>While you were away</h2>
        <p style={{ ...mutedNoteStyle, margin: 0 }}>
          What's new in Loore since your last visit.
        </p>

        {notifications.map((n) => (
          <NotificationItem
            key={`n-${n.id}`}
            notification={n}
            onDone={() => removeFrom(setNotifications)(n.id, "id")}
          />
        ))}

        {polls.map((p) => (
          <PollItem
            key={`p-${p.id}`}
            poll={p}
            onDone={() => removeFrom(setPolls)(p.id, "id")}
          />
        ))}

        {changelog.map((s) => (
          <ChangelogItem
            key={`c-${s.id}`}
            section={s}
            onDone={() => removeFrom(setChangelog)(s.id, "id")}
          />
        ))}
      </div>
    </div>
  );
}

export default UpdatesModal;
