import React, { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api";
import Bubble from "./Bubble";
import DeleteConfirmDialog from "./DeleteConfirmDialog";
import { useToast } from "../contexts/ToastContext";

function Feed({ onSearchClick }) {
  const [feedNodes, setFeedNodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [page, setPage] = useState(1);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const { addToast } = useToast();
  const navigate = useNavigate();

  const fetchPage = useCallback((pageNum) => {
    const isFirst = pageNum === 1;
    if (isFirst) setLoading(true);
    else setLoadingMore(true);

    api.get(`/feed?page=${pageNum}&per_page=20`)
      .then(response => {
        const { nodes, has_more } = response.data;
        setFeedNodes(prev => isFirst ? nodes : [...prev, ...nodes]);
        setHasMore(has_more);
        setPage(pageNum);
      })
      .catch(err => {
        console.error(err);
        setError("Error loading feed.");
      })
      .finally(() => {
        setLoading(false);
        setLoadingMore(false);
      });
  }, []);

  useEffect(() => {
    fetchPage(1);
  }, [fetchPage]);

  // Auto-load on scroll near bottom
  useEffect(() => {
    if (!hasMore || loading || loadingMore) return;

    const handleScroll = () => {
      const scrollBottom = window.innerHeight + window.scrollY;
      const docHeight = document.documentElement.scrollHeight;
      if (docHeight - scrollBottom < 300) {
        fetchPage(page + 1);
      }
    };

    window.addEventListener("scroll", handleScroll, { passive: true });
    // Check immediately in case content doesn't fill the viewport
    handleScroll();

    return () => window.removeEventListener("scroll", handleScroll);
  }, [hasMore, loading, loadingMore, page, fetchPage]);

  const handleBubbleClick = (nodeId, e) => {
    const card = feedNodes.find(n => n.id === nodeId);
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

  const handleConfirmDeleteThread = ({ withDescendants }) => {
    if (!deleteTarget) return;
    // The backend's `thread_root_id` is the actual thread root (matters
    // when the displayed card is the first child of a system-prompt root).
    const targetId = deleteTarget.thread_root_id || deleteTarget.id;
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
        setFeedNodes(prev => prev.filter(card => card.id !== deleteTarget.id));
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

  if (loading) return <div style={{ padding: "20px", color: "var(--text-muted)" }}>Loading feed...</div>;
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
      {feedNodes.length === 0 && !loading ? (
        <p style={{
          color: "var(--text-muted)",
          fontFamily: "var(--sans)",
          fontSize: "0.95rem",
          lineHeight: 1.6,
        }}>
          Your entries will appear here as you share thoughts with Loore.
        </p>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "1rem"}}>
          {feedNodes.map(node => (
            <Bubble
              key={node.id}
              node={node}
              onClick={handleBubbleClick}
              actions={[{
                label: 'Delete thread',
                action: () => handleDeleteThread(node),
                color: 'var(--accent)',
              }]}
            />
          ))}
        </div>
      )}
      {loadingMore && <div style={{ padding: "20px", textAlign: "center", color: "var(--text-muted)" }}>Loading more...</div>}
      {hasMore && !loadingMore && (
        <div
          style={{ padding: "20px", textAlign: "center", cursor: "pointer", color: "var(--text-muted)" }}
          onClick={() => fetchPage(page + 1)}
        >
          Load more...
        </div>
      )}
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

export default Feed;
