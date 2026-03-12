import React, { useState, useCallback } from "react";
import { useUser } from "../contexts/UserContext";
import ModelSelector from "../components/ModelSelector";
import api from "../api";

export default function AccountPage() {
  const { user, setUser } = useUser();

  // Username editing
  const [username, setUsername] = useState(user?.username || "");
  const [usernameSaving, setUsernameSaving] = useState(false);
  const [usernameMsg, setUsernameMsg] = useState(null); // { type, text }

  // Model selector state — read from user preference, localStorage fallback
  const [selectedModel, setSelectedModel] = useState(() => {
    if (user && user.preferred_model) return user.preferred_model;
    return localStorage.getItem("loore_selected_model") || null;
  });

  // Privacy / AI usage defaults
  const [privacySaving, setPrivacySaving] = useState(false);
  const [aiUsageSaving, setAiUsageSaving] = useState(false);

  const usernameValid = (val) => {
    const v = val.trim();
    if (!v) return "Username cannot be empty.";
    if (v.length > 64) return "Username must be 64 characters or fewer.";
    if (!/^[a-zA-Z0-9_]+$/.test(v))
      return "Only letters, numbers, and underscores allowed.";
    return null;
  };

  const saveUsername = async () => {
    const err = usernameValid(username);
    if (err) {
      setUsernameMsg({ type: "error", text: err });
      return;
    }
    setUsernameSaving(true);
    setUsernameMsg(null);
    try {
      const res = await api.put("/dashboard/user", { username: username.trim() });
      if (res.data.user) setUser(res.data.user);
      setUsernameMsg({ type: "success", text: "Username updated." });
    } catch (e) {
      setUsernameMsg({
        type: "error",
        text: e.response?.data?.error || "Failed to update username.",
      });
    } finally {
      setUsernameSaving(false);
    }
  };

  const handleModelChange = useCallback(
    async (model) => {
      setSelectedModel(model);
      localStorage.setItem("loore_selected_model", model);
      try {
        const res = await api.put("/dashboard/user", { preferred_model: model });
        if (res.data.user) setUser(res.data.user);
      } catch (e) {
        // Silently fall back to localStorage
      }
    },
    [setUser]
  );

  const saveField = async (field, value, setSaving) => {
    setSaving(true);
    try {
      const res = await api.put("/dashboard/user", { [field]: value });
      if (res.data.user) setUser(res.data.user);
    } catch (e) {
      // silent
    } finally {
      setSaving(false);
    }
  };

  const labelStyle = {
    fontFamily: "var(--sans)",
    fontWeight: 400,
    fontSize: "0.85rem",
    color: "var(--text-secondary)",
    marginBottom: "4px",
  };

  const helperStyle = {
    fontFamily: "var(--sans)",
    fontWeight: 300,
    fontSize: "0.78rem",
    color: "var(--text-muted)",
    marginTop: "4px",
  };

  const inputStyle = {
    width: "100%",
    padding: "10px 12px",
    borderRadius: "6px",
    border: "1px solid var(--border)",
    backgroundColor: "var(--bg-input)",
    color: "var(--text-primary)",
    fontFamily: "var(--sans)",
    fontWeight: 300,
    fontSize: "0.95rem",
    boxSizing: "border-box",
  };

  const selectStyle = {
    ...inputStyle,
    cursor: "pointer",
    WebkitAppearance: "none",
    appearance: "none",
  };

  const rowStyle = { marginBottom: "1.25rem" };

  if (!user) return null;

  return (
    <div
      style={{
        maxWidth: 600,
        margin: "0 auto",
        padding: "3rem 1.5rem",
      }}
    >
      <h2
        style={{
          fontFamily: "var(--serif)",
          fontWeight: 300,
          fontSize: "1.4rem",
          color: "var(--text-primary)",
          marginBottom: "1.5rem",
        }}
      >
        Account
      </h2>

      {/* ─── Account info ─── */}

      <div style={rowStyle}>
        <div style={labelStyle}>Username</div>
        <div style={{ display: "flex", gap: "8px" }}>
          <input
            value={username}
            onChange={(e) => {
              setUsername(e.target.value);
              setUsernameMsg(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") saveUsername();
            }}
            style={{ ...inputStyle, flex: 1 }}
          />
          <button
            onClick={saveUsername}
            disabled={usernameSaving || username.trim() === user.username}
            style={{
              padding: "8px 16px",
              borderRadius: "6px",
              border: "1px solid var(--accent)",
              background: "none",
              color: "var(--accent)",
              fontFamily: "var(--sans)",
              fontWeight: 300,
              fontSize: "0.85rem",
              cursor:
                usernameSaving || username.trim() === user.username
                  ? "default"
                  : "pointer",
              opacity:
                usernameSaving || username.trim() === user.username ? 0.4 : 1,
            }}
          >
            {usernameSaving ? "Saving..." : "Save"}
          </button>
        </div>
        {usernameMsg && (
          <div
            style={{
              ...helperStyle,
              color:
                usernameMsg.type === "error"
                  ? "var(--accent)"
                  : "var(--text-muted)",
            }}
          >
            {usernameMsg.text}
          </div>
        )}
        <div style={helperStyle}>Letters, numbers, and underscores only.</div>
      </div>

      <div style={rowStyle}>
        <div style={labelStyle}>Email</div>
        <div
          style={{
            ...inputStyle,
            backgroundColor: "transparent",
            border: "1px solid var(--border)",
            opacity: 0.6,
          }}
        >
          {user.email || "—"}
        </div>
      </div>

      <div style={rowStyle}>
        <div style={labelStyle}>Plan</div>
        <div
          style={{
            ...inputStyle,
            backgroundColor: "transparent",
            border: "1px solid var(--border)",
            opacity: 0.6,
            textTransform: "capitalize",
          }}
        >
          {user.plan || "free"}
        </div>
      </div>

      {/* ─── Settings ─── */}

      <h3
        style={{
          fontFamily: "var(--serif)",
          fontWeight: 300,
          fontSize: "1.15rem",
          color: "var(--text-primary)",
          marginTop: "2rem",
          marginBottom: "1.25rem",
        }}
      >
        Settings
      </h3>

      <div style={rowStyle}>
        <div style={labelStyle}>Default model</div>
        <ModelSelector
          nodeId={null}
          selectedModel={selectedModel}
          onModelChange={handleModelChange}
          style={{
            padding: "10px 12px",
            fontSize: "0.95rem",
            color: "var(--text-primary)",
          }}
        />
        <div style={helperStyle}>
          Used for profile generation and LLM responses.
        </div>
      </div>

      <div style={rowStyle}>
        <div style={labelStyle}>Default privacy</div>
        <select
          value={user.default_privacy_level || "private"}
          disabled={privacySaving}
          onChange={(e) =>
            saveField("default_privacy_level", e.target.value, setPrivacySaving)
          }
          style={selectStyle}
        >
          <option value="private">Private</option>
          <option value="circles" disabled>
            Circles (coming soon)
          </option>
          <option value="public">Public</option>
        </select>
        <div style={helperStyle}>
          Default visibility for new entries.
        </div>
      </div>

      <div style={rowStyle}>
        <div style={labelStyle}>Default AI usage</div>
        <select
          value={user.default_ai_usage || "chat"}
          disabled={aiUsageSaving}
          onChange={(e) =>
            saveField("default_ai_usage", e.target.value, setAiUsageSaving)
          }
          style={selectStyle}
        >
          <option value="none">None</option>
          <option value="chat">Chat</option>
          <option value="train">Train</option>
        </select>
        <div style={helperStyle}>
          Controls how AI can use your new entries by default.
        </div>
      </div>
    </div>
  );
}
