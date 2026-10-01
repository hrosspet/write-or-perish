import React, { useState, useCallback, useRef, useEffect } from "react";
import { Link, useNavigate, useLocation } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import ModelSelector from "../components/ModelSelector";
import CraftIcon from "../components/CraftIcon";
import api from "../api";
import useSubmitShortcut from "../hooks/useSubmitShortcut";
import { emailState } from "../utils/emailState";

const backendUrl = process.env.REACT_APP_BACKEND_URL || "";

// Where Connect X (#311) comes back to: /account?x_login=<outcome>#x.
const X_LOGIN_MESSAGES = {
  linked: { type: "success", text: "X connected. Sign in with X now opens this account." },
  cancelled: { type: "info", text: "X was not connected." },
  failed: { type: "error", text: "Could not read your X account. Please try again." },
  other_x: { type: "error", text: "This account is already connected to a different X account." },
  taken: {
    type: "error",
    text: "That X account already signs in to another Loore account, so it can't be connected here. If that account was made by accident, write to info@loore.org and we'll remove it so you can connect X here.",
  },
  taken_placeholder: {
    type: "error",
    text: "An account was already set up for that X account, and no one has signed in to it yet. Write to info@loore.org and we'll join it with this one.",
  },
};

export default function AccountPage() {
  const { user, setUser } = useUser();
  const navigate = useNavigate();
  const location = useLocation();

  // Connect X outcome: read once, then drop it from the URL so a reload
  // doesn't repeat the message.
  const [xMsg, setXMsg] = useState(
    () => X_LOGIN_MESSAGES[new URLSearchParams(location.search).get("x_login")] || null
  );
  const [xSaving, setXSaving] = useState(false);
  useEffect(() => {
    if (new URLSearchParams(location.search).has("x_login")) {
      navigate({ pathname: location.pathname, hash: location.hash }, { replace: true });
    }
  }, [location.search, location.pathname, location.hash, navigate]);

  const disconnectX = async () => {
    setXSaving(true);
    setXMsg(null);
    try {
      await api.delete("/dashboard/x");
      setUser((prev) => ({ ...prev, twitter_login: false, twitter_handle: null }));
      setXMsg({ type: "success", text: "X disconnected. You sign in with email." });
    } catch (e) {
      setXMsg({ type: "error", text: e.response?.data?.error || "Could not disconnect X." });
    } finally {
      setXSaving(false);
    }
  };

  // Deep-link anchors (e.g. /account#model from the changelog): scroll
  // the target row into view on arrival. BrowserRouter doesn't handle
  // hash scrolling on SPA navigations, and the previous page's scroll
  // offset carries over — without this the linked setting can sit just
  // off-screen.
  useEffect(() => {
    if (!location.hash) return;
    const el = document.getElementById(location.hash.slice(1));
    if (el) el.scrollIntoView({ block: "start" });
  }, [location.hash]);

  // Username editing
  const [username, setUsername] = useState(user?.username || "");
  const [usernameSaving, setUsernameSaving] = useState(false);
  const [usernameMsg, setUsernameMsg] = useState(null); // { type, text }
  const usernameInputRef = useRef(null);

  const [selectedModel, setSelectedModel] = useState(user?.preferred_model || null);

  // Email change / add (#260). The address only binds when the link sent
  // to it is confirmed from inside this account (ConfirmEmailPage); until
  // then the backend keeps it as pending_email and we show "check your
  // inbox".
  const [emailInput, setEmailInput] = useState("");
  const [emailSaving, setEmailSaving] = useState(false);
  const [emailMsg, setEmailMsg] = useState(null); // { type, text }
  const emailInputRef = useRef(null);
  // One request at a time. emailSaving alone does not do it: state lands a
  // render later, so a second handler in the same tick (or a double click)
  // still saw false, and each POST mails a link that voids the one before.
  const emailInFlight = useRef(false);

  const sendEmailLink = async (address, { resend = false } = {}) => {
    const value = (address || "").trim();
    if (!value || emailInFlight.current) return;
    emailInFlight.current = true;
    setEmailSaving(true);
    setEmailMsg(null);
    try {
      const res = await api.post("/dashboard/email", { email: value });
      setUser((prev) => ({ ...prev, ...emailState(res.data) }));
      setEmailInput("");
      // The pending notice below is the confirmation of a first send.
      if (resend) {
        setEmailMsg({ type: "success", text: "New link sent. The earlier one no longer works." });
      }
    } catch (e) {
      setEmailMsg({
        type: "error",
        text: e.response?.data?.error || "Could not send the confirmation email.",
      });
    } finally {
      emailInFlight.current = false;
      setEmailSaving(false);
    }
  };

  // Both answer with the account's email state as the server has it, so a
  // change confirmed elsewhere since this page loaded shows up here.
  const cancelPendingEmail = async () => {
    if (emailInFlight.current) return;
    emailInFlight.current = true;
    setEmailSaving(true);
    setEmailMsg(null);
    try {
      const res = await api.delete("/dashboard/email/pending");
      setUser((prev) => ({ ...prev, ...emailState(res.data) }));
    } catch (e) {
      setEmailMsg({ type: "error", text: "Could not cancel the pending change." });
    } finally {
      emailInFlight.current = false;
      setEmailSaving(false);
    }
  };

  const removeEmail = async () => {
    if (emailInFlight.current) return;
    emailInFlight.current = true;
    setEmailSaving(true);
    setEmailMsg(null);
    try {
      const res = await api.delete("/dashboard/email");
      setUser((prev) => ({ ...prev, ...emailState(res.data) }));
      setEmailMsg({ type: "success", text: "Email removed. You sign in with X." });
    } catch (e) {
      setEmailMsg({ type: "error", text: e.response?.data?.error || "Could not remove the email." });
    } finally {
      emailInFlight.current = false;
      setEmailSaving(false);
    }
  };

  // Cmd+Return / Ctrl+Enter; plain Enter is the input's onKeyDown, which
  // leaves the modified key to this hook so one keypress sends one link.
  useSubmitShortcut(
    emailInputRef,
    () => sendEmailLink(emailInput),
    !emailSaving && !!emailInput.trim(),
  );

  // Privacy / AI usage defaults
  const [privacySaving, setPrivacySaving] = useState(false);
  const [publicSideSaving, setPublicSideSaving] = useState(false);
  const [externalContentSaving, setExternalContentSaving] = useState(false);
  const [aiUsageSaving, setAiUsageSaving] = useState(false);
  const [craftSaving, setCraftSaving] = useState(false);

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

  // Cmd+Return / Ctrl+Enter saves the username (#129). Plain Enter already
  // saves via the input's onKeyDown; this keeps the shortcut uniform.
  useSubmitShortcut(
    usernameInputRef,
    () => { if (!usernameSaving && username.trim() !== user?.username) saveUsername(); },
    !usernameSaving && username.trim() !== user?.username,
  );

  const handleModelChange = useCallback(
    async (model) => {
      setSelectedModel(model);
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

  const inlineActionStyle = {
    background: "none", border: "none", padding: 0,
    color: "var(--accent)", cursor: "pointer",
    fontFamily: "var(--sans)", fontSize: "inherit",
  };

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
            ref={usernameInputRef}
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

      <div style={rowStyle} id="email">
        <div style={labelStyle}>Email</div>
        <div style={{ display: "flex", gap: "8px" }}>
          <input
            ref={emailInputRef}
            type="email"
            aria-label={user.email ? `New email address (current: ${user.email})` : "Email address"}
            value={emailInput}
            placeholder={user.email || "Add an email to sign in with"}
            onChange={(e) => {
              setEmailInput(e.target.value);
              setEmailMsg(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.metaKey && !e.ctrlKey) sendEmailLink(emailInput);
            }}
            style={{ ...inputStyle, flex: 1 }}
          />
          <button
            onClick={() => sendEmailLink(emailInput)}
            disabled={emailSaving || !emailInput.trim()}
            style={{
              padding: "8px 16px",
              borderRadius: "6px",
              border: "1px solid var(--accent)",
              background: "none",
              color: "var(--accent)",
              fontFamily: "var(--sans)",
              fontWeight: 300,
              fontSize: "0.85rem",
              whiteSpace: "nowrap",
              cursor: emailSaving || !emailInput.trim() ? "default" : "pointer",
              opacity: emailSaving || !emailInput.trim() ? 0.4 : 1,
            }}
          >
            {emailSaving ? "Sending..." : "Send confirmation link"}
          </button>
        </div>
        {user.pending_email && (
          <div style={helperStyle}>
            {user.pending_email_expired
              ? `The confirmation link sent to ${user.pending_email} has expired.`
              : `Confirmation link sent to ${user.pending_email}. Open it to make it your sign-in address. Nothing after a few minutes? Check the spelling; an address that already signs in to Loore can't be added here.`}
            {" "}
            <span style={{ whiteSpace: "nowrap" }}>
              <button
                type="button"
                onClick={() => sendEmailLink(user.pending_email, { resend: true })}
                disabled={emailSaving}
                style={inlineActionStyle}
              >
                {user.pending_email_expired ? "Send a new link" : "Resend"}
              </button>
              {" · "}
              <button
                type="button"
                onClick={cancelPendingEmail}
                disabled={emailSaving}
                style={inlineActionStyle}
              >
                Cancel
              </button>
            </span>
          </div>
        )}
        {emailMsg && (
          <div
            style={{
              ...helperStyle,
              color: emailMsg.type === "error" ? "var(--accent)" : "var(--text-muted)",
            }}
          >
            {emailMsg.text}
          </div>
        )}
        <div style={helperStyle}>
          {user.email
            ? "The new address becomes yours once you confirm it from the link we send to it."
            : "You currently sign in with X only."}
          {user.email && user.twitter_login && (
            <>
              {" "}
              <button
                type="button"
                onClick={removeEmail}
                disabled={emailSaving}
                style={{
                  background: "none", border: "none", padding: 0,
                  color: "var(--text-muted)", textDecoration: "underline",
                  cursor: "pointer", fontFamily: "var(--sans)", fontSize: "inherit",
                }}
              >
                Remove email
              </button>
            </>
          )}
        </div>
      </div>

      {/* Connect X (#311). The button leaves for X and comes back here
          with ?x_login=<outcome>#x; scrollMarginTop clears the navbar. */}
      <div id="x" style={{ ...rowStyle, scrollMarginTop: "72px" }}>
        <div style={labelStyle}>X</div>
        {user.twitter_login ? (
          <div
            style={{
              ...inputStyle,
              backgroundColor: "transparent",
              border: "1px solid var(--border)",
              opacity: 0.6,
            }}
          >
            {user.twitter_handle ? `Connected as @${user.twitter_handle}` : "Connected"}
          </div>
        ) : (
          <a
            href={`${backendUrl}/auth/x/connect`}
            style={{
              display: "inline-block",
              padding: "8px 16px",
              borderRadius: "6px",
              border: "1px solid var(--accent)",
              color: "var(--accent)",
              fontFamily: "var(--sans)",
              fontWeight: 300,
              fontSize: "0.85rem",
              textDecoration: "none",
            }}
          >
            Connect X
          </a>
        )}
        {xMsg && (
          <div
            style={{
              ...helperStyle,
              color: xMsg.type === "error" ? "var(--accent)" : "var(--text-muted)",
            }}
          >
            {xMsg.text}
          </div>
        )}
        <div style={helperStyle}>
          {user.twitter_login
            ? "Sign in with X opens this account."
            : "Lets you sign in with X as well. X will ask you to allow Loore."}
          {user.twitter_login && user.email && (
            <>
              {" "}
              <button
                type="button"
                onClick={disconnectX}
                disabled={xSaving}
                style={{
                  background: "none", border: "none", padding: 0,
                  color: "var(--text-muted)", textDecoration: "underline",
                  cursor: "pointer", fontFamily: "var(--sans)", fontSize: "inherit",
                }}
              >
                Disconnect X
              </button>
            </>
          )}
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

      {/* scrollMarginTop keeps the anchored row clear of the fixed navbar */}
      <div id="model" style={{ ...rowStyle, scrollMarginTop: "72px" }}>
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

      {user.external_content_available && (
        <div id="references" style={{ ...rowStyle, scrollMarginTop: "72px" }}>
          <div style={labelStyle}>External references</div>
          <select
            value={user.external_content_enabled ? "on" : "off"}
            disabled={externalContentSaving}
            onChange={(e) =>
              saveField("external_content_enabled", e.target.value === "on",
                setExternalContentSaving)
            }
            style={selectStyle}
          >
            <option value="off">Off</option>
            <option value="on">On (experimental)</option>
          </select>
          <div style={helperStyle}>
            Let Loore also search your saved external references (imported
            tweets, bookmarks and clipped web pages) during conversations,
            and quote what it finds. Your own archive is always searchable;
            this switch only adds references. Import bookmarks or set up
            the Chrome clipper on the{" "}
            <Link to="/import" style={{ color: "var(--accent)" }}>
              Import page
            </Link>. Experimental.
          </div>
        </div>
      )}

      {user.share_v1_available && (
        <div style={rowStyle}>
          <div style={labelStyle}>Public sharing</div>
          <select
            value={user.public_sharing_enabled ? "on" : "off"}
            disabled={publicSideSaving}
            onChange={(e) =>
              saveField("public_sharing_enabled", e.target.value === "on",
                setPublicSideSaving)
            }
            style={selectStyle}
          >
            <option value="off">Off</option>
            <option value="on">On (experimental)</option>
          </select>
          <div style={helperStyle}>
            The public side of Loore: publish shares to your public page
            and the Commons, and respond in public threads. Experimental.
          </div>
        </div>
      )}

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

      {/* The craft-mode switch also lives in the ⋮ menu; this row is the
          place a user who turned it on and forgot can find out what it
          does and switch it off. Anchor for deep links: /account#craft */}
      <div id="craft" style={{ ...rowStyle, scrollMarginTop: "72px" }}>
        <div style={{ ...labelStyle, display: "flex", alignItems: "center", gap: "8px" }}>
          <CraftIcon />Craft mode
        </div>
        <select
          value={user.craft_mode ? "on" : "off"}
          disabled={craftSaving}
          onChange={(e) =>
            saveField("craft_mode", e.target.value === "on", setCraftSaving)
          }
          style={selectStyle}
        >
          <option value="off">Off</option>
          <option value="on">On</option>
        </select>
        <div style={helperStyle}>
          Shows extra controls for people who want to steer the details:
          privacy and AI usage on each entry, the auto-generate switch and
          model picker on threads, audio upload, prompt editing and data
          export. Off is the simpler Loore. Menu items and controls added
          by craft mode carry the sliders icon.
        </div>
      </div>

      <div style={rowStyle}>
        <div style={labelStyle}>AI Preferences</div>
        <button
          onClick={() => navigate("/artifacts/ai_preferences")}
          style={{
            ...inputStyle,
            cursor: "pointer",
            textAlign: "left",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <span style={{ color: "var(--text-muted)", fontWeight: 300 }}>
            How AI interacts with you
          </span>
          <span style={{ color: "var(--accent)", fontSize: "0.85rem" }}>
            View
          </span>
        </button>
        <div style={helperStyle}>
          Tone, style, boundaries. Updated automatically during Voice sessions.
        </div>
      </div>
    </div>
  );
}
