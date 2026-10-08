// The user came back to the page (#374). A phone suspends a page whose
// screen is off or whose app is in the background, and so does the
// back/forward cache: a poll stops, an EventSource can die without an
// error event, a request sent before the suspension can be lost. What
// the page was waiting for is then re-checked against the server.

/**
 * Calls *handler(reason)* each time the user returns to the page:
 * - 'visible': the tab or app is shown again (screen on, app switched back)
 * - 'pageshow': the page is restored from the back/forward cache
 * - 'online': the browser has a network connection again
 *
 * A first load's pageshow (not restored from the cache) doesn't count:
 * everything on the page starts fresh then anyway. One return can fire
 * more than one of these, so the handler must be safe to repeat.
 *
 * @returns {Function} removes the listeners
 */
export function onPageReturn(handler) {
  const onVisibility = () => {
    if (document.visibilityState === 'visible') handler('visible');
  };
  const onPageShow = (event) => {
    if (event.persisted) handler('pageshow');
  };
  const onOnline = () => handler('online');
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('pageshow', onPageShow);
  window.addEventListener('online', onOnline);
  return () => {
    document.removeEventListener('visibilitychange', onVisibility);
    window.removeEventListener('pageshow', onPageShow);
    window.removeEventListener('online', onOnline);
  };
}
