import React, { useState, useRef, useEffect, useCallback } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import api from "../api";
import useSubmitShortcut from "../hooks/useSubmitShortcut";
import { BUILTIN_KIND_ORDER, isBuiltinKind } from "../utils/artifactKinds";

// Show at most this many artifacts before the list becomes scrollable.
const MAX_VISIBLE = 5;

const titleFromKind = (k) =>
  k.replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const slugify = (name) =>
  name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);

function ArtifactsMenu({ dropdownStyle, dropdownItemStyle }) {
  const location = useLocation();
  const navigate = useNavigate();
  const currentPath = location.pathname;

  const [open, setOpen] = useState(false);
  const [artifacts, setArtifacts] = useState(null); // null = not yet loaded

  const [modalOpen, setModalOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [newContent, setNewContent] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  const menuRef = useRef(null);
  const modalNameRef = useRef(null);
  const modalDescRef = useRef(null);
  const modalContentRef = useRef(null);

  const fetchArtifacts = useCallback(async () => {
    try {
      const res = await api.get("/artifacts");
      setArtifacts(res.data.artifacts || []);
    } catch (err) {
      console.error("Failed to load artifacts:", err);
      setArtifacts([]);
    }
  }, []);

  // Lazy-load the list the first time the menu opens.
  useEffect(() => {
    if (open && artifacts === null) fetchArtifacts();
  }, [open, artifacts, fetchArtifacts]);

  // Close the dropdown on click outside.
  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [open]);

  // Close the dropdown on navigation.
  useEffect(() => {
    setOpen(false);
  }, [currentPath]);

  const byKind = Object.fromEntries((artifacts || []).map((a) => [a.kind, a]));
  // Built-in artifacts (memory, scratchpad, predictions, intentions-when-it
  // exists) are pinned above the divider in canonical order — they're not
  // user-created. Only user/AI-created custom kinds go in the list below.
  const pinnedArtifacts = BUILTIN_KIND_ORDER
    .filter((k) => byKind[k])
    .map((k) => byKind[k]);
  const dynamicArtifacts = (artifacts || [])
    .filter((a) => !isBuiltinKind(a.kind))
    .slice()
    .sort((a, b) =>
      (a.title || a.kind).localeCompare(b.title || b.kind)
    );

  const isMenuActive =
    currentPath.startsWith("/artifacts") ||
    currentPath === "/profile" ||
    currentPath === "/todo" ||
    currentPath === "/ai-preferences";

  const openCreateModal = () => {
    setOpen(false);
    setNewName("");
    setNewDescription("");
    setNewContent("");
    setCreateError("");
    setModalOpen(true);
  };

  const handleCreate = async () => {
    const slug = slugify(newName);
    if (!slug) {
      setCreateError("Please enter a name.");
      return;
    }
    setCreating(true);
    setCreateError("");
    try {
      await api.put(`/artifacts/${slug}`, {
        content: newContent,
        title: newName.trim(),
        description: newDescription.trim() || undefined,
        generated_by: "user",
      });
      setModalOpen(false);
      setNewName("");
      setNewDescription("");
      setNewContent("");
      await fetchArtifacts();
      window.dispatchEvent(new CustomEvent('loore_artifacts_changed'));
      navigate(`/artifacts/${slug}`);
    } catch (err) {
      console.error("Failed to create artifact:", err);
      setCreateError(
        err.response?.data?.error || "Failed to create artifact."
      );
    }
    setCreating(false);
  };

  // Cmd+Return / Ctrl+Enter creates the artifact (#129) from either modal
  // field, matching the Create button's enabled state (a name is required).
  const createEnabled = modalOpen && !creating
    && !!newName.trim() && !!newDescription.trim();
  useSubmitShortcut(modalNameRef, () => handleCreate(), createEnabled);
  useSubmitShortcut(modalDescRef, () => handleCreate(), createEnabled);
  useSubmitShortcut(modalContentRef, () => handleCreate(), createEnabled);

  const itemStyle = (active) => ({
    ...dropdownItemStyle,
    color: active ? "var(--accent)" : "var(--text-muted)",
  });

  return (
    <div ref={menuRef} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          color: isMenuActive ? "var(--accent)" : "var(--text-muted)",
          textDecoration: "none",
          fontFamily: "var(--sans)",
          fontWeight: 300,
          fontSize: "0.85rem",
          letterSpacing: "0.02em",
          transition: "color 0.3s ease",
          background: "none",
          border: "none",
          cursor: "pointer",
          padding: 0,
          display: "flex",
          alignItems: "center",
          gap: "3px",
        }}
      >
        Artifacts
        <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor" style={{ opacity: 0.6 }}>
          <path d="M1 2.5L4 5.5L7 2.5" stroke="currentColor" strokeWidth="1.2" fill="none" />
        </svg>
      </button>

      {open && (
        <div style={dropdownStyle}>
          {/* Fixed entries */}
          <Link to="/profile" style={itemStyle(currentPath === "/profile")}>
            Profile
          </Link>
          <Link to="/todo" style={itemStyle(currentPath === "/todo")}>
            Todo
          </Link>
          {/* Built-in artifacts (intentions, predictions, memory, scratchpad)
              pinned here in canonical order */}
          {pinnedArtifacts.map((a) => (
            <Link
              key={a.kind}
              to={`/artifacts/${a.kind}`}
              title={a.description || undefined}
              style={itemStyle(currentPath === `/artifacts/${a.kind}`)}
            >
              {a.title || titleFromKind(a.kind)}
            </Link>
          ))}
          <Link
            to="/ai-preferences"
            style={itemStyle(currentPath === "/ai-preferences")}
          >
            AI interaction preferences
          </Link>

          <div style={{ borderTop: "1px solid var(--border)", margin: "4px 0" }} />

          {/* Section header + create button */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "6px 16px 4px",
            }}
          >
            <span
              style={{
                fontFamily: "var(--sans)",
                fontWeight: 300,
                fontSize: "0.7rem",
                letterSpacing: "0.08em",
                textTransform: "uppercase",
                color: "var(--text-muted)",
                opacity: 0.6,
              }}
            >
              Your artifacts
            </span>
            <button
              onClick={openCreateModal}
              title="Create a new artifact"
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: "var(--text-muted)",
                fontSize: "1.1rem",
                lineHeight: 1,
                padding: "0 2px",
              }}
            >
              +
            </button>
          </div>

          {/* Dynamic, alphabetical artifact list (scrolls past MAX_VISIBLE) */}
          <div
            style={{
              maxHeight:
                dynamicArtifacts.length > MAX_VISIBLE ? "190px" : "none",
              overflowY:
                dynamicArtifacts.length > MAX_VISIBLE ? "auto" : "visible",
            }}
          >
            {artifacts === null ? (
              <div style={{ ...dropdownItemStyle, opacity: 0.6, cursor: "default" }}>
                Loading…
              </div>
            ) : dynamicArtifacts.length === 0 ? (
              <div style={{ ...dropdownItemStyle, opacity: 0.6, cursor: "default" }}>
                No artifacts yet.
              </div>
            ) : (
              dynamicArtifacts.map((a) => (
                <Link
                  key={a.kind}
                  to={`/artifacts/${a.kind}`}
                  title={a.description || undefined}
                  style={itemStyle(currentPath === `/artifacts/${a.kind}`)}
                >
                  {a.title || a.kind}
                </Link>
              ))
            )}
          </div>
        </div>
      )}

      {/* Create-artifact modal */}
      {modalOpen && (
        <div
          onClick={() => !creating && setModalOpen(false)}
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.6)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 2000,
            padding: "24px",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "var(--bg-card)",
              border: "1px solid var(--border)",
              borderRadius: "12px",
              padding: "24px",
              width: "min(440px, 92vw)",
              boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
            }}
          >
            <h2
              style={{
                fontFamily: "var(--serif)",
                fontWeight: 300,
                fontSize: "1.3rem",
                color: "var(--text-primary)",
                margin: "0 0 16px 0",
              }}
            >
              New artifact
            </h2>

            <input
              ref={modalNameRef}
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Name"
              autoFocus
              style={{
                width: "100%",
                boxSizing: "border-box",
                marginBottom: "12px",
                background: "var(--bg-input)",
                border: "1px solid var(--border)",
                borderRadius: "8px",
                color: "var(--text-primary)",
                fontFamily: "var(--sans)",
                fontSize: "0.9rem",
                fontWeight: 300,
                padding: "10px 14px",
              }}
            />

            <input
              ref={modalDescRef}
              value={newDescription}
              onChange={(e) => setNewDescription(e.target.value)}
              placeholder="One-line description (what it's for)"
              style={{
                width: "100%",
                boxSizing: "border-box",
                marginBottom: "12px",
                background: "var(--bg-input)",
                border: "1px solid var(--border)",
                borderRadius: "8px",
                color: "var(--text-primary)",
                fontFamily: "var(--sans)",
                fontSize: "0.9rem",
                fontWeight: 300,
                padding: "10px 14px",
              }}
            />

            <textarea
              ref={modalContentRef}
              value={newContent}
              onChange={(e) => setNewContent(e.target.value)}
              placeholder="Content (markdown)…"
              style={{
                width: "100%",
                boxSizing: "border-box",
                minHeight: "180px",
                background: "var(--bg-input)",
                border: "1px solid var(--border)",
                borderRadius: "8px",
                color: "var(--text-primary)",
                fontFamily: "var(--sans)",
                fontSize: "0.9rem",
                fontWeight: 300,
                padding: "12px 14px",
                lineHeight: 1.6,
                resize: "vertical",
              }}
            />

            {createError && (
              <p
                style={{
                  color: "var(--error)",
                  fontFamily: "var(--sans)",
                  fontSize: "0.8rem",
                  margin: "10px 0 0 0",
                }}
              >
                {createError}
              </p>
            )}

            <div style={{ marginTop: "16px", display: "flex", gap: "8px", justifyContent: "flex-end" }}>
              <button
                onClick={() => setModalOpen(false)}
                disabled={creating}
                style={{
                  padding: "8px 18px",
                  background: "none",
                  border: "1px solid var(--border)",
                  borderRadius: "6px",
                  color: "var(--text-muted)",
                  fontFamily: "var(--sans)",
                  fontSize: "0.85rem",
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
              <button
                onClick={handleCreate}
                disabled={!createEnabled}
                style={{
                  padding: "8px 18px",
                  background: "var(--accent)",
                  border: "none",
                  borderRadius: "6px",
                  color: "var(--bg-deep)",
                  fontFamily: "var(--sans)",
                  fontSize: "0.85rem",
                  fontWeight: 400,
                  cursor: !createEnabled ? "not-allowed" : "pointer",
                  opacity: !createEnabled ? 0.6 : 1,
                }}
              >
                {creating ? "Creating…" : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default ArtifactsMenu;
