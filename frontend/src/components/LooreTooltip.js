import React, { cloneElement, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

// A tooltip in Loore's own type and colours, for a control whose native
// `title` would show in the browser's style (#435: Peter, on Glean). It
// shows after a short pause on hover, and at once on keyboard focus; it
// hides on leave, blur and Escape. The text is the control's accessible
// description (aria-describedby), so a screen reader reads it as a
// title would be read.
//
// The tooltip is drawn in a portal with fixed position, above the control
// (below it when there is no room above), kept inside the viewport, so
// no container's overflow clips it. The child must be a single element
// that takes a ref (a DOM element such as a button).
const SHOW_DELAY_MS = 350;
const GAP_PX = 8;
const EDGE_PX = 8;

const tipStyle = {
  position: 'fixed',
  zIndex: 1000,
  maxWidth: '260px',
  padding: '8px 11px',
  background: 'var(--bg-card)',
  border: '1px solid var(--border-hover)',
  borderRadius: '6px',
  boxShadow: '0 6px 18px rgba(0, 0, 0, 0.25)',
  color: 'var(--text-primary)',
  fontFamily: 'var(--sans)',
  fontSize: '0.78rem',
  fontWeight: 300,
  lineHeight: 1.45,
  letterSpacing: '0.01em',
  textAlign: 'left',
  whiteSpace: 'normal',
  pointerEvents: 'none',
};

export default function LooreTooltip({ text, children }) {
  const id = useId();
  const anchorRef = useRef(null);
  const tipRef = useRef(null);
  const timerRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);

  const clearTimer = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };
  const show = useCallback((delay) => {
    clearTimer();
    if (delay) {
      timerRef.current = setTimeout(() => setOpen(true), delay);
    } else {
      setOpen(true);
    }
  }, []);
  const hide = useCallback(() => {
    clearTimer();
    setOpen(false);
    setPos(null);
  }, []);

  useEffect(() => () => clearTimer(), []);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') hide(); };
    const onScroll = () => hide();
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onScroll, true);
    return () => {
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onScroll, true);
    };
  }, [open, hide]);

  // Placed once it has a size: above the control, centred on it, kept
  // inside the viewport; below the control when there is no room above.
  useLayoutEffect(() => {
    if (!open || !anchorRef.current || !tipRef.current) return;
    const a = anchorRef.current.getBoundingClientRect();
    const t = tipRef.current.getBoundingClientRect();
    const vw = document.documentElement.clientWidth || window.innerWidth;
    let left = a.left + a.width / 2 - t.width / 2;
    left = Math.max(EDGE_PX, Math.min(left, vw - t.width - EDGE_PX));
    let top = a.top - t.height - GAP_PX;
    if (top < EDGE_PX) top = a.bottom + GAP_PX;
    setPos({ left, top });
  }, [open, text]);

  if (!text) return children;
  const child = React.Children.only(children);
  const chain = (name, fn) => (e) => {
    fn(e);
    if (child.props[name]) child.props[name](e);
  };
  const anchor = cloneElement(child, {
    ref: anchorRef,
    'aria-describedby': open ? id : undefined,
    onMouseEnter: chain('onMouseEnter', () => show(SHOW_DELAY_MS)),
    onMouseLeave: chain('onMouseLeave', hide),
    onFocus: chain('onFocus', (e) => {
      // Keyboard focus only: a click focuses the button too, and the
      // tooltip would then stay up after the click.
      let keyboard = true;
      try { keyboard = e.target.matches(':focus-visible'); } catch (err) { /* old browsers */ }
      if (keyboard) show(0);
    }),
    onBlur: chain('onBlur', hide),
    onClick: chain('onClick', hide),
  });

  return (
    <>
      {anchor}
      {open && createPortal(
        <div
          ref={tipRef}
          id={id}
          role="tooltip"
          className="loore-tooltip"
          style={{ ...tipStyle, left: pos ? pos.left : -9999, top: pos ? pos.top : -9999 }}
        >
          {text}
        </div>,
        document.body,
      )}
    </>
  );
}
