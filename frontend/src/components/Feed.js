import React, { useState, useEffect, useCallback, useRef } from "react";
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
  const observerRef = useRef();
  const sentinelRef = useRef();

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

  // Infinite scroll via IntersectionObserver
  useEffect(() => {
    if (loading || loadingMore || !hasMore) return;

    if (observerRef.current) observerRef.current.disconnect();

    observerRef.current = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) {
        fetchPage(page + 1);
      }
    });

    if (sentinelRef.current) {
      observerRef.current.observe(sentinelRef.current);
    }

    return () => {
      if (observerRef.current) observerRef.current.disconnect();
    };
  }, [loading, loadingMore, hasMore, page, fetchPage]);

  const handleBubbleClick = (nodeId) => {
    navigate(`/node/${nodeId}`);
  };

  if (loading) return <div style={{ padding: "20px" }}>Loading feed...</div>;
  if (error) return <div style={{ padding: "20px", color: "red" }}>{error}</div>;

  return (
    <div style={{ padding: "20px", maxWidth: "1000px", margin: "0 auto" }}>
      <h2 style={{ color: "#e0e0e0" }}>Feed</h2>
      <div style={{ display: "flex", flexDirection: "column"}}>
        {feedNodes.map(node => (
          <Bubble key={node.id} isHighlighted={true} node={node} onClick={handleBubbleClick} />
        ))}
      </div>
      {loadingMore && <div style={{ padding: "20px", textAlign: "center", color: "#888" }}>Loading more...</div>}
      {hasMore && <div ref={sentinelRef} style={{ height: "1px" }} />}
    </div>
  );
}

export default Feed;
