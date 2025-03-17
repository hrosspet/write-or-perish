import React, { createContext, useContext, useState, useEffect } from "react";
import api from "../api";

// Create a new context
const UserContext = createContext(null);

// Provider component
export const UserProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Fetch current user info when the provider mounts.
  useEffect(() => {
    api.get("/dashboard")  // Or another endpoint you use to get current user info.
      .then((response) => {
        // Assume the endpoint returns a { user: { id: ..., username: ... } } object.
        setUser(response.data.user);
        setLoading(false);
      })
      .catch((err) => {
        console.error("Error fetching user:", err);
        setError(err);
        setLoading(false);
      });
  }, []);

  return (
    <UserContext.Provider value={{ user, loading, error, setUser }}>
      {children}
    </UserContext.Provider>
  );
};

// Hook for components to use the context
export const useUser = () => useContext(UserContext);