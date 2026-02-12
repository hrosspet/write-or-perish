import React, { useState, useRef, useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import GlobalAudioPlayer from "./GlobalAudioPlayer";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

const aboutPaths = ["/why-loore", "/vision", "/how-to"];

function NavBar({ onNewEntryClick }) {
  const { user } = useUser();
  const location = useLocation();
  const [aboutOpen, setAboutOpen] = useState(false);
  const aboutRef = useRef(null);

  // When "Write" is clicked:
  // If no user is logged in, redirect to login page with return URL.
  // Otherwise, proceed to open the entry modal.
  const handleWriteClick = (e) => {
    e.preventDefault();
    if (!user) {
      const returnUrl = encodeURIComponent(location.pathname + location.search);
      window.location.href = `/login?returnUrl=${returnUrl}`;
    } else {
      onNewEntryClick();
    }
  };

  const currentPath = location.pathname;
  const isAboutPage = aboutPaths.includes(currentPath);

  // Close dropdown on click outside
  useEffect(() => {
    if (!aboutOpen) return;
    const handleClickOutside = (e) => {
      if (aboutRef.current && !aboutRef.current.contains(e.target)) {
        setAboutOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [aboutOpen]);

  // Close dropdown on navigation
  useEffect(() => {
    setAboutOpen(false);
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
        <GlobalAudioPlayer />

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
            }}
          >
            About
          </button>
          {aboutOpen && (
            <div style={{
              position: "absolute",
              top: "100%",
              left: "50%",
              transform: "translateX(-50%)",
              marginTop: "12px",
              background: "var(--bg-card)",
              border: "1px solid var(--border)",
              borderRadius: "8px",
              padding: "6px 0",
              minWidth: "140px",
              boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
              zIndex: 1001,
            }}>
              <Link
                to="/why-loore"
                style={{
                  display: "block",
                  padding: "10px 16px",
                  ...linkStyle("/why-loore"),
                  fontSize: "0.85rem",
                }}
              >
                Why Loore
              </Link>
              <Link
                to="/vision"
                style={{
                  display: "block",
                  padding: "10px 16px",
                  ...linkStyle("/vision"),
                  fontSize: "0.85rem",
                }}
              >
                Vision
              </Link>
              <Link
                to="/how-to"
                style={{
                  display: "block",
                  padding: "10px 16px",
                  ...linkStyle("/how-to"),
                  fontSize: "0.85rem",
                }}
              >
                How To
              </Link>
            </div>
          )}
        </div>

        <Link to="/dashboard" style={linkStyle("/dashboard")}>
          Dashboard
        </Link>
        <Link to="/feed" style={linkStyle("/feed")}>
          Feed
        </Link>

        <Link
          to="#"
          onClick={handleWriteClick}
          style={linkStyle("")}
        >
          Write
        </Link>

        {user && user.is_admin && (
          <Link
            to="/admin"
            style={linkStyle("/admin")}
          >
            Admin
          </Link>
        )}

        {!user && (
          <Link
            to={`/login?returnUrl=${encodeURIComponent(location.pathname + location.search)}`}
            style={linkStyle("/login")}
          >
            Login
          </Link>
        )}

        {user && (
          <a
            href={`${backendUrl}/auth/logout`}
            style={linkStyle("")}
          >
            Logout
          </a>
        )}

      </div>
    </nav>
  );
}

export default NavBar;
