import React, { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import api from "../api";
import NodeForm from "./NodeForm";

function NodeDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { user: currentUser } = useUser();

  const [node, setNode] = useState(null);
  const [children, setChildren] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // State to control overlay modals.
  const [showChildFormOverlay, setShowChildFormOverlay] = useState(false);
  const [showEditOverlay, setShowEditOverlay] = useState(false);

  const backendUrl = process.env.REACT_APP_BACKEND_URL;

  useEffect(() => {
    api
      .get(`/nodes/${id}`)
      .then((response) => {
        setNode(response.data);
        setChildren(response.data.children || []);
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

  // Determine whether the current user is the owner.
  const isOwner =
    node && node.user && currentUser && node.user.id === currentUser.id;

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

  // Add a keydown event listener for the ESC key
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") {
        if (showChildFormOverlay) setShowChildFormOverlay(false);
        if (showEditOverlay) setShowEditOverlay(false);
      }
    };

    // Only add the listener if an overlay is open.
    if (showChildFormOverlay || showEditOverlay) {
      window.addEventListener("keydown", handleKeyDown);
    }

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [showChildFormOverlay, showEditOverlay]);

  if (loading) return <div>Loading node...</div>;
  if (error) return <div>{error}</div>;
  if (!node) return <div>No node found.</div>;

  // Inline modal styles (you can extract these to a separate component if desired)
  const modalOverlayStyle = {
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
  };
  const modalContentStyle = {
    background: "#1e1e1e",
    padding: "20px",
    borderRadius: "8px",
    width: "400px",
    position: "relative",
  };

  return (
    <div style={{ padding: "20px" }}>
      <h2>Node Detail</h2>

      {/* Display ancestors if available */}
      {node.ancestors && node.ancestors.length > 0 && (
        <div>
          <h3>Ancestors</h3>
          <ul>
            {node.ancestors.map((ancestor) => (
              <li key={ancestor.id} style={{ margin: "5px 0" }}>
                <a href={`/node/${ancestor.id}`}>
                  {ancestor.username}: {ancestor.preview} | children:{" "}
                  {ancestor.child_count}
                </a>
              </li>
            ))}
          </ul>
          <hr />
        </div>
      )}

      {/* Highlighted Node Content */}
      <div style={{ marginTop: "20px" }}>
        <p>{node.content}</p>
      </div>

      {/* Action Buttons placed with the highlighted node */}
      <div style={{ marginTop: "20px" }}>
        <button onClick={() => setShowChildFormOverlay(true)}>Add Text</button>{" "}
        <button onClick={handleLLMResponse}>LLM Response</button>{" "}
        {isOwner && (
          <>
            <button onClick={() => setShowEditOverlay(true)}>Edit</button>{" "}
            <button onClick={handleDelete}>Delete</button>
          </>
        )}
      </div>

      {/* Overlay modal for "Add Text" */}
      {showChildFormOverlay && (
        <div
          style={modalOverlayStyle}
          onClick={() => setShowChildFormOverlay(false)}
        >
          <div
            style={modalContentStyle}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                position: "absolute",
                top: "10px",
                right: "10px",
                fontSize: "24px",
                fontWeight: "bold",
                color: "#e0e0e0",
                cursor: "pointer",
              }}
              onClick={() => setShowChildFormOverlay(false)}
            >
              &times;
            </div>
            <h2 style={{ color: "#e0e0e0", marginBottom: "20px" }}>
              Add Child Node
            </h2>
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

      {/* Overlay modal for editing */}
      {showEditOverlay && (
        <div
          style={modalOverlayStyle}
          onClick={() => setShowEditOverlay(false)}
        >
          <div
            style={modalContentStyle}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                position: "absolute",
                top: "10px",
                right: "10px",
                fontSize: "24px",
                fontWeight: "bold",
                color: "#e0e0e0",
                cursor: "pointer",
              }}
              onClick={() => setShowEditOverlay(false)}
            >
              &times;
            </div>
            <h2 style={{ color: "#e0e0e0", marginBottom: "20px" }}>
              Edit Node
            </h2>
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

      <hr />

      {/* Child Nodes List */}
      <h3>Child Nodes</h3>
      {children.length === 0 && <p>No child nodes.</p>}
      <ul>
        {children.map((child) => (
          <li key={child.id} style={{ margin: "5px 0" }}>
            <a href={`/node/${child.id}`}>
              {child.username}: {child.preview} | children: {child.child_count}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default NodeDetail;