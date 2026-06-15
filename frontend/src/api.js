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

// Surface the per-user monthly spend cap (HTTP 402) as a global banner,
// regardless of which cost action hit it. The call site's own error handling
// still runs (we re-reject), so spinners stop as usual. SpendCapBanner listens.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const data = error.response && error.response.data;
    if (error.response && error.response.status === 402 &&
        data && data.error === "monthly_spend_limit_reached") {
      try {
        window.dispatchEvent(new CustomEvent("loore:spend-capped", {
          detail: { message: data.message },
        }));
      } catch (e) {
        // Ignore — CustomEvent unavailable; the rejection below still flows.
      }
    }
    return Promise.reject(error);
  }
);

export default api;