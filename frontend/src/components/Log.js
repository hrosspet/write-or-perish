import React, { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api";
import Bubble, { splitPreview } from "./Bubble";
import DeleteConfirmDialog from "./DeleteConfirmDialog";
import RenameThreadDialog from "./RenameThreadDialog";
import { useToast } from "../contexts/ToastContext";

function Log({ onSearchClick }) {
  const [logNodes, setLogNodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  // A failed later page must not replace the cards already on screen
  // (the component-wide `error` does); it gets its own line + retry.
  const [loadMoreError, setLoadMoreError] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [renameTarget, setRenameTarget] = useState(null);
  const [renaming, setRenaming] = useState(false);
  const { addToast } = useToast();
  const navigate = useNavigate();

  // The next page starts after the cards on screen, not at a page
  // number: a card removed here (delete) or one that appeared since
  // (another tab) would otherwise shift the offsets and skip or repeat
  // a thread. Cards already held are dropped from the new page too.
  const loadedCount = logNodes.length;
  const fetchMore = useCallback((isFirst) => {
    const offset = isFirst ? 0 : loadedCount;
    if (isFirst) setLoading(true);
    else setLoadingMore(true);
    setLoadMoreError(false);

    api.get(`/log?offset=${offset}&per_page=20`)
      .then(response => {
        const { nodes, has_more } = response.data;
        setLogNodes(prev => {
          if (isFirst) return nodes;
          const held = new Set(prev.map(card => card.id));
          return [...prev, ...nodes.filter(card => !held.has(card.id))];
        });
        setHasMore(has_more);
      })
      .catch(err => {
        console.error(err);
        if (isFirst) setError("Error loading log.");
        else setLoadMoreError(true);
      })
      .finally(() => {
        setLoading(false);
        setLoadingMore(false);
      });
  }, [loadedCount]);

  useEffect(() => {
    fetchMore(true);
    // Only on mount: later pages go through the scroll handler / links.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-load on scroll near bottom. Paused after a failed page so a
  // dead backend doesn't get a request per scroll tick; the retry link
  // below the cards resumes it.
  useEffect(() => {
    if (!hasMore || loading || loadingMore || loadMoreError) return;

    const handleScroll = () => {
      const scrollBottom = window.innerHeight + window.scrollY;
      const docHeight = document.documentElement.scrollHeight;
      if (docHeight - scrollBottom < 300) {
        fetchMore(false);
      }
    };

    window.addEventListener("scroll", handleScroll, { passive: true });
    // Check immediately in case content doesn't fill the viewport
    handleScroll();

    return () => window.removeEventListener("scroll", handleScroll);
  }, [hasMore, loading, loadingMore, loadMoreError, fetchMore]);

  const handleBubbleClick = (nodeId, e) => {
    const card = logNodes.find(n => n.id === nodeId);
    const targetId = (card && card.newest_node_id) || nodeId;
    if (e && (e.metaKey || e.ctrlKey)) {
      window.open(`/node/${targetId}`, '_blank');
    } else {
      navigate(`/node/${targetId}`);
    }
  };

  const handleDeleteThread = (cardNode) => {
    setDeleteTarget(cardNode);
  };

  const handleCloseRename = useCallback(() => setRenameTarget(null), []);

  // A thread can hold several cards: its root and any pinned replies.
  // Rename and delete act on the thread, so every card of it follows.
  const threadOf = (card) => card.thread_root_id || card.id;

  // Empty name = clear it; the card then falls back to the entry's own
  // title. The name lives on the thread root (see thread_root_id note
  // in handleConfirmDeleteThread).
  const handleSaveThreadName = (name) => {
    if (!renameTarget) return;
    const targetId = threadOf(renameTarget);
    setRenaming(true);
    api.put(`/nodes/${targetId}/thread-name`, { thread_name: name })
      .then(response => {
        const saved = (response.data && response.data.thread_name) || null;
        setLogNodes(prev => prev.map(card => (
          threadOf(card) === targetId ? { ...card, thread_name: saved } : card
        )));
        setRenameTarget(null);
      })
      .catch(err => {
        console.error(err);
        const msg = (err.response && err.response.data && err.response.data.error)
          || "Error renaming thread.";
        addToast(msg, 4000);
      })
      .finally(() => setRenaming(false));
  };

  const handleConfirmDeleteThread = ({ withDescendants }) => {
    if (!deleteTarget) return;
    // The backend's `thread_root_id` is the actual thread root (matters
    // when the displayed card is the first child of a system-prompt
    // root, or a pinned reply). For a reply pinned in someone else's
    // thread it is the reply itself.
    const targetId = threadOf(deleteTarget);
    api.delete(`/nodes/${targetId}`, {
      params: { delete_descendants: withDescendants },
    })
      .then(response => {
        const data = response.data || {};
        const n = data.scheduled || 1;
        addToast(
          `Deleted ${n} node${n === 1 ? "" : "s"}`,
          3000,
        );
        setLogNodes(prev => prev.filter(card => threadOf(card) !== targetId));
      })
      .catch(err => {
        console.error(err);
        const msg = (err.response && err.response.data && err.response.data.error)
          || "Error deleting thread.";
        addToast(msg, 4000);
      })
      .finally(() => {
        setDeleteTarget(null);
      });
  };

  if (loading) return <div style={{ padding: "20px", color: "var(--text-muted)" }}>Loading log...</div>;
  if (error) return <div style={{ padding: "20px", color: "var(--accent)" }}>{error}</div>;

  return (
    <div style={{ padding: "3rem 2rem 4rem", maxWidth: "720px", margin: "0 auto" }}>
      <div style={{ marginBottom: "2.5rem" }}>
        <div style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "0.8rem",
        }}>
          <h2 style={{
            color: "var(--text-primary)",
            fontFamily: "var(--serif)",
            fontWeight: 300,
            fontSize: "2rem",
            margin: 0,
          }}>
            Log
          </h2>
          {onSearchClick && (
            <button
              type="button"
              onClick={onSearchClick}
              aria-label="Search your entries"
              title="Search your entries (⌘K)"
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: "var(--text-muted)",
                // Larger tap target for mobile without shifting the layout.
                padding: "8px",
                margin: "-8px",
                display: "flex",
                alignItems: "center",
                transition: "color 0.15s ease",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "var(--accent)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "var(--text-muted)"; }}
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"
                strokeLinejoin="round" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <line x1="20" y1="20" x2="16.05" y2="16.05" />
              </svg>
            </button>
          )}
        </div>
        <div style={{
          width: "40px",
          height: "1px",
          backgroundColor: "var(--accent)",
          opacity: 0.5,
        }} />
      </div>
      {logNodes.length === 0 && !loading ? (
        <p style={{
          color: "var(--text-muted)",
          fontFamily: "var(--sans)",
          fontSize: "0.95rem",
          lineHeight: 1.6,
        }}>
          Your private entries will appear here as you share thoughts with Loore.
        </p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "1rem"}}>
          {logNodes.map(node => (
            <Bubble
              key={node.id}
              node={node}
              onClick={handleBubbleClick}
              actions={[
                {
                  label: 'Rename thread',
                  action: () => setRenameTarget(node),
                  color: 'var(--text-primary)',
                },
                {
                  label: 'Delete thread',
                  action: () => handleDeleteThread(node),
                  color: 'var(--accent)',
                },
              ]}
            />
          ))}
        </div>
      )}
      {loadingMore && <div style={{ padding: "20px", textAlign: "center", color: "var(--text-muted)" }}>Loading more...</div>}
      {loadMoreError && !loadingMore && (
        <div style={{ padding: "20px", textAlign: "center", color: "var(--text-muted)" }}>
          Couldn't load more entries.{" "}
          <span
            role="button"
            tabIndex={0}
            onClick={() => fetchMore(false)}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") fetchMore(false); }}
            style={{ color: "var(--accent)", cursor: "pointer" }}
          >
            Retry
          </span>
        </div>
      )}
      {hasMore && !loadingMore && !loadMoreError && (
        <div
          style={{ padding: "20px", textAlign: "center", cursor: "pointer", color: "var(--text-muted)" }}
          onClick={() => fetchMore(false)}
        >
          Load more...
        </div>
      )}
      <RenameThreadDialog
        open={renameTarget !== null}
        currentName={renameTarget ? renameTarget.thread_name : ""}
        fallbackTitle={renameTarget ? splitPreview(renameTarget.preview).title : ""}
        saving={renaming}
        onClose={handleCloseRename}
        onSave={handleSaveThreadName}
      />
      <DeleteConfirmDialog
        open={deleteTarget !== null}
        mode="thread"
        hasChildren={!!(deleteTarget && deleteTarget.child_count)}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleConfirmDeleteThread}
      />
    </div>
  );
}

export default Log;
