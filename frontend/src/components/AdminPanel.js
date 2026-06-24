import React, { useState, useEffect } from "react";
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

  const updateSpendLimit = async (userId) => {
    const raw = limitEdits[userId];
    if (raw === undefined || raw === "" || isNaN(parseFloat(raw))) {
      setError("Spend limit must be a number.");
      return;
    }
    try {
      const response = await api.put(
        `/admin/users/${userId}/update_spend_limit`,
        { limit_usd: parseFloat(raw) }
      );
      setError("");
      const blocked = response.data.spend_blocked;
      alert(
        `Spend limit set to $${parseFloat(raw).toFixed(2)}. User is now ` +
          `${blocked ? "BLOCKED" : "unblocked"} for this month.`
      );
      fetchUsers();
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
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Spent</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>This Month</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Limit ($)</th>
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
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                ${(u.total_spending_usd || 0).toFixed(2)}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                ${(u.current_month_spending_usd || 0).toFixed(2)}
                {u.spend_blocked && (
                  <span style={{ color: "var(--error)", fontWeight: 600, marginLeft: "6px" }}>
                    blocked
                  </span>
                )}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px", whiteSpace: "nowrap" }}>
                <input
                  type="number"
                  min="0"
                  step="1"
                  value={limitEdits[u.id] ?? ""}
                  onChange={(e) =>
                    setLimitEdits((prev) => ({ ...prev, [u.id]: e.target.value }))
                  }
                  onKeyDown={(e) => {
                    if (e.key === "Enter") updateSpendLimit(u.id);
                  }}
                  style={{ width: "70px", padding: "4px", marginRight: "6px" }}
                  title={
                    u.spend_limit_is_override
                      ? "Custom per-user limit"
                      : "Inherited from the global default"
                  }
                />
                <button onClick={() => updateSpendLimit(u.id)}>Save</button>
                {!u.spend_limit_is_override && (
                  <div style={{ fontSize: "0.75em", color: "var(--text-muted)" }}>
                    default
                  </div>
                )}
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