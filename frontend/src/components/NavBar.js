import React from "react";
import { Link } from "react-router-dom";
import { useUser } from "../contexts/UserContext";  // Import the user context

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function NavBar({ onNewEntryClick }) {
  const { user } = useUser();

  // When "Write" is clicked:
  // If no user is logged in, redirect to login.
  // Otherwise, proceed to open the entry modal.
  const handleWriteClick = (e) => {
    e.preventDefault();
    if (!user) {
      window.location.href = `${backendUrl}/auth/login`;
    } else {
      onNewEntryClick();
    }
  };

  return (
    <nav
      style={{
        padding: "10px",
        backgroundColor: "#1f1f1f",
        borderBottom: "1px solid #333",
        display: "flex",
        alignItems: "center"
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

      {user && user.username === "hrosspet" && (
        <Link
          to="/admin"
          style={{ color: "#e0e0e0", textDecoration: "none", marginRight: "10px" }}
        >
          Admin
        </Link>
      )}

      {user && (
        <a
          href={`${backendUrl}/auth/logout`}
          style={{ color: "#e0e0e0", textDecoration: "none" }}
        >
          Logout
        </a>
      )}
    </nav>
  );
}

export default NavBar;