import { useState, useEffect } from 'react';

/**
 * Returns true when the viewport is at or below `maxWidth` (default 640px).
 * 640px matches the breakpoint already used in LandingPage.js.
 *
 * Uses matchMedia so it updates on resize / orientation change. This is
 * the app's first shared mobile-detection hook (introduced for #28 — the
 * mobile floating audio player).
 */
export function useIsMobile(maxWidth = 640) {
  const query = `(max-width: ${maxWidth}px)`;
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia(query).matches
  );

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined;
    }
    const mql = window.matchMedia(query);
    const onChange = (e) => setIsMobile(e.matches);
    // Sync immediately in case the viewport changed between render and effect.
    setIsMobile(mql.matches);
    // addEventListener is the modern API; older Safari needs addListener.
    if (mql.addEventListener) {
      mql.addEventListener('change', onChange);
      return () => mql.removeEventListener('change', onChange);
    }
    mql.addListener(onChange);
    return () => mql.removeListener(onChange);
  }, [query]);

  return isMobile;
}
