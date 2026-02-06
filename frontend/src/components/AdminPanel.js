import React, { useState, useEffect } from "react";
import api from "../api";

function AdminPanel() {
  const [users, setUsers] = useState([]);
  const [allowedPlans, setAllowedPlans] = useState([]);
  const [error, setError] = useState("");
  const [newHandle, setNewHandle] = useState("");
  const [newHandleError, setNewHandleError] = useState("");

  const fetchUsers = async () => {
    try {
      const response = await api.get("/admin/users");
      setUsers(response.data.users);
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
      <div style={{ marginBottom: "20px", padding: "10px", border: "1px solid #333" }}>
        <h2>Whitelist a New User</h2>
        <input
          type="text"
          value={newHandle}
          onChange={(e) => setNewHandle(e.target.value)}
          placeholder="Enter user handle"
          style={{ padding: "8px", marginRight: "10px" }}
        />
        <button onClick={handleWhitelistUser}>Whitelist User</button>
        {newHandleError && <div style={{ color: "red" }}>{newHandleError}</div>}
      </div>

      {error && <div style={{ color: "red" }}>{error}</div>}
      <table style={{ width: "100%", borderCollapse: "collapse", color: "#e0e0e0" }}>
        <thead>
          <tr>
            <th style={{ border: "1px solid #333", padding: "8px" }}>ID</th>
            <th style={{ border: "1px solid #333", padding: "8px" }}>Username</th>
            <th style={{ border: "1px solid #333", padding: "8px" }}>Approved</th>
            <th style={{ border: "1px solid #333", padding: "8px" }}>Plan</th>
            <th style={{ border: "1px solid #333", padding: "8px" }}>Email</th>
            <th style={{ border: "1px solid #333", padding: "8px" }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td style={{ border: "1px solid #333", padding: "8px" }}>{u.id}</td>
              <td style={{ border: "1px solid #333", padding: "8px" }}>{u.username}</td>
              <td style={{ border: "1px solid #333", padding: "8px" }}>
                {u.approved ? "Active" : "Inactive"}
              </td>
              <td style={{ border: "1px solid #333", padding: "8px" }}>
                <select
                  value={u.plan || "free"}
                  onChange={(e) => updatePlan(u.id, e.target.value)}
                >
                  {allowedPlans.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </td>
              <td style={{ border: "1px solid #333", padding: "8px" }}>
                {u.email || "None"}
              </td>
              <td style={{ border: "1px solid #333", padding: "8px" }}>
                <button onClick={() => toggleApproved(u.id)}>
                  {u.approved ? "Deactivate" : "Activate"}
                </button>{" "}
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