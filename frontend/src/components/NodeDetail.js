import React, { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import NodeFooter from "./NodeFooter";
import SpeakerIcon from "./SpeakerIcon";
import ModelSelector from "./ModelSelector";
import { useUser } from "../contexts/UserContext";
import api from "../api";
import NodeForm from "./NodeForm";
import Bubble from "./Bubble";

// Recursive component to render children nodes.
function RenderChildTree({ nodes, onBubbleClick }) {
  return (
    <div>
      {nodes.map((child, index) => {
        const containerStyle = (nodes.length > 1)
          ? { marginLeft: "20px", paddingLeft: "10px", borderLeft: "2px solid #61dafb" }
          : { marginLeft: "0px" };

        return (
          <div key={child.id}>
            <div style={containerStyle}>
              <Bubble node={child} onClick={onBubbleClick} leftAlign={true} />
              {child.children && child.children.length > 0 &&
                <RenderChildTree nodes={child.children} onBubbleClick={onBubbleClick} />
              }
            </div>
            {index < nodes.length - 1 && (
              <hr style={{ borderColor: "#333", marginLeft: containerStyle.marginLeft }} />
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
  const [selectedModel, setSelectedModel] = useState("gpt-5");
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

  const handleBubbleClick = (nodeId) => {
    navigate(`/node/${nodeId}`);
  };

  const isOwner = node.user && currentUser && node.user.id === currentUser.id;

  // Define handleDelete: confirm deletion and delete via API.
  const handleDelete = () => {
    if (window.confirm("Are you sure you want to delete this node? This will orphan all children.")) {
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

  // Define handleLLMResponse: send request and navigate to the new node on success.
  const handleLLMResponse = () => {
    api
      .post(`/nodes/${id}/llm`, { model: selectedModel })
      .then((response) => {
        navigate(`/node/${response.data.node.id}`);
      })
      .catch((err) => {
        console.error(err);
        setError("Error requesting LLM response.");
      });
  };

  // Ancestors section rendered as a list of bubbles.
  const ancestorsSection = (
    <div style={{ display: "flex", flexDirection: "column", marginBottom: "10px" }}>
      {node.ancestors &&
        node.ancestors.map((ancestor) => (
          <Bubble key={ancestor.id} node={ancestor} onClick={handleBubbleClick} leftAlign={true} />
        ))}
    </div>
  );

  // Highlighted node section – note the left indent applied on both the content and the footer/buttons container.
  const highlightedTextStyle = {
    boxSizing: "border-box",
    padding: "10px",
    margin: "10px 0",
    backgroundColor: "#2e2e2e",
    border: "2px solid #61dafb",
    borderLeft: "2px solid #61dafb",
    width: "95%",
    maxWidth: "1500px",
    marginLeft: "20px"
  };

  const actionContainerStyle = {
    marginLeft: "20px",
    marginBottom: "10px"
  };

  // Compute children count for the highlighted node.
  const highlightedChildrenCount = typeof node.child_count !== "undefined"
    ? node.child_count
    : (node.children ? node.children.length : 0);

  const highlightedNodeSection = (
    <div>
      <hr style={{ borderColor: "#333" }} />
      <div style={highlightedTextStyle}>
        <ReactMarkdown
          components={{
            p: ({ node, ...props }) => (
              <p style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props} />
            ),
            code: ({ node, inline, className, children, ...props }) =>
              inline ? (
                <code style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props}>
                  {children}
                </code>
              ) : (
                <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props}>
                  <code>{children}</code>
                </pre>
              ),
            li: ({ node, ...props }) => (
              <li style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props} />
            )
          }}
        >
          {node.content}
        </ReactMarkdown>
      </div>
      <div style={actionContainerStyle}>
        <NodeFooter
          username={node.user.username}
          createdAt={node.created_at}
          childrenCount={highlightedChildrenCount}
        />
        <div style={{ marginTop: "8px", display: 'flex', alignItems: 'center', gap: '8px' }}>
          <button onClick={() => setShowChildFormOverlay(true)}>Add Text</button>
          {/* Model selector dropdown */}
          <ModelSelector
            nodeId={node.id}
            selectedModel={selectedModel}
            onModelChange={setSelectedModel}
          />
          <button onClick={handleLLMResponse}>LLM Response</button>
          {isOwner && <button onClick={() => setShowEditOverlay(true)}>Edit</button>}
          {isOwner && <button onClick={handleDelete}>Delete</button>}
          {/* Speaker icon for audio playback */}
          <SpeakerIcon nodeId={node.id} />
        </div>
      </div>
      <hr style={{ borderColor: "#333" }} />
    </div>
  );

  // Render the child nodes recursively.
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

      {/* Overlay for "Add Text" */}
      {showChildFormOverlay && (
        <div
          style={{
            position: "fixed",
            top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: "rgba(0,0,0,0.8)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000
          }}
          onClick={() => setShowChildFormOverlay(false)}
        >
          <div
            style={{
              background: "#1e1e1e",
              padding: "20px",
              borderRadius: "8px",
              width: "1000px",
              position: "relative"
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                position: "absolute",
                top: "10px",
                right: "10px",
                fontSize: "24px",
                cursor: "pointer"
              }}
              onClick={() => setShowChildFormOverlay(false)}
            >
              &times;
            </div>
            <h2 style={{ marginBottom: "20px" }}>Add Text</h2>
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

      {/* Overlay for "Edit Text" */}
      {showEditOverlay && (
        <div
          style={{
            position: "fixed",
            top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: "rgba(0,0,0,0.8)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000
          }}
          onClick={() => setShowEditOverlay(false)}
        >
          <div
            style={{
              background: "#1e1e1e",
              padding: "20px",
              borderRadius: "8px",
              width: "1000px",
              position: "relative"
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                position: "absolute",
                top: "10px",
                right: "10px",
                fontSize: "24px",
                cursor: "pointer"
              }}
              onClick={() => setShowEditOverlay(false)}
            >
              &times;
            </div>
            <h2 style={{ marginBottom: "20px" }}>Edit Text</h2>
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