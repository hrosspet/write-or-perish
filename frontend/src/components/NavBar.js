import React from "react";
import { Link } from "react-router-dom";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function NavBar() {
  return (
    <nav
      style={{
        padding: "10px",
        /* Use a dark background and a darker border */
        backgroundColor: "#1f1f1f",
        borderBottom: "1px solid #333"
      }}
    >
      <Link style={{ color: "#e0e0e0", textDecoration: "none" }} to="/">
        Home
      </Link>{" "}
      {" | "}
      <Link style={{ color: "#e0e0e0", textDecoration: "none" }} to="/dashboard">
        Dashboard
      </Link>{" "}
      {" | "}
      <Link style={{ color: "#e0e0e0", textDecoration: "none" }} to="/feed">
        Feed
      </Link>{" "}
      {" | "}
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