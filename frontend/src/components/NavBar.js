import React, { useState, useRef, useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import GlobalAudioPlayer from "./GlobalAudioPlayer";
import ModelSelector from "./ModelSelector";
import api from "../api";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

const aboutPaths = ["/why-loore", "/vision", "/how-to"];

function NavBar({ onNewEntryClick }) {
  const { user, setUser } = useUser();
  const location = useLocation();
  const [aboutOpen, setAboutOpen] = useState(false);
  const [overflowOpen, setOverflowOpen] = useState(false);
  const aboutRef = useRef(null);
  const overflowRef = useRef(null);

  // Craft mode state — read from user object or localStorage fallback
  const [craftMode, setCraftMode] = useState(() => {
    if (user && user.craft_mode !== undefined) return user.craft_mode;
    return localStorage.getItem('loore_craft_mode') === 'true';
  });

  // Profile generation state (craft mode)
  const [selectedModel, setSelectedModel] = useState(null);
  const [generatingProfile, setGeneratingProfile] = useState(false);
  const [profileTaskId, setProfileTaskId] = useState(null);
  const [profileProgress, setProfileProgress] = useState(0);
  const pollRef = useRef(null);

  // Sync craft mode when user loads
  useEffect(() => {
    if (user && user.craft_mode !== undefined) {
      setCraftMode(user.craft_mode);
    }
  }, [user]);

  const toggleCraftMode = async () => {
    const newValue = !craftMode;
    setCraftMode(newValue);
    localStorage.setItem('loore_craft_mode', String(newValue));

    // Try to persist to backend if the user has craft_mode field
    if (user) {
      try {
        const response = await api.put('/dashboard/user', { craft_mode: newValue });
        if (response.data.user) {
          setUser(response.data.user);
        }
      } catch (e) {
        // Silently fall back to localStorage — backend may not support craft_mode yet
      }
    }
  };

  // Fetch default model once
  useEffect(() => {
    if (user && !selectedModel) {
      api.get("/nodes/default-model")
        .then(r => setSelectedModel(r.data.suggested_model))
        .catch(() => setSelectedModel("claude-opus-4.6"));
    }
  }, [user, selectedModel]);

  // Profile generation handler
  const handleGenerateProfile = async () => {
    if (generatingProfile || profileTaskId) return;
    setGeneratingProfile(true);
    try {
      const res = await api.post("/export/update_profile", { model: selectedModel });
      if (res.data.status === "already_running") {
        setProfileTaskId(res.data.task_id);
      } else {
        setProfileTaskId(res.data.task_id);
      }
    } catch (err) {
      console.error("Profile generation error:", err);
      setGeneratingProfile(false);
    }
  };

  // Poll profile task status
  useEffect(() => {
    if (!profileTaskId) return;
    const poll = async () => {
      try {
        const res = await api.get(`/export/profile-status/${profileTaskId}`, {
          timeout: 10000, headers: { 'Cache-Control': 'no-cache' }
        });
        const { status, progress } = res.data;
        setProfileProgress(progress || 0);
        if (status === 'completed' || status === 'failed') {
          setGeneratingProfile(false);
          setProfileTaskId(null);
          setProfileProgress(0);
          if (status === 'completed') {
            // Reload page to show new profile
            window.location.reload();
          }
        }
      } catch (err) {
        console.error("Profile poll error:", err);
      }
    };
    poll();
    pollRef.current = setInterval(poll, 3000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [profileTaskId]);

  // When "Write" is clicked:
  const handleWriteClick = (e) => {
    e.preventDefault();
    setOverflowOpen(false);
    if (!user) {
      const returnUrl = encodeURIComponent(location.pathname + location.search);
      window.location.href = `/login?returnUrl=${returnUrl}`;
    } else {
      onNewEntryClick();
    }
  };

  const currentPath = location.pathname;
  const isAboutPage = aboutPaths.includes(currentPath);

  // Close dropdowns on click outside
  useEffect(() => {
    if (!aboutOpen && !overflowOpen) return;
    const handleClickOutside = (e) => {
      if (aboutOpen && aboutRef.current && !aboutRef.current.contains(e.target)) {
        setAboutOpen(false);
      }
      if (overflowOpen && overflowRef.current && !overflowRef.current.contains(e.target)) {
        setOverflowOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [aboutOpen, overflowOpen]);

  // Close dropdowns on navigation
  useEffect(() => {
    setAboutOpen(false);
    setOverflowOpen(false);
  }, [currentPath]);

  const linkStyle = (path) => ({
    color: currentPath === path ? 'var(--accent)' : 'var(--text-muted)',
    textDecoration: "none",
    fontFamily: "var(--sans)",
    fontWeight: 300,
    fontSize: "0.85rem",
    letterSpacing: "0.02em",
    transition: "color 0.3s ease",
  });

  const dropdownStyle = {
    position: "absolute",
    top: "100%",
    right: 0,
    marginTop: "12px",
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "8px",
    padding: "6px 0",
    minWidth: "220px",
    boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
    zIndex: 1001,
  };

  const dropdownItemStyle = {
    display: "block",
    padding: "10px 16px",
    fontFamily: "var(--sans)",
    fontWeight: 300,
    fontSize: "0.85rem",
    color: "var(--text-muted)",
    textDecoration: "none",
    cursor: "pointer",
    background: "none",
    border: "none",
    width: "100%",
    textAlign: "left",
    transition: "color 0.2s ease",
  };

  return (
    <nav
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        height: "56px",
        backgroundColor: "var(--bg-surface)",
        borderBottom: "1px solid var(--border)",
        display: "flex",
        alignItems: "center",
        zIndex: 1000,
        padding: "0 24px",
      }}
    >
      <Link
        to="/"
        style={{
          color: "var(--text-secondary)",
          textDecoration: "none",
          fontFamily: "var(--serif)",
          fontWeight: 300,
          fontSize: "1.15rem",
          textTransform: "uppercase",
          letterSpacing: "0.3em",
          marginRight: "auto",
          display: "flex",
          alignItems: "center",
          gap: "10px",
        }}
      >
        <img
          src="/loore-logo-transparent.svg"
          alt=""
          style={{ height: "22px", width: "auto", opacity: 0.7 }}
        />
        Loore
      </Link>

      <div style={{ display: "flex", alignItems: "center", gap: "clamp(0.8rem, 2vw, 1.8rem)" }}>
        {currentPath !== '/reflect' && currentPath !== '/orient' && <GlobalAudioPlayer />}

        {/* About dropdown */}
        <div ref={aboutRef} style={{ position: "relative" }}>
          <button
            onClick={() => setAboutOpen(!aboutOpen)}
            style={{
              color: isAboutPage ? 'var(--accent)' : 'var(--text-muted)',
              textDecoration: "none",
              fontFamily: "var(--sans)",
              fontWeight: 300,
              fontSize: "0.85rem",
              letterSpacing: "0.02em",
              transition: "color 0.3s ease",
              background: "none",
              border: "none",
              cursor: "pointer",
              padding: 0,
              display: "flex",
              alignItems: "center",
              gap: "3px",
            }}
          >
            About
            <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor" style={{ opacity: 0.6 }}>
              <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" fill="none" />
            </svg>
          </button>
          {aboutOpen && (
            <div style={{ ...dropdownStyle, left: "50%", right: "auto", transform: "translateX(-50%)" }}>
              <Link to="/why-loore" style={{ ...dropdownItemStyle, color: currentPath === "/why-loore" ? "var(--accent)" : "var(--text-muted)" }}>
                Why Loore
              </Link>
              <Link to="/vision" style={{ ...dropdownItemStyle, color: currentPath === "/vision" ? "var(--accent)" : "var(--text-muted)" }}>
                Vision
              </Link>
              <Link to="/how-to" style={{ ...dropdownItemStyle, color: currentPath === "/how-to" ? "var(--accent)" : "var(--text-muted)" }}>
                How To
              </Link>
            </div>
          )}
        </div>

        <Link to="/profile" style={linkStyle("/profile")}>
          Profile
        </Link>
        <Link to="/todo" style={linkStyle("/todo")}>
          Todo
        </Link>
        <Link to="/log" style={linkStyle("/log")}>
          Log
        </Link>

        {!user && (
          <Link
            to="/login?returnUrl=%2F"
            style={linkStyle("/login")}
          >
            Login
          </Link>
        )}

        {/* Three-dot overflow menu */}
        {user && (
          <div ref={overflowRef} style={{ position: "relative" }}>
            <button
              onClick={() => setOverflowOpen(!overflowOpen)}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                padding: "4px",
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: "3px",
                opacity: 0.5,
                transition: "opacity 0.2s ease",
              }}
              onMouseEnter={(e) => e.currentTarget.style.opacity = '0.8'}
              onMouseLeave={(e) => e.currentTarget.style.opacity = '0.5'}
            >
              <span style={{ width: "3px", height: "3px", borderRadius: "50%", background: "var(--text-muted)" }} />
              <span style={{ width: "3px", height: "3px", borderRadius: "50%", background: "var(--text-muted)" }} />
              <span style={{ width: "3px", height: "3px", borderRadius: "50%", background: "var(--text-muted)" }} />
            </button>
            {overflowOpen && (
              <div style={dropdownStyle}>
                {/* Always visible */}
                <Link to="/import" onClick={() => setOverflowOpen(false)} style={dropdownItemStyle}>
                  Import data
                </Link>

                {/* Craft mode items */}
                {craftMode && (
                  <>
                    <div style={{ borderTop: "1px solid var(--border)", margin: "4px 0" }} />
                    <button onClick={handleWriteClick} style={dropdownItemStyle}>
                      Write new entry
                    </button>
                    <Link to="/export" onClick={() => setOverflowOpen(false)} style={dropdownItemStyle}>
                      Export data
                    </Link>
                    <Link to="/prompts" onClick={() => setOverflowOpen(false)} style={dropdownItemStyle}>
                      Prompts
                    </Link>
                    <div style={{ ...dropdownItemStyle, cursor: "default", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span>Model</span>
                      <span style={{ fontSize: "0.75rem" }}>
                        <ModelSelector
                          nodeId={null}
                          selectedModel={selectedModel}
                          onModelChange={setSelectedModel}
                        />
                      </span>
                    </div>
                    <button
                      onClick={() => { setOverflowOpen(false); handleGenerateProfile(); }}
                      disabled={generatingProfile || !!profileTaskId}
                      style={{
                        ...dropdownItemStyle,
                        color: "var(--text-muted)",
                        cursor: (generatingProfile || profileTaskId) ? "not-allowed" : "pointer",
                        opacity: (generatingProfile || profileTaskId) ? 0.6 : 1,
                      }}
                    >
                      {profileTaskId
                        ? `Generating... ${profileProgress}%`
                        : generatingProfile
                        ? "Starting..."
                        : "Generate Profile"}
                    </button>
                  </>
                )}

                {/* Bottom section */}
                <div style={{ borderTop: "1px solid var(--border)", margin: "4px 0" }} />

                {user.is_admin && (
                  <Link to="/admin" onClick={() => setOverflowOpen(false)} style={dropdownItemStyle}>
                    Admin
                  </Link>
                )}

                {/* Craft mode toggle */}
                <button
                  onClick={toggleCraftMode}
                  style={{ ...dropdownItemStyle, display: "flex", justifyContent: "space-between", alignItems: "center" }}
                >
                  <span>Craft mode</span>
                  <div style={{
                    width: "32px",
                    height: "18px",
                    borderRadius: "9px",
                    background: craftMode ? "var(--accent)" : "var(--border)",
                    position: "relative",
                    transition: "background 0.2s ease",
                  }}>
                    <div style={{
                      width: "14px",
                      height: "14px",
                      borderRadius: "50%",
                      background: "var(--text-primary)",
                      position: "absolute",
                      top: "2px",
                      left: craftMode ? "16px" : "2px",
                      transition: "left 0.2s ease",
                    }} />
                  </div>
                </button>

                <a href={`${backendUrl}/auth/logout`} style={dropdownItemStyle}>
                  Logout
                </a>
              </div>
            )}
          </div>
        )}
      </div>
    </nav>
  );
}

export default NavBar;
