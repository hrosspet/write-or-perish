import React, { useState, useRef, useEffect, useLayoutEffect } from 'react';
import { FaEllipsisV } from 'react-icons/fa';

// `inline`: the ⋮ sits in the flow of a row (the artifact meta line,
// #603) instead of floating outside the right edge of a bubble, with a
// 32 px tap target, and the menu opens below it, right-aligned. `style`
// adds to the wrapper's style (e.g. marginLeft: 'auto' to push it to the
// end of the row).
function BubbleKebabMenu({ visible, items, onFocus, onBlur, inline = false, style }) {
  const [showMenu, setShowMenu] = useState(false);
  // When the dropdown would clip the viewport's right edge (narrow
  // screens), flip it to open leftward of the kebab instead. Reset on
  // each open so a viewport resize while the menu is closed is picked
  // up next time.
  const [flipLeft, setFlipLeft] = useState(false);
  const ref = useRef(null);
  const menuRef = useRef(null);

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

  useLayoutEffect(() => {
    // The inline menu opens right-aligned under the ⋮, so it never
    // reaches past the ⋮'s own right edge.
    if (!showMenu || !menuRef.current || inline) {
      setFlipLeft(false);
      return;
    }
    const rect = menuRef.current.getBoundingClientRect();
    setFlipLeft(rect.right > window.innerWidth - 8);
  }, [showMenu, inline]);

  if (!items || items.length === 0) return null;

  const effectiveVisible = visible || showMenu;

  const wrapperStyle = inline
    ? { position: 'relative', flexShrink: 0 }
    : {
        position: 'absolute',
        top: '50%',
        left: '100%',
        marginLeft: '8px',
        transform: 'translateY(-50%)',
      };

  const buttonStyle = inline
    ? {
        width: '32px',
        height: '32px',
        // A 32 px target whose icon lines up with the row's right edge
        // and text, without making the row taller.
        margin: '-8px -9px -8px 0',
        justifyContent: 'center',
        padding: 0,
        borderRadius: '6px',
      }
    : { padding: '4px 6px' };

  const menuPlacement = inline
    ? {
        top: 'calc(100% + 6px)',
        right: '-9px',
      }
    : {
        top: '50%',
        left: flipLeft ? 'auto' : '100%',
        right: flipLeft ? '100%' : 'auto',
        marginLeft: flipLeft ? 0 : '4px',
        marginRight: flipLeft ? '4px' : 0,
        transform: 'translateY(-50%)',
      };

  return (
    <div
      ref={ref}
      onClick={(e) => e.stopPropagation()}
      style={{
        ...wrapperStyle,
        opacity: effectiveVisible ? 1 : 0,
        transition: 'opacity 0.15s ease',
        pointerEvents: effectiveVisible ? 'auto' : 'none',
        ...style,
      }}
    >
      <button
        type="button"
        onClick={() => setShowMenu((v) => !v)}
        onFocus={onFocus}
        onBlur={onBlur}
        title="More actions"
        aria-label="More actions"
        aria-haspopup="menu"
        aria-expanded={showMenu}
        style={{
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          color: 'var(--text-muted)',
          display: 'inline-flex',
          alignItems: 'center',
          ...buttonStyle,
        }}
      >
        <FaEllipsisV size={14} />
      </button>
      {showMenu && (
        <div ref={menuRef} style={{
          position: 'absolute',
          ...menuPlacement,
          background: 'var(--bg-card)',
          border: '1px solid var(--border)',
          borderRadius: '6px',
          boxShadow: '0 4px 12px rgba(0,0,0,0.25)',
          minWidth: '160px',
          zIndex: 1000,
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
