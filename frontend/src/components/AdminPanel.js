import React, { useState, useEffect } from "react";
import { FaTimesCircle, FaFilter, FaCaretDown, FaCaretUp, FaEye, FaEyeSlash } from "react-icons/fa";
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
const POLL_DATA_SOURCES = [
  { value: "derived", label: "Profile + recent + intentions" },
  { value: "recent_window", label: "Recent writing (context window)" },
];

// Bubble tabs, same treatment as ArtifactsNav (the documents workspace) —
// the admin dashboard is organized as Users / Feedback / Polls sections.
const adminBubbleStyle = (active) => ({
  display: "inline-flex",
  alignItems: "center",
  padding: "6px 14px",
  background: active ? "var(--bg-card)" : "none",
  border: "1px solid",
  borderColor: active ? "var(--accent)" : "var(--border)",
  borderRadius: "16px",
  color: active ? "var(--text-primary)" : "var(--text-muted)",
  fontFamily: "var(--sans)",
  fontSize: "0.8rem",
  fontWeight: 300,
  cursor: "pointer",
});

const ADMIN_TABS = [
  { key: "users", label: "Users" },
  { key: "activity", label: "Activity" },
  { key: "feedback", label: "Feedback" },
  { key: "polls", label: "Polls" },
];

// Same select treatment as AccountPage's Settings (inputStyle+selectStyle
// there) so admin controls don't render as bare browser widgets.
const adminSelectStyle = {
  padding: "10px 12px",
  borderRadius: "6px",
  border: "1px solid var(--border)",
  backgroundColor: "var(--bg-input)",
  color: "var(--text-primary)",
  fontFamily: "var(--sans)",
  fontWeight: 300,
  fontSize: "0.95rem",
  boxSizing: "border-box",
  cursor: "pointer",
  WebkitAppearance: "none",
  appearance: "none",
};

function AdminPolls() {
  const [polls, setPolls] = useState([]);
  const [question, setQuestion] = useState("");
  const [models, setModels] = useState([]);
  const [modelId, setModelId] = useState("");
  const [dataSource, setDataSource] = useState("derived");
  const [responses, setResponses] = useState({}); // poll_id -> array
  const [pollsError, setPollsError] = useState("");

  const fetchPolls = async () => {
    try {
      const res = await api.get("/admin/polls");
      setPolls(res.data.polls);
      return res.data.default_model_id;
    } catch (err) {
      setPollsError("Failed to fetch polls.");
      return null;
    }
  };

  useEffect(() => {
    // Preselect the app default as a concrete model — no ambiguous
    // "default" option that resolves invisibly at creation time.
    Promise.all([
      fetchPolls(),
      api.get("/nodes/models")
        .then((res) => res.data.models || [])
        .catch(() => []),
    ]).then(([defaultModelId, fetchedModels]) => {
      setModels(fetchedModels);
      setModelId((current) => {
        if (current) return current;
        if (fetchedModels.some((m) => m.id === defaultModelId)) {
          return defaultModelId;
        }
        return fetchedModels[0]?.id || "";
      });
    });
  }, []);

  const createPoll = async () => {
    if (!question.trim() || !modelId) return;
    try {
      await api.post("/admin/polls", {
        question,
        model_id: modelId,
        data_source: dataSource,
      });
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
    <div>
      <div style={{ display: "flex", gap: "10px", marginBottom: "12px" }}>
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask the community a question…"
          style={{ padding: "8px", flex: 1 }}
        />
        <select
          value={modelId}
          onChange={(e) => setModelId(e.target.value)}
          title="Model that drafts answers"
          style={adminSelectStyle}
        >
          {models.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
        <select
          value={dataSource}
          onChange={(e) => setDataSource(e.target.value)}
          title="What the draft may read (shown to users before opt-in)"
          style={adminSelectStyle}
        >
          {POLL_DATA_SOURCES.map((s) => (
            <option key={s.value} value={s.value}>{s.label}</option>
          ))}
        </select>
        <button onClick={createPoll}>Create poll</button>
      </div>
      {pollsError && <div style={{ color: "var(--error)" }}>{pollsError}</div>}
      {polls.map((p) => (
        <div key={p.id} style={{ borderTop: "1px solid var(--border)", padding: "8px 0" }}>
          <div style={{ display: "flex", gap: "10px", alignItems: "baseline" }}>
            <span style={{ flex: 1 }}>
              {p.question}
              <span style={{ color: "var(--text-muted)", fontSize: "0.8rem" }}>
                {" "}— {p.model_id || "default model"},{" "}
                {(POLL_DATA_SOURCES.find(
                  (s) => s.value === p.data_source) || POLL_DATA_SOURCES[0]
                ).label.toLowerCase()}
              </span>
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

// User feedback (#158): proposed by the AI in conversation, sent only on
// the user's explicit confirm. The admin API existed without a UI —
// this lists items and manages their triage status.
const FEEDBACK_STATUSES = ["new", "reviewed", "done"];

// Engagement since activation, measured directly (writes / asks / voice),
// not through spend. Active accounts only. The strip is one cell per day:
// filled when the person wrote or asked that day; a dot marks voice.
const relTime = (iso) => {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso + (iso.endsWith("Z") ? "" : "Z")).getTime();
  const m = Math.round(ms / 60000);
  if (m < 60) return `${Math.max(m, 0)}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
};

const pathArea = (p) => {
  if (!p) return null;
  const seg = p.replace(/^\/api\//, "").split("/")[0];
  return seg || null;
};

function AdminActivity() {
  const [data, setData] = useState(null);
  const [cohort, setCohort] = useState("seeded");
  const [days, setDays] = useState(14);
  const [err, setErr] = useState("");

  useEffect(() => {
    setData(null);
    api.get("/admin/activity", { params: { days } })
      .then((res) => setData(res.data))
      .catch(() => setErr("Failed to fetch activity."));
  }, [days]);

  const COHORTS = [
    ["seeded", "seeded"], ["x", "from X"], ["ca", "from CA"],
    ["unseeded", "not seeded"], ["optout", "opted out"], ["all", "all"],
  ];
  const inCohort = (u) => {
    if (cohort === "all") return true;
    if (cohort === "seeded") return !!u.seeded;
    if (cohort === "unseeded") return !u.seeded;
    if (cohort === "optout") return u.prefill_consent === "no";
    return u.seeded === cohort;
  };
  const users = (data?.users || []).filter(inCohort);
  const cell = { border: "1px solid var(--border)", padding: "6px 8px", whiteSpace: "nowrap", fontSize: "0.9em" };
  const th = { ...cell, textAlign: "left", color: "var(--text-secondary)", fontWeight: 400 };

  return (
    <div>
      <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap", marginBottom: "12px" }}>
        {COHORTS.map(([k, label]) => (
          <button
            key={k}
            onClick={() => setCohort(k)}
            style={{
              padding: "4px 10px", borderRadius: "999px", cursor: "pointer", fontSize: "0.85em",
              border: `1px solid ${cohort === k ? "var(--accent)" : "var(--border)"}`,
              background: "transparent", color: cohort === k ? "var(--accent)" : "var(--text-secondary)",
            }}
          >
            {label}
          </button>
        ))}
        <span style={{ flex: 1 }} />
        <label style={{ fontSize: "0.85em", color: "var(--text-secondary)" }}>
          days{" "}
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ ...adminSelectStyle, padding: "4px 8px", fontSize: "0.85em" }}>
            {[7, 14, 30, 60].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
      </div>
      {err && <div style={{ color: "var(--error)" }}>{err}</div>}
      {!data && !err && <div style={{ color: "var(--text-muted)" }}>Loading…</div>}
      {data && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ borderCollapse: "collapse", color: "var(--text-primary)", width: "100%" }}>
            <thead>
              <tr>
                <th style={th}>user</th>
                <th style={th} title="Seeded from X API / Community Archive; ✗ = declined tweet seed">seed</th>
                <th style={th} title="Activation (approved) — everything on this tab counts from here">activated</th>
                <th style={th} title="Last authenticated request (5-min resolution) and the area they were in">last seen</th>
                <th style={th} title={`One cell per day (oldest → newest). Filled = wrote or asked that day; · = voice. Grey = before activation.`}>{data.days.length}d</th>
                <th style={th} title="Days with at least one write or ask / days since activation">active</th>
                <th style={th} title="Human-written nodes (text or voice) since activation">writes</th>
                <th style={th} title="LLM responses requested since activation">asks</th>
                <th style={th} title="Transcribed voice minutes since activation">voice</th>
                <th style={th} title="Nodes imported by the user (archives) since activation">imports</th>
                <th style={th} title="Profile versions created since activation / latest">profile</th>
                <th style={th} title="Artifacts opened since activation (views · most recent first); hover a row for last-view times">artifacts</th>
                <th style={th} title="Came back the day after activation / any time in days 2–7">d1 / d7</th>
                <th style={th} title="User-initiated spend only (conversation, transcription, TTS, search) since activation">$ own</th>
              </tr>
            </thead>
            <tbody>
              {users.length === 0 && (
                <tr><td style={cell} colSpan={14}><span style={{ color: "var(--text-muted)" }}>No accounts in this cohort.</span></td></tr>
              )}
              {users.map((u) => (
                <tr key={u.id}>
                  <td style={cell}>{u.username}</td>
                  <td style={cell}>
                    {u.seeded === "x" ? "X" : u.seeded === "ca" ? "CA" : "—"}
                    {u.prefill_consent === "no" && <span title="declined tweet seed" style={{ color: "var(--text-muted)" }}> ✗</span>}
                  </td>
                  <td style={cell} title={u.activated_at || ""}>{relTime(u.activated_at) || "—"}</td>
                  <td
                    style={cell}
                    title={u.last_seen_at
                      ? `${u.last_seen_at} UTC · ${u.last_seen_path || "no page yet"}`
                      : (u.accepted_terms_at ? "no last_seen yet — accepted terms " + u.accepted_terms_at : "never logged in")}
                  >
                    {u.last_seen_at
                      ? <>{relTime(u.last_seen_at)}{pathArea(u.last_seen_path) && <span style={{ color: "var(--text-muted)" }}> · {pathArea(u.last_seen_path)}</span>}</>
                      : u.accepted_terms_at
                        ? <span style={{ color: "var(--text-muted)" }}>terms {relTime(u.accepted_terms_at)}</span>
                        : <span style={{ color: "var(--error)" }}>never</span>}
                  </td>
                  <td style={{ ...cell, fontFamily: "monospace", fontSize: "1.25em", letterSpacing: "2px", lineHeight: 1 }}>
                    {u.strip.map((d) => {
                      const active = d.w + d.a > 0;
                      return (
                        <span
                          key={d.d}
                          title={`${d.d}: ${d.w} writes, ${d.a} asks${d.v ? `, ${d.v} voice` : ""}${d.pre ? " (before activation)" : ""}`}
                          style={{ color: d.pre ? "var(--border)" : active ? "var(--accent)" : "var(--text-muted)" }}
                        >
                          {d.pre ? "·" : active ? (d.v ? "◉" : "●") : "○"}
                        </span>
                      );
                    })}
                  </td>
                  <td style={cell}>{u.active_days}/{u.days_since_activation}</td>
                  <td style={cell}>{u.writes}</td>
                  <td style={cell}>{u.asks}</td>
                  <td style={cell}>{u.voice_minutes ? `${u.voice_minutes}m` : "—"}</td>
                  <td style={cell}>{u.imports || "—"}</td>
                  <td style={cell} title={u.latest_profile_at || ""}>{u.profile_versions_since_activation || "—"}{u.latest_profile_at ? <span style={{ color: "var(--text-muted)" }}> · {relTime(u.latest_profile_at)}</span> : null}</td>
                  <td style={cell} title={(u.artifact_views || []).map((v) => `${v.kind} ×${v.count} — last ${relTime(v.last)}`).join("\n")}>
                    {(u.artifact_views || []).length === 0 ? "—" : (
                      <>
                        {u.artifact_views.slice(0, 2).map((v) => `${v.kind} ×${v.count}`).join(", ")}
                        {u.artifact_views.length > 2 ? ` +${u.artifact_views.length - 2}` : ""}
                      </>
                    )}
                  </td>
                  <td style={cell}>{u.day1_return ? "✓" : "○"} / {u.day7_return ? "✓" : "○"}</td>
                  <td style={cell}>${(u.user_spend_usd || 0).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function AdminFeedback() {
  const [items, setItems] = useState([]);
  const [filter, setFilter] = useState("new");
  const [feedbackError, setFeedbackError] = useState("");

  useEffect(() => {
    api.get("/admin/feedback")
      .then((res) => setItems(res.data.feedback || []))
      .catch(() => setFeedbackError("Failed to fetch feedback."));
  }, []);

  const updateStatus = async (id, status) => {
    try {
      await api.put(`/admin/feedback/${id}`, { status });
      setItems((prev) =>
        prev.map((f) => (f.id === id ? { ...f, status } : f)));
    } catch (err) {
      setFeedbackError("Failed to update feedback status.");
    }
  };

  const visible = filter === "all"
    ? items
    : items.filter((f) => f.status === filter);
  const countFor = (s) => items.filter((f) => f.status === s).length;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: "12px" }}>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ ...adminSelectStyle, padding: "6px 10px", fontSize: "0.85rem" }}
        >
          <option value="all">All ({items.length})</option>
          {FEEDBACK_STATUSES.map((s) => (
            <option key={s} value={s}>{s} ({countFor(s)})</option>
          ))}
        </select>
      </div>
      {feedbackError && (
        <div style={{ color: "var(--error)" }}>{feedbackError}</div>
      )}
      {visible.length === 0 ? (
        <div style={{ color: "var(--text-muted)", padding: "8px 0" }}>
          Nothing here.
        </div>
      ) : (
        visible.map((f) => (
          <div key={f.id} style={{ borderTop: "1px solid var(--border)", padding: "10px 0" }}>
            <div style={{ display: "flex", gap: "10px", alignItems: "baseline" }}>
              <strong>{f.username || `user ${f.user_id}`}</strong>
              <span style={{ color: "var(--text-muted)", fontSize: "0.8rem" }}>
                {f.category} · {f.source} ·{" "}
                {f.created_at ? new Date(f.created_at).toLocaleDateString() : ""}
              </span>
              <span style={{ flex: 1 }} />
              <select
                value={f.status}
                onChange={(e) => updateStatus(f.id, e.target.value)}
                style={{ ...adminSelectStyle, padding: "6px 10px", fontSize: "0.85rem" }}
              >
                {FEEDBACK_STATUSES.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </div>
            <div style={{ whiteSpace: "pre-wrap", color: "var(--text-secondary)", marginTop: "4px" }}>
              {f.content}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

function AdminPanel() {
  const [activeTab, setActiveTab] = useState("users");
  const [users, setUsers] = useState([]);
  const [allowedPlans, setAllowedPlans] = useState([]);
  const [error, setError] = useState("");
  const [newHandle, setNewHandle] = useState("");
  const [newHandleError, setNewHandleError] = useState("");
  // Per-user spend-limit input values, keyed by user id (controlled inputs).
  const [limitEdits, setLimitEdits] = useState({});
  // Column filters: hide rows with $0 in Spent / This Month (independent).
  const [hideZeroSpent, setHideZeroSpent] = useState(false);
  // Admin-flagged spam signups are hidden by default (crossed-out eye).
  const [showSpam, setShowSpam] = useState(false);
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

  const buildProfile = async (userId) => {
    try {
      const res = await api.post(`/admin/users/${userId}/build_profile`);
      if (!res.data.queued) setError(res.data.message);
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error queueing profile build.");
    }
  };

  const toggleSpam = async (userId) => {
    try {
      await api.post(`/admin/users/${userId}/toggle_spam`);
      fetchUsers();
    } catch (err) {
      console.error(err);
      setError("Error toggling spam flag.");
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

  // Community Archive pre-fill (admin cold-start bootstrap): per-row inline
  // form -> POST /admin/users/:id/prefill -> poll /admin/prefill/status.
  // prefill[userId] = { open, handle, includeReplies, taskId, status, done,
  //                     total, stage, result, error }
  const [prefill, setPrefill] = useState({});
  const patchPrefill = (userId, patch) =>
    setPrefill((prev) => ({ ...prev, [userId]: { ...(prev[userId] || {}), ...patch } }));

  // source: "ca" (Community Archive, free) | "x" (X API, paid, ≤3,200 posts)
  const openPrefill = (u, source = "ca") =>
    patchPrefill(u.id, {
      open: true, source, handle: u.username || "", includeReplies: true,
      maxTweets: "", check: null, status: null, result: null, error: null,
    });

  const checkPrefill = async (userId) => {
    const form = prefill[userId] || {};
    patchPrefill(userId, { checking: true, check: null, error: null });
    try {
      const res = form.source === "x"
        ? await api.get("/admin/prefill/x/check", {
          params: { handle: form.handle, user_id: userId, max_tweets: form.maxTweets || undefined },
        })
        : await api.get("/admin/prefill/check", {
          params: { handle: form.handle, user_id: userId },
        });
      const patch = { checking: false, check: res.data };
      // Default the X max input to what the timeline can actually serve.
      if (form.source === "x" && !form.maxTweets) patch.maxTweets = String(res.data.fetchable || "");
      patchPrefill(userId, patch);
    } catch (err) {
      patchPrefill(userId, {
        checking: false,
        check: null,
        error: err.response?.data?.error || "Check failed.",
      });
    }
  };

  const xCheckLabel = (c) => {
    const n = (v) => (v == null ? "?" : v.toLocaleString());
    const parts = [`${n(c.tweet_count)} tweets on X`];
    if (c.protected) parts.push("PROTECTED — timeline not readable");
    else parts.push(`fetch up to ${n(c.fetchable)} posts incl. retweets (cap ${n(c.timeline_cap)}) ≈ $${(c.est_cost_usd || 0).toFixed(2)}`);
    if (c.already_imported) parts.push(`${n(c.already_imported)} already imported`);
    return parts.join(" · ");
  };

  const checkLabel = (c) => {
    if (!c) return null;
    if (c.timeline_cap != null) return xCheckLabel(c);
    const n = (v) => (v == null ? "?" : v.toLocaleString());
    const parts = [`${n(c.archived)} archived`];
    if (c.archived_live != null) parts.push(`(${n(c.archived_live)} live)`);
    if (c.retweets != null) parts.push(`${n(c.retweets)} RT`);
    if (c.replies != null) parts.push(`${n(c.replies)} replies`);
    if (c.originals != null) parts.push(`${n(c.originals)} originals`);
    if (c.est_tokens != null) {
      const below = c.est_tokens < (c.profile_threshold_tokens || 10000);
      parts.push(`~${n(c.est_tokens)} tokens${below ? " — below profile threshold" : ""}`);
    }
    parts.push(`account reports ${n(c.account_num_tweets)}`);
    parts.push(c.ingestion === "twitter_import" ? "extension-ingested (partial, grows)" : `via ${c.ingestion || "archive"}`);
    parts.push(`import via ${c.import_source}`);
    if (c.already_imported) parts.push(`${n(c.already_imported)} already imported`);
    return parts.join(" · ");
  };

  const startPrefill = async (userId) => {
    const form = prefill[userId] || {};
    try {
      const res = form.source === "x"
        ? await api.post(`/admin/users/${userId}/prefill-x`, {
          handle: form.handle,
          max_tweets: form.maxTweets ? Number(form.maxTweets) : undefined,
          include_replies: form.includeReplies !== false,
        })
        : await api.post(`/admin/users/${userId}/prefill`, {
          handle: form.handle,
          include_replies: form.includeReplies !== false,
        });
      patchPrefill(userId, { taskId: res.data.task_id, status: "queued", error: null, result: null });
    } catch (err) {
      patchPrefill(userId, { error: err.response?.data?.error || "Error starting pre-fill." });
    }
  };

  useEffect(() => {
    const active = Object.entries(prefill).filter(
      ([, p]) => p.taskId && (p.status === "queued" || p.status === "running")
    );
    if (active.length === 0) return undefined;
    const timer = setInterval(async () => {
      for (const [userId, p] of active) {
        try {
          const res = await api.get(`/admin/prefill/status/${p.taskId}`);
          patchPrefill(Number(userId), res.data);
          if (res.data.status === "completed") fetchUsers();
        } catch (err) {
          patchPrefill(Number(userId), { status: "failed", error: "Status check failed." });
        }
      }
    }, 2000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefill]);

  const prefillLabel = (p) => {
    if (!p || !p.status) return null;
    if (p.status === "queued") return "Queued…";
    if (p.status === "running") {
      const n = (p.done || 0).toLocaleString();
      const t = p.total ? ` / ${p.total.toLocaleString()}` : "";
      if ((p.stage || "").startsWith("downloading")) {
        // Nightly parquet snapshot (~1 GB, once per export); done/total in MB.
        const file = p.stage.replace("downloading", "").trim();
        return `Downloading snapshot${file ? ` ${file}` : ""} ${n}${t} MB`;
      }
      return `${p.stage === "importing" ? "Importing" : "Fetching"} ${n}${t}`;
    }
    if (p.status === "completed") {
      const r = p.result || {};
      if (r.source === "x-api") {
        return `${r.partial ? "PARTIAL" : "Done"}: ${(r.created || 0).toLocaleString()} nodes from ${(r.fetched || 0).toLocaleString()} posts via X` +
          `${r.partial ? ` (stopped: ${r.fetch_error})` : ""}` +
          ` (≈ $${(r.est_cost_usd || 0).toFixed(2)})` +
          `${r.retweets_skipped ? `, ${r.retweets_skipped.toLocaleString()} retweets skipped` : ""}` +
          `${r.skipped ? `, ${r.skipped} already imported` : ""}` +
          `${r.imported_tokens != null ? `, ~${r.imported_tokens.toLocaleString()} tokens` : ""}` +
          `${r.profile_batch_queued ? " — batch profile queued" : " — below profile threshold"}`;
      }
      const archived = r.archived != null ? ` of ${r.archived.toLocaleString()} archived` : "";
      const rts = r.retweets_skipped ? `, ${r.retweets_skipped.toLocaleString()} retweets skipped` : "";
      const reported = r.account_num_tweets != null && r.account_num_tweets !== r.archived
        ? `; account reports ${r.account_num_tweets.toLocaleString()} tweets`
        : "";
      return `Done: ${(r.created || 0).toLocaleString()} nodes${archived} from @${r.handle}` +
        `${r.source === "parquet" ? " (snapshot)" : ""}${rts}${reported}` +
        `${r.skipped ? `, ${r.skipped} already imported` : ""}` +
        `${r.imported_tokens != null ? `, ~${r.imported_tokens.toLocaleString()} tokens` : ""}` +
        `${r.profile_batch_queued ? " — batch profile queued" : " — below profile threshold"}`;
    }
    if (p.status === "failed") return `Failed: ${p.error}`;
    return null;
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
    .filter((u) => showSpam || !u.spam)
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
      <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "20px" }}>
        {ADMIN_TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            style={adminBubbleStyle(activeTab === t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <h1 style={{
        fontFamily: "var(--serif)", fontSize: "2rem", fontWeight: 300,
        color: "var(--text-primary)", margin: "0 0 20px",
      }}>
        {ADMIN_TABS.find((t) => t.key === activeTab).label}
      </h1>

      {activeTab === "polls" && <AdminPolls />}
      {activeTab === "activity" && <AdminActivity />}
      {activeTab === "feedback" && <AdminFeedback />}

      {activeTab === "users" && (<>
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

      {error && <div style={{ color: "var(--error)" }}>{error}</div>}
      <table style={{ width: "100%", borderCollapse: "collapse", color: "var(--text-primary)" }}>
        <thead>
          <tr>
            <th style={{ border: "1px solid var(--border)", padding: "8px" }}>ID</th>
            <th style={{ border: "1px solid var(--border)", padding: "8px", whiteSpace: "nowrap" }}>
              Username
              <button
                type="button"
                onClick={() => setShowSpam((v) => !v)}
                title={showSpam ? "Showing spam accounts — click to hide" : "Spam accounts hidden — click to show"}
                aria-label={showSpam ? "Hide spam accounts" : "Show spam accounts"}
                style={{
                  marginLeft: "6px", padding: 0, background: "none", border: "none",
                  cursor: "pointer", verticalAlign: "middle", fontSize: "0.9em",
                  color: showSpam ? "var(--accent)" : "var(--text-muted)",
                }}
              >
                {showSpam ? <FaEye /> : <FaEyeSlash />}
              </button>
            </th>
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
              style={{ border: "1px solid var(--border)", padding: "8px", width: "150px", whiteSpace: "nowrap" }}
              title="Profile chain: ✓ complete = one version, or the latest version is an integration; ⏳ generating = batch job in flight / rebuild requested / chain not yet integrated. 'pre-filled @handle' = bootstrapped from the Community Archive."
            >
              Profile
            </th>
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
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                {u.username}
                {u.spam && (
                  <span style={{ marginLeft: "6px", fontSize: "0.75em", color: "var(--error)" }} title="Flagged as spam">spam</span>
                )}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                {u.approved ? "Active" : "Inactive"}
              </td>
              <td style={{ border: "1px solid var(--border)", padding: "8px" }}>
                <select
                  value={u.plan || "free"}
                  onChange={(e) => updatePlan(u.id, e.target.value)}
                  style={{ ...adminSelectStyle,
                           padding: "6px 10px", fontSize: "0.85rem" }}
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
                style={{ border: "1px solid var(--border)", padding: "8px", width: "150px", whiteSpace: "nowrap", fontSize: "0.9em" }}
                title={
                  u.profile?.last_created_at
                    ? `Latest version: ${u.profile.last_generation_type} at ${new Date(u.profile.last_created_at).toLocaleString()}`
                    : "No profile yet"
                }
              >
                {u.profile?.state === "complete" && (
                  <span style={{ color: "var(--success)" }}>
                    ✓ {u.profile.versions} {u.profile.versions === 1 ? "version" : "versions"}
                  </span>
                )}
                {u.profile?.state === "generating" && u.profile.waiting === "inactive" && (
                  <span
                    style={{ color: "var(--text-muted)" }}
                    title="A profile build is requested but the account is Inactive: the hourly seeder only walks approved accounts, so nothing will happen until you Activate it"
                  >
                    ⏸ waiting: inactive{u.profile.versions ? ` (${u.profile.versions} so far)` : ""}
                  </span>
                )}
                {u.profile?.state === "generating" && u.profile.waiting !== "inactive" && (
                  <span
                    style={{ color: u.profile.incomplete || u.profile.batch_attempts ? "var(--error)" : "var(--warning)" }}
                    title={u.profile.incomplete ? "Data remains after the latest version's cutoff and no batch job is in flight — the last chunk failed; the hourly seeder retries" : undefined}
                  >
                    ⏳ {u.profile.incomplete ? "stuck" : "generating"}{u.profile.versions ? ` (${u.profile.versions} so far)` : ""}{u.profile.batch_attempts ? ` · ${u.profile.batch_attempts} failed` : ""}
                  </span>
                )}
                {(!u.profile || u.profile.state === "none") && (
                  <span style={{ color: "var(--text-muted)" }}>—</span>
                )}
                {u.prefilled_handle && (
                  <div style={{ color: "var(--text-muted)", fontSize: "0.85em" }}>
                    pre-filled @{u.prefilled_handle}
                  </div>
                )}
                {u.prefill_consent && (
                  <div
                    style={{ color: u.prefill_consent === "yes" ? "var(--success)" : "var(--text-muted)", fontSize: "0.85em" }}
                    title={u.prefill_consent_at ? `answered ${new Date(u.prefill_consent_at).toLocaleString()}` : undefined}
                  >
                    {u.prefill_consent === "yes" ? "✓ opted in to tweet seed" : "✗ declined tweet seed"}
                  </div>
                )}
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
                </button>{" "}
                <button
                  onClick={() => openPrefill(u, "ca")}
                  title="Import this account's public tweets from the Community Archive and queue a batch profile build"
                >
                  Pre-fill from CA
                </button>{" "}
                <button
                  onClick={() => openPrefill(u, "x")}
                  title="Paid: pull the account's most recent posts from the X API (~$0.005/post incl. retweets, ≤3,200; retweets are dropped on import) and queue a batch profile build"
                >
                  Pre-fill from X
                </button>{" "}
                <button
                  onClick={() => buildProfile(u.id)}
                  disabled={u.profile_batch_pending}
                  title="Force a from-scratch batch profile build now, ignoring the token gate (for small corpora you want profiled anyway)"
                >
                  Build profile
                </button>{" "}
                <button
                  onClick={() => toggleSpam(u.id)}
                  title={u.spam ? "Unmark as spam" : "Mark as spam (hides the row behind the eye toggle; does not deactivate)"}
                  style={u.spam ? { color: "var(--error)" } : undefined}
                >
                  {u.spam ? "Not spam" : "Spam"}
                </button>
                {prefill[u.id]?.open && (
                  <div style={{ marginTop: "6px", display: "flex", gap: "6px", alignItems: "center", flexWrap: "wrap" }}>
                    <input
                      type="text"
                      value={prefill[u.id].handle || ""}
                      placeholder={prefill[u.id].source === "x" ? "X handle" : "CA handle"}
                      onChange={(e) => patchPrefill(u.id, { handle: e.target.value, check: null })}
                      onKeyDown={(e) => { if (e.key === "Enter") startPrefill(u.id); }}
                      style={{ width: "140px", padding: "4px" }}
                      disabled={["queued", "running"].includes(prefill[u.id].status)}
                    />
                    {prefill[u.id].source === "x" && (
                      <input
                        type="number"
                        min="1"
                        max={prefill[u.id].check?.fetchable || 3200}
                        value={prefill[u.id].maxTweets || ""}
                        placeholder="max (≤3200)"
                        title="Most recent posts to fetch; capped at min(3200, account's tweet count)"
                        onChange={(e) => patchPrefill(u.id, { maxTweets: e.target.value })}
                        style={{ width: "110px", padding: "4px" }}
                        disabled={["queued", "running"].includes(prefill[u.id].status)}
                      />
                    )}
                    <label style={{ fontSize: "0.85em", whiteSpace: "nowrap" }}>
                      <input
                        type="checkbox"
                        checked={prefill[u.id].includeReplies !== false}
                        onChange={(e) => patchPrefill(u.id, { includeReplies: e.target.checked })}
                      />{" "}
                      replies
                    </label>
                    <button
                      onClick={() => checkPrefill(u.id)}
                      disabled={prefill[u.id].checking || ["queued", "running"].includes(prefill[u.id].status)}
                      title={prefill[u.id].source === "x"
                        ? "Look up the account on X: tweet count, protected?, fetchable posts and cost (one ~$0.01 user read)"
                        : "What the Community Archive actually holds for this handle (before paying for an import)"}
                    >
                      {prefill[u.id].checking ? "Checking…" : "Check"}
                    </button>
                    <button
                      onClick={() => startPrefill(u.id)}
                      disabled={["queued", "running"].includes(prefill[u.id].status) || (prefill[u.id].source === "x" && prefill[u.id].check?.protected)}
                      style={prefill[u.id].check && prefill[u.id].check.est_tokens != null && prefill[u.id].check.est_tokens < (prefill[u.id].check.profile_threshold_tokens || 10000) ? { opacity: 0.6 } : undefined}
                    >
                      Start
                    </button>
                    <button onClick={() => patchPrefill(u.id, { open: false })}>Close</button>
                    {prefill[u.id].check && !prefill[u.id].status && (
                      <span style={{ fontSize: "0.85em", color: "var(--text-secondary)", flexBasis: "100%" }}>
                        @{prefill[u.id].check.username}: {checkLabel(prefill[u.id].check)}
                      </span>
                    )}
                    {(prefillLabel(prefill[u.id]) || prefill[u.id].error) && (
                      <span style={{ fontSize: "0.85em", color: prefill[u.id].status === "failed" || prefill[u.id].error ? "var(--error)" : "var(--text-secondary)" }}>
                        {prefill[u.id].error && !prefill[u.id].status ? prefill[u.id].error : prefillLabel(prefill[u.id])}
                      </span>
                    )}
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </>)}
    </div>
  );
}

export default AdminPanel;