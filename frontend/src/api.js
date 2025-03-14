import axios from "axios";

// Base URL is set by the proxy in package.json (or you can set REACT_APP_API_URL in .env)
const api = axios.create({
  baseURL: process.env.REACT_APP_API_URL || "/api",
  withCredentials: true, // important if your Flask app uses sessions/cookies
});

export default api;