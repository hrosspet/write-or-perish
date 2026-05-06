import React, { useState, useRef, useEffect } from 'react';
import { FaEllipsisV } from 'react-icons/fa';

function BubbleKebabMenu({ visible, items, onFocus, onBlur }) {
  const [showMenu, setShowMenu] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!showMenu) return undefined;
    const onMouseDown = (e) => {
      if (ref.current && !ref.current.contains(e.target)) {
        setShowMenu(false);
      }
    };
    const onKeyDown = (e) => {
      if (e.key === 'Escape') setShowMenu(false);
    };
    document.addEventListener('mousedown', onMouseDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onMouseDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [showMenu]);

  if (!items || items.length === 0) return null;

  const effectiveVisible = visible || showMenu;

  return (
    <div
      ref={ref}
      onClick={(e) => e.stopPropagation()}
      style={{
        position: 'absolute',
        top: '50%',
        left: '100%',
        marginLeft: '8px',
        transform: 'translateY(-50%)',
        opacity: effectiveVisible ? 1 : 0,
        transition: 'opacity 0.15s ease',
        pointerEvents: effectiveVisible ? 'auto' : 'none',
      }}
    >
      <button
        type="button"
        onClick={() => setShowMenu((v) => !v)}
        onFocus={onFocus}
        onBlur={onBlur}
        title="More actions"
        aria-label="More actions"
        style={{
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          color: 'var(--text-muted)',
          padding: '4px 6px',
          display: 'inline-flex',
          alignItems: 'center',
        }}
      >
        <FaEllipsisV size={14} />
      </button>
      {showMenu && (
        <div style={{
          position: 'absolute',
          top: '50%',
          left: '100%',
          marginLeft: '4px',
          transform: 'translateY(-50%)',
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          borderRadius: '6px',
          boxShadow: '0 4px 12px rgba(0,0,0,0.25)',
          minWidth: '160px',
          zIndex: 5,
          overflow: 'hidden',
        }}>
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              onClick={() => {
                setShowMenu(false);
                item.action();
              }}
              style={{
                display: 'block', width: '100%', textAlign: 'left',
                background: 'none', border: 'none', cursor: 'pointer',
                padding: '8px 12px',
                fontFamily: 'var(--sans)', fontSize: '0.85rem', fontWeight: 300,
                color: item.color || 'var(--text-primary)',
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default BubbleKebabMenu;
