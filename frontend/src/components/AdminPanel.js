import React, { useState, useEffect } from "react";
import api from "../api";

function AdminPanel() {
  const [users, setUsers] = useState([]);
  const [error, setError] = useState("");

  const fetchUsers = async () => {
    try {
      const response = await api.get("/admin/users");
      setUsers(response.data.users);
    } catch (err) {
      console.error(err);
      setError("Error fetching users.");
    }
  };

  useEffect(() => {
    fetchUsers();
  }, []);

  // Toggle a user's approved status.
  const toggleApproved = async (userId) => {
    try {
      await api.post(`/admin/users/${userId}/toggle`);
      fetchUsers(); // refresh list after toggling
    } catch (err) {
      console.error(err);
      setError("Error toggling user status.");
    }
  };

  // Allow updating the user's email.
  const updateEmail = async (userId, currentEmail) => {
    const newEmail = prompt("Enter new email:", currentEmail || "");
    if (newEmail === null) return; // canceled
    try {
      await api.put(`/admin/users/${userId}/update_email`, { email: newEmail });
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error updating email.");
    }
  };

  return (
    <div style={{ padding: "20px" }}>
      <h1>Admin Panel</h1>
      {error && <div style={{ color: "red" }}>{error}</div>}
      <table style={{ width: "100%", borderCollapse: "collapse", color: "#e0e0e0" }}>
        <thead>
          <tr>
            <th style={{ border: "1px solid #333", padding: "8px" }}>ID</th>
            <th style={{ border: "1px solid #333", padding: "8px" }}>Username</th>
            <th style={{ border: "1px solid #333", padding: "8px" }}>Approved</th>
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