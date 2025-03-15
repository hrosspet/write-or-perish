import React, { useState, useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import api from "../api";
import NodeForm from "./NodeForm";
import DashboardContent from "./DashboardContent";  // Import the new component

function Dashboard() {
  const { username } = useParams(); // if present, we are viewing someone else's dashboard
  const [dashboardData, setDashboardData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  
  // For profile editing—only allowed for your own dashboard.
  const [editingProfile, setEditingProfile] = useState(false);
  const [editUsername, setEditUsername] = useState("");
  const [editDescription, setEditDescription] = useState("");
  
  const backendUrl = process.env.REACT_APP_BACKEND_URL;

  // Decide which endpoint to call
  const endpoint = username ? `/dashboard/${username}` : "/dashboard";

  useEffect(() => {
    api.get(endpoint)
      .then((response) => {
        setDashboardData(response.data);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        if (err.response && err.response.status === 401) {
          window.location.href = `${backendUrl}/auth/login`;
        } else {
          setError("Error fetching dashboard data. Are you logged in?");
          setLoading(false);
        }
      });
  }, [endpoint, backendUrl]);

  const handleProfileSubmit = (e) => {
    e.preventDefault();
    api.put("/dashboard/user", { username: editUsername, description: editDescription })
      .then((response) => {
        setDashboardData({
          ...dashboardData,
          user: response.data.user
        });
        setEditingProfile(false);
      })
      .catch((err) => {
        console.error(err);
        setError("Error updating profile.");
      });
  };

  if (loading) return <div>Loading dashboard...</div>;
  if (error) return <div>{error}</div>;

  const { user, stats, nodes } = dashboardData;

  return (
    <div style={{ padding: "20px" }}>
      <h1>{user.username}</h1>
      
      {/* Only allow profile editing on your own dashboard */}
      {!username && (
        <>
          {editingProfile ? (
            <form onSubmit={handleProfileSubmit}>
              <div>
                <label>
                  Handle:
                  <input
                    type="text"
                    value={editUsername}
                    onChange={(e) => setEditUsername(e.target.value)}
                  />
                </label>
              </div>
              <div>
                <label>
                  Description:
                  <input
                    type="text"
                    maxLength={128}
                    value={editDescription}
                    onChange={(e) => setEditDescription(e.target.value)}
                  />
                </label>
              </div>
              <button type="submit">Save Profile</button>
              <button type="button" onClick={() => setEditingProfile(false)}>
                Cancel
              </button>
            </form>
          ) : (
            <div>
              <p>{user.description || "No description provided."}</p>
              <button
                onClick={() => {
                  setEditingProfile(true);
                  setEditUsername(user.username);
                  setEditDescription(user.description);
                }}
              >
                Edit Profile
              </button>
            </div>
          )}
        </>
      )}

      {/* Only your own dashboard shows token stats */}
      {dashboardData.stats && <DashboardContent dashboardData={dashboardData} />}

      <h3>Top-Level Entries</h3>
      <ul>
        {nodes.map((node) => (
          <li key={node.id} style={{ margin: "10px 0" }}>
            <Link to={`/node/${node.id}`}>
              <div>
                <p>{node.preview}</p>
                <small>
                  {new Date(node.created_at).toLocaleString()} | children: {node.child_count}
                </small>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default Dashboard;