import React from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import { here, signInPath } from "../utils/destination";

function ProtectedRoute({ children }) {
  const { user, loading } = useUser();
  const location = useLocation();

  // While loading, show a loading state
  if (loading) {
    return (
      <div style={{ padding: "40px", textAlign: "center" }}>
        <p>Loading...</p>
      </div>
    );
  }

  // If not authenticated, redirect to login, on the way back here (#704)
  if (!user) {
    return <Navigate to={signInPath(here(location))} replace />;
  }

  // If authenticated but not approved, redirect to alpha-thank-you
  if (!user.approved) {
    return <Navigate to="/alpha-thank-you" replace />;
  }

  // User is authenticated and approved, render children
  return children;
}

export default ProtectedRoute;
