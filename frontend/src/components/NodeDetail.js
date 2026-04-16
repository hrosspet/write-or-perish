import React, { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { FaThumbtack, FaMicrophone, FaEllipsisV } from "react-icons/fa";
import NodeFooter from "./NodeFooter";
import SpeakerIcon from "./SpeakerIcon";
import DownloadAudioIcon from "./DownloadAudioIcon";
import ModelSelector from "./ModelSelector";
import NodeForm from "./NodeForm";
import ProposalInline, { hasProposalSections, stripProposalSections } from "./ProposalInline";
import { useUser } from "../contexts/UserContext";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";
import api from "../api";
import { useCheckboxToggle } from "../utils/markdown";
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
  const [searchParams, setSearchParams] = useSearchParams();
  const { user: currentUser } = useUser();
  const craftMode = !!currentUser?.craft_mode;
  const [node, setNode] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showEditOverlay, setShowEditOverlay] = useState(false);
  const [selectedModel, setSelectedModel] = useState(currentUser?.preferred_model || null);
  const [llmTaskNodeId, setLlmTaskNodeId] = useState(null);
  const [quotes, setQuotes] = useState({});
  const [pinLoading, setPinLoading] = useState(false);
  const [voiceLoading, setVoiceLoading] = useState(false);
  const [toolActionsExpanded, setToolActionsExpanded] = useState(false);
  const [showPromptEditConfirm, setShowPromptEditConfirm] = useState(false);
  // autoGenerate is shared across the whole text-mode experience — the
  // NodeDetailWrapper uses `key={id}` which remounts NodeDetail on every
  // node navigation, so local useState would reset the toggle. Persist to
  // localStorage so it survives the remount.
  const [autoGenerate, setAutoGenerateState] = useState(() => {
    const stored = localStorage.getItem('loore_auto_generate');
    return stored === null ? true : stored === 'true';
  });
  const setAutoGenerate = useCallback((next) => {
    setAutoGenerateState(prev => {
      const resolved = typeof next === 'function' ? next(prev) : next;
      localStorage.setItem('loore_auto_generate', String(resolved));
      return resolved;
    });
  }, []);
  const [showKebabMenu, setShowKebabMenu] = useState(false);
  const highlightedNodeRef = useRef(null);
  const kebabMenuRef = useRef(null);

  // Auto-generate is forced ON when Craft mode is OFF (no toggle shown).
  const autoGenerateActive = !craftMode || autoGenerate;

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
        if (err.response && err.response.status === 404) {
          setError("This node no longer exists or was deleted.");
        } else if (err.response && err.response.status === 403) {
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

  // If we arrived with ?awaitLlm=NID (e.g. from WritePage), pick up the
  // pending LLM task and let the polling navigate to it on completion.
  // Re-runs when `id` changes so internal navigations to another node
  // with ?awaitLlm= also take effect (react-router reuses the component).
  useEffect(() => {
    const awaitLlm = searchParams.get('awaitLlm');
    if (awaitLlm) {
      setLlmTaskNodeId(parseInt(awaitLlm, 10));
      const next = new URLSearchParams(searchParams);
      next.delete('awaitLlm');
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Close kebab menu on outside click
  useEffect(() => {
    if (!showKebabMenu) return;
    const handler = (e) => {
      if (kebabMenuRef.current && !kebabMenuRef.current.contains(e.target)) {
        setShowKebabMenu(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showKebabMenu]);

  // Handle LLM completion
  useEffect(() => {
    if (llmStatus === 'completed' && llmData) {
      // Prefer the id of the node returned in payload; fall back to the
      // polled node id (llmTaskNodeId).
      const completedId = llmData.node?.id || llmTaskNodeId;
      if (completedId && String(completedId) === String(id)) {
        // We're already viewing the pending LLM node — patch its state
        // in place so the rendered content switches from "Thinking…" to
        // the final response without a navigation jump.
        setNode(prev => prev ? {
          ...prev,
          content: llmData.content ?? prev.content,
          tool_calls_meta: llmData.tool_calls_meta ?? prev.tool_calls_meta,
          llm_task_status: 'completed',
        } : prev);
      } else if (completedId) {
        navigate(`/node/${completedId}`);
      }
      setLlmTaskNodeId(null);
    } else if (llmStatus === 'failed') {
      setError(llmError || 'LLM response generation failed');
      setLlmTaskNodeId(null);
    }
  }, [llmStatus, llmData, llmError, navigate, id, llmTaskNodeId]);

  const handleCheckboxToggle = useCheckboxToggle(
    useCallback(() => node?.content, [node]),
    useCallback((newContent) => setNode(prev => ({ ...prev, content: newContent })), []),
    useCallback((newContent) => api.put(`/nodes/${id}`, { content: newContent }), [id]),
  );

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

  const requestLlmFor = async (parentNodeId) => {
    const response = await api.post(`/nodes/${parentNodeId}/llm`, {
      model: selectedModel,
      source_mode: 'textmode',
    });
    const newNodeId = response.data.node_id;
    if (!newNodeId) throw new Error("Failed to get a task ID for the new LLM node.");
    return newNodeId;
  };

  const handleLLMResponse = () => {
    setError("");
    requestLlmFor(id)
      .then((newNodeId) => setLlmTaskNodeId(newNodeId))
      .catch((err) => {
        console.error(err);
        setError(err.response?.data?.error || err.message || "Error requesting LLM response.");
      });
  };

  // Called by NodeForm after it successfully POSTs /nodes/.
  // `data` is the newly-created child node from the backend.
  const handleInlineSuccess = async (data) => {
    const newNodeId = data?.id;
    if (!newNodeId) return;
    setError("");
    try {
      if (autoGenerateActive && node.ai_usage !== 'none') {
        const llmNodeId = await requestLlmFor(newNodeId);
        // Navigate directly to the pending LLM node so the inline input
        // stays anchored below it throughout generation.
        navigate(`/node/${llmNodeId}?awaitLlm=${llmNodeId}`);
      } else {
        navigate(`/node/${newNodeId}`);
      }
    } catch (err) {
      console.error(err);
      setError(err.response?.data?.error || err.message || "Error sending message.");
    }
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
    marginLeft: "20px",
    position: "relative",
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

  const isLlmNode = node.node_type === "llm" || !!node.llm_model;
  const isLlmPending = isLlmNode && (
    node.llm_task_status === 'pending'
    || node.llm_task_status === 'processing'
  );
  const showProposal = isLlmNode && !isLlmPending && node.content
    && hasProposalSections(node.content);
  const displayContent = showProposal ? stripProposalSections(node.content) : node.content;
  // Inline input shows whenever the user owns the thread and AI is allowed.
  // Craft mode does NOT hide it — it only adds extra buttons.
  const showInlineInput = isOwner && node.ai_usage !== 'none';
  const showCraftBar = isOwner && craftMode && !autoGenerate && node.ai_usage !== 'none';

  const topRightButtonStyle = {
    background: 'none',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    padding: '6px 12px',
    color: 'var(--text-muted)',
    fontFamily: 'var(--sans)',
    fontSize: '0.78rem',
    fontWeight: 300,
    cursor: 'pointer',
    display: 'inline-flex',
    alignItems: 'center',
    gap: '6px',
  };

  const highlightedNodeSection = (
    <div ref={highlightedNodeRef} style={{ position: 'relative' }}>
      <hr style={{ borderColor: "var(--border)" }} />
      {isOwner && node.ai_usage !== 'none' && (
        <div style={{
          position: 'fixed',
          top: '72px',
          right: '20px',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'flex-end',
          gap: '10px',
          zIndex: 50,
        }}>
          <button
            onClick={() => handleSessionFromNode('voice')}
            disabled={voiceLoading}
            style={topRightButtonStyle}
            title="Continue this conversation by voice"
          >
            <FaMicrophone size={11} />
            <span>{voiceLoading ? 'Starting…' : 'Voice Mode'}</span>
          </button>
          {craftMode && (
            <button
              type="button"
              onClick={() => setAutoGenerate(v => !v)}
              title={autoGenerate ? 'Auto-generate is on — click to turn off' : 'Auto-generate is off — click to turn on'}
              style={{
                background: 'none',
                border: 'none',
                padding: '4px 2px',
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: '8px',
                color: 'var(--text-muted)',
                fontFamily: 'var(--sans)',
                fontSize: '0.74rem',
                fontWeight: 300,
              }}
            >
              <span style={{
                width: '16px',
                height: '16px',
                borderRadius: '50%',
                border: `1.5px solid ${autoGenerate ? 'var(--accent)' : 'var(--border-hover)'}`,
                background: autoGenerate ? 'var(--accent)' : 'transparent',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '0.55rem',
                color: 'var(--bg-deep)',
                fontWeight: 600,
                flexShrink: 0,
              }}>
                {autoGenerate ? '✓' : ''}
              </span>
              <span>Auto-generate</span>
            </button>
          )}
        </div>
      )}
      <div style={highlightedTextStyle}>
        {isOwner && (
          <div ref={kebabMenuRef} style={{
            position: 'absolute',
            top: '10px',
            right: '10px',
          }}>
            <button
              onClick={() => setShowKebabMenu((v) => !v)}
              title="More actions"
              style={{
                background: 'none', border: 'none', cursor: 'pointer',
                color: 'var(--text-muted)', padding: '4px 6px',
                display: 'inline-flex', alignItems: 'center',
              }}
            >
              <FaEllipsisV size={14} />
            </button>
            {showKebabMenu && (
              <div style={{
                position: 'absolute',
                top: '100%',
                right: 0,
                marginTop: '4px',
                background: 'var(--bg-card)',
                border: '1px solid var(--border)',
                borderRadius: '6px',
                boxShadow: '0 4px 12px rgba(0,0,0,0.25)',
                minWidth: '120px',
                zIndex: 5,
                overflow: 'hidden',
              }}>
                <button
                  onClick={() => {
                    setShowKebabMenu(false);
                    if (node.context_artifacts?.prompt) {
                      setShowPromptEditConfirm(true);
                    } else {
                      setShowEditOverlay(true);
                    }
                  }}
                  style={{
                    display: 'block', width: '100%', textAlign: 'left',
                    background: 'none', border: 'none', cursor: 'pointer',
                    padding: '8px 12px',
                    fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
                    color: 'var(--text-primary)',
                  }}
                >Edit</button>
                <button
                  onClick={() => { setShowKebabMenu(false); handleDelete(); }}
                  style={{
                    display: 'block', width: '100%', textAlign: 'left',
                    background: 'none', border: 'none', cursor: 'pointer',
                    padding: '8px 12px',
                    fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
                    color: 'var(--accent)',
                  }}
                >Delete</button>
              </div>
            )}
          </div>
        )}
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
        {isLlmPending ? (
          <div style={{
            display: 'flex', alignItems: 'center', gap: '10px',
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)', fontSize: '0.95rem', fontWeight: 300,
            fontStyle: 'italic',
            padding: '8px 0',
          }}>
            <span>Thinking</span>
            <span style={{ display: 'inline-flex', gap: '3px' }}>
              {[0, 1, 2].map(i => (
                <span key={i} style={{
                  width: '5px', height: '5px', borderRadius: '50%',
                  background: 'var(--text-muted)',
                  animation: `wopPulseDot 1.2s ease-in-out ${i * 0.15}s infinite`,
                }} />
              ))}
            </span>
            <style>{`
              @keyframes wopPulseDot {
                0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
                30% { opacity: 1; transform: translateY(-2px); }
              }
            `}</style>
          </div>
        ) : (
          <QuotedContent
            content={displayContent}
            quotes={quotes}
            contextArtifacts={node.context_artifacts || null}
            onQuoteClick={handleBubbleClick}
            onCheckboxToggle={isOwner ? handleCheckboxToggle : undefined}
          />
        )}
        {showProposal && (
          <ProposalInline
            content={node.content}
            nodeId={node.id}
            toolCallsMeta={node.tool_calls_meta}
          />
        )}
        {(() => {
          const visibleTools = (node.tool_calls_meta || [])
            .filter(tc => !tc.name || !tc.name.startsWith('_'));
          if (visibleTools.length === 0) return null;
          return (
          <div style={{ marginTop: '12px', borderTop: '1px solid var(--border)', paddingTop: '8px' }}>
            <button
              onClick={() => setToolActionsExpanded(!toolActionsExpanded)}
              style={{
                background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                fontFamily: 'var(--sans)', fontSize: '0.75rem', fontWeight: 300,
                color: 'var(--text-muted)',
              }}
            >
              {toolActionsExpanded ? '▾' : '▸'} Actions taken ({visibleTools.length})
            </button>
            {toolActionsExpanded && (
              <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {visibleTools.map((tc, i) => (
                  <div key={i} style={{
                    fontFamily: 'var(--sans)', fontSize: '0.78rem', fontWeight: 300,
                    color: 'var(--text-secondary)', padding: '6px 10px',
                    background: 'var(--bg-surface)', borderRadius: '6px',
                    border: '1px solid var(--border)',
                  }}>
                    {tc.status === 'success' ? '✓' : '✗'}{' '}
                    {tc.name === 'propose_todo' && (
                      <>Todo update proposed{tc.apply_status === 'completed' && ' (applied)'}{tc.apply_status === 'started' && ' (applying...)'}{tc.apply_status === 'failed' && ' (failed)'}</>
                    )}
                    {tc.name === 'propose_github_issue' && (
                      <>Issue proposed{tc.apply_status === 'completed' && ' (created)'}{tc.apply_status === 'failed' && ' (failed)'}</>
                    )}
                    {tc.name === 'apply_todo_changes' && (
                      tc.status !== 'success' ? 'Todo apply failed'
                        : tc.apply_status === 'completed' ? 'Todo changes applied'
                        : tc.apply_status === 'failed' ? `Todo apply failed${tc.apply_error ? ': ' + tc.apply_error : ''}`
                        : 'Todo apply in progress...'
                    )}
                    {tc.name === 'apply_github_issue' && (
                      tc.status === 'success' ? 'Issue creation confirmed' : 'Issue creation failed'
                    )}
                    {tc.name === 'update_ai_preferences' && 'Preferences updated'}
                    {!['propose_todo', 'propose_github_issue', 'apply_todo_changes', 'apply_github_issue', 'update_ai_preferences'].includes(tc.name) && tc.name}
                    {tc.error && <span style={{ color: 'var(--accent)', marginLeft: '8px' }}> — {tc.error}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
          );
        })()}
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
        {showCraftBar && (
          <div style={{ marginTop: "8px", display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
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
          </div>
        )}
        {llmTaskNodeId && !showCraftBar && (
          <div style={{
            marginTop: '8px',
            fontFamily: 'var(--sans)', fontSize: '0.78rem', fontWeight: 300,
            color: 'var(--text-muted)',
          }}>
            {llmStatus === 'processing' && llmProgress > 0
              ? `Generating… ${llmProgress}%`
              : llmStatus === 'pending'
              ? 'Waiting for AI…'
              : 'Generating…'}
          </div>
        )}
      </div>
      {showInlineInput && (
        <div style={{
          width: '95%',
          maxWidth: '1500px',
          marginLeft: '20px',
          marginRight: 'auto',
          marginTop: '4px',
          marginBottom: '12px',
        }}>
          <NodeForm
            key={`inline-${id}`}
            parentId={parseInt(id, 10)}
            hidePowerFeatures={!craftMode}
            placeholder="Type what's on your mind…"
            submitLabel="Send"
            onSuccess={handleInlineSuccess}
          />
        </div>
      )}
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

      {showPromptEditConfirm && (
        <div
          onClick={() => setShowPromptEditConfirm(false)}
          style={{
            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: 'rgba(0,0,0,0.7)',
            backdropFilter: 'blur(8px)',
            WebkitBackdropFilter: 'blur(8px)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            zIndex: 1000,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: 'var(--bg-card)', border: '1px solid var(--border)',
              borderRadius: '12px', padding: '2rem', maxWidth: '440px', width: '90vw',
            }}
          >
            <h3 style={{
              fontFamily: 'var(--serif)', fontWeight: 400, fontSize: '1.15rem',
              color: 'var(--text-primary)', margin: '0 0 12px 0',
            }}>Edit prompt for this thread only?</h3>
            <p style={{
              fontFamily: 'var(--sans)', fontSize: '0.88rem', fontWeight: 300,
              color: 'var(--text-secondary)', lineHeight: 1.6, margin: '0 0 16px 0',
            }}>
              This changes the system prompt in this conversation only. Future threads
              won't be affected, and updates to your prompt template won't reach this
              thread anymore.
            </p>
            <p style={{
              fontFamily: 'var(--sans)', fontSize: '0.82rem', fontWeight: 300,
              color: 'var(--text-muted)', lineHeight: 1.5, margin: '0 0 20px 0',
            }}>
              To edit the template for all future threads, go to{' '}
              <a href="/prompts" style={{ color: 'var(--accent)', textDecoration: 'underline' }}>Prompts</a>.
            </p>
            <div style={{ display: 'flex', gap: '12px', justifyContent: 'center' }}>
              <button
                onClick={() => setShowPromptEditConfirm(false)}
                style={{
                  padding: '8px 20px', background: 'none',
                  border: '1px solid var(--border)', borderRadius: '6px',
                  color: 'var(--text-secondary)', fontFamily: 'var(--sans)',
                  fontSize: '0.85rem', cursor: 'pointer',
                }}
              >Cancel</button>
              <button
                onClick={() => {
                  setShowPromptEditConfirm(false);
                  setShowEditOverlay(true);
                }}
                style={{
                  padding: '8px 20px', background: 'none',
                  border: '1px solid var(--accent)', borderRadius: '6px',
                  color: 'var(--accent)', fontFamily: 'var(--sans)',
                  fontSize: '0.85rem', cursor: 'pointer',
                }}
              >Edit for this thread</button>
            </div>
          </div>
        </div>
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
            detachPrompt: !!node.context_artifacts?.prompt,
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
