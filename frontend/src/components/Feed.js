import React, { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import api from "../api";

function Feed() {
  const [feedNodes, setFeedNodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Note: The backend must provide an endpoint for global feed data (e.g., GET /api/feed).
  useEffect(() => {
    api
      .get("/feed")
      .then((response) => {
        // Assume response.data.nodes is an array of top nodes
        setFeedNodes(response.data.nodes);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        setError("Error loading feed.");
        setLoading(false);
      });
  }, []);

  if (loading) return <div>Loading feed...</div>;
  if (error) return <div>{error}</div>;

  return (
    <div style={{ padding: "20px" }}>
      <h1>Global Feed</h1>
      <ul>
        {feedNodes.map((node) => (
          <li key={node.id} style={{ margin: "10px 0" }}>
            <Link to={`/node/${node.id}`}>
              <div>
                <p>{node.preview}</p>
                <small>Child Count: {node.child_count}</small>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default Feed;