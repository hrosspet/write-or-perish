import axios from "axios";

// Base URL is set by the proxy in package.json (or you can set REACT_APP_API_URL in .env)
const api = axios.create({
  baseURL: process.env.REACT_APP_API_URL || "/api",
  withCredentials: true, // important if your Flask app uses sessions/cookies
  timeout: 60000, // 60 seconds timeout (long operations now handled by async Celery tasks)
});

// Attach the browser's IANA timezone on every request as a lightweight signal
// for the backend (used for LLM temporal grounding, #130). The authoritative
// persistence happens via PATCH /dashboard/timezone in UserContext; this header
// is a redundant, always-current hint. Detection is wrapped defensively so a
// missing Intl API never breaks requests.
api.interceptors.request.use((config) => {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz) {
      config.headers = config.headers || {};
      config.headers["X-Timezone"] = tz;
    }
  } catch (e) {
    // Ignore — timezone is an optional hint.
  }
  return config;
});

export default api;