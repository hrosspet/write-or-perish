import React from "react";
import { Link } from "react-router-dom";
import { useUser } from "../contexts/UserContext";  // Import the user context

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function NavBar({ onNewEntryClick }) {
  const { user } = useUser();
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
        onClick={(e) => {
          e.preventDefault(); // Prevent navigation
          onNewEntryClick();
        }}
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
      <a
        style={{ color: "#e0e0e0", textDecoration: "none" }}
        href={`${backendUrl}/auth/logout`}
      >
        Logout
      </a>
    </nav>
  );
}

export default NavBar;