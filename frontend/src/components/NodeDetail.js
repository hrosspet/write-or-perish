import React, { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useNavigate, useSearchParams, Link } from "react-router-dom";
import { FaThumbtack, FaMicrophone } from "react-icons/fa";
import NodeFooter from "./NodeFooter";
import SpeakerIcon from "./SpeakerIcon";
import DownloadAudioIcon from "./DownloadAudioIcon";
import ModelSelector from "./ModelSelector";
import NodeForm from "./NodeForm";
import ProposalInline, { hasProposalSections, splitProposalText } from "./ProposalInline";
import SemanticNeighbors from "./SemanticNeighbors";
import { useUser } from "../contexts/UserContext";
import { useToast } from "../contexts/ToastContext";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";
import api from "../api";
import { useCheckboxToggle, useTaskInsert } from "../utils/markdown";
import { contextAllowsAi } from "../utils/aiUsage";
import NodeFormModal from "./NodeFormModal";
import Bubble from "./Bubble";
import BubbleKebabMenu from "./BubbleKebabMenu";
import QuotedContent from "./QuotedContent";
import DeleteConfirmDialog from "./DeleteConfirmDialog";

// Recursive component to render children nodes.
function RenderChildTree({ nodes, onBubbleClick, buildActions }) {
  return (
    <div>
      {nodes.map((child, index) => {
        const containerStyle = (nodes.length > 1)
          ? { marginLeft: "20px", paddingLeft: "10px", borderLeft: "2px solid var(--border)" }
          : { marginLeft: "0px" };

        return (
          <div key={child.id}>
            <div style={containerStyle}>
              <Bubble
                node={child}
                onClick={onBubbleClick}
                leftAlign={true}
                actions={buildActions ? buildActions(child) : null}
              />
              {child.children && child.children.length > 0 &&
                <RenderChildTree nodes={child.children} onBubbleClick={onBubbleClick} buildActions={buildActions} />
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

function NodeDetail({ nodeIdOverride }) {
  const { id: paramId } = useParams();
  // Under /u/:username/:slug the id arrives resolved; under /node/:id it
  // comes from params. Everything downstream just uses `id`.
  const id = nodeIdOverride || paramId;
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { user: currentUser } = useUser();
  const { addToast } = useToast();
  const craftMode = !!currentUser?.craft_mode;
  const [node, setNode] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showEditOverlay, setShowEditOverlay] = useState(false);
  const [selectedModel, setSelectedModel] = useState(currentUser?.preferred_model || null);
  const [llmTaskNodeId, setLlmTaskNodeId] = useState(null);
  const [quotes, setQuotes] = useState({});
  const [externalQuotes, setExternalQuotes] = useState({});
  const [pinLoading, setPinLoading] = useState(false);
  const [voiceLoading, setVoiceLoading] = useState(false);
  const [toolActionsExpanded, setToolActionsExpanded] = useState(false);
  const [showPromptEditConfirm, setShowPromptEditConfirm] = useState(false);
  // Per-bubble action targets. The kebab on any rendered Bubble (focal,
  // ancestor, child) sets exactly one of these via setExclusiveTarget.
  const [replyTarget, setReplyTarget] = useState(null);
  const [editTarget, setEditTarget] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const setExclusiveTarget = useCallback((slot, value) => {
    setReplyTarget(slot === 'reply' ? value : null);
    setEditTarget(slot === 'edit' ? value : null);
    setDeleteTarget(slot === 'delete' ? value : null);
  }, []);
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
  const highlightedNodeRef = useRef(null);

  // `autoGenerate` is the single source of truth. Defaults to true on
  // a fresh install (see useState initializer) and persists across
  // remounts via localStorage. The toggle is only visible in Craft
  // mode, but the stored preference governs behavior in both modes so
  // the UI state always matches observed behavior.
  //
  // PUBLIC threads (#228) are the exception: generation there is always a
  // deliberate act — auto-generate is forced off (and its toggle hidden),
  // and the explicit LLM Response bar shows for the node's owner instead.
  const isPublicThread = node?.privacy_level === 'public';
  const autoGenerateActive = isPublicThread ? false : autoGenerate;

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
    setExternalQuotes({});
    api
      .get(`/nodes/${id}`)
      .then((response) => {
        setNode(response.data);
        setLoading(false);
        // Human-readable address (#228): when the node has a permalink and
        // we arrived via /node/<id>, show the pretty URL instead. Display
        // only — router state is untouched, and revisiting the pretty URL
        // resolves through the permalink route.
        if (response.data.permalink
            && window.location.pathname === `/node/${id}`) {
          window.history.replaceState(null, '', response.data.permalink);
        }
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

    // Check if content contains {quote:ID} / {quote_ext:ID} patterns
    const quotePattern = /\{quote(?:_ext)?:(\d+)\}/;
    if (!quotePattern.test(node.content)) return;

    // Fetch quotes for this node
    api
      .get(`/nodes/${id}/resolve-quotes`)
      .then((response) => {
        if (response.data.has_quotes && response.data.quotes) {
          setQuotes(response.data.quotes);
        }
        if (response.data.has_quotes && response.data.external_quotes) {
          setExternalQuotes(response.data.external_quotes);
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

  // editTarget drives Edit's two-path branch: prompt-rooted nodes show
  // a confirmation first; everything else opens the edit overlay
  // directly. Both paths route through editTarget.id, so focal and
  // non-focal edits share one code path.
  useEffect(() => {
    if (!editTarget) return;
    if (editTarget.context_artifacts?.prompt) {
      setShowPromptEditConfirm(true);
    } else {
      setShowEditOverlay(true);
    }
  }, [editTarget]);

  // Handle LLM completion
  useEffect(() => {
    if (llmStatus === 'completed' && llmData) {
      // Prefer the id of the node returned in payload; fall back to the
      // polled node id (llmTaskNodeId).
      const completedId = llmData.node?.id || llmTaskNodeId;
      // Within-turn retrieval (#158): an interim node carries a
      // continuation_node_id pointing at the node that holds (or will hold)
      // the answer. Follow the chain — view the continuation and keep polling
      // it — so the interim retrieval step renders as its own bubble and the
      // final answer arrives in the next. Repeats for each retrieval round.
      if (llmData.continuation_node_id) {
        const contId = llmData.continuation_node_id;
        // Navigate WITH ?awaitLlm so polling re-establishes on the
        // continuation node — NodeDetail remounts on :id change, so bare
        // llmTaskNodeId state would be lost (this matches WritePage's
        // handoff). The awaitLlm effect picks it up after the remount.
        setLlmTaskNodeId(null);
        navigate(`/node/${contId}?awaitLlm=${contId}`);
        return;
      }
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

  const getNodeContent = useCallback(() => node?.content, [node]);
  const setNodeContent = useCallback((newContent) => setNode(prev => ({ ...prev, content: newContent })), []);
  const saveNodeContent = useCallback((newContent) => api.put(`/nodes/${id}`, { content: newContent }), [id]);
  const handleCheckboxToggle = useCheckboxToggle(getNodeContent, setNodeContent, saveNodeContent);
  const handleTaskInsert = useTaskInsert(getNodeContent, setNodeContent, saveNodeContent);

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

  // Mirror the focal isOwner derivation (same as NodeDetail.js:216) for
  // any ancestor or child node, using the user_id / parent_user_id
  // fields added in the matching backend change.
  const ownedByMe = (n) => !!currentUser && (
    n.user_id === currentUser.id
    || (n.node_type === "llm" && n.parent_user_id === currentUser.id)
  );

  const buildActions = (n) => {
    // `kind` is the stable identifier — Bubble pulls the reply action
    // out of this list to wire the comment-icon click. The label is
    // free to change without breaking that coupling.
    const actions = [{
      kind: 'reply',
      label: 'Reply',
      action: () => setExclusiveTarget('reply', n),
      color: 'var(--text-primary)',
    }];
    if (ownedByMe(n)) {
      actions.push({
        label: 'Edit',
        action: () => setExclusiveTarget('edit', n),
        color: 'var(--text-primary)',
      });
      actions.push({
        label: 'Delete',
        action: () => setExclusiveTarget('delete', n),
        color: 'var(--accent)',
      });
    }
    return actions;
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

  const handleConfirmDelete = ({ withDescendants }) => {
    if (!deleteTarget) return;
    const targetId = deleteTarget.id;
    const wasFocal = targetId === node.id;
    setDeleteTarget(null);
    api
      .delete(`/nodes/${targetId}`, { params: { delete_descendants: withDescendants } })
      .then((response) => {
        const data = response.data || {};
        const n = data.scheduled || 1;
        addToast(
          `Deleted ${n} node${n === 1 ? "" : "s"}`,
          3000,
        );
        // If the cascade swept the focal node away (target is an
        // ancestor of focal AND descendants were included), refetching
        // focal would 404. Treat this like a focal-target delete and
        // walk up to the closest alive ancestor of the deleted target.
        const ancestorIdx = (!wasFocal && withDescendants && node.ancestors)
          ? node.ancestors.findIndex((a) => a.id === targetId)
          : -1;
        const focalCascaded = ancestorIdx !== -1;
        if (!wasFocal && !focalCascaded) {
          // Non-focal target: refetch the focal node so the just-deleted
          // ancestor/child surfaces as a tombstone preview in place.
          return api.get(`/nodes/${id}`).then((r) => setNode(r.data));
        }
        // Walk up to the closest alive ancestor. For the focal-target
        // case, that's everything in node.ancestors; for the cascade
        // case, it's everything strictly above the deleted target. The
        // immediate parent may itself be a tombstone (chained "this
        // only"), and the surviving ancestor's view shows the just-
        // deleted node as a tombstoned child preview — so the tombstone
        // stays visible without dropping the user on a 404.
        const upperBound = focalCascaded ? ancestorIdx : (node.ancestors?.length ?? 0);
        if (node.ancestors) {
          for (let i = upperBound - 1; i >= 0; i -= 1) {
            if (!node.ancestors[i].deleted) {
              navigate(`/node/${node.ancestors[i].id}`);
              return;
            }
          }
        }
        // No alive ancestor (deleted a root). Public roots live in the
        // Commons, so land back there; everything else goes to the Log.
        if (node.privacy_level === "public"
            && currentUser?.share_v1_enabled) {
          navigate("/commons");
        } else {
          navigate("/log");
        }
        return undefined;
      })
      .catch((err) => {
        console.error(err);
        setError("Error deleting node.");
      });
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

  // Shared handling for a failed LLM-request (the /nodes/:id/llm POST),
  // used by the explicit button and the auto-generate-on-submit/edit paths.
  // - 402 (monthly spend cap): surfaced globally by SpendCapBanner — return
  //   silently so the thread stays exactly where the user was. setError()
  //   here replaces the whole node view with the message, so echoing the
  //   raw code would both blank the thread and look technical.
  // - 400 (user-correctable, e.g. a bad {user_export} placeholder): toast,
  //   keep the page intact.
  // - otherwise: surface in the node view.
  const handleLlmRequestError = (err) => {
    console.error(err);
    const status = err?.response?.status;
    const apiErr = err?.response?.data?.error;
    if (status === 402) return;
    if (status === 400 && apiErr) {
      addToast(apiErr, 8000);
      return;
    }
    setError(apiErr || err.message || "Error requesting LLM response.");
  };

  const handleLLMResponse = () => {
    setError("");
    requestLlmFor(id)
      .then((newNodeId) => setLlmTaskNodeId(newNodeId))
      .catch(handleLlmRequestError);
  };

  // Gate + fire a child LLM generation under `parentNodeId`. Returns the
  // new pending LLM node id, or null if auto-generate is off or the
  // ancestry blocks AI. Re-checks context at submit time (not just on
  // mount): if ANY node in `chainNodes` has `ai_usage` outside {chat,
  // train}, auto-generate silently skips and toasts the user. Prevents
  // firing an LLM call that would omit parts of the thread from context
  // and produce partial / confusing replies.
  const tryAutoGenerateFor = async (parentNodeId, chainNodes) => {
    if (!autoGenerateActive) return null;
    if (!contextAllowsAi(chainNodes)) {
      setAutoGenerate(false);
      addToast(
        'Turning off auto-generate. AI usage on some nodes is turned off.',
        8000,
      );
      return null;
    }
    return await requestLlmFor(parentNodeId);
  };

  // Called by NodeForm after it successfully POSTs /nodes/.
  // `data` is the newly-created child node from the backend.
  const handleInlineSuccess = async (data) => {
    const newNodeId = data?.id;
    if (!newNodeId) return;
    setError("");
    try {
      const chain = [node, ...(node.ancestors || [])];
      const llmNodeId = await tryAutoGenerateFor(newNodeId, chain);
      if (llmNodeId) {
        // Navigate directly to the pending LLM node so the inline input
        // stays anchored below it throughout generation.
        navigate(`/node/${llmNodeId}?awaitLlm=${llmNodeId}`);
      } else {
        navigate(`/node/${newNodeId}`);
      }
    } catch (err) {
      // Spend cap: the user's node WAS created; only the LLM reply was
      // blocked. Land on the new node (same as the auto-generate-off path)
      // so it shows and the input resets — the banner fires globally.
      if (err?.response?.status === 402) {
        navigate(`/node/${newNodeId}`);
        return;
      }
      handleLlmRequestError(err);
    }
  };

  // Called after NodeForm successfully PUTs /nodes/<id>. Updates local
  // state, closes the overlay, and — if the edited node is user-authored
  // — fires a fresh LLM child off it. Re-running on a node that already
  // has an LLM child produces a new sibling, i.e. a new branch off the
  // edit. Editing an LLM node never triggers generation: nothing for it
  // to reply to.
  const handleEditSuccess = async (data) => {
    const wasFocal = editTarget?.id === node.id;
    setShowEditOverlay(false);
    setEditTarget(null);

    if (!wasFocal) {
      // Non-focal edit: refetch the focal node so the edited
      // ancestor/child reflects new content. No auto-LLM trigger —
      // the user is in a different conversation context.
      try {
        const refreshed = await api.get(`/nodes/${id}`).then((r) => r.data);
        setNode(refreshed);
      } catch (err) {
        console.error(err);
      }
      return;
    }

    const updated = data.node ? data.node : { ...node, content: data.content };
    setNode(updated);
    const editedIsLlm = updated.node_type === "llm" || !!updated.llm_model;
    if (editedIsLlm) return;
    try {
      const chain = [updated, ...(updated.ancestors || [])];
      const llmNodeId = await tryAutoGenerateFor(updated.id, chain);
      if (llmNodeId) navigate(`/node/${llmNodeId}?awaitLlm=${llmNodeId}`);
    } catch (err) {
      handleLlmRequestError(err);
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
        setVoiceLoading(false);
        // 402 = spend cap, surfaced globally by SpendCapBanner; don't echo
        // the raw error code into the view.
        if (err?.response?.status === 402) return;
        setError(err.response?.data?.error || `Error starting ${sessionType} session.`);
      });
  };

  // Ancestors section rendered as a list of bubbles.
  const ancestorsSection = node.ancestors && node.ancestors.length > 0 && (
    <div style={{ display: "flex", flexDirection: "column", marginBottom: "10px" }}>
      {node.ancestors.map((ancestor) => (
        <Bubble
          key={ancestor.id}
          node={ancestor}
          onClick={handleBubbleClick}
          leftAlign={true}
          actions={buildActions(ancestor)}
        />
      ))}
    </div>
  );

  // Highlighted node section
  const highlightedTextStyle = {
    boxSizing: "border-box",
    padding: "1.8rem 2rem",
    margin: "18px 0 10px 0",
    backgroundColor: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderLeft: "3px solid var(--accent)",
    borderRadius: "10px",
    // Reserve right-side space for the always-outside kebab so the
    // visible gap to the right of the icon matches the gap between
    // bubble and icon (~14px). Drops the 95% scaling — maxWidth still
    // caps the bubble on wide viewports.
    width: "calc(100% - 50px)",
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
  // When a proposal is present, the lead-in renders above the card and any
  // trailing commentary below it (proposalAfter). Otherwise show full content.
  const proposalSplit = showProposal ? splitProposalText(node.content) : null;
  const displayContent = showProposal ? proposalSplit.before : node.content;
  const proposalAfter = showProposal ? proposalSplit.after : '';
  // Inline input is always available to any viewer (reply + branch from
  // someone else's public node → new thread owned by the replier). The
  // only way to add a child text node since "Add Text" was removed. On
  // `ai_usage='none'` nodes the submit path skips the LLM request.
  // Ownership gates apply elsewhere: Voice Mode (backend 403s non-owners
  // via /voice/from-node), Craft-bar LLM Response + ModelSelector, and
  // the kebab Edit/Delete menu.
  const showInlineInput = !!currentUser;
  const showCraftBar = isOwner && (craftMode || isPublicThread)
    && !autoGenerateActive && node.ai_usage !== 'none';

  // Shared shell for the top-right controls. Voice Mode + Auto-generate
  // share padding/border/typography; Auto-generate uses `space-between`
  // (label left, pill right) to match the Craft-mode toggle in the
  // NavBar overflow menu. They sit side-by-side in a single row so the
  // fixed strip stays above the Thread / ancestor hr separator.
  const topRightButtonStyle = {
    background: 'none',
    border: '1px solid var(--border)',
    borderRadius: '6px',
    padding: '0 12px',
    color: 'var(--text-muted)',
    fontFamily: 'var(--sans)',
    fontSize: '0.78rem',
    fontWeight: 300,
    cursor: 'pointer',
    display: 'inline-flex',
    alignItems: 'center',
    width: '160px',
    height: '32px',
    boxSizing: 'border-box',
  };

  // Top-right controls (Voice Mode + Auto-generate). Rendered in the
  // same flex row as the Thread heading so they align vertically and
  // scroll away with content (no absolute positioning / viewport
  // anchoring).
  const topRightControls = isOwner && node.ai_usage !== 'none'
    && !isPublicThread && (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'flex-end',
      gap: '6px',
    }}>
      <button
        onClick={() => handleSessionFromNode('voice')}
        disabled={voiceLoading}
        style={{ ...topRightButtonStyle, justifyContent: 'space-between' }}
        title="Continue this conversation by voice"
      >
        <span>{voiceLoading ? 'Starting…' : 'Voice Mode'}</span>
        <span style={{
          width: '32px',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          lineHeight: 0,
        }}>
          <FaMicrophone size={12} />
        </span>
      </button>
      {craftMode && (
        <button
          type="button"
          onClick={() => setAutoGenerate(v => !v)}
          title={autoGenerate ? 'Auto-generate is on — click to turn off' : 'Auto-generate is off — click to turn on'}
          style={{ ...topRightButtonStyle, justifyContent: 'space-between' }}
        >
          <span>Auto-generate</span>
          <span style={{
            width: '32px',
            height: '18px',
            borderRadius: '9px',
            background: autoGenerate ? 'var(--accent)' : 'var(--border)',
            position: 'relative',
            transition: 'background 0.2s ease',
            flexShrink: 0,
          }}>
            <span style={{
              width: '14px',
              height: '14px',
              borderRadius: '50%',
              background: 'var(--text-primary)',
              position: 'absolute',
              top: '2px',
              left: autoGenerate ? '16px' : '2px',
              transition: 'left 0.2s ease',
            }} />
          </span>
        </button>
      )}
    </div>
  );

  const highlightedNodeSection = (
    <div ref={highlightedNodeRef} style={{ position: 'relative' }}>
      <hr style={{ borderColor: "var(--border)", margin: 0 }} />
      <div style={highlightedTextStyle}>
        {isOwner && (
          <BubbleKebabMenu
            visible={true}
            items={[
              {
                label: 'Edit',
                action: () => setExclusiveTarget('edit', node),
                color: 'var(--text-primary)',
              },
              {
                label: 'Delete',
                action: () => setExclusiveTarget('delete', node),
                color: 'var(--accent)',
              },
            ]}
          />
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
          (!showProposal || displayContent) && (
            <QuotedContent
              content={displayContent}
              quotes={quotes}
              externalQuotes={externalQuotes}
              contextArtifacts={node.context_artifacts || null}
              onQuoteClick={handleBubbleClick}
              onCheckboxToggle={isOwner ? handleCheckboxToggle : undefined}
              onAddTask={isOwner ? handleTaskInsert : undefined}
            />
          )
        )}
        {showProposal && (
          <ProposalInline
            content={node.content}
            nodeId={node.id}
            toolCallsMeta={node.tool_calls_meta}
            onContentChange={isOwner
              ? (newContent) => setNode(prev => prev ? { ...prev, content: newContent } : prev)
              : undefined}
            onApplied={(toolName, updates) => setNode(prev => {
              if (!prev || !Array.isArray(prev.tool_calls_meta)) return prev;
              return {
                ...prev,
                tool_calls_meta: prev.tool_calls_meta.map(tc =>
                  tc.name === toolName ? { ...tc, ...updates } : tc),
              };
            })}
            onError={(msg) => addToast(msg)}
          />
        )}
        {showProposal && proposalAfter && (
          <div style={{ marginTop: '20px' }}>
            <QuotedContent
              content={proposalAfter}
              quotes={quotes}
              externalQuotes={externalQuotes}
              contextArtifacts={node.context_artifacts || null}
              onQuoteClick={handleBubbleClick}
            />
          </div>
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
                    {tc.name === 'propose_feedback' && (
                      <>Feedback proposed{tc.apply_status === 'completed' && ' (sent)'}{tc.apply_status === 'failed' && ' (failed)'}</>
                    )}
                    {tc.name === 'propose_share' && (
                      <>Share proposed{tc.apply_status === 'completed' && ' (saved as draft)'}{tc.apply_status === 'failed' && ' (failed)'}</>
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
                    {tc.name === 'apply_feedback' && (
                      tc.status === 'success' ? 'Feedback sent' : 'Feedback send failed'
                    )}
                    {tc.name === 'apply_share' && (
                      tc.status === 'success'
                        ? <>Share saved as a draft — <Link to="/share" style={{ color: 'var(--accent)', textDecoration: 'none' }}>Share page</Link></>
                        : 'Share save failed'
                    )}
                    {tc.name === 'update_ai_preferences' && 'Preferences updated'}
                    {tc.name === 'update_artifact' && (
                      <>{tc.created ? 'Created' : 'Updated'} artifact{tc.kind ? <> <Link to={`/artifacts/${tc.kind}`} style={{ color: 'var(--accent)', textDecoration: 'none' }}><code style={{ fontSize: '0.95em' }}>{tc.kind}</code></Link></> : ''}</>
                    )}
                    {tc.name === 'read_artifact' && (
                      <>Read artifact{tc.kind ? <> <Link to={`/artifacts/${tc.kind}`} style={{ color: 'var(--accent)', textDecoration: 'none' }}><code style={{ fontSize: '0.95em' }}>{tc.kind}</code></Link></> : ''}</>
                    )}
                    {tc.name === 'read_todo' && (
                      <>Read <Link to="/todo" style={{ color: 'var(--accent)', textDecoration: 'none' }}>todo list</Link></>
                    )}
                    {tc.name === 'semantic_search' && (
                      <>Searched archive & references{tc.query ? <> — <span style={{ fontStyle: 'italic' }}>“{tc.query}”</span></> : ''}</>
                    )}
                    {tc.name === 'read_full' && (
                      tc.status !== 'success' ? 'Read in full (failed)'
                        : tc.kind === 'external' ? (
                          tc.url
                            ? <>Read in full — <a href={tc.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', textDecoration: 'none' }}>{tc.author_handle ? `@${tc.author_handle}'s post` : 'saved reference'}</a></>
                            : <>Read a saved reference in full</>
                        ) : (
                          <>Read in full — <Link to={`/node/${tc.ref_id}`} style={{ color: 'var(--accent)', textDecoration: 'none' }}>entry #{tc.ref_id}</Link></>
                        )
                    )}
                    {!['propose_todo', 'propose_github_issue', 'propose_feedback', 'propose_share', 'apply_todo_changes', 'apply_github_issue', 'apply_feedback', 'apply_share', 'update_ai_preferences', 'update_artifact', 'read_artifact', 'read_todo', 'semantic_search', 'read_full'].includes(tc.name) && tc.name}
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
          publicPage={node.privacy_level === 'public'}
        >
          <button
            onClick={canPin ? handlePin : undefined}
            disabled={!canPin || pinLoading}
            title={
              !isOwner ? "Only the owner can pin"
              : node.privacy_level === "private" ? "Cannot pin a private node"
              : isPinned ? "Unpin from your public page"
              : "Pin to the top of your public page"
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
          <SpeakerIcon nodeId={node.id} content={node.content} isPublic={node.privacy_level === 'public'} aiUsage={node.ai_usage} onTtsGenerated={() => setNode(prev => prev ? { ...prev, has_tts: true } : prev)} />
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
            hideAudioUpload={!craftMode}
            compact
            placeholder="Type what's on your mind…"
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
        <RenderChildTree
          nodes={node.children}
          onBubbleClick={handleBubbleClick}
          buildActions={buildActions}
        />
      )}
    </div>
  );

  return (
    <div style={{ padding: "8px 12px 12px" }}>
      <div style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        gap: "16px",
        marginBottom: "12px",
      }}>
        <h2 style={{
          fontFamily: "var(--serif)",
          fontWeight: 300,
          fontSize: "1.8rem",
          color: "var(--text-primary)",
          margin: 0,
        }}>Thread</h2>
        {topRightControls}
      </div>
      <SemanticNeighbors nodeId={node.id} />
      {ancestorsSection}
      {highlightedNodeSection}
      {childrenSection}

      {showPromptEditConfirm && (
        <div
          onClick={() => { setShowPromptEditConfirm(false); setEditTarget(null); }}
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
                onClick={() => { setShowPromptEditConfirm(false); setEditTarget(null); }}
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

      {showEditOverlay && editTarget && (
        <NodeFormModal
          title="Edit Text"
          onClose={() => { setShowEditOverlay(false); setEditTarget(null); }}
          nodeFormProps={{
            editMode: true,
            nodeId: editTarget.id,
            initialContent: editTarget.content,
            initialPrivacyLevel: editTarget.privacy_level,
            initialAiUsage: editTarget.ai_usage,
            detachPrompt: !!editTarget.context_artifacts?.prompt,
            hasGeneratedTts: !!editTarget.has_tts,
            onSuccess: handleEditSuccess,
          }}
        />
      )}
      <DeleteConfirmDialog
        open={!!deleteTarget}
        mode="single"
        hasChildren={!!(deleteTarget && (
          deleteTarget.child_count > 0
          || (deleteTarget.children && deleteTarget.children.length > 0)
        ))}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleConfirmDelete}
      />
      {replyTarget && (
        <NodeFormModal
          title="Reply"
          onClose={() => setReplyTarget(null)}
          nodeFormProps={{
            parentId: replyTarget.id,
            hidePowerFeatures: !craftMode,
            onSuccess: (data) => {
              setReplyTarget(null);
              navigate(`/node/${data.id}`);
            },
          }}
        />
      )}
    </div>
  );
}

export default NodeDetail;
