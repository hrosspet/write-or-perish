import React, { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import api from "../api";
import NodeForm from "./NodeForm";

// Bubble component – shows a node preview or (if highlighted) full text.
function Bubble({ node, isHighlighted = false, onClick }) {
  // Use node.content if provided; if not, use node.preview or an empty string.
  const text = node.content || node.preview || "";
  // Format the datetime (assuming node.created_at is an ISO string).
  const datetime = node.created_at ? new Date(node.created_at).toLocaleString() : "";
  // Determine the number of children.
  // Use node.child_count if provided; otherwise fall back to node.children.length.
  const childrenCount =
    node.child_count !== undefined
      ? node.child_count
      : node.children
      ? node.children.length
      : 0;

  const style = {
    padding: "10px",
    margin: "5px 0",
    background: isHighlighted ? "#2e2e2e" : "#1e1e1e",
    border: isHighlighted ? "2px solid #61dafb" : "1px solid #333",
    cursor: "pointer",
    whiteSpace: "pre-wrap",
  };

  return (
    <div style={style} onClick={() => onClick(node.id)}>
      <div>
        {isHighlighted
          ? text
          : text.length > 80
          ? text.substring(0, 80) + "..."
          : text}
      </div>
      <div style={{ fontSize: "0.7em", color: "#aba9a9", marginTop: "5px", marginLeft: "5px" }}>
        {datetime} | {childrenCount} {childrenCount === 1 ? "child" : "children"}
      </div>
    </div>
  );
}

// Recursive component to render the entire descendants tree.
// Each level is indented (via marginLeft) and shows a left border edge.
function RenderChildTree({ nodes, onBubbleClick }) {
  return (
    <div>
      {nodes.map((child, index) => {
        // Only apply indenting if there is more than one node in this array.
        const shouldIndent = nodes.length > 1;
        const containerStyle = shouldIndent
          ? {
              marginLeft: "20px",
              paddingLeft: "10px",
              borderLeft: "2px solid #61dafb",
            }
          : { marginLeft: "0px" };

        return (
          <div key={child.id}>
            <div style={containerStyle}>
              <Bubble node={child} onClick={onBubbleClick} />
              {child.children &&
                child.children.length > 0 && (
                  <RenderChildTree
                    nodes={child.children}
                    onBubbleClick={onBubbleClick}
                  />
                )}
            </div>
            {index < nodes.length - 1 && (
              <hr
                style={{
                  borderColor: "#333",
                  marginLeft: shouldIndent ? "20px" : "0px",
                }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}


function NodeDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user: currentUser } = useUser();
  const [node, setNode] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showChildFormOverlay, setShowChildFormOverlay] = useState(false);
  const [showEditOverlay, setShowEditOverlay] = useState(false);
  const backendUrl = process.env.REACT_APP_BACKEND_URL;

  useEffect(() => {
    api
      .get(`/nodes/${id}`)
      .then((response) => {
        setNode(response.data);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        if (err.response?.status === 401) {
          window.location.href = `${backendUrl}/auth/login`;
        } else {
          setError("Error fetching node details.");
          setLoading(false);
        }
      });
  }, [id, backendUrl]);

  if (loading) return <div>Loading node...</div>;
  if (error) return <div>{error}</div>;
  if (!node) return <div>No node found.</div>;

  // Helper to navigate when a bubble is clicked.
  const handleBubbleClick = (nodeId) => {
    navigate(`/node/${nodeId}`);
  };

  // Only show edit & delete if the current user is the owner.
  const isOwner = node.user && currentUser && node.user.id === currentUser.id;

  // Action handlers for buttons.
  const handleDelete = () => {
    if (
      window.confirm(
        "Are you sure you want to delete this node? This will remove the node and orphan all its children."
      )
    ) {
      api
        .delete(`/nodes/${id}`)
        .then(() => {
          navigate("/dashboard");
        })
        .catch((err) => {
          console.error(err);
          setError("Error deleting node.");
        });
    }
  };

  const handleLLMResponse = () => {
    api
      .post(`/nodes/${id}/llm`)
      .then((response) => {
        navigate(`/node/${response.data.node.id}`);
      })
      .catch((err) => {
        console.error(err);
        setError("Error requesting LLM response.");
      });
  };

  // Render ancestors as a vertical list.
  const ancestorsSection = (
    <div style={{ display: "flex", flexDirection: "column", marginBottom: "10px" }}>
      {node.ancestors && node.ancestors.map((ancestor) => (
      <Bubble key={ancestor.id} node={ancestor} onClick={handleBubbleClick} />
    ))}
    </div>
  );

  // Highlighted node section – full text with action buttons directly beneath.
  const highlightedNodeSection = (
    <div>
      <hr style={{ borderColor: "#333" }} />
      <div
        style={{
          padding: "10px",
          margin: "10px 0",
          backgroundColor: "#2e2e2e",
          border: "2px solid #61dafb",
          borderLeft: "4px solid #61dafb",
          whiteSpace: "pre-wrap",
        }}
      >
        {node.content}
      </div>
      <div style={{ marginBottom: "10px" }}>
        <button onClick={() => setShowChildFormOverlay(true)}>Add Text</button>{" "}
        <button onClick={handleLLMResponse}>LLM Response</button>{" "}
        {isOwner && <button onClick={() => setShowEditOverlay(true)}>Edit</button>}{" "}
        {isOwner && <button onClick={handleDelete}>Delete</button>}
      </div>
      <hr style={{ borderColor: "#333" }} />
    </div>
  );

  // Render the full descendant tree recursively.
  const childrenSection = (
    <div>
      {node.children && node.children.length > 0 && (
      <RenderChildTree nodes={node.children} onBubbleClick={handleBubbleClick} />
    )}
    </div>
  );

  return (
    <div style={{ padding: "20px" }}>
      <h2>Thread</h2>
      {ancestorsSection}
      {highlightedNodeSection}
      {childrenSection}

      {/* Modal overlay for "Add Text" */}
      {showChildFormOverlay && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: "rgba(0,0,0,0.8)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}
          onClick={() => setShowChildFormOverlay(false)}
        >
          <div
            style={{
              background: "#1e1e1e",
              padding: "20px",
              borderRadius: "8px",
              width: "400px",
              position: "relative",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                position: "absolute",
                top: "10px",
                right: "10px",
                fontSize: "24px",
                cursor: "pointer",
              }}
              onClick={() => setShowChildFormOverlay(false)}
            >
              &times;
            </div>
            <h2 style={{ marginBottom: "20px" }}>Add Child Node</h2>
            <NodeForm
              parentId={node.id}
              onSuccess={(data) => {
                navigate(`/node/${data.id}`);
                setShowChildFormOverlay(false);
              }}
            />
          </div>
        </div>
      )}

      {/* Modal overlay for "Edit Node" */}
      {showEditOverlay && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: "rgba(0,0,0,0.8)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}
          onClick={() => setShowEditOverlay(false)}
        >
          <div
            style={{
              background: "#1e1e1e",
              padding: "20px",
              borderRadius: "8px",
              width: "400px",
              position: "relative",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                position: "absolute",
                top: "10px",
                right: "10px",
                fontSize: "24px",
                cursor: "pointer",
              }}
              onClick={() => setShowEditOverlay(false)}
            >
              &times;
            </div>
            <h2 style={{ marginBottom: "20px" }}>Edit Node</h2>
            <NodeForm
              editMode={true}
              nodeId={node.id}
              initialContent={node.content}
              onSuccess={(data) => {
                setNode(data.node ? data.node : { ...node, content: data.content });
                setShowEditOverlay(false);
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

export default NodeDetail;