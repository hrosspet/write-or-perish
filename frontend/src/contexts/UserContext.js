import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import api from "../api";
import { markSpendBlocked } from "../utils/spendCap";

// Create a new context
const UserContext = createContext(null);

// Provider component
export const UserProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Fetch current user info when the provider mounts. profile=0: only `user`
  // is read here, so the server leaves out the profile and decrypts nothing
  // on an app load (#481); the Profile page asks for the profile itself.
  useEffect(() => {
    api.get("/dashboard", { params: { profile: 0 } })
      .then((response) => {
        const fetchedUser = response.data.user;
        setUser(fetchedUser);
        setLoading(false);
        // Remember an already-capped user so cost actions (e.g. starting a
        // voice recording) can be blocked up front this session (issue #85).
        if (fetchedUser?.spend_blocked) markSpendBlocked();

        // Persist the browser timezone so the LLM context can render absolute
        // local-time stamps (#130). Only PATCH when it differs from what the
        // backend has stored, to avoid a redundant write on every load.
        try {
          const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
          if (browserTz && fetchedUser && browserTz !== fetchedUser.timezone) {
            api.patch("/dashboard/timezone", { timezone: browserTz })
              .then(() => {
                setUser((prev) =>
                  prev ? { ...prev, timezone: browserTz } : prev
                );
              })
              .catch((tzErr) => {
                // Non-fatal: stamps just fall back to the stored/UTC timezone.
                console.warn("Failed to persist timezone:", tzErr);
              });
          }
        } catch (tzDetectErr) {
          console.warn("Timezone detection unavailable:", tzDetectErr);
        }
      })
      .catch((err) => {
        console.error("Error fetching user:", err);
        setError(err);
        setLoading(false);
      });
  }, []);

  // The user just wrote an entry (Voice or Text): from now on the screens
  // ask "What's on your mind?" instead of the welcome question (#391).
  // Set here rather than refetched from /dashboard.
  const markHasOwnEntries = useCallback(() => {
    setUser((prev) => (prev && prev.has_own_entries === false
      ? { ...prev, has_own_entries: true }
      : prev));
  }, []);

  return (
    <UserContext.Provider value={{ user, loading, error, setUser, markHasOwnEntries }}>
      {children}
    </UserContext.Provider>
  );
};

// Hook for components to use the context
export const useUser = () => useContext(UserContext);