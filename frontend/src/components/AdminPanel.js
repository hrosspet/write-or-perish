import React, { useState, useEffect } from "react";
import { FaTimesCircle } from "react-icons/fa";
import api from "../api";

function AdminPanel() {
  const [users, setUsers] = useState([]);
  const [allowedPlans, setAllowedPlans] = useState([]);
  const [error, setError] = useState("");
  const [newHandle, setNewHandle] = useState("");
  const [newHandleError, setNewHandleError] = useState("");
  // Per-user spend-limit input values, keyed by user id (controlled inputs).
  const [limitEdits, setLimitEdits] = useState({});

  const fetchUsers = async () => {
    try {
      const response = await api.get("/admin/users");
      const fetched = response.data.users;
      setUsers(fetched);
      // Pre-fill each row's limit input with the user's effective cap.
      const edits = {};
      fetched.forEach((u) => {
        edits[u.id] = String(u.spend_limit_usd ?? "");
      });
      setLimitEdits(edits);
      if (response.data.allowed_plans) {
        setAllowedPlans(response.data.allowed_plans);
      }
    } catch (err) {
      console.error(err);
      setError("Error fetching users.");
    }
  };

  useEffect(() => {
    fetchUsers();
  }, []);

  const toggleApproved = async (userId) => {
    try {
      await api.post(`/admin/users/${userId}/toggle`);
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error toggling user status.");
    }
  };

  const updateEmail = async (userId, currentEmail) => {
    const newEmail = prompt("Enter new email:", currentEmail || "");
    if (newEmail === null) return; // cancelled
    try {
      await api.put(`/admin/users/${userId}/update_email`, { email: newEmail });
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error updating email.");
    }
  };

  const updatePlan = async (userId, newPlan) => {
    try {
      await api.put(`/admin/users/${userId}/update_plan`, { plan: newPlan });
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error updating plan.");
    }
  };

  // Auto-save the limit input on Enter / Cmd|Ctrl+Enter / blur — but only when
  // the value actually changed from the user's last-saved limit. Invalid input
  // reverts to the saved value. Block/unblock feedback shows via the row's
  // red-cross icon after the refresh (no modal — keeps auto-save seamless).
  const maybeSaveLimit = (userId) => {
    const user = users.find((u) => u.id === userId);
    if (!user) return;
    const raw = limitEdits[userId];
    const original = user.spend_limit_usd ?? null;
    const parsed = parseFloat(raw);
    if (raw === undefined || raw === "" || isNaN(parsed) || parsed < 0) {
      // Revert the input to the last saved value.
      setLimitEdits((prev) => ({ ...prev, [userId]: String(original ?? "") }));
      return;
    }
    if (parsed === original) return; // unchanged — nothing to save
    saveSpendLimit(userId, parsed);
  };

  const saveSpendLimit = async (userId, value) => {
    try {
      await api.put(`/admin/users/${userId}/update_spend_limit`, {
        limit_usd: value,
      });
      setError("");
      fetchUsers(); // refreshes the row (incl. the blocked icon)
    } catch (err) {
      console.error(err);
      setError(err.response?.data?.error || "Error updating spend limit.");
    }
  };

  const activateAndWelcome = async (userId) => {
    try {
      const response = await api.post(`/admin/users/${userId}/activate_and_welcome`);
      const msg = response.data.email_sent
        ? "User approved and welcome email sent!"
        : "User approved but email failed to send.";
      alert(msg);
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError(err.response?.data?.error || "Error activating user.");
    }
  };

  const handleWhitelistUser = async () => {
    if (!newHandle.trim()) {
      setNewHandleError("Handle is required.");
      return;
    }
    try {
      await api.post("/admin/whitelist", { handle: newHandle });
      setNewHandle("");
      setNewHandleError("");
      fetchUsers();
    } catch (err) {
      console.error(err);
      setNewHandleError(err.response?.data?.error || "Error whitelisting user.");
    }
  };

  return (
    <div style={{ padding: "20px" }}>
      <h1>Admin Panel</h1>

      {/* Whitelist New User Section */}
      <div style={{ marginBottom: "20px", padding: "10px", border: "1px solid var(--border)" }}>
        <h2>Whitelist a New User</h2>
        <input
          type="text"
          value={newHandle}
          onChange={(e) => setNewHandle(e.target.value)}
          placeholder="Enter user handle"
          style={{ padding: "8px", marginRight: "10px" }}
        />
        <button onClick={handleWhitelistUser}>Whitelist User</button>
        {newHandleError && <div style={{ color: "var(--error)" }}>{newHandleError}</div>}
      </div>

      {error && <div style={{ color: "var(--error)" }}>{error}</div>}
      <table style={{ width: "100%", borderCollapse: "collapse", color: "var(--text-primary)" }}>
        <thead>
          <tr>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>ID</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Username</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Approved</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Plan</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Email</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px", width: "90px" }}>Spent</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px", width: "90px" }}>This<br />Month</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px", width: "64px" }}>Limit ($)</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>{u.id}</td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>{u.username}</td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                {u.approved ? "Active" : "Inactive"}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                <select
                  value={u.plan || "free"}
                  onChange={(e) => updatePlan(u.id, e.target.value)}
                >
                  {allowedPlans.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                {u.email || "None"}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px", width: "90px" }}>
                ${(u.total_spending_usd || 0).toFixed(2)}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px", width: "90px", whiteSpace: "nowrap" }}>
                ${(u.current_month_spending_usd || 0).toFixed(2)}
                {u.spend_blocked && (
                  <FaTimesCircle
                    title="Blocked this month"
                    aria-label="Blocked this month"
                    style={{ color: "var(--error)", marginLeft: "6px", verticalAlign: "middle" }}
                  />
                )}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px", width: "64px" }}>
                <input
                  type="number"
                  min="0"
                  step="1"
                  value={limitEdits[u.id] ?? ""}
                  onChange={(e) =>
                    setLimitEdits((prev) => ({ ...prev, [u.id]: e.target.value }))
                  }
                  onKeyDown={(e) => {
                    // Enter and Cmd/Ctrl+Enter both fire with key === "Enter".
                    if (e.key === "Enter") e.currentTarget.blur();
                  }}
                  onBlur={() => maybeSaveLimit(u.id)}
                  style={{ width: "48px", padding: "8px 4px", boxSizing: "border-box" }}
                  title={
                    u.spend_limit_is_override
                      ? "Custom per-user limit"
                      : "Inherited from the global default"
                  }
                />
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                {!u.approved && u.email ? (
                  <>
                    <button onClick={() => activateAndWelcome(u.id)}>
                      Activate &amp; Welcome
                    </button>{" "}
                    <button onClick={() => toggleApproved(u.id)}>
                      Activate
                    </button>
                  </>
                ) : (
                  <button onClick={() => toggleApproved(u.id)}>
                    {u.approved ? "Deactivate" : "Activate"}
                  </button>
                )}{" "}
                <button onClick={() => updateEmail(u.id, u.email)}>
                  Update Email
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default AdminPanel;