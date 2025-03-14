import React, { useState, useEffect } from "react";
import { useParams } from "react-router-dom";
import api from "../api";
import NodeForm from "./NodeForm";

function NodeDetail() {
  const { id } = useParams();
  const [node, setNode] = useState(null);
  const [children, setChildren] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [editedContent, setEditedContent] = useState("");
  const [showChildForm, setShowChildForm] = useState(false);
  const [childFormType, setChildFormType] = useState("text");

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
        if (err.response && err.response.status === 401) {
          window.location.href = `${backendUrl}/auth/login`;
        } else {
          setError("Error fetching node details.");
          setLoading(false);
        }
      });
  }, [id, backendUrl]);

  const handleEdit = () => {
    setEditing(true);
    setEditedContent(node.content);
  };

  const handleEditSubmit = (e) => {
    e.preventDefault();
    api
      .put(`/nodes/${id}`, { content: editedContent })
      .then((response) => {
        setNode(response.data.node);
        setEditing(false);
      })
      .catch((err) => {
        console.error(err);
        setError("Error updating node.");
      });
  };

  const requestLLMResponse = () => {
    api
      .post(`/nodes/${id}/llm`)
      .then((response) => {
        const newChild = {
          id: response.data.node.id,
          username: response.data.node.username || "Unknown",
          preview: response.data.node.content.substring(0, 200),
          child_count: response.data.node.token_count ? response.data.node.token_count : 0,
        };
        setChildren([...children, newChild]);
      })
      .catch((err) => {
        console.error(err);
        setError("Error requesting LLM response.");
      });
  };

  if (loading) return <div>Loading node...</div>;
  if (error) return <div>{error}</div>;
  if (!node) return <div>No node found.</div>;

  return (
    <div style={{ padding: "20px" }}>
      <h2>Node Detail</h2>

      {/* Ancestors with similar preview style */}
      {node.ancestors && node.ancestors.length > 0 && (
        <div>
          <h3>Ancestors</h3>
          <ul>
            {node.ancestors.map((ancestor) => (
              <li key={ancestor.id} style={{ margin: "5px 0" }}>
                <a href={`/node/${ancestor.id}`}>
                  {ancestor.username}: {ancestor.preview} | children: {ancestor.child_count}
                </a>
              </li>
            ))}
          </ul>
          <hr />
        </div>
      )}

      {/* Highlighted Node Content */}
      {editing ? (
        <form onSubmit={handleEditSubmit}>
          <textarea
            value={editedContent}
            onChange={(e) => setEditedContent(e.target.value)}
            rows={4}
            style={{ width: "100%" }}
          />
          <button type="submit">Save</button>
          <button type="button" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <div>
          <p>{node.content}</p>
          <button onClick={handleEdit}>Edit</button>
        </div>
      )}

      <hr />
      
      {/* Children preview in the same style */}
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

      <hr />

      <h3>Add Child Node</h3>
      <button
        onClick={() => {
          setChildFormType("text");
          setShowChildForm(!showChildForm);
        }}
      >
        Add Text
      </button>
      {"  "}
      <button onClick={requestLLMResponse}>LLM Response</button>
      {showChildForm && childFormType === "text" && (
        <NodeForm
          parentId={node.id}
          onSuccess={(data) => {
            const newChild = {
              id: data.id,
              username: data.username || "Unknown",
              preview: data.content.substring(0, 200),
              child_count: data.child_count || 0,
            };
            setChildren([...children, newChild]);
            setShowChildForm(false);
          }}
        />
      )}
    </div>
  );
}

export default NodeDetail;