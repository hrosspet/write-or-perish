import React, { useEffect, useRef, useState } from 'react';
import { useTheme } from '../contexts/ThemeContext';

/**
 * The real tweet, rendered by X's widget, for a saved tweet reference
 * (#232). The stored text stays the searchable copy and what the AI
 * reads; this is for the reader, who wants the images and the video.
 *
 * This is Loore's one third-party call from the browser: viewing a
 * tweet reference loads X's script and an iframe from X, so X learns
 * the viewer's IP and which tweet was opened. `dnt` asks X not to use
 * it for ad targeting. Nothing is loaded anywhere else in Loore, and
 * the References log shows only the stored text.
 *
 * States reported through onStatus: 'loading' → 'shown' | 'unavailable'
 * (deleted tweet, blocked script, offline), so the page can fall back
 * to the stored text.
 */

const WIDGETS_SRC = 'https://platform.twitter.com/widgets.js';
let widgetsPromise = null;

function loadWidgets() {
  if (window.twttr && window.twttr.widgets) return Promise.resolve(window.twttr);
  if (widgetsPromise) return widgetsPromise;
  widgetsPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = WIDGETS_SRC;
    script.async = true;
    script.onload = () => {
      if (window.twttr && window.twttr.ready) window.twttr.ready(resolve);
      else reject(new Error('widgets.js loaded without twttr'));
    };
    script.onerror = () => {
      widgetsPromise = null;
      reject(new Error('widgets.js failed to load'));
    };
    document.head.appendChild(script);
  });
  return widgetsPromise;
}

function TweetEmbed({ tweetId, onStatus }) {
  const ref = useRef(null);
  const { theme } = useTheme();
  const [status, setStatus] = useState('loading');

  useEffect(() => {
    let cancelled = false;
    const el = ref.current;
    if (!el || !tweetId) return undefined;
    el.innerHTML = '';
    setStatus('loading');
    loadWidgets()
      .then((twttr) => twttr.widgets.createTweet(String(tweetId), el, {
        theme, dnt: true, align: 'center', conversation: 'none',
      }))
      .then((node) => {
        // The widget appends its iframe before resolving. A run that was
        // cancelled meanwhile (StrictMode's double mount in dev, a theme
        // switch) must take its iframe back out, or the card shows two.
        if (cancelled) { if (node) node.remove(); return; }
        setStatus(node ? 'shown' : 'unavailable');
      })
      .catch(() => { if (!cancelled) setStatus('unavailable'); });
    return () => { cancelled = true; };
  }, [tweetId, theme]);

  useEffect(() => { if (onStatus) onStatus(status); }, [status, onStatus]);

  return (
    <div
      ref={ref}
      style={{
        minHeight: status === 'loading' ? '120px' : 0,
        // Hide the container while unavailable: the page shows the
        // stored text instead, and an empty box would be a scar.
        display: status === 'unavailable' ? 'none' : 'block',
      }}
    />
  );
}

export default TweetEmbed;
