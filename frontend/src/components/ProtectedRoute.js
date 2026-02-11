import React from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useUser } from "../contexts/UserContext";

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

  // If not authenticated, redirect to login with returnUrl
  if (!user) {
    const returnUrl = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?returnUrl=${returnUrl}`} replace />;
  }

  // If authenticated but not approved, redirect to alpha-thank-you
  if (!user.approved) {
    return <Navigate to="/alpha-thank-you" replace />;
  }

  // User is authenticated and approved, render children
  return children;
}

export default ProtectedRoute;
