import React, { useState, useEffect, useRef, useCallback } from "react";
import { useParams, useNavigate, useSearchParams, Link } from "react-router-dom";
import { FaThumbtack, FaMicrophone, FaSpinner, FaBookOpen } from "react-icons/fa";
import NodeFooter from "./NodeFooter";
import SpeakerIcon from "./SpeakerIcon";
import DownloadAudioIcon from "./DownloadAudioIcon";
import ModelSelector from "./ModelSelector";
import NodeForm from "./NodeForm";
import ProposalInline, { hasProposalSections, hasShareBlocks, splitProposalText } from "./ProposalInline";
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
import FeedPicks from "./FeedPicks";
import { ReadWindowLine, ReadReplyTail } from "./ReadReply";

import DeleteConfirmDialog from "./DeleteConfirmDialog";


// One tooltip for both "Read further" buttons on the thread page: the
// top-right one and the one in the action row under each node of a
// read thread.
const READ_FURTHER_TITLE = "Another pass over the day's tweets, against everything in this thread so far "
  + "— your marks on these picks included.";
const READ_ENTRY_TITLE = "Loore reads the last day of Community Archive tweets and shows you the ones relevant to this thread";
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

// The browser tab's title for a node: its first line, or a state word
// while an AI reply is still being generated.
const tabTitleFor = (node) => {
  const pending = (node?.node_type === 'llm' || !!node?.llm_model)
    && (node?.llm_task_status === 'pending' || node?.llm_task_status === 'processing');
  if (pending) {
    const batch = Array.isArray(node?.tool_calls_meta)
      && node.tool_calls_meta.some(tc => tc?.name === '_batch'
                                      && ['submitted', 'cancelling'].includes(tc.status));
    return `${batch ? 'Processing' : 'Thinking'}… — Loore`;
  }
  const firstLine = (node?.content || '')
    .trim().split('\n')[0].replace(/^[#>\s]+/, '').slice(0, 120);
  return firstLine ? `${firstLine} — Loore` : 'Loore';
};

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
  // The Read button's own model: reads run only on the read models
  // (#355), so it never shares the LLM Response picker's choice. Null
  // until its picker loads; the backend then picks (resolve_read_model).
  const [readModel, setReadModel] = useState(null);
  const [llmTaskNodeId, setLlmTaskNodeId] = useState(null);
  // True from the click until POST /nodes/:id/llm answers: the spinner is
  // otherwise driven by the returned node id, so the round trip (a cold
  // dev server can take seconds) showed a button that ignored the click.
  const [llmRequesting, setLlmRequesting] = useState(false);
  const [quotes, setQuotes] = useState({});
  const [externalQuotes, setExternalQuotes] = useState({});
  const [pinLoading, setPinLoading] = useState(false);
  const [voiceLoading, setVoiceLoading] = useState(false);
  const [readLoading, setReadLoading] = useState(false);
  const [rerunning, setRerunning] = useState(false);
  const [toolActionsExpanded, setToolActionsExpanded] = useState(false);
  const [showPromptEditConfirm, setShowPromptEditConfirm] = useState(false);
  // Per-bubble action targets. The kebab on any rendered Bubble (focal,
  // ancestor, child) sets exactly one of these via setExclusiveTarget.
  const [replyTarget, setReplyTarget] = useState(null);
  const [editTarget, setEditTarget] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);
  // A confirmed delete that would leave a text/voice session with only
  // its system prompt: { targetId, withDescendants }. The follow-up
  // dialog asks whether to delete the prompt as well.
  const [pendingPromptDelete, setPendingPromptDelete] = useState(null);
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
  // Batch stage ({ca_tweets}): the synchronous part is done and the turn
  // is queued at the provider (minutes, up to 24 h). Poll slowly and
  // don't time out at the hook's 30-minute default.
  const batchMeta = Array.isArray(node?.tool_calls_meta)
    ? node.tool_calls_meta.find(tc => tc?.name === '_batch'
                                     && ['submitted', 'cancelling'].includes(tc.status))
    : null;
  // (Only the pending node's own meta counts here: on the parent page the
  // batch stage triggers a navigation to that node — see the completion
  // effect — so the slow cadence is needed there, not before.)
  const isBatchWait = !!batchMeta;
  const {
    status: llmStatus,
    data: llmData,
    error: llmError
  } = useAsyncTaskPolling(
    llmTaskNodeId ? `/nodes/${llmTaskNodeId}/llm-status` : null,
    {
      enabled: !!llmTaskNodeId,  // Auto-start when llmTaskNodeId is set
      interval: isBatchWait ? 15000 : 2000,
      maxDuration: isBatchWait ? 25 * 60 * 60 * 1000 : 30 * 60 * 1000,
    }
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
        // /node/<id> is server-rendered: a private node arrives in the
        // "Not found" shell (404 for private-or-missing alike, so ids can't
        // be probed), so the tab title has to be set here once the
        // logged-in fetch succeeds — same first-line rule PublicThreadPage
        // uses. Reset on unmount below.
        document.title = tabTitleFor(response.data);
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
        // 404 covers missing, deleted and not-shared-with-you alike (the
        // backend never says which), so the message has to as well.
        if (err.response && (err.response.status === 404 || err.response.status === 403)) {
          setError("This node doesn't exist, was deleted, or isn't shared with you.");
        } else {
          setError("Error fetching node details.");
        }
        setLoading(false);
      });
    return () => {
      document.title = 'Loore';
    };
  }, [id]);

  // Keep the tab title with the content: a pending reply loads as its
  // placeholder text and is patched in place when the batch lands, so
  // the title set on fetch would otherwise stay "[LLM response generation
  // pending...]" for the finished reply.
  useEffect(() => {
    if (node) document.title = tabTitleFor(node);
  }, [node]);

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

  // Opening a pending LLM node directly (the batch wait parks the view
  // there) resumes polling on it, so the reply patches in when it lands.
  useEffect(() => {
    if (!node || llmTaskNodeId) return;
    const pending = (node.node_type === 'llm' || !!node.llm_model)
      && (node.llm_task_status === 'pending' || node.llm_task_status === 'processing');
    if (pending && String(node.id) === String(id)) {
      setLlmTaskNodeId(node.id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node?.id, node?.llm_task_status]);

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

  // Handle LLM completion. Both branches require llmTaskNodeId: after it
  // is cleared, this effect re-runs once (llmTaskNodeId is a dep) while
  // llmStatus/llmData are still stale — the polling hook resets them a
  // render later — and without the guard that stale pass acts twice
  // (duplicate toast / duplicate navigation).
  useEffect(() => {
    if (!llmTaskNodeId) return;
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
          feed_picks_count: llmData.feed_picks_count ?? prev.feed_picks_count,
          llm_task_status: 'completed',
        } : prev);
      } else if (completedId) {
        navigate(`/node/${completedId}`);
      }
      setLlmTaskNodeId(null);
    } else if (llmData?.stage === 'batch'
               && String(llmTaskNodeId) !== String(id)) {
      // The turn is queued in a provider batch: the generate button's job
      // is done. Hand the wait to the pending node itself (it shows
      // "Processing" and keeps polling slowly via the effect above).
      setLlmTaskNodeId(null);
      navigate(`/node/${llmTaskNodeId}`);
    } else if (llmStatus === 'cancelled') {
      // A read withdrawn before it ran (the spend cap was reached while
      // it was queued): nothing was billed, and the node's text says so.
      addToast(llmData?.error || 'Read cancelled', 8000);
      if (String(llmTaskNodeId) === String(id)) {
        setNode(prev => prev ? {
          ...prev,
          content: llmData?.content ?? prev.content,
          tool_calls_meta: llmData?.tool_calls_meta ?? prev.tool_calls_meta,
          llm_task_status: 'cancelled',
        } : prev);
      }
      setLlmTaskNodeId(null);
    } else if (llmStatus === 'failed') {
      // Toast, never setError — setError replaces the entire thread view
      // with the raw failure text, hiding the thread and the inline form.
      addToast(llmError || 'LLM response generation failed', 8000);
      // awaitLlm flows can park the view on the pending LLM node itself;
      // on failure that's an empty dead node — hop to its parent (the
      // user's entry), replacing the dead history entry so a back press
      // walks real nodes.
      if (String(llmTaskNodeId) === String(id)) {
        const parent = node?.ancestors?.[node.ancestors.length - 1];
        if (parent && !parent.deleted) {
          navigate(`/node/${parent.id}`, { replace: true });
        }
      }
      setLlmTaskNodeId(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  // The thread's root as this page knows it: the topmost ancestor the
  // viewer can see, else the node itself. The orphaned-prompt check, the
  // prompt dialog's copy and the landing once the session is gone all
  // read this one node, so the dialog's "leaves your public page" and
  // the page the delete lands on agree in a mixed-privacy thread too.
  const threadRoot = node.ancestors?.length ? node.ancestors[0] : node;

  const handleConfirmDelete = ({ withDescendants }) => {
    if (!deleteTarget) return;
    const targetId = deleteTarget.id;
    setDeleteTarget(null);
    // Would this leave the session with only its system prompt? The
    // root would stay alive and the Log would show a card whose title
    // and preview are the prompt text. Ask before that happens. Only a
    // system-prompt thread can end up there, and the root is already on
    // the page, so other threads skip the round trip. The check is
    // advisory: if it fails, the delete goes ahead as asked.
    if (!threadRoot.is_system_prompt || targetId === threadRoot.id) {
      performDelete(targetId, withDescendants, false);
      return;
    }
    api
      .get(`/nodes/${targetId}/delete-impact`, { params: { delete_descendants: withDescendants } })
      .then((r) => (r.data && r.data.orphaned_system_prompt_id) || null)
      .catch(() => null)
      .then((promptRootId) => {
        if (promptRootId) {
          setPendingPromptDelete({ targetId, withDescendants });
        } else {
          performDelete(targetId, withDescendants, false);
        }
      });
  };

  const handleConfirmPromptDelete = ({ includePrompt }) => {
    if (!pendingPromptDelete) return;
    const { targetId, withDescendants } = pendingPromptDelete;
    setPendingPromptDelete(null);
    // One request either way: the answer rides along as a flag and the
    // server re-checks under the root's lock that nothing else is left,
    // so an entry that arrived since the check (another device, the
    // Voice chain) keeps the session.
    performDelete(targetId, withDescendants, includePrompt);
  };

  const performDelete = (targetId, withDescendants, includePrompt) => {
    const wasFocal = targetId === node.id;
    api
      .delete(`/nodes/${targetId}`, {
        params: {
          delete_descendants: withDescendants,
          delete_orphaned_prompt: includePrompt,
        },
      })
      .then((response) => {
        const data = response.data || {};
        const n = data.scheduled || 1;
        addToast(
          `Deleted ${n} node${n === 1 ? "" : "s"}`,
          3000,
        );
        // Where to land when nothing of this thread is left to show:
        // public roots live in the Commons, everything else in the Log.
        // `listed` is the node whose privacy says which of the two.
        const leaveThread = (listed) => {
          if (listed.privacy_level === "public"
              && currentUser?.share_v1_enabled) {
            navigate("/commons");
          } else {
            navigate("/log");
          }
        };
        // The session root went with the target: nothing of ours is
        // alive in the thread any more (the server took the root only
        // because nothing was left), so there is no node to refetch or
        // walk up to — the page being viewed is the root, the target,
        // or something under them. The root is what was listed, and
        // what the prompt dialog's copy was written from.
        if (data.orphaned_prompt_deleted) {
          leaveThread(threadRoot);
          return undefined;
        }
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
        // No alive ancestor (deleted a root).
        leaveThread(node);
        return undefined;
      })
      .catch((err) => {
        console.error(err);
        setError("Error deleting node.");
      });
  };

  const requestLlmFor = async (parentNodeId, { sourceMode = 'textmode' } = {}) => {
    const response = await api.post(`/nodes/${parentNodeId}/llm`, {
      model: selectedModel,
      source_mode: sourceMode,
    });
    const newNodeId = response.data.node_id;
    if (!newNodeId) throw new Error("Failed to get a task ID for the new LLM node.");
    return newNodeId;
  };

  // Shared handling for a failed LLM-request (the /nodes/:id/llm POST),
  // used by the explicit button and the auto-generate-on-submit/edit paths.
  // - 402 (monthly spend cap): surfaced globally by SpendCapBanner — return
  //   silently so the thread stays exactly where the user was.
  // - otherwise: toast. Never setError() here — that replaces the whole
  //   node view (thread, inline form and all) with the failure text and
  //   strands the user on a dead end.
  const handleLlmRequestError = (err) => {
    console.error(err);
    const status = err?.response?.status;
    const apiErr = err?.response?.data?.error;
    if (status === 402) return;
    addToast(apiErr || err.message || "Error requesting LLM response.", 8000);
  };

  const handleLLMResponse = () => {
    setError("");
    setLlmRequesting(true);
    requestLlmFor(id)
      .then((newNodeId) => setLlmTaskNodeId(newNodeId))
      .catch(handleLlmRequestError)
      .finally(() => setLlmRequesting(false));
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
        // Land on the NEW USER node so it gets its own URL/history step
        // (a back press then walks the actual entries); ?awaitLlm keeps
        // the pending LLM response polling anchored here and navigates
        // to the response on completion.
        navigate(`/node/${newNodeId}?awaitLlm=${llmNodeId}`);
      } else {
        navigate(`/node/${newNodeId}`);
      }
    } catch (err) {
      // The user's node WAS created; only the LLM request failed. Always
      // land on the new node so it shows, gets its history entry, and the
      // input resets — then surface the failure (402's banner fires
      // globally; the rest toast via handleLlmRequestError).
      navigate(`/node/${newNodeId}`);
      handleLlmRequestError(err);
    }
  };

  // A reply typed or recorded under a read reply is a Text-mode message
  // (#323): the backend attaches the textmode prompt under the read reply
  // when no agentic prompt sits above it, so the conversation about the
  // picks runs with the assistant's tools and what the user says there
  // can move their intentions and memory. The read itself never runs
  // under that prompt (the task strips it on read turns). Auto-generate
  // is honoured like Text mode's own entry, and the reply fires
  // server-side, so there is no /nodes/<id>/llm follow-up here.
  const submitReadReplyMessage = async ({ content, streaming_session_id }) => {
    if (streaming_session_id) {
      const res = await api.post(
        `/drafts/streaming/${streaming_session_id}/save-as-node`,
        { content, agentic: true, auto_generate: autoGenerateActive, model: selectedModel },
      );
      return res.data;
    }
    const res = await api.post(`/textmode/from-node/${id}`, {
      content, model: selectedModel, auto_generate: autoGenerateActive,
    });
    return res.data;
  };
  const handleReadReplySuccess = (data) => {
    const userNodeId = data?.user_node_id || data?.id;
    if (!userNodeId) return;
    const suffix = data?.llm_node_id ? `?awaitLlm=${data.llm_node_id}` : '';
    navigate(`/node/${userNodeId}${suffix}`);
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

    // Privacy / AI usage cascaded to the user's replies as well.
    const cascaded = data?.descendants_updated || 0;
    if (cascaded) {
      addToast(
        `Applied to ${cascaded} repl${cascaded === 1 ? "y" : "ies"} too`,
        3000,
      );
    }

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

    // PUT returns the node's own fields only (no ancestors/children —
    // re-serializing the thread on every save was the slow part); the
    // tree we already hold is unchanged by an edit, so merge.
    let updated = data.node ? { ...node, ...data.node } : { ...node, content: data.content };
    if (cascaded) {
      // The replies' settings changed too — the tree we hold is stale.
      try {
        updated = await api.get(`/nodes/${id}`).then((r) => r.data);
      } catch (err) {
        console.error(err);
      }
    }
    setNode(updated);
    const editedIsLlm = updated.node_type === "llm" || !!updated.llm_model;
    if (editedIsLlm) return;
    try {
      const chain = [updated, ...(updated.ancestors || [])];
      const llmNodeId = await tryAutoGenerateFor(updated.id, chain);
      // Focal is already the edited node — start polling in place (no
      // navigation), so the edited node keeps its history entry and the
      // completion effect pushes the response when it lands.
      if (llmNodeId) setLlmTaskNodeId(llmNodeId);
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

  // Community Archive read against this thread (admin-only PoC): the
  // 'read_thread' prompt is attached under this node by reference. With
  // auto-generate on, the batch reply parks under it and we land on the
  // pending reply, which this page polls as "Processing…" until the
  // picks arrive. With it off, only the prompt is attached and we land
  // on it, where Read and its model picker wait for the user.
  // Inside a read thread the same call reads further (no prompt, a read
  // turn under this node; the click is the request, so auto-generate
  // does not apply) and we land on the pending reply.
  const handleReadFromNode = () => {
    setReadLoading(true);
    setError("");
    api
      .post(`/read/from-node/${id}`, {
        model: readModel || undefined,
        auto_generate: autoGenerateActive,
      })
      .then((response) => {
        const { llm_node_id, prompt_node_id } = response.data;
        navigate(`/node/${llm_node_id || prompt_node_id}`);
      })
      .catch((err) => {
        setReadLoading(false);
        if (err?.response?.status === 402) return;
        const msg = err.response?.data?.error || 'Could not start the read.';
        addToast(msg, 6000);
      });
  };

  // Admin's rerun of a read reply (a batch takes minutes to a day):
  // cancels the submitted batch and runs the same reply again, through
  // the batch or the live API. The node stays the same, so the page
  // keeps polling it.
  const rerunRead = (live) => {
    setRerunning(true);
    api
      .post(`/read/${id}/rerun`, { live })
      .then(() => api.get(`/nodes/${id}`))
      .then((response) => {
        setNode(response.data);
        setLlmTaskNodeId(response.data.id);
        addToast(live ? 'Running the read live.' : 'Read resubmitted as a batch.', 4000);
      })
      .catch((err) => {
        addToast(err?.response?.data?.error || 'Could not rerun the read.', 6000);
      })
      .finally(() => setRerunning(false));
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
  // A read reply: it carries (or carried) a batch, or hangs under one of
  // the read prompts. Only these get the admin's rerun controls, while
  // the reply is pending or after it failed.
  const parentAncestor = node.ancestors?.[node.ancestors.length - 1];
  const isReadReply = isLlmNode && (
    !!node.read_reply
    || (Array.isArray(node.tool_calls_meta)
      && node.tool_calls_meta.some(tc => ['_batch', '_read'].includes(tc?.name)))
    || ['read', 'read_thread'].includes(parentAncestor?.prompt_key)
  );
  // A read reply's picks are its {quote_ext:ID} markers. Their read state
  // and good / bad verdicts live in externalQuotes, kept in step by the
  // bubbles' own controls and the tail's "Mark all as read", so the
  // tail's "n unread" is always the list as shown.
  const pickIds = isReadReply
    ? Array.from(new Set(((node.content || '').match(/\{quote_ext:(\d+)\}/g) || [])
        .map(m => m.match(/\d+/)[0])))
    : [];
  const picksLoaded = pickIds.every(pid => pid in externalQuotes);
  const picksUnread = pickIds.filter(pid => externalQuotes[pid] && !externalQuotes[pid].read_at).length;
  const handleExternalReadChange = (itemId, readAt) => setExternalQuotes(prev => (
    prev[itemId] ? { ...prev, [itemId]: { ...prev[itemId], read_at: readAt } } : prev
  ));
  const handleExternalFeedbackChange = (itemId, feedback) => setExternalQuotes(prev => (
    prev[itemId] ? { ...prev, [itemId]: { ...prev[itemId], feedback } } : prev
  ));
  const handlePicksMarkedAll = (readAt) => setExternalQuotes(prev => {
    const next = { ...prev };
    Object.entries(readAt).forEach(([itemId, ts]) => {
      if (next[itemId]) next[itemId] = { ...next[itemId], read_at: ts };
    });
    return next;
  });
  // Inside a read thread the Read button (top right, and the tail's
  // "Read further") reads further: /read/from-node makes a read turn
  // under this node with the whole thread in view, no second prompt.
  // The backend says which it is (in_read_thread: a read prompt above,
  // by key or by the {ca_tweets} placeholder in a PoC-era prompt's
  // text), the same test the route applies; a read reply is one by
  // definition.
  const inReadThread = !!node.in_read_thread || isReadReply;
  // Before the thread has any picks the read action is "Read"; once a
  // read reply sits at or above this node it is "Read further".
  const readReplyAbove = !!node.read_reply_above || isReadReply;
  const readLabel = readReplyAbove ? 'Read further' : 'Read';
  const readTitle = readReplyAbove ? READ_FURTHER_TITLE : READ_ENTRY_TITLE;
  const canRerunRead = !!currentUser?.is_admin && isOwner && isReadReply
    && (isLlmPending || node.llm_task_status === 'failed');
  const showProposal = !!node.content && !isLlmPending && (
    (isLlmNode && hasProposalSections(node.content))
    // User-authored nodes: the owner can write/paste fenced :::share
    // blocks directly (e.g. after splitting one proposed post into two
    // nodes) — same card + Save flow as LLM proposals. Fence syntax only
    // (legacy ### Share headings stay LLM-only), and gated on the user's
    // sharing opt-in so no Save button appears that would 404.
    || (!isLlmNode && isOwner && !!currentUser?.share_v1_enabled
        && hasShareBlocks(node.content))
  );
  // When a proposal is present, the lead-in renders above the card and any
  // trailing commentary below it (proposalAfter). Otherwise show full content.
  const proposalShareOnly = showProposal && !isLlmNode;
  const proposalSplit = showProposal
    ? splitProposalText(node.content, { shareOnly: proposalShareOnly })
    : null;
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
    && !autoGenerateActive && node.ai_usage !== 'none' && !isLlmPending;
  // In a read thread the action row under every node also carries
  // "Read further" — the conversation under the picks can get long and
  // nothing is pinned to the viewport (small screens), so the action
  // travels with the node the user is on. It shows whenever the owner
  // could act, not only in craft mode; LLM Response keeps the craft-bar
  // rule. Each carries its own model picker: reads run only on the read
  // models (#355). Directly under a finished read reply LLM Response is
  // disabled: a reply asked for there is another read (the task's
  // parent rule), and the way to talk about the picks is a comment
  // first, whose own row then offers LLM Response again.
  const underReadReply = isReadReply && node.llm_task_status === 'completed';
  const readActions = isOwner && inReadThread && node.ai_usage !== 'none'
    && !isLlmPending;
  // Before the first picks (the read prompt itself, or a note typed
  // under it) a reply asked for here would be that first read, so the
  // generic LLM Response is not offered at all: the row is "Read" and
  // its model picker.
  const showLlmResponse = showCraftBar && !(inReadThread && !readReplyAbove);
  // Each action carries its own model picker, joined to its right edge:
  // [LLM Response | Opus 4.6 v]  [Read further | GPT-6 Luna v] (#355).
  const actionGroupStyle = { display: 'inline-flex', alignItems: 'stretch' };
  const joinedButtonStyle = {
    borderTopRightRadius: 0, borderBottomRightRadius: 0, borderRight: 'none',
  };
  const joinedPickerStyle = { borderTopLeftRadius: 0, borderBottomLeftRadius: 0 };
  const readBusy = readLoading || llmRequesting || !!llmTaskNodeId;
  const readButton = (
    <span style={actionGroupStyle}>
      <button
        onClick={handleReadFromNode}
        disabled={readBusy}
        title={readTitle}
        style={{ display: 'inline-flex', alignItems: 'center', gap: '8px', ...joinedButtonStyle }}
      >
        {readLoading ? 'Starting…' : readLabel}
      </button>
      <ModelSelector
        nodeId={node.id}
        purpose="read"
        selectedModel={readModel}
        onModelChange={setReadModel}
        disabled={readBusy}
        style={joinedPickerStyle}
      />
    </span>
  );
  const llmResponseTitle = underReadReply
    ? "To chat about the recommendations, send your reply first. To read further, use the button on the right."
    : (inReadThread ? "Chat about the picks" : undefined);

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

  const rerunControls = canRerunRead ? (
    <div style={{
      display: 'inline-flex', gap: '8px', flexWrap: 'wrap',
      marginLeft: isLlmPending ? '12px' : 0,
      marginTop: isLlmPending ? 0 : '8px',
    }}>
      <button
        type="button"
        onClick={() => rerunRead(true)}
        disabled={rerunning}
        style={{ ...topRightButtonStyle, width: 'auto', height: '26px', fontStyle: 'normal' }}
        title="Cancel the batch and run this read through the live API now"
      >
        {rerunning ? 'Rerunning…' : 'Rerun live'}
      </button>
      <button
        type="button"
        onClick={() => rerunRead(false)}
        disabled={rerunning}
        style={{ ...topRightButtonStyle, width: 'auto', height: '26px', fontStyle: 'normal' }}
        title="Cancel the batch and submit this read as a new batch"
      >
        Resubmit batch
      </button>
    </div>
  ) : null;

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
      {currentUser?.is_admin && (
        <button
          onClick={handleReadFromNode}
          disabled={readLoading}
          style={{ ...topRightButtonStyle, justifyContent: 'space-between' }}
          title={inReadThread ? readTitle : READ_ENTRY_TITLE}
        >
          <span>{readLoading ? 'Starting…' : (inReadThread ? readLabel : 'Relevant tweets')}</span>
          <span style={{
            width: '32px',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            lineHeight: 0,
          }}>
            <FaBookOpen size={12} />
          </span>
        </button>
      )}
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
        {isReadReply && node.read_window && (
          <ReadWindowLine window={node.read_window} />
        )}
        {isLlmPending ? (
          <div style={{
            display: 'flex', alignItems: 'center', gap: '10px',
            color: 'var(--text-muted)',
            fontFamily: 'var(--sans)', fontSize: '0.95rem', fontWeight: 300,
            fontStyle: 'italic',
            padding: '8px 0',
          }}>
            <span>{batchMeta ? 'Processing' : 'Thinking'}</span>
            <span style={{ display: 'inline-flex', gap: '3px' }}>
              {[0, 1, 2].map(i => (
                <span key={i} style={{
                  width: '5px', height: '5px', borderRadius: '50%',
                  background: 'var(--text-muted)',
                  animation: `wopPulseDot 1.2s ease-in-out ${i * 0.15}s infinite`,
                }} />
              ))}
            </span>
            {rerunControls}
            <style>{`
              @keyframes wopPulseDot {
                0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
                30% { opacity: 1; transform: translateY(-2px); }
              }
            `}</style>
          </div>
        ) : (
          (!showProposal || displayContent) && (
            <div className={isReadReply ? 'read-reply-body' : undefined}>
              <QuotedContent
                content={displayContent}
                quotes={quotes}
                externalQuotes={externalQuotes}
                onExternalReadChange={handleExternalReadChange}
                onExternalFeedbackChange={handleExternalFeedbackChange}
                contextArtifacts={node.context_artifacts || null}
                onQuoteClick={handleBubbleClick}
                onCheckboxToggle={isOwner ? handleCheckboxToggle : undefined}
                onAddTask={isOwner ? handleTaskInsert : undefined}
              />
            </div>
          )
        )}
        {showProposal && (
          <ProposalInline
            content={node.content}
            nodeId={node.id}
            toolCallsMeta={node.tool_calls_meta}
            shareOnly={proposalShareOnly}
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
              onExternalReadChange={handleExternalReadChange}
              onExternalFeedbackChange={handleExternalFeedbackChange}
              contextArtifacts={node.context_artifacts || null}
              onQuoteClick={handleBubbleClick}
            />
          </div>
        )}
        {!isLlmPending && rerunControls}
        {/* Replies from before 2026-09-16 kept their picks in rows only;
            since then the reply text quotes each pick ({quote_ext:ID}),
            so the list is rendered by QuotedContent above. */}
        {!isLlmPending && isOwner && node.feed_picks_count > 0
          && !/\{quote_ext:\d+\}/.test(node.content || '') && (
          <FeedPicks nodeId={node.id} />
        )}
        {!isLlmPending && isOwner && isReadReply
          && node.llm_task_status === 'completed' && (
          <ReadReplyTail
            nodeId={node.id}
            unread={picksUnread}
            total={pickIds.length}
            loaded={picksLoaded}
            onMarkedAll={handlePicksMarkedAll}
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
          origin={node.origin}
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
        {(showCraftBar || readActions) && (
          <div style={{ marginTop: "8px", display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            {showLlmResponse && (
              /* The group span carries the tooltip, for the button and its
                 model picker alike (they are one control, disabled
                 together): a disabled button or select gets no hover
                 events in some browsers. */
              <span title={llmResponseTitle} style={actionGroupStyle}>
                <button
                  onClick={handleLLMResponse}
                  disabled={llmRequesting || !!llmTaskNodeId || underReadReply}
                  aria-disabled={underReadReply || undefined}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: '8px', ...joinedButtonStyle }}
                >
                  {(llmRequesting || llmTaskNodeId) ? (
                    <>
                      <FaSpinner className="spin" aria-hidden="true" />
                      {llmRequesting ? 'Requesting…'
                        : llmStatus === 'pending' ? 'Waiting for AI…' : 'Generating…'}
                    </>
                  ) : 'LLM Response'}
                </button>
                <ModelSelector
                  nodeId={node.id}
                  selectedModel={selectedModel}
                  onModelChange={setSelectedModel}
                  disabled={llmRequesting || !!llmTaskNodeId || underReadReply}
                  style={joinedPickerStyle}
                />
              </span>
            )}
            {/* Before the first picks "Read" stands alone, where LLM
                Response usually is: the response action users know, under
                its own name. After them "Read further" sits to the right
                of the (disabled or live) LLM Response. */}
            {readActions && readButton}
          </div>
        )}
        {llmTaskNodeId && !showCraftBar && !isLlmPending && (
          <div style={{
            marginTop: '8px',
            display: 'flex', alignItems: 'center', gap: '8px',
            fontFamily: 'var(--sans)', fontSize: '0.78rem', fontWeight: 300,
            color: 'var(--text-muted)',
          }}>
            <FaSpinner className="spin" aria-hidden="true" />
            {llmStatus === 'pending' ? 'Waiting for AI…' : 'Generating…'}
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
            placeholder={isReadReply
              ? "Ask about these picks, or say what you make of them…"
              : "Type what's on your mind…"}
            onSubmitOverride={isReadReply ? submitReadReplyMessage : undefined}
            onSuccess={isReadReply ? handleReadReplySuccess : handleInlineSuccess}
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
        // The controls column is three rows tall; the heading sits at
        // the row's foot, on the rule below, rather than floating at
        // the column's middle.
        alignItems: "flex-end",
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
            // Drives the "apply to replies too?" choice on a privacy /
            // AI-usage change (same test as the delete dialog).
            hasChildren: !!(
              editTarget.child_count > 0
              || (editTarget.children && editTarget.children.length > 0)
            ),
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
      <DeleteConfirmDialog
        open={!!pendingPromptDelete}
        mode="prompt"
        listedIn={
          threadRoot.privacy_level === "public" ? "your public page" : "your Log"
        }
        onClose={() => setPendingPromptDelete(null)}
        onConfirm={handleConfirmPromptDelete}
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
