import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';

function SearchModal({ onClose }) {
  const [query, setQuery] = useState('');
  const [showDateFilter, setShowDateFilter] = useState(false);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [results, setResults] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [page, setPage] = useState(1);
  const inputRef = useRef(null);
  const debounceRef = useRef(null);
  const loadMoreRef = useRef(null);
  const navigate = useNavigate();

  // Auto-focus input on mount
  useEffect(() => {
    if (inputRef.current) inputRef.current.focus();
  }, []);

  const doSearch = useCallback(async (q, from, to) => {
    const trimmed = q.trim();
    if (!trimmed && !from && !to) {
      setResults([]);
      setTotal(0);
      setHasSearched(false);
      setPage(1);
      return;
    }

    setLoading(true);
    setHasSearched(true);
    setPage(1);
    try {
      const params = {};
      if (trimmed) params.q = trimmed;
      if (from) params.from = from;
      if (to) params.to = to;
      params.per_page = 20;
      params.page = 1;

      const res = await api.get('/search', { params });
      setResults(res.data.results);
      setTotal(res.data.total);
    } catch (err) {
      if (err.response?.status !== 400) {
        console.error('Search error:', err);
      }
      setResults([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMore = useCallback(async () => {
    const nextPage = page + 1;
    setLoadingMore(true);
    try {
      const params = { per_page: 20, page: nextPage };
      const trimmed = query.trim();
      if (trimmed) params.q = trimmed;
      if (dateFrom) params.from = dateFrom;
      if (dateTo) params.to = dateTo;

      const res = await api.get('/search', { params });
      setResults((prev) => [...prev, ...res.data.results]);
      setTotal(res.data.total);
      setPage(nextPage);
    } catch (err) {
      console.error('Load more error:', err);
    } finally {
      setLoadingMore(false);
    }
  }, [page, query, dateFrom, dateTo]);

  // Auto-load more when the button scrolls into view
  useEffect(() => {
    const el = loadMoreRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && !loadingMore) {
          loadMore();
        }
      },
      { threshold: 0.5 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [loadMore, loadingMore]);

  // Debounced search on query change
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      doSearch(query, dateFrom, dateTo);
    }, 300);
    return () => clearTimeout(debounceRef.current);
  }, [query, dateFrom, dateTo, doSearch]);

  // Escape to close
  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const handleResultClick = (id, e) => {
    if (e && (e.metaKey || e.ctrlKey)) {
      window.open(`/node/${id}`, '_blank');
    } else {
      onClose();
      navigate(`/node/${id}`);
    }
  };

  const formatDate = (iso) => {
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        top: 0, left: 0, right: 0, bottom: 0,
        backgroundColor: 'rgba(5, 4, 3, 0.75)',
        backdropFilter: 'blur(10px)',
        WebkitBackdropFilter: 'blur(10px)',
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        paddingTop: '12vh',
        zIndex: 1100,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          borderRadius: '12px',
          width: '640px',
          maxWidth: '90vw',
          maxHeight: '70vh',
          display: 'flex',
          flexDirection: 'column',
          boxShadow: '0 24px 80px rgba(0,0,0,0.5)',
          overflow: 'hidden',
        }}
      >
        {/* Search input row */}
        <div style={{
          display: 'flex',
          alignItems: 'center',
          padding: '16px 20px',
          borderBottom: '1px solid var(--border)',
          gap: '12px',
        }}>
          <span style={{ color: 'var(--text-muted)', fontSize: '18px', flexShrink: 0 }}>
            &#x2315;
          </span>
          <input
            ref={inputRef}
            type="text"
            placeholder="Search your entries..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{
              flex: 1,
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--text-primary)',
              fontSize: '16px',
              fontFamily: 'var(--sans)',
            }}
          />
          <button
            onClick={() => setShowDateFilter(!showDateFilter)}
            style={{
              background: showDateFilter ? 'var(--accent-subtle)' : 'transparent',
              border: '1px solid ' + (showDateFilter ? 'var(--accent-dim)' : 'var(--border)'),
              borderRadius: '6px',
              padding: '4px 10px',
              fontSize: '12px',
              color: showDateFilter ? 'var(--accent)' : 'var(--text-muted)',
              cursor: 'pointer',
              flexShrink: 0,
              fontFamily: 'var(--sans)',
            }}
          >
            Dates
          </button>
          <span style={{
            fontSize: '11px',
            color: 'var(--text-muted)',
            padding: '2px 6px',
            border: '1px solid var(--border)',
            borderRadius: '4px',
            flexShrink: 0,
          }}>
            ESC
          </span>
        </div>

        {/* Date filter row */}
        {showDateFilter && (
          <div style={{
            display: 'flex',
            alignItems: 'center',
            padding: '10px 20px',
            borderBottom: '1px solid var(--border)',
            gap: '10px',
            fontSize: '13px',
          }}>
            <span style={{ color: 'var(--text-muted)' }}>From</span>
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
              style={{
                background: 'var(--bg-input)',
                border: '1px solid var(--border)',
                borderRadius: '4px',
                padding: '4px 8px',
                color: 'var(--text-primary)',
                fontSize: '13px',
                fontFamily: 'var(--sans)',
              }}
            />
            <span style={{ color: 'var(--text-muted)' }}>to</span>
            <input
              type="date"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
              style={{
                background: 'var(--bg-input)',
                border: '1px solid var(--border)',
                borderRadius: '4px',
                padding: '4px 8px',
                color: 'var(--text-primary)',
                fontSize: '13px',
                fontFamily: 'var(--sans)',
              }}
            />
            {(dateFrom || dateTo) && (
              <button
                onClick={() => { setDateFrom(''); setDateTo(''); }}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--text-muted)',
                  cursor: 'pointer',
                  fontSize: '13px',
                  padding: '2px 6px',
                }}
              >
                Clear
              </button>
            )}
          </div>
        )}

        {/* Results area */}
        <div style={{
          flex: 1,
          overflowY: 'auto',
          padding: '8px 0',
        }}>
          {loading && (
            <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '14px' }}>
              Searching...
            </div>
          )}

          {!loading && hasSearched && results.length === 0 && (
            <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '14px' }}>
              No results found
            </div>
          )}

          {!loading && !hasSearched && (
            <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '14px' }}>
              Type to search your entries
            </div>
          )}

          {!loading && results.map((r) => (
            <div
              key={r.id}
              onClick={(e) => handleResultClick(r.id, e)}
              style={{
                padding: '12px 20px',
                cursor: 'pointer',
                borderBottom: '1px solid var(--border)',
                transition: 'background-color 0.15s',
              }}
              onMouseEnter={(e) => e.currentTarget.style.backgroundColor = 'var(--bg-card-hover)'}
              onMouseLeave={(e) => e.currentTarget.style.backgroundColor = 'transparent'}
            >
              <div style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                marginBottom: '4px',
              }}>
                <span style={{
                  fontSize: '11px',
                  color: 'var(--accent-dim)',
                  background: 'var(--accent-subtle)',
                  padding: '1px 6px',
                  borderRadius: '3px',
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                }}>
                  {r.node_type}
                </span>
                <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                  {formatDate(r.created_at)}
                </span>
                {r.child_count > 0 && (
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                    {r.child_count} {r.child_count === 1 ? 'reply' : 'replies'}
                  </span>
                )}
              </div>
              <div
                style={{
                  fontSize: '14px',
                  color: 'var(--text-secondary)',
                  lineHeight: '1.5',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  display: '-webkit-box',
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: 'vertical',
                }}
                dangerouslySetInnerHTML={{ __html: r.snippet || r.preview }}
              />
            </div>
          ))}

          {!loading && hasSearched && total > results.length && (
            <div ref={loadMoreRef} style={{ padding: '12px 20px', textAlign: 'center' }}>
              <button
                onClick={loadMore}
                disabled={loadingMore}
                style={{
                  background: 'transparent',
                  border: '1px solid var(--border)',
                  borderRadius: '6px',
                  padding: '6px 16px',
                  fontSize: '13px',
                  color: 'var(--text-secondary)',
                  cursor: loadingMore ? 'default' : 'pointer',
                  fontFamily: 'var(--sans)',
                }}
              >
                {loadingMore ? 'Loading...' : `Show more (${results.length} of ${total})`}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default SearchModal;
