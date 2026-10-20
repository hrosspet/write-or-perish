/**
 * Which markdown images load on their own (#441).
 *
 * Markdown in Loore does not always come from the viewer: LLM replies can be
 * steered by outside content the model read (Read's tweets, bookmarked pages,
 * imports), and public nodes come from other users. An image that loads as
 * the text renders tells its host that the node was opened, from which IP,
 * and its URL can carry text from the reply. So only Loore's own media loads
 * by itself. Any other image renders as a placeholder that names its host,
 * and the image is fetched only when the user clicks it.
 *
 * The Content-Security-Policy in public/index.html (`img-src 'self' data:`)
 * blocks every other image host as a second layer. A page cannot relax its
 * own policy for one image, so a clicked placeholder opens the image in a
 * new tab instead of in place.
 */

// Loore's own media route (backend/routes/media.py, proxied by nginx). Only
// plain path characters: no percent-escapes, which could hide an encoded
// "../" that the server decodes into another route.
const OWN_MEDIA_PATH = /^\/media\/[A-Za-z0-9_\-./]+$/;

function currentHref() {
  return typeof window !== 'undefined' && window.location ? window.location.href : undefined;
}

/** The origins Loore's own media is served from: this page's, and the configured backend's. */
function ownOrigins() {
  const origins = new Set();
  if (typeof window !== 'undefined' && window.location) origins.add(window.location.origin);
  const backend = process.env.REACT_APP_BACKEND_URL;
  if (backend) {
    try {
      origins.add(new URL(backend).origin);
    } catch (e) {
      // Not an absolute URL (the dev build leaves it empty): same origin only.
    }
  }
  return origins;
}

/**
 * How a markdown image renders:
 *   { kind: 'own', url }          Loore's own media: loads as the text renders.
 *   { kind: 'remote', url, host } any other web image: a placeholder naming
 *                                 the host; opens only when clicked.
 *   { kind: 'none' }              no usable http(s) URL: the alt text only.
 */
export function classifyImageSource(src) {
  if (!src || typeof src !== 'string') return { kind: 'none' };
  let url;
  try {
    url = new URL(src, currentHref());
  } catch (e) {
    return { kind: 'none' };
  }
  if ((url.protocol !== 'https:' && url.protocol !== 'http:') || !url.host) return { kind: 'none' };
  // URL() has already resolved "." and ".." segments, also percent-encoded ones.
  if (ownOrigins().has(url.origin) && OWN_MEDIA_PATH.test(url.pathname)) {
    return { kind: 'own', url: url.href };
  }
  return { kind: 'remote', url: url.href, host: url.host };
}
