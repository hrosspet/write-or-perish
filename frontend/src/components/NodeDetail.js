import React, { useState, useEffect, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { FaThumbtack } from "react-icons/fa";
import NodeFooter from "./NodeFooter";
import SpeakerIcon from "./SpeakerIcon";
import DownloadAudioIcon from "./DownloadAudioIcon";
import ModelSelector from "./ModelSelector";
import { useUser } from "../contexts/UserContext";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";
import api from "../api";
import NodeFormModal from "./NodeFormModal";
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
  const [selectedModel, setSelectedModel] = useState(currentUser?.preferred_model || null);
  const [llmTaskNodeId, setLlmTaskNodeId] = useState(null);
  const [quotes, setQuotes] = useState({});
  const [pinLoading, setPinLoading] = useState(false);
  const [voiceLoading, setVoiceLoading] = useState(false);
  const [toolActionsExpanded, setToolActionsExpanded] = useState(false);
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

  const handleBubbleClick = (nodeId, e) => {
    if (e && (e.metaKey || e.ctrlKey)) {
      window.open(`/node/${nodeId}`, '_blank');
    } else {
      navigate(`/node/${nodeId}`);
    }
  };

  // For user-typed nodes: owner must match current user
  // For LLM nodes: parent node's owner must match current user (they requested the response)
  const isOwner = node.user && currentUser && (
    node.user.id === currentUser.id ||
    (node.node_type === "llm" && node.parent_user_id === currentUser.id)
  );

  const canPin = isOwner && node.privacy_level !== "private";
  const isPinned = !!node.pinned_at;

  const handlePin = async () => {
    if (pinLoading) return;
    setPinLoading(true);
    try {
      if (isPinned) {
        await api.delete(`/nodes/${id}/pin`);
        setNode({ ...node, pinned_at: null });
      } else {
        const res = await api.post(`/nodes/${id}/pin`);
        setNode({ ...node, pinned_at: res.data.pinned_at });
      }
    } catch (err) {
      console.error("Error toggling pin:", err);
      setError(err.response?.data?.error || "Error toggling pin.");
    }
    setPinLoading(false);
  };

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
            navigate("/log");
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

  const handleSessionFromNode = (sessionType) => {
    setVoiceLoading(true);
    setError("");
    api
      .post(`/${sessionType}/from-node/${id}`, { model: selectedModel })
      .then((response) => {
        const { mode, llm_node_id, parent_id, fresh } = response.data;
        if (mode === "processing") {
          let url = `/voice?resume=${llm_node_id}`;
          if (parent_id) url += `&parent=${parent_id}`;
          if (fresh) url += `&fresh=1`;
          navigate(url);
        } else {
          navigate(`/voice?parent=${parent_id}`);
        }
      })
      .catch((err) => {
        console.error(err);
        setError(err.response?.data?.error || `Error starting ${sessionType} session.`);
        setVoiceLoading(false);
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

  // Determine humanOwnerUsername for LLM nodes in NodeDetail
  // node.parent_user_id is the human owner's user_id for LLM nodes
  const humanOwnerUsername = node.node_type === "llm" && node.parent_user_id
    ? (node.ancestors && node.ancestors.length > 0
      ? (() => {
          // Walk ancestors to find the human owner username
          for (let i = node.ancestors.length - 1; i >= 0; i--) {
            const a = node.ancestors[i];
            if (a.node_type !== "llm") return a.username;
          }
          return null;
        })()
      : null)
    : null;

  const highlightedNodeSection = (
    <div ref={highlightedNodeRef}>
      <hr style={{ borderColor: "var(--border)" }} />
      <div style={highlightedTextStyle}>
        {node.is_system_prompt && node.prompt_title && (
          <div style={{
            fontFamily: "var(--sans)",
            fontSize: "0.8rem",
            fontWeight: 300,
            color: "var(--text-muted)",
            marginBottom: "0.6rem",
            display: "flex",
            alignItems: "center",
            gap: "0.5em",
          }}>
            <span>{node.prompt_title}{node.prompt_version_number ? ` v${node.prompt_version_number}` : ''}</span>
            {node.context_artifacts?.profile && (
              <span style={{ opacity: 0.7 }}>
                {'\u00B7'} Profile v{node.context_artifacts.profile.version_number}
              </span>
            )}
            {node.context_artifacts?.todo && (
              <span style={{ opacity: 0.7 }}>
                {'\u00B7'} TODO v{node.context_artifacts.todo.version_number}
              </span>
            )}
          </div>
        )}
        <QuotedContent
          content={node.content}
          quotes={quotes}
          contextArtifacts={node.context_artifacts || null}
          onQuoteClick={handleBubbleClick}
        />
        {node.tool_calls_meta && node.tool_calls_meta.length > 0 && (
          <div style={{ marginTop: '12px', borderTop: '1px solid var(--border)', paddingTop: '8px' }}>
            <button
              onClick={() => setToolActionsExpanded(!toolActionsExpanded)}
              style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                color: 'var(--text-muted)',
              }}
            >
              {toolActionsExpanded ? '▾' : '▸'} Actions taken ({node.tool_calls_meta.length})
            </button>
            {toolActionsExpanded && (
              <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {node.tool_calls_meta.map((tc, i) => (
                  <div key={i} style={{
                    fontFamily: 'var(--sans)', fontSize: '0.78rem', fontWeight: 300,
                    color: 'var(--text-secondary)', padding: '6px 10px',
                    background: 'var(--bg-surface)', borderRadius: '6px',
                    border: '1px solid var(--border)',
                  }}>
                    {tc.status === 'success' ? '✓' : '✗'}{' '}
                    {tc.name === 'update_todo' && 'Todo update proposed'}
                    {tc.name === 'apply_todo_changes' && 'Todo changes applied'}
                    {tc.name === 'update_ai_preferences' && 'Preferences updated'}
                    {!['update_todo', 'apply_todo_changes', 'update_ai_preferences'].includes(tc.name) && tc.name}
                    {tc.error && <span style={{ color: 'var(--accent)', marginLeft: '8px' }}>{tc.error}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      <div style={actionContainerStyle}>
        <NodeFooter
          username={node.user.username}
          createdAt={node.created_at}
          childrenCount={highlightedChildrenCount}
          humanOwnerUsername={humanOwnerUsername}
          llmModel={node.llm_model}
        >
          <button
            onClick={canPin ? handlePin : undefined}
            disabled={!canPin || pinLoading}
            title={
              !isOwner ? "Only the owner can pin"
              : node.privacy_level === "private" ? "Cannot pin a private node"
              : isPinned ? "Unpin from profile"
              : "Pin to profile"
            }
            style={{
              background: "none",
              border: "none",
              cursor: canPin ? "pointer" : "not-allowed",
              padding: 0,
              opacity: canPin ? 1 : 0.35,
              color: isPinned ? "var(--accent)" : "inherit",
              display: "flex",
              alignItems: "center",
            }}
          >
            <FaThumbtack />
          </button>
          <SpeakerIcon nodeId={node.id} content={node.content} isPublic={node.privacy_level === 'public'} aiUsage={node.ai_usage} />
          <DownloadAudioIcon nodeId={node.id} isPublic={node.privacy_level === 'public'} aiUsage={node.ai_usage} />
        </NodeFooter>
        <div style={{ marginTop: "8px", display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <button onClick={() => setShowChildFormOverlay(true)}>Add Text</button>
          {isOwner && node.ai_usage !== 'none' && (
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
              <button onClick={() => handleSessionFromNode('voice')} disabled={voiceLoading}>
                {voiceLoading ? "Starting..." : "Voice"}
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

      {showChildFormOverlay && (
        <NodeFormModal
          title="Add Text"
          onClose={() => setShowChildFormOverlay(false)}
          nodeFormProps={{
            parentId: node.id,
            onSuccess: (data) => {
              navigate(`/node/${data.id}`);
              setShowChildFormOverlay(false);
            },
          }}
        />
      )}

      {showEditOverlay && (
        <NodeFormModal
          title="Edit Text"
          onClose={() => setShowEditOverlay(false)}
          nodeFormProps={{
            editMode: true,
            nodeId: node.id,
            initialContent: node.content,
            initialPrivacyLevel: node.privacy_level,
            initialAiUsage: node.ai_usage,
            onSuccess: (data) => {
              setNode(data.node ? data.node : { ...node, content: data.content });
              setShowEditOverlay(false);
            },
          }}
        />
      )}
    </div>
  );
}

export default NodeDetail;
