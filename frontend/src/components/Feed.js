import React, { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import api from "../api";

function Feed() {
  const [feedNodes, setFeedNodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  
  const backendUrl = process.env.REACT_APP_BACKEND_URL;

  useEffect(() => {
    api
      .get("/feed")
      .then((response) => {
        setFeedNodes(response.data.nodes);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        if (err.response && err.response.status === 401) {
          window.location.href = `${backendUrl}/auth/login`;
        } else {
          setError("Error loading feed.");
          setLoading(false);
        }
      });
  }, [backendUrl]);

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
                <p>{node.username}: {node.preview}</p>
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

export default Feed;