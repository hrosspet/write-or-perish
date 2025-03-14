import React, { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import api from "../api";
import NodeForm from "./NodeForm";

function Dashboard() {
  const [dashboardData, setDashboardData] = useState(null);
  const [showNewNodeForm, setShowNewNodeForm] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Fetch dashboard info on mount
  useEffect(() => {
    api
      .get("/dashboard")
      .then((response) => {
        setDashboardData(response.data);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        setError("Error fetching dashboard data. Are you logged in?");
        setLoading(false);
      });
  }, []);

  const handleNewNode = () => {
    setShowNewNodeForm(!showNewNodeForm);
  };

  if (loading) return <div>Loading dashboard...</div>;
  if (error) return <div>{error}</div>;

  const { user, stats, nodes } = dashboardData;

  return (
    <div style={{ padding: "20px" }}>
      <h1>Dashboard</h1>
      <h2>Welcome, {user.username}</h2>
      <p>Description: {user.description || "No description provided."}</p>
      <h3>Your Token Stats</h3>
      <ul>
        <li>Daily tokens: {stats.daily_tokens}</li>
        <li>Total tokens: {stats.total_tokens}</li>
        <li>Global tokens: {stats.global_tokens}</li>
        <li>Daily target tokens: {stats.target_daily_tokens}</li>
      </ul>
      <h3>Your Entries</h3>
      <button onClick={handleNewNode}>
        {showNewNodeForm ? "Cancel" : "Write New Entry"}
      </button>
      {showNewNodeForm && (
        <NodeForm parentId={null} onSuccess={() => window.location.reload()} />
      )}
      <ul>
        {nodes.map((node) => (
          <li key={node.id} style={{ margin: "10px 0" }}>
            <Link to={`/node/${node.id}`}>
              <div>
                <p>{node.preview}</p>
                <small>
                  Created at: {new Date(node.created_at).toLocaleString()}
                </small>
                <br />
                <small>Child Count: {node.child_count}</small>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default Dashboard;