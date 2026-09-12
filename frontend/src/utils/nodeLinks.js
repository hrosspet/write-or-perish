import api from '../api';

/**
 * In-text links to other Loore nodes (`https://loore.org/node/123`) render
 * as the target's title instead of the raw URL. This module is the two
 * halves behind that: recognising such an href, and fetching titles in
 * one batched request per render pass.
 */

const NODE_HOSTS = new Set(['loore.org', 'www.loore.org', 'staging.loore.org']);

/**
 * The node id an href points at, or null when it is not a node link.
 * Accepts absolute URLs on a Loore host (production, staging, or the
 * origin the app is currently served from — so local dev links resolve
 * too) and app-relative `/node/123` paths.
 */
export function parseNodeLink(href) {
  if (!href || typeof href !== 'string') return null;
  let path;
  if (href.startsWith('/')) {
    path = href;
  } else {
    let url;
    try {
      url = new URL(href);
    } catch (e) {
      return null;
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
    const sameOrigin = typeof window !== 'undefined' && window.location
      && url.origin === window.location.origin;
    if (!NODE_HOSTS.has(url.hostname) && !sameOrigin) return null;
    path = url.pathname;
  }
  const m = /^\/node\/(\d+)\/?$/.exec(path);
  return m ? Number(m[1]) : null;
}

// id -> { title } | null (not visible). Module-level so a title fetched
// once serves every body on the page and survives re-renders.
const cache = new Map();
// id -> Promise resolving to the cache value, for requests in flight.
const pending = new Map();
// ids collected during the current tick, flushed as one request.
let queue = null;
let queueResolvers = null;

function flush() {
  const ids = Array.from(queue.keys());
  const resolvers = queueResolvers;
  queue = null;
  queueResolvers = null;
  api.get('/nodes/titles', { params: { ids: ids.join(',') } })
    .then((res) => {
      const titles = (res.data && res.data.titles) || {};
      ids.forEach((id) => {
        const value = Object.prototype.hasOwnProperty.call(titles, String(id))
          ? titles[String(id)] : null;
        cache.set(id, value);
        pending.delete(id);
        resolvers.get(id).forEach((resolve) => resolve(value));
      });
    })
    .catch(() => {
      // Leave the raw URL in place; a failed lookup is not cached so a
      // later render (e.g. after login) tries again.
      ids.forEach((id) => {
        pending.delete(id);
        resolvers.get(id).forEach((resolve) => resolve(undefined));
      });
    });
}

/**
 * Title record for a node id: `{ title }`, `null` when the viewer cannot
 * see the node, `undefined` when the lookup failed. Calls made in the
 * same tick share one request.
 */
export function fetchNodeTitle(id) {
  if (cache.has(id)) return Promise.resolve(cache.get(id));
  if (pending.has(id)) return pending.get(id);
  if (!queue) {
    queue = new Map();
    queueResolvers = new Map();
    Promise.resolve().then(flush);
  }
  const promise = new Promise((resolve) => {
    if (!queueResolvers.has(id)) queueResolvers.set(id, []);
    queueResolvers.get(id).push(resolve);
  });
  queue.set(id, true);
  pending.set(id, promise);
  return promise;
}

/** Synchronous cache read so a known title renders without a flash of URL. */
export function cachedNodeTitle(id) {
  return cache.has(id) ? cache.get(id) : undefined;
}

/** Test hook. */
export function _resetNodeTitleCache() {
  cache.clear();
  pending.clear();
  queue = null;
  queueResolvers = null;
}
