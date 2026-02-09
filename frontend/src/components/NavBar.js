import React from "react";
import { Link, useLocation } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import GlobalAudioPlayer from "./GlobalAudioPlayer";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function NavBar({ onNewEntryClick }) {
  const { user } = useUser();
  const location = useLocation();

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

        <GlobalAudioPlayer />
      </div>
    </nav>
  );
}

export default NavBar;
