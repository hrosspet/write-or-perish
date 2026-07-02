/**
 * CRA dev-server proxy (replaces the old `"proxy"` field in package.json).
 *
 * The static field hardcoded http://localhost:5010, which is wrong inside
 * the Docker dev stack — there, `localhost` is the frontend container
 * itself and every /api call died with ECONNREFUSED. The target is now
 * env-configurable: docker-compose.override.yml sets
 * PROXY_TARGET=http://backend:5010 (the compose service name); running
 * `npm start` directly on the host keeps the old default.
 *
 * Only used by the dev server — production serves the built bundle via
 * nginx, which does its own routing.
 */
const { createProxyMiddleware } = require('http-proxy-middleware');

module.exports = function setupProxy(app) {
  const target = process.env.PROXY_TARGET || 'http://localhost:5010';
  app.use(
    ['/api', '/auth', '/media'],
    createProxyMiddleware({
      target,
      // Keep the browser's Host header: Flask answers /api/dashboard with a
      // 308 to the trailing-slash URL, and it builds that Location from the
      // Host — changeOrigin would point it at the in-network name
      // (backend:5010), which the browser can't resolve.
      changeOrigin: false,
    })
  );
};
