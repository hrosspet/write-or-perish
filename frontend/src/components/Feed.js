import React, { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api";

// Bubble component styled similar to the NodeDetail view
function Bubble({ node, onClick }) {
  const text = node.content || node.preview || "";
  const datetime = node.created_at ? new Date(node.created_at).toLocaleString() : "";
  const childrenCount = node.child_count !== undefined ? node.child_count : 0;

  const style = {
    padding: "15px",
    margin: "10px 0",  // vertical margin between bubbles
    background: "#1e1e1e",
    border: "2px solid #61dafb",
    borderRadius: "8px",
    cursor: "pointer",
    whiteSpace: "pre-wrap",
    width: "100%",  // allow full width in the container
    boxShadow: "2px 2px 6px rgba(0,0,0,0.5)"
  };

  return (
    <div style={style} onClick={() => onClick(node.id)}>
      <div style={{ marginBottom: "8px" }}>
        {text.length > 120 ? text.substring(0, 250) + "..." : text}
      </div>
      <div style={{ fontSize: "0.8em", color: "#aba9a9" }}>
        {datetime} | {childrenCount} {childrenCount === 1 ? "child" : "children"}
      </div>
    </div>
  );
}

function Feed() {
  const [feedNodes, setFeedNodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    api.get("/feed")
      .then(response => {
        // Assuming the API returns only top-level nodes.
        setFeedNodes(response.data.nodes);
        setLoading(false);
      })
      .catch(err => {
        console.error(err);
        setError("Error loading feed.");
        setLoading(false);
      });
  }, []);

  const handleBubbleClick = (nodeId) => {
    navigate(`/node/${nodeId}`);
  };

  if (loading) return <div style={{ padding: "20px" }}>Loading feed...</div>;
  if (error) return <div style={{ padding: "20px", color: "red" }}>{error}</div>;

  return (
    <div style={{ padding: "20px", maxWidth: "600px", margin: "0 auto" }}>
      <h2 style={{ color: "#e0e0e0" }}>Feed</h2>
      {/* Vertical layout: */}
      <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
        {feedNodes.map(node => (
          <Bubble key={node.id} node={node} onClick={handleBubbleClick} />
        ))}
      </div>
    </div>
  );
}

export default Feed;