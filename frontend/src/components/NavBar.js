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

  return (
    <nav
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        padding: "10px",
        backgroundColor: "#1f1f1f",
        borderBottom: "1px solid #333",
        display: "flex",
        alignItems: "center",
        zIndex: 1000
      }}
    >
      <Link to="/" style={{ color: "#e0e0e0", textDecoration: "none", marginRight: "10px" }}>
        Home
      </Link>
      <Link to="/dashboard" style={{ color: "#e0e0e0", textDecoration: "none", marginRight: "10px" }}>
        Dashboard
      </Link>
      <Link to="/feed" style={{ color: "#e0e0e0", textDecoration: "none", marginRight: "10px" }}>
        Feed
      </Link>

      <Link
        to="#"
        onClick={handleWriteClick}
        style={{ color: "#e0e0e0", textDecoration: "none", marginRight: "10px" }}
      >
        Write
      </Link>

      {user && user.is_admin && (
        <Link
          to="/admin"
          style={{ color: "#e0e0e0", textDecoration: "none", marginRight: "10px" }}
        >
          Admin
        </Link>
      )}

      {!user && (
        <Link
          to={`/login?returnUrl=${encodeURIComponent(location.pathname + location.search)}`}
          style={{ color: "#e0e0e0", textDecoration: "none", marginRight: "10px" }}
        >
          Login
        </Link>
      )}

      {user && (
        <a
          href={`${backendUrl}/auth/logout`}
          style={{ color: "#e0e0e0", textDecoration: "none", marginRight: "10px" }}
        >
          Logout
        </a>
      )}

      <GlobalAudioPlayer />
    </nav>
  );
}

export default NavBar;