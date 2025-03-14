import React from "react";
import { Link } from "react-router-dom";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function NavBar() {
  return (
    <nav style={{ padding: "10px", borderBottom: "1px solid #ccc" }}>
      <Link to="/">Home</Link> {" | "}
      <Link to="/dashboard">Dashboard</Link> {" | "}
      <Link to="/feed">Feed</Link> {" | "}
      <a href={`${backendUrl}/auth/logout`}>Logout</a>
    </nav>
  );
}

export default NavBar;