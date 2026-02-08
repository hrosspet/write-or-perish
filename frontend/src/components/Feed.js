import React, { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import api from "../api";
import Bubble from "./Bubble";

function Feed() {
  const [feedNodes, setFeedNodes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [page, setPage] = useState(1);
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

  const handleBubbleClick = (nodeId) => {
    navigate(`/node/${nodeId}`);
  };

  if (loading) return <div style={{ padding: "20px", color: "var(--text-muted)" }}>Loading feed...</div>;
  if (error) return <div style={{ padding: "20px", color: "var(--accent)" }}>{error}</div>;

  return (
    <div style={{ padding: "20px", maxWidth: "680px", margin: "0 auto" }}>
      <h2 style={{
        color: "var(--text-primary)",
        fontFamily: "var(--serif)",
        fontWeight: 300,
        fontSize: "2rem",
        marginBottom: "8px",
      }}>
        Feed
      </h2>
      <div style={{
        width: "40px",
        height: "1px",
        backgroundColor: "var(--accent)",
        marginBottom: "32px",
      }} />
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem"}}>
        {feedNodes.map(node => (
          <Bubble key={node.id} node={node} onClick={handleBubbleClick} />
        ))}
      </div>
      {loadingMore && <div style={{ padding: "20px", textAlign: "center", color: "var(--text-muted)" }}>Loading more...</div>}
      {hasMore && !loadingMore && (
        <div
          style={{ padding: "20px", textAlign: "center", cursor: "pointer", color: "var(--text-muted)" }}
          onClick={() => fetchPage(page + 1)}
        >
          Load more...
        </div>
      )}
    </div>
  );
}

export default Feed;
