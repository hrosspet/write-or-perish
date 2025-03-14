import React from "react";
import { Link } from "react-router-dom";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function NavBar() {
  return (
    <nav style={{ padding: "10px", borderBottom: "1px solid #ccc" }}>
      <Link to="/">Home</Link> {" | "}
      <Link to="/dashboard">Dashboard</Link> {" | "}
      <Link to="/feed">Feed</Link> {" | "}
      {/* 
          The login button simply sends the user to the Flask
          OAuth endpoint. (Logout would be similar.)
      */}
      <a href={`${backendUrl}/auth/login`}>Login with Twitter</a>
    </nav>
  );
}

export default NavBar;