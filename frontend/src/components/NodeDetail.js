import React, { useState, useEffect, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import NodeFooter from "./NodeFooter";
import SpeakerIcon from "./SpeakerIcon";
import DownloadAudioIcon from "./DownloadAudioIcon";
import ModelSelector from "./ModelSelector";
import { useUser } from "../contexts/UserContext";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";
import api from "../api";
import NodeForm from "./NodeForm";
import Bubble from "./Bubble";
import QuotedContent from "./QuotedContent";

// Recursive component to render children nodes.
function RenderChildTree({ nodes, onBubbleClick }) {
  return (
    <div>
      {nodes.map((child, index) => {
        const containerStyle = (nodes.length > 1)
          ? { marginLeft: "20px", paddingLeft: "10px", borderLeft: "2px solid var(--border)" }
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
              <hr style={{ borderColor: "var(--border)", marginLeft: containerStyle.marginLeft }} />
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
  const [llmTaskNodeId, setLlmTaskNodeId] = useState(null);
  const [quotes, setQuotes] = useState({});
  const highlightedNodeRef = useRef(null);

  // LLM completion polling - enabled automatically when llmTaskNodeId is set
  const {
    status: llmStatus,
    progress: llmProgress,
    data: llmData,
    error: llmError
  } = useAsyncTaskPolling(
    llmTaskNodeId ? `/nodes/${llmTaskNodeId}/llm-status` : null,
    { enabled: !!llmTaskNodeId }  // Auto-start when llmTaskNodeId is set
  );

  useEffect(() => {
    setLoading(true);
    setError("");
    setQuotes({}); // Reset quotes when node changes
    api
      .get(`/nodes/${id}`)
      .then((response) => {
        setNode(response.data);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        if (err.response && err.response.status === 403) {
          setError("You don't have access to this node.");
        } else {
          setError("Error fetching node details.");
        }
        setLoading(false);
      });
  }, [id]);

  // Fetch quote data when node loads (if content contains {quote:ID} placeholders)
  useEffect(() => {
    if (!node || !node.content) return;

    // Check if content contains {quote:ID} patterns
    const quotePattern = /\{quote:(\d+)\}/;
    if (!quotePattern.test(node.content)) return;

    // Fetch quotes for this node
    api
      .get(`/nodes/${id}/resolve-quotes`)
      .then((response) => {
        if (response.data.has_quotes && response.data.quotes) {
          setQuotes(response.data.quotes);
        }
      })
      .catch((err) => {
        console.error("Error fetching quotes:", err);
        // Don't show error to user - quotes will just not render
      });
  }, [id, node]);

  // Scroll to the highlighted node after loading
  useEffect(() => {
    if (!loading && node && highlightedNodeRef.current) {
      highlightedNodeRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, [loading, node]);

  // Handle LLM completion
  useEffect(() => {
    if (llmStatus === 'completed' && llmData) {
      const newNodeId = llmData.node?.id;
      if (newNodeId) {
        navigate(`/node/${newNodeId}`);
      }
      setLlmTaskNodeId(null);
    } else if (llmStatus === 'failed') {
      setError(llmError || 'LLM response generation failed');
      setLlmTaskNodeId(null);
    }
  }, [llmStatus, llmData, llmError, navigate]);

  if (loading) return <div style={{ color: "var(--text-muted)", padding: "20px" }}>Loading node...</div>;
  if (error) return <div style={{ color: "var(--accent)", padding: "20px" }}>{error}</div>;
  if (!node) return <div style={{ color: "var(--text-muted)", padding: "20px" }}>No node found.</div>;

  const handleBubbleClick = (nodeId) => {
    navigate(`/node/${nodeId}`);
  };

  // For user-typed nodes: owner must match current user
  // For LLM nodes: parent node's owner must match current user (they requested the response)
  const isOwner = node.user && currentUser && (
    node.user.id === currentUser.id ||
    (node.node_type === "llm" && node.parent_user_id === currentUser.id)
  );

  // Define handleDelete: confirm deletion and delete via API.
  const handleDelete = () => {
    if (window.confirm("Are you sure you want to delete this node? This will orphan all children.")) {
      api
        .delete(`/nodes/${id}`)
        .then(() => {
          // Navigate to parent node if it exists, otherwise go to dashboard
          if (node.ancestors && node.ancestors.length > 0) {
            const parentNode = node.ancestors[node.ancestors.length - 1];
            navigate(`/node/${parentNode.id}`);
          } else {
            navigate("/dashboard");
          }
        })
        .catch((err) => {
          console.error(err);
          setError("Error deleting node.");
        });
    }
  };

  const handleLLMResponse = () => {
    setError(""); // Clear previous errors
    api
      .post(`/nodes/${id}/llm`, { model: selectedModel })
      .then((response) => {
        // The backend now creates a placeholder and returns its ID.
        // We use this ID for polling.
        const newNodeId = response.data.node_id;
        if (newNodeId) {
          setLlmTaskNodeId(newNodeId);
        } else {
          // Fallback or error for safety, though the backend should always return it
          setError("Failed to get a task ID for the new LLM node.");
        }
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

  // Highlighted node section
  const highlightedTextStyle = {
    boxSizing: "border-box",
    padding: "1.8rem 2rem",
    margin: "10px 0",
    backgroundColor: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderLeft: "3px solid var(--accent)",
    borderRadius: "10px",
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
    <div ref={highlightedNodeRef}>
      <hr style={{ borderColor: "var(--border)" }} />
      <div style={highlightedTextStyle}>
        <QuotedContent
          content={node.content}
          quotes={quotes}
          onQuoteClick={handleBubbleClick}
        />
      </div>
      <div style={actionContainerStyle}>
        <NodeFooter
          username={node.user.username}
          createdAt={node.created_at}
          childrenCount={highlightedChildrenCount}
        />
        <div style={{ marginTop: "8px", display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <button onClick={() => setShowChildFormOverlay(true)}>Add Text</button>
          {node.ai_usage !== 'none' && (
            <>
              <button onClick={handleLLMResponse} disabled={!!llmTaskNodeId}>
                {llmTaskNodeId && llmStatus === 'processing' && llmProgress > 0
                  ? `Generating... ${llmProgress}%`
                  : llmTaskNodeId && llmStatus === 'pending'
                  ? "Waiting for AI..."
                  : llmTaskNodeId
                  ? "Generating..."
                  : "LLM Response"}
              </button>
              <ModelSelector
                nodeId={node.id}
                selectedModel={selectedModel}
                onModelChange={setSelectedModel}
              />
            </>
          )}
          {isOwner && <button onClick={() => setShowEditOverlay(true)}>Edit</button>}
          {isOwner && <button onClick={handleDelete}>Delete</button>}
          <SpeakerIcon nodeId={node.id} content={node.content} isPublic={node.privacy_level === 'public'} />
          <DownloadAudioIcon nodeId={node.id} isPublic={node.privacy_level === 'public'} />
        </div>
      </div>
      <hr style={{ borderColor: "var(--border)" }} />
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

  // Modal overlay style
  const modalOverlayStyle = {
    position: "fixed",
    top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: "rgba(0,0,0,0.7)",
    backdropFilter: "blur(8px)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000
  };

  const modalContentStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    padding: "2rem",
    borderRadius: "12px",
    width: "780px",
    maxWidth: "90vw",
    maxHeight: "90vh",
    overflowY: "auto",
    position: "relative"
  };

  const modalCloseStyle = {
    position: "absolute",
    top: "12px",
    right: "16px",
    fontSize: "24px",
    cursor: "pointer",
    color: "var(--text-muted)",
    background: "none",
    border: "none",
    padding: "4px",
    lineHeight: 1,
  };

  const modalHeadingStyle = {
    marginBottom: "20px",
    fontFamily: "var(--serif)",
    fontSize: "1.4rem",
    fontWeight: 300,
    color: "var(--text-primary)",
  };

  return (
    <div style={{ padding: "20px" }}>
      <h2 style={{
        fontFamily: "var(--serif)",
        fontWeight: 300,
        fontSize: "1.8rem",
        color: "var(--text-primary)",
      }}>Thread</h2>
      {ancestorsSection}
      {highlightedNodeSection}
      {childrenSection}

      {/* Overlay for "Add Text" */}
      {showChildFormOverlay && (
        <div
          style={modalOverlayStyle}
          onClick={() => setShowChildFormOverlay(false)}
        >
          <div
            style={modalContentStyle}
            onClick={(e) => e.stopPropagation()}
          >
            <button
              style={modalCloseStyle}
              onClick={() => setShowChildFormOverlay(false)}
            >
              &times;
            </button>
            <h2 style={modalHeadingStyle}>Add Text</h2>
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
          style={modalOverlayStyle}
          onClick={() => setShowEditOverlay(false)}
        >
          <div
            style={modalContentStyle}
            onClick={(e) => e.stopPropagation()}
          >
            <button
              style={modalCloseStyle}
              onClick={() => setShowEditOverlay(false)}
            >
              &times;
            </button>
            <h2 style={modalHeadingStyle}>Edit Text</h2>
            <NodeForm
              editMode={true}
              nodeId={node.id}
              initialContent={node.content}
              initialPrivacyLevel={node.privacy_level}
              initialAiUsage={node.ai_usage}
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
