import React, { useState, useEffect } from "react";
import { FaTimesCircle, FaFilter, FaCaretDown, FaCaretUp } from "react-icons/fa";
import api from "../api";

// Small header toggle that hides rows with $0 in its column.
function ZeroFilterToggle({ active, onToggle, label }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      title={
        active
          ? `Showing only rows with ${label} > $0 — click to show all`
          : `Hide rows with ${label} = $0`
      }
      style={{
        marginLeft: "4px",
        padding: 0,
        background: "none",
        border: "none",
        cursor: "pointer",
        verticalAlign: "middle",
        fontSize: "0.8em",
        color: active ? "var(--accent)" : "var(--text-muted)",
      }}
    >
      <FaFilter />
    </button>
  );
}

// Small header toggle that sorts by its column. `dir` is 'desc' | 'asc' when
// this is the active sort column, else null (shown as a muted down triangle).
function SortToggle({ dir, onToggle, label }) {
  const Icon = dir === "asc" ? FaCaretUp : FaCaretDown;
  return (
    <button
      type="button"
      onClick={onToggle}
      title={
        dir === "asc"
          ? `Sorted by ${label}, smallest first — click for largest first`
          : dir === "desc"
          ? `Sorted by ${label}, largest first — click for smallest first`
          : `Sort by ${label}, largest first`
      }
      style={{
        marginLeft: "4px",
        padding: 0,
        background: "none",
        border: "none",
        cursor: "pointer",
        verticalAlign: "middle",
        fontSize: "0.9em",
        color: dir ? "var(--accent)" : "var(--text-muted)",
      }}
    >
      <Icon />
    </button>
  );
}

// Polls — the admin side of the dev-update channel (#207). Ask the
// community a question; read only the answers users explicitly sent.
function AdminPolls() {
  const [polls, setPolls] = useState([]);
  const [question, setQuestion] = useState("");
  const [responses, setResponses] = useState({}); // poll_id -> array
  const [pollsError, setPollsError] = useState("");

  const fetchPolls = async () => {
    try {
      const res = await api.get("/admin/polls");
      setPolls(res.data.polls);
    } catch (err) {
      setPollsError("Failed to fetch polls.");
    }
  };

  useEffect(() => { fetchPolls(); }, []);

  const createPoll = async () => {
    if (!question.trim()) return;
    try {
      await api.post("/admin/polls", { question });
      setQuestion("");
      fetchPolls();
    } catch (err) {
      setPollsError(err.response?.data?.error || "Failed to create poll.");
    }
  };

  const toggleResponses = async (pollId) => {
    if (responses[pollId]) {
      setResponses((r) => {
        const next = { ...r };
        delete next[pollId];
        return next;
      });
      return;
    }
    try {
      const res = await api.get(`/admin/polls/${pollId}/responses`);
      setResponses((r) => ({ ...r, [pollId]: res.data.responses }));
    } catch (err) {
      setPollsError("Failed to fetch responses.");
    }
  };

  const closePoll = async (pollId) => {
    try {
      await api.post(`/admin/polls/${pollId}/close`);
      fetchPolls();
    } catch (err) {
      setPollsError("Failed to close poll.");
    }
  };

  return (
    <div style={{ marginBottom: "20px", padding: "10px", border: "1px solid var(--border)" }}>
      <h2>Polls</h2>
      <div style={{ display: "flex", gap: "10px", marginBottom: "12px" }}>
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask the community a question…"
          style={{ padding: "8px", flex: 1 }}
        />
        <button onClick={createPoll}>Create poll</button>
      </div>
      {pollsError && <div style={{ color: "var(--error)" }}>{pollsError}</div>}
      {polls.map((p) => (
        <div key={p.id} style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
          <div style={{ display: "flex", gap: "10px", alignItems: "baseline" }}>
            <span style={{ flex: 1 }}>
              {p.question}
              {p.closed_at && (
                <em style={{ color: "var(--text-muted)" }}> (closed)</em>
              )}
            </span>
            <span style={{ color: "var(--text-muted)", fontSize: "0.85rem", whiteSpace: "nowrap" }}>
              {p.sent_count} sent / {p.declined_count} declined
            </span>
            <button onClick={() => toggleResponses(p.id)}>
              {responses[p.id] ? "Hide" : "Responses"}
            </button>
            {!p.closed_at && (
              <button onClick={() => closePoll(p.id)}>Close</button>
            )}
          </div>
          {responses[p.id] && (
            responses[p.id].length === 0 ? (
              <div style={{ color: "var(--text-muted)", padding: "6px 0 0 12px" }}>
                No responses sent yet.
              </div>
            ) : (
              responses[p.id].map((r) => (
                <div key={r.id} style={{ padding: "6px 0 0 12px" }}>
                  <strong>{r.username}</strong>
                  {r.llm_drafted && (
                    <span style={{ color: "var(--text-muted)", fontSize: "0.8rem" }}> (AI-drafted)</span>
                  )}
                  <div style={{ whiteSpace: "pre-wrap", color: "var(--text-secondary)" }}>
                    {r.content}
                  </div>
                </div>
              ))
            )
          )}
        </div>
      ))}
    </div>
  );
}

function AdminPanel() {
  const [users, setUsers] = useState([]);
  const [allowedPlans, setAllowedPlans] = useState([]);
  const [error, setError] = useState("");
  const [newHandle, setNewHandle] = useState("");
  const [newHandleError, setNewHandleError] = useState("");
  // Per-user spend-limit input values, keyed by user id (controlled inputs).
  const [limitEdits, setLimitEdits] = useState({});
  // Column filters: hide rows with $0 in Spent / This Month (independent).
  const [hideZeroSpent, setHideZeroSpent] = useState(false);
  const [hideZeroMonth, setHideZeroMonth] = useState(false);
  // Column sort: one of Spent / This Month at a time, toggling desc <-> asc.
  const [sortColumn, setSortColumn] = useState(null); // 'spent' | 'month' | null
  const [sortDir, setSortDir] = useState("desc");

  const toggleSort = (column) => {
    if (sortColumn !== column) {
      setSortColumn(column);
      setSortDir("desc");
    } else {
      setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    }
  };

  const fetchUsers = async () => {
    try {
      const response = await api.get("/admin/users");
      const fetched = response.data.users;
      setUsers(fetched);
      // Pre-fill each row's limit input with the user's effective cap.
      const edits = {};
      fetched.forEach((u) => {
        edits[u.id] = String(u.spend_limit_usd ?? "");
      });
      setLimitEdits(edits);
      if (response.data.allowed_plans) {
        setAllowedPlans(response.data.allowed_plans);
      }
    } catch (err) {
      console.error(err);
      setError("Error fetching users.");
    }
  };

  useEffect(() => {
    fetchUsers();
  }, []);

  const toggleApproved = async (userId) => {
    try {
      await api.post(`/admin/users/${userId}/toggle`);
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error toggling user status.");
    }
  };

  const updateEmail = async (userId, currentEmail) => {
    const newEmail = prompt("Enter new email:", currentEmail || "");
    if (newEmail === null) return; // cancelled
    try {
      await api.put(`/admin/users/${userId}/update_email`, { email: newEmail });
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error updating email.");
    }
  };

  const updatePlan = async (userId, newPlan) => {
    try {
      await api.put(`/admin/users/${userId}/update_plan`, { plan: newPlan });
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error updating plan.");
    }
  };

  // Auto-save the limit input on Enter / Cmd|Ctrl+Enter / blur — but only when
  // the value actually changed from the user's last-saved limit. Invalid input
  // reverts to the saved value. Block/unblock feedback shows via the row's
  // red-cross icon after the refresh (no modal — keeps auto-save seamless).
  const maybeSaveLimit = (userId) => {
    const user = users.find((u) => u.id === userId);
    if (!user) return;
    const raw = limitEdits[userId];
    const original = user.spend_limit_usd ?? null;
    const parsed = parseFloat(raw);
    if (raw === undefined || raw === "" || isNaN(parsed) || parsed < 0) {
      // Revert the input to the last saved value.
      setLimitEdits((prev) => ({ ...prev, [userId]: String(original ?? "") }));
      return;
    }
    if (parsed === original) return; // unchanged — nothing to save
    saveSpendLimit(userId, parsed);
  };

  const saveSpendLimit = async (userId, value) => {
    try {
      await api.put(`/admin/users/${userId}/update_spend_limit`, {
        limit_usd: value,
      });
      setError("");
      fetchUsers(); // refreshes the row (incl. the blocked icon)
    } catch (err) {
      console.error(err);
      setError(err.response?.data?.error || "Error updating spend limit.");
    }
  };

  const activateAndWelcome = async (userId) => {
    try {
      const response = await api.post(`/admin/users/${userId}/activate_and_welcome`);
      const msg = response.data.email_sent
        ? "User approved and welcome email sent!"
        : "User approved but email failed to send.";
      alert(msg);
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError(err.response?.data?.error || "Error activating user.");
    }
  };

  const handleWhitelistUser = async () => {
    if (!newHandle.trim()) {
      setNewHandleError("Handle is required.");
      return;
    }
    try {
      await api.post("/admin/whitelist", { handle: newHandle });
      setNewHandle("");
      setNewHandleError("");
      fetchUsers();
    } catch (err) {
      console.error(err);
      setNewHandleError(err.response?.data?.error || "Error whitelisting user.");
    }
  };

  let displayedUsers = users
    .filter((u) => !hideZeroSpent || (u.total_spending_usd || 0) > 0)
    .filter((u) => !hideZeroMonth || (u.current_month_spending_usd || 0) > 0);
  if (sortColumn) {
    const key =
      sortColumn === "spent"
        ? "total_spending_usd"
        : "current_month_spending_usd";
    displayedUsers = [...displayedUsers].sort((a, b) => {
      const av = a[key] || 0;
      const bv = b[key] || 0;
      return sortDir === "desc" ? bv - av : av - bv;
    });
  }

  return (
    <div style={{ padding: "20px" }}>
      <h1>Admin Panel</h1>

      {/* Whitelist New User Section */}
      <div style={{ marginBottom: "20px", padding: "10px", border: "1px solid var(--border)" }}>
        <h2>Whitelist a New User</h2>
        <input
          type="text"
          value={newHandle}
          onChange={(e) => setNewHandle(e.target.value)}
          placeholder="Enter user handle"
          style={{ padding: "8px", marginRight: "10px" }}
        />
        <button onClick={handleWhitelistUser}>Whitelist User</button>
        {newHandleError && <div style={{ color: "var(--error)" }}>{newHandleError}</div>}
      </div>

      <AdminPolls />

      {error && <div style={{ color: "var(--error)" }}>{error}</div>}
      <table style={{ width: "100%", borderCollapse: "collapse", color: "var(--text-primary)" }}>
        <thead>
          <tr>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>ID</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Username</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Approved</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Plan</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Email</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px", width: "110px", whiteSpace: "nowrap" }}>
              Spent
              <ZeroFilterToggle
                active={hideZeroSpent}
                onToggle={() => setHideZeroSpent((v) => !v)}
                label="Spent"
              />
              <SortToggle
                dir={sortColumn === "spent" ? sortDir : null}
                onToggle={() => toggleSort("spent")}
                label="Spent"
              />
            </th>
            <th style={{ border: "1px solid var(--border)", padding: "8px", width: "140px", whiteSpace: "nowrap" }}>
              This Month
              <ZeroFilterToggle
                active={hideZeroMonth}
                onToggle={() => setHideZeroMonth((v) => !v)}
                label="This Month"
              />
              <SortToggle
                dir={sortColumn === "month" ? sortDir : null}
                onToggle={() => toggleSort("month")}
                label="This Month"
              />
            </th>
            <th style={{ border: "1px solid var(--border)", padding: "8px", width: "85px", whiteSpace: "nowrap" }}>Limit ($)</th>
            <th
              style={{ border: "1px solid var(--border)", padding: "8px", width: "85px", whiteSpace: "nowrap" }}
              title="Prompt-cache hit-rate over conversation turns (all-time): input tokens served from cache ÷ total prompt input. Covers both Anthropic and OpenAI caching."
            >
              Cache
            </th>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {displayedUsers.map((u) => (
            <tr key={u.id}>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>{u.id}</td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>{u.username}</td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                {u.approved ? "Active" : "Inactive"}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                <select
                  value={u.plan || "free"}
                  onChange={(e) => updatePlan(u.id, e.target.value)}
                >
                  {allowedPlans.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                {u.email || "None"}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px", width: "110px" }}>
                ${(u.total_spending_usd || 0).toFixed(2)}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px", width: "140px", whiteSpace: "nowrap" }}>
                ${(u.current_month_spending_usd || 0).toFixed(2)}
                {u.spend_blocked && (
                  <FaTimesCircle
                    title="Blocked this month"
                    aria-label="Blocked this month"
                    style={{ color: "var(--error)", marginLeft: "6px", verticalAlign: "middle" }}
                  />
                )}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px", width: "85px" }}>
                <input
                  type="number"
                  min="0"
                  step="1"
                  value={limitEdits[u.id] ?? ""}
                  onChange={(e) =>
                    setLimitEdits((prev) => ({ ...prev, [u.id]: e.target.value }))
                  }
                  onKeyDown={(e) => {
                    // Enter and Cmd/Ctrl+Enter both fire with key === "Enter".
                    if (e.key === "Enter") e.currentTarget.blur();
                  }}
                  onBlur={() => maybeSaveLimit(u.id)}
                  style={{ width: "48px", padding: "8px 4px", boxSizing: "border-box" }}
                  title={
                    u.spend_limit_is_override
                      ? "Custom per-user limit"
                      : "Inherited from the global default"
                  }
                />
              </td>
              <td
                style={{ border: "1px solid var(--border)", padding: "8px", width: "85px", whiteSpace: "nowrap" }}
                title={
                  u.cache_hit_rate == null
                    ? "No conversation prompt input yet"
                    : `${(u.cache_served_tokens || 0).toLocaleString()} of ${(u.cache_input_tokens || 0).toLocaleString()} prompt-input tokens served from cache`
                }
              >
                {u.cache_hit_rate == null
                  ? "—"
                  : `${(u.cache_hit_rate * 100).toFixed(0)}%`}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                {!u.approved && u.email ? (
                  <>
                    <button onClick={() => activateAndWelcome(u.id)}>
                      Activate &amp; Welcome
                    </button>{" "}
                    <button onClick={() => toggleApproved(u.id)}>
                      Activate
                    </button>
                  </>
                ) : (
                  <button onClick={() => toggleApproved(u.id)}>
                    {u.approved ? "Deactivate" : "Activate"}
                  </button>
                )}{" "}
                <button onClick={() => updateEmail(u.id, u.email)}>
                  Update Email
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default AdminPanel;