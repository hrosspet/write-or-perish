import React, { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api";
import Bubble from "./Bubble";

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
    <div style={{ padding: "20px", maxWidth: "1000px", margin: "0 auto" }}>
      <h2 style={{ color: "#e0e0e0" }}>Feed</h2>
      <div style={{ display: "flex", flexDirection: "column"}}>
        {feedNodes.map(node => (
          <Bubble key={node.id} isHighlighted={true} node={node} onClick={handleBubbleClick} />
        ))}
      </div>
    </div>
  );
}

export default Feed;