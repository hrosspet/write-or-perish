import React, { useState, useEffect } from "react";
import ReactDOM from "react-dom";
import JSZip from "jszip";
import { FaSpinner } from "react-icons/fa";
import api from "../api";
import PrivacySelector from "./PrivacySelector";

const ghostBtnStyle = {
  backgroundColor: "transparent",
  color: "var(--text-secondary)",
  border: "1px solid var(--border)",
  padding: "8px 16px",
  cursor: "pointer",
  borderRadius: "6px",
  fontFamily: "var(--sans)",
  fontSize: "0.85rem",
  fontWeight: 300,
};

const primaryBtnStyle = {
  ...ghostBtnStyle,
  borderColor: "var(--accent)",
  color: "var(--accent)",
};

const cancelBtnStyle = {
  ...ghostBtnStyle,
};

// Coarse stage labels shown while an import is in flight.
const STAGE_LABELS = {
  extracting: "Extracting…",
  analyzing: "Analyzing…",
  importing: "Importing…",
};

function importErr(userMessage) {
  const e = new Error(userMessage);
  e.userMessage = userMessage;
  return e;
}

// A spinning icon + stage label, shown inside import buttons while an
// import is in flight. `stage` is one of STAGE_LABELS' keys; falls back to
// the provided label when the stage is unknown.
function ImportSpinner({ stage, fallback }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.4em" }}>
      <FaSpinner className="spin" aria-hidden="true" />
      {STAGE_LABELS[stage] || fallback}
    </span>
  );
}

// Extract the conversations.json blob from a Claude/ChatGPT export zip in
// the browser, so the full (potentially multi-GB) export with images/audio
// never has to traverse the network.
//
// Entry matching is intentionally flexible:
//   1. Prefer a file entry whose name ends with "conversations.json".
//   2. Otherwise fall back to the largest .json entry whose parsed
//      top-level value is an array (the export's conversation list).
// Throws an Error with a `.userMessage` if no suitable entry is found or
// the zip cannot be read.
async function extractConversationsBlob(file) {
  let zip;
  try {
    zip = await JSZip.loadAsync(file);
  } catch {
    throw importErr(
      "Could not read the zip file. Please make sure it's a valid data export."
    );
  }

  const entries = Object.values(zip.files).filter((f) => !f.dir);

  const named = entries.find((f) => f.name.endsWith("conversations.json"));
  if (named) {
    return named.async("blob");
  }

  // Fallback: among .json entries, pick the largest whose top-level
  // parsed value is an array. Sort by uncompressed size descending so we
  // parse the most likely candidate first.
  const jsonEntries = entries
    .filter((f) => f.name.toLowerCase().endsWith(".json"))
    .sort((a, b) => {
      const sa = (a._data && a._data.uncompressedSize) || 0;
      const sb = (b._data && b._data.uncompressedSize) || 0;
      return sb - sa;
    });

  for (const entry of jsonEntries) {
    try {
      const text = await entry.async("string");
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) {
        return new Blob([text], { type: "application/json" });
      }
    } catch {
      // Not valid JSON or not an array — keep looking.
    }
  }

  throw importErr(
    "Could not find conversations.json in the zip archive. Please upload the original data export."
  );
}

export default function ImportData({ buttonStyle: customButtonStyle, buttonLabel, buttonHoverStyle, onProfileUpdateStarted, inline }) {
  const btnStyle = customButtonStyle || ghostBtnStyle;
  const [hovered, setHovered] = useState(false);
  const [showImportDialog, setShowImportDialog] = useState(false);
  const [importFiles, setImportFiles] = useState(null);
  const [importType, setImportType] = useState("separate_nodes");
  const [dateOrdering, setDateOrdering] = useState("modified");
  const [importing, setImporting] = useState(false);
  const [importStage, setImportStage] = useState(null);
  const [importPrivacy, setImportPrivacy] = useState("private");
  const [importAiUsage, setImportAiUsage] = useState("none");
  const [error, setError] = useState("");

  const [showTwitterImportDialog, setShowTwitterImportDialog] = useState(false);
  const [twitterImportData, setTwitterImportData] = useState(null);
  const [includeReplies, setIncludeReplies] = useState(false);

  const [showClaudeImportDialog, setShowClaudeImportDialog] = useState(false);
  const [claudeImportData, setClaudeImportData] = useState(null);

  const [showChatGPTImportDialog, setShowChatGPTImportDialog] = useState(false);
  const [chatGPTImportData, setChatGPTImportData] = useState(null);

  const [showPicker, setShowPicker] = useState(false);

  // Restore-or-skip prompt shown when a confirm collides with content
  // the user previously deleted: { count, retry }. `retry` re-runs the
  // confirm with on_deleted set to the user's choice.
  const [deletedPrompt, setDeletedPrompt] = useState(null);

  // Confirm-response stats ({ created, skipped, restored, ... }) shown
  // as a summary after the import finishes; dismissing it reloads the
  // page so the new nodes appear.
  const [importResult, setImportResult] = useState(null);

  // Returns true when the error is the backend's 409 "this import
  // matches soft-deleted nodes" conflict, in which case the prompt is
  // shown instead of an error message.
  const handleDeletedConflict = (err, retry) => {
    const data = err.response?.data;
    if (err.response?.status === 409 && data?.error === "deleted_content_matches") {
      setDeletedPrompt({ count: data.deleted_matches, retry });
      setImporting(false);
      setImportStage(null);
      return true;
    }
    return false;
  };

  // Close picker dialog on Escape
  useEffect(() => {
    if (!showPicker) return;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") setShowPicker(false);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [showPicker]);

  const handleImportFile = (event) => {
    const file = event.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("zip_file", file);

    setImporting(true);
    setImportStage("analyzing");
    setShowPicker(false);
    api.post("/import/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" }
    })
      .then((response) => {
        setImportFiles(response.data);
        setShowImportDialog(true);
        setImporting(false);
        setImportStage(null);
      })
      .catch((err) => {
        console.error("Error analyzing import file:", err);
        setError(err.response?.data?.error || "Error analyzing import file. Please try again.");
        setImporting(false);
        setImportStage(null);
      });
  };

  const handleConfirmImport = (onDeleted) => {
    if (!importFiles) return;

    setImporting(true);
    setImportStage("importing");
    api.post("/import/confirm", {
      files: importFiles.files,
      import_type: importType,
      date_ordering: dateOrdering,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage,
      ...(onDeleted ? { on_deleted: onDeleted } : {})
    })
      .then((response) => {
        setShowImportDialog(false);
        setImportFiles(null);
        setImporting(false);
        setImportStage(null);
        setError("");
        if (response.data.profile_update_task_id && onProfileUpdateStarted) {
          onProfileUpdateStarted(response.data.profile_update_task_id);
        }
        setImportResult(response.data);
      })
      .catch((err) => {
        if (handleDeletedConflict(err, handleConfirmImport)) return;
        console.error("Error importing data:", err);
        setError(err.response?.data?.error || "Error importing data. Please try again.");
        setImporting(false);
        setImportStage(null);
      });
  };

  const handleCancelImport = () => {
    setShowImportDialog(false);
    setImportFiles(null);
  };

  const handleTwitterImportFile = (event) => {
    const file = event.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("zip_file", file);

    setImporting(true);
    setImportStage("analyzing");
    setShowPicker(false);
    api.post("/import/twitter/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" }
    })
      .then((response) => {
        setTwitterImportData(response.data);
        setShowTwitterImportDialog(true);
        setImporting(false);
        setImportStage(null);
      })
      .catch((err) => {
        console.error("Error analyzing Twitter import:", err);
        setError(err.response?.data?.error || "Error analyzing Twitter export. Please try again.");
        setImporting(false);
        setImportStage(null);
      });

    event.target.value = "";
  };

  const handleConfirmTwitterImport = (onDeleted) => {
    if (!twitterImportData) return;

    setImporting(true);
    setImportStage("importing");
    api.post("/import/twitter/confirm", {
      tweets: twitterImportData.tweets,
      import_type: importType,
      include_replies: includeReplies,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage,
      ...(onDeleted ? { on_deleted: onDeleted } : {})
    })
      .then((response) => {
        setShowTwitterImportDialog(false);
        setTwitterImportData(null);
        setImporting(false);
        setImportStage(null);
        setError("");
        if (response.data.profile_update_task_id && onProfileUpdateStarted) {
          onProfileUpdateStarted(response.data.profile_update_task_id);
        }
        setImportResult(response.data);
      })
      .catch((err) => {
        if (handleDeletedConflict(err, handleConfirmTwitterImport)) return;
        console.error("Error importing Twitter data:", err);
        setError(err.response?.data?.error || "Error importing Twitter data. Please try again.");
        setImporting(false);
        setImportStage(null);
      });
  };

  const handleCancelTwitterImport = () => {
    setShowTwitterImportDialog(false);
    setTwitterImportData(null);
  };

  const handleClaudeImportFile = async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;

    setImporting(true);
    setImportStage("extracting");
    setShowPicker(false);

    let conversationsBlob;
    try {
      conversationsBlob = await extractConversationsBlob(file);
    } catch (err) {
      console.error("Error reading Claude export zip:", err);
      setError(
        err && err.userMessage
          ? err.userMessage
          : "Could not read the zip file. Please make sure it's a valid Claude data export."
      );
      setImporting(false);
      setImportStage(null);
      return;
    }

    const formData = new FormData();
    formData.append("conversations_file", conversationsBlob, "conversations.json");

    setImportStage("analyzing");
    api.post("/import/claude/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" }
    })
      .then((response) => {
        setClaudeImportData(response.data);
        setShowClaudeImportDialog(true);
        setImporting(false);
        setImportStage(null);
      })
      .catch((err) => {
        console.error("Error analyzing Claude import:", err);
        setError(err.response?.data?.error || "Error analyzing Claude export. Please try again.");
        setImporting(false);
        setImportStage(null);
      });
  };

  const handleConfirmClaudeImport = (onDeleted) => {
    if (!claudeImportData) return;

    setImporting(true);
    setImportStage("importing");
    api.post("/import/claude/confirm", {
      conversations: claudeImportData.conversations,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage,
      ...(onDeleted ? { on_deleted: onDeleted } : {})
    })
      .then((response) => {
        setShowClaudeImportDialog(false);
        setClaudeImportData(null);
        setImporting(false);
        setImportStage(null);
        setError("");
        if (response.data.profile_update_task_id && onProfileUpdateStarted) {
          onProfileUpdateStarted(response.data.profile_update_task_id);
        }
        setImportResult(response.data);
      })
      .catch((err) => {
        if (handleDeletedConflict(err, handleConfirmClaudeImport)) return;
        console.error("Error importing Claude data:", err);
        setError(err.response?.data?.error || "Error importing Claude data. Please try again.");
        setImporting(false);
        setImportStage(null);
      });
  };

  const handleCancelClaudeImport = () => {
    setShowClaudeImportDialog(false);
    setClaudeImportData(null);
  };

  const handleChatGPTImportFile = async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;

    setImporting(true);
    setImportStage("extracting");
    setShowPicker(false);

    let conversationsBlob;
    try {
      conversationsBlob = await extractConversationsBlob(file);
    } catch (err) {
      console.error("Error reading ChatGPT export zip:", err);
      setError(
        err && err.userMessage
          ? err.userMessage
          : "Could not read the zip file. Please make sure it's a valid ChatGPT data export."
      );
      setImporting(false);
      setImportStage(null);
      return;
    }

    const formData = new FormData();
    formData.append("conversations_file", conversationsBlob, "conversations.json");

    setImportStage("analyzing");
    api.post("/import/chatgpt/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" }
    })
      .then((response) => {
        setChatGPTImportData(response.data);
        setShowChatGPTImportDialog(true);
        setImporting(false);
        setImportStage(null);
      })
      .catch((err) => {
        console.error("Error analyzing ChatGPT import:", err);
        const status = err.response?.status;
        const data = err.response?.data;
        const backendMsg = data?.error || data?.details;
        let msg;
        if (backendMsg) {
          msg = data?.details && data?.error
            ? `${data.error}: ${data.details}`
            : backendMsg;
        } else if (status === 413) {
          msg = "conversations.json is too large to upload. Please contact support.";
        } else if (status) {
          msg = `Error analyzing ChatGPT export (HTTP ${status}). Please try again.`;
        } else {
          msg = "Error analyzing ChatGPT export. The request did not reach the server — check your connection.";
        }
        setError(msg);
        setImporting(false);
        setImportStage(null);
      });
  };

  const handleConfirmChatGPTImport = (onDeleted) => {
    if (!chatGPTImportData) return;

    setImporting(true);
    setImportStage("importing");
    api.post("/import/chatgpt/confirm", {
      conversations: chatGPTImportData.conversations,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage,
      ...(onDeleted ? { on_deleted: onDeleted } : {})
    })
      .then((response) => {
        setShowChatGPTImportDialog(false);
        setChatGPTImportData(null);
        setImporting(false);
        setImportStage(null);
        setError("");
        if (response.data.profile_update_task_id && onProfileUpdateStarted) {
          onProfileUpdateStarted(response.data.profile_update_task_id);
        }
        setImportResult(response.data);
      })
      .catch((err) => {
        if (handleDeletedConflict(err, handleConfirmChatGPTImport)) return;
        console.error("Error importing ChatGPT data:", err);
        setError(err.response?.data?.error || "Error importing ChatGPT data. Please try again.");
        setImporting(false);
        setImportStage(null);
      });
  };

  const handleCancelChatGPTImport = () => {
    setShowChatGPTImportDialog(false);
    setChatGPTImportData(null);
  };

  return (
    <div>
      {error && <div style={{ color: "var(--accent)", marginBottom: "0.8rem", fontSize: "0.88rem" }}>{error}</div>}

      {/* Post-import summary: what was actually imported vs skipped */}
      {importResult && (
        <div style={{
          marginTop: "20px",
          padding: "2rem",
          backgroundColor: "var(--bg-card)",
          borderRadius: "10px",
          border: "1px solid var(--border)"
        }}>
          <h3 style={{ fontFamily: "var(--serif)", fontWeight: 300, color: "var(--text-primary)", margin: "0 0 12px 0" }}>
            Import Finished
          </h3>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            <strong style={{ color: "var(--text-primary)" }}>{importResult.created}</strong>{" "}
            new node{importResult.created !== 1 ? "s" : ""} imported
            {importResult.restored > 0 && (
              <>
                {", "}
                <strong style={{ color: "var(--text-primary)" }}>{importResult.restored}</strong>{" "}
                restored from deleted
              </>
            )}
            {importResult.skipped > 0 && (
              <>
                {", "}
                <strong style={{ color: "var(--text-primary)" }}>{importResult.skipped}</strong>{" "}
                skipped (already imported)
              </>
            )}
            .
          </p>
          {importResult.created === 0 && !importResult.restored && (
            <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              Everything in this archive was already imported — nothing new was added.
            </p>
          )}
          <div style={{ display: "flex", gap: "10px", marginTop: "15px" }}>
            <button
              onClick={() => window.location.reload()}
              style={primaryBtnStyle}
            >
              OK
            </button>
          </div>
        </div>
      )}

      {/* Restore-or-skip prompt for imports matching deleted content */}
      {deletedPrompt && (
        <div style={{
          marginTop: "20px",
          padding: "2rem",
          backgroundColor: "var(--bg-card)",
          borderRadius: "10px",
          border: "1px solid var(--border)"
        }}>
          <h3 style={{ fontFamily: "var(--serif)", fontWeight: 300, color: "var(--text-primary)", margin: "0 0 12px 0" }}>
            Previously Deleted Content Found
          </h3>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            <strong style={{ color: "var(--text-primary)" }}>{deletedPrompt.count}</strong>{" "}
            message{deletedPrompt.count !== 1 ? "s" : ""} in this import match
            content you previously deleted. Do you want to restore{" "}
            {deletedPrompt.count !== 1 ? "them" : "it"}, or keep{" "}
            {deletedPrompt.count !== 1 ? "them" : "it"} deleted?
          </p>
          <div style={{ display: "flex", gap: "10px", marginTop: "15px", flexWrap: "wrap" }}>
            <button
              onClick={() => {
                const retry = deletedPrompt.retry;
                setDeletedPrompt(null);
                retry("restore");
              }}
              style={primaryBtnStyle}
            >
              Restore deleted content
            </button>
            <button
              onClick={() => {
                const retry = deletedPrompt.retry;
                setDeletedPrompt(null);
                retry("skip");
              }}
              style={cancelBtnStyle}
            >
              Keep it deleted
            </button>
            <button
              onClick={() => setDeletedPrompt(null)}
              style={cancelBtnStyle}
            >
              Cancel import
            </button>
          </div>
        </div>
      )}

      {/* Shared import option labels */}
      {(() => {
        const importLabelStyle = {
          display: "block",
          padding: "14px 20px",
          cursor: importing ? "not-allowed" : "pointer",
          fontFamily: "var(--sans)",
          fontSize: "0.92rem",
          fontWeight: 300,
          color: "var(--text-secondary)",
          border: "1px solid var(--border)",
          borderRadius: "8px",
          textAlign: "center",
          transition: "border-color 0.3s ease",
          opacity: importing ? 0.6 : 1,
        };

        const importOptions = (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
            {importing ? (
              // Pre-dialog progress. While we extract the zip client-side and
              // upload the conversations blob for analysis, the buttons are
              // disabled and no dialog has appeared yet — without this the page
              // looks frozen (esp. large Claude/ChatGPT zips, where extraction
              // takes a few seconds). The .spin transform keeps animating on
              // the compositor thread even while extraction is busy.
              <div style={{ ...importLabelStyle, opacity: 1, cursor: "default", color: "var(--accent)" }}>
                <ImportSpinner stage={importStage} fallback="Working…" />
              </div>
            ) : (
              <>
                <label style={importLabelStyle}>
                  Import Claude
                  <input type="file" accept=".zip" onChange={handleClaudeImportFile} style={{ display: "none" }} />
                </label>
                <label style={importLabelStyle}>
                  Import ChatGPT
                  <input type="file" accept=".zip" onChange={handleChatGPTImportFile} style={{ display: "none" }} />
                </label>
                <label style={importLabelStyle}>
                  Import Markdown (e.g. Obsidian)
                  <input type="file" accept=".zip" onChange={handleImportFile} style={{ display: "none" }} />
                </label>
                <label style={importLabelStyle}>
                  Import Tweets
                  <input type="file" accept=".zip" onChange={handleTwitterImportFile} style={{ display: "none" }} />
                </label>
              </>
            )}
          </div>
        );

        if (inline) {
          // Render import options directly on the page
          return importOptions;
        }

        return (
          <>
            {/* Main Import Data button */}
            {!showImportDialog && !showTwitterImportDialog && !showClaudeImportDialog && !showChatGPTImportDialog && (
              <button
                onClick={() => setShowPicker(!showPicker)}
                onMouseEnter={() => setHovered(true)}
                onMouseLeave={() => setHovered(false)}
                disabled={importing}
                style={{
                  ...btnStyle,
                  ...(hovered && buttonHoverStyle ? buttonHoverStyle : {}),
                  cursor: importing ? "not-allowed" : "pointer",
                  opacity: importing ? 0.6 : 1,
                }}
              >
                {importing
                  ? <ImportSpinner stage={importStage} fallback="Analyzing…" />
                  : (buttonLabel || "Import Data")}
              </button>
            )}

            {/* Import type picker dialog - rendered via portal to escape overflow:hidden */}
            {showPicker && ReactDOM.createPortal(
              <div
                onClick={() => setShowPicker(false)}
                style={{
                  position: "fixed",
                  top: 0, left: 0, right: 0, bottom: 0,
                  backgroundColor: "rgba(5, 4, 3, 0.75)",
                  backdropFilter: "blur(10px)",
                  WebkitBackdropFilter: "blur(10px)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  zIndex: 2000,
                }}
              >
                <div
                  onClick={(e) => e.stopPropagation()}
                  style={{
                    background: "var(--bg-card)",
                    border: "1px solid var(--border)",
                    borderRadius: "12px",
                    padding: "2rem",
                    minWidth: "300px",
                    maxWidth: "90vw",
                    boxShadow: "0 24px 80px rgba(0,0,0,0.5)",
                  }}
                >
                  <h3 style={{
                    fontFamily: "var(--serif)",
                    fontWeight: 300,
                    fontSize: "1.2rem",
                    color: "var(--text-primary)",
                    margin: "0 0 1.2rem 0",
                    textAlign: "center",
                  }}>Import Data</h3>
                  {importOptions}
                </div>
              </div>,
              document.body
            )}
          </>
        );
      })()}

      {/* Markdown import confirmation dialog */}
      {showImportDialog && importFiles && (
        <div style={{
          marginTop: "20px",
          padding: "2rem",
          backgroundColor: "var(--bg-card)",
          borderRadius: "10px",
          border: "1px solid var(--border)"
        }}>
          <h3 style={{ fontFamily: "var(--serif)", fontWeight: 300, color: "var(--text-primary)", margin: "0 0 12px 0" }}>Confirm Import</h3>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Found <strong style={{ color: "var(--text-primary)" }}>{importFiles.total_files}</strong> .md file{importFiles.total_files !== 1 ? 's' : ''}
            ({importFiles.total_size.toLocaleString()} bytes)
          </p>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Estimated tokens: <strong style={{ color: "var(--text-primary)" }}>{importFiles.total_tokens.toLocaleString()}</strong>
          </p>

          <div style={{ marginTop: "15px", marginBottom: "15px" }}>
            <label style={{
              display: "block",
              marginBottom: "10px",
              fontFamily: "var(--sans)",
              fontWeight: 400,
              fontSize: "0.75rem",
              textTransform: "uppercase",
              letterSpacing: "0.12em",
              color: "var(--text-muted)"
            }}>
              Import Type
            </label>
            <label style={{ display: "block", marginBottom: "8px", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              <input
                type="radio"
                value="separate_nodes"
                checked={importType === "separate_nodes"}
                onChange={(e) => setImportType(e.target.value)}
                style={{ marginRight: "8px" }}
              />
              Import as separate top-level nodes (one thread per file)
            </label>
            <label style={{ display: "block", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              <input
                type="radio"
                value="single_thread"
                checked={importType === "single_thread"}
                onChange={(e) => setImportType(e.target.value)}
                style={{ marginRight: "8px" }}
              />
              Import as a single thread (all files connected sequentially)
            </label>
          </div>

          <div style={{ marginTop: "15px", marginBottom: "15px" }}>
            <label style={{
              display: "block",
              marginBottom: "10px",
              fontFamily: "var(--sans)",
              fontWeight: 400,
              fontSize: "0.75rem",
              textTransform: "uppercase",
              letterSpacing: "0.12em",
              color: "var(--text-muted)"
            }}>
              Date Ordering
            </label>
            <label style={{ display: "block", marginBottom: "8px", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              <input
                type="radio"
                value="modified"
                checked={dateOrdering === "modified"}
                onChange={(e) => setDateOrdering(e.target.value)}
                style={{ marginRight: "8px" }}
              />
              Order by modification date
            </label>
            <label style={{ display: "block", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              <input
                type="radio"
                value="created"
                checked={dateOrdering === "created"}
                onChange={(e) => setDateOrdering(e.target.value)}
                style={{ marginRight: "8px" }}
              />
              Order by creation date
            </label>
          </div>

          <PrivacySelector
            privacyLevel={importPrivacy}
            aiUsage={importAiUsage}
            onPrivacyChange={setImportPrivacy}
            onAIUsageChange={setImportAiUsage}
          />

          <div style={{ display: "flex", gap: "10px", marginTop: "15px" }}>
            <button
              onClick={() => handleConfirmImport()}
              disabled={importing}
              style={{
                ...primaryBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              {importing ? <ImportSpinner stage={importStage} fallback="Importing…" /> : "Confirm Import"}
            </button>
            <button
              onClick={handleCancelImport}
              disabled={importing}
              style={{
                ...cancelBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Claude import confirmation dialog */}
      {showClaudeImportDialog && claudeImportData && (
        <div style={{
          marginTop: "20px",
          padding: "2rem",
          backgroundColor: "var(--bg-card)",
          borderRadius: "10px",
          border: "1px solid var(--border)"
        }}>
          <h3 style={{ fontFamily: "var(--serif)", fontWeight: 300, color: "var(--text-primary)", margin: "0 0 12px 0" }}>Confirm Claude Import</h3>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Found <strong style={{ color: "var(--text-primary)" }}>{claudeImportData.total_conversations}</strong> conversation{claudeImportData.total_conversations !== 1 ? 's' : ''} with{" "}
            <strong style={{ color: "var(--text-primary)" }}>{claudeImportData.total_messages}</strong> messages
          </p>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Estimated tokens: <strong style={{ color: "var(--text-primary)" }}>{claudeImportData.total_tokens.toLocaleString()}</strong>
          </p>
          <p style={{ color: "var(--text-muted)", fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.85rem" }}>
            Each conversation will be imported as a separate thread.
          </p>

          <PrivacySelector
            privacyLevel={importPrivacy}
            aiUsage={importAiUsage}
            onPrivacyChange={setImportPrivacy}
            onAIUsageChange={setImportAiUsage}
          />

          <div style={{ display: "flex", gap: "10px", marginTop: "15px" }}>
            <button
              onClick={() => handleConfirmClaudeImport()}
              disabled={importing}
              style={{
                ...primaryBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              {importing ? <ImportSpinner stage={importStage} fallback="Importing…" /> : "Confirm Import"}
            </button>
            <button
              onClick={handleCancelClaudeImport}
              disabled={importing}
              style={{
                ...cancelBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* ChatGPT import confirmation dialog */}
      {showChatGPTImportDialog && chatGPTImportData && (
        <div style={{
          marginTop: "20px",
          padding: "2rem",
          backgroundColor: "var(--bg-card)",
          borderRadius: "10px",
          border: "1px solid var(--border)"
        }}>
          <h3 style={{ fontFamily: "var(--serif)", fontWeight: 300, color: "var(--text-primary)", margin: "0 0 12px 0" }}>Confirm ChatGPT Import</h3>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Found <strong style={{ color: "var(--text-primary)" }}>{chatGPTImportData.total_conversations}</strong> conversation{chatGPTImportData.total_conversations !== 1 ? 's' : ''} with{" "}
            <strong style={{ color: "var(--text-primary)" }}>{chatGPTImportData.total_messages}</strong> messages
          </p>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Estimated tokens: <strong style={{ color: "var(--text-primary)" }}>{chatGPTImportData.total_tokens.toLocaleString()}</strong>
          </p>
          <p style={{ color: "var(--text-muted)", fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.85rem" }}>
            Each conversation will be imported as a separate thread.
          </p>

          <PrivacySelector
            privacyLevel={importPrivacy}
            aiUsage={importAiUsage}
            onPrivacyChange={setImportPrivacy}
            onAIUsageChange={setImportAiUsage}
          />

          <div style={{ display: "flex", gap: "10px", marginTop: "15px" }}>
            <button
              onClick={() => handleConfirmChatGPTImport()}
              disabled={importing}
              style={{
                ...primaryBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              {importing ? <ImportSpinner stage={importStage} fallback="Importing…" /> : "Confirm Import"}
            </button>
            <button
              onClick={handleCancelChatGPTImport}
              disabled={importing}
              style={{
                ...cancelBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Twitter import confirmation dialog */}
      {showTwitterImportDialog && twitterImportData && (
        <div style={{
          marginTop: "20px",
          padding: "2rem",
          backgroundColor: "var(--bg-card)",
          borderRadius: "10px",
          border: "1px solid var(--border)"
        }}>
          <h3 style={{ fontFamily: "var(--serif)", fontWeight: 300, color: "var(--text-primary)", margin: "0 0 12px 0" }}>Confirm Twitter Import</h3>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Found <strong style={{ color: "var(--text-primary)" }}>{twitterImportData.total_tweets}</strong> tweets
            ({twitterImportData.original_count} original, {twitterImportData.reply_count} replies)
          </p>
          {twitterImportData.skipped_retweets > 0 && (
            <p style={{ color: "var(--text-muted)", fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.85rem" }}>
              Skipped {twitterImportData.skipped_retweets} retweets
            </p>
          )}
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Estimated tokens: <strong style={{ color: "var(--text-primary)" }}>
              {includeReplies
                ? twitterImportData.total_tokens.toLocaleString()
                : twitterImportData.tweets
                    .filter(t => !t.is_reply)
                    .reduce((sum, t) => sum + t.token_count, 0)
                    .toLocaleString()
              }
            </strong>
            {" "}({includeReplies ? twitterImportData.total_tweets : twitterImportData.original_count} tweets)
          </p>

          <div style={{ marginTop: "15px", marginBottom: "15px" }}>
            <label style={{ display: "block", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              <input
                type="checkbox"
                checked={includeReplies}
                onChange={(e) => setIncludeReplies(e.target.checked)}
                style={{ marginRight: "8px" }}
              />
              Include replies ({twitterImportData.reply_count})
            </label>
          </div>

          <div style={{ marginTop: "15px", marginBottom: "15px" }}>
            <label style={{
              display: "block",
              marginBottom: "10px",
              fontFamily: "var(--sans)",
              fontWeight: 400,
              fontSize: "0.75rem",
              textTransform: "uppercase",
              letterSpacing: "0.12em",
              color: "var(--text-muted)"
            }}>
              Import Type
            </label>
            <label style={{ display: "block", marginBottom: "8px", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              <input
                type="radio"
                value="separate_nodes"
                checked={importType === "separate_nodes"}
                onChange={(e) => setImportType(e.target.value)}
                style={{ marginRight: "8px" }}
              />
              Import as separate top-level nodes (one node per tweet)
            </label>
            <label style={{ display: "block", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              <input
                type="radio"
                value="single_thread"
                checked={importType === "single_thread"}
                onChange={(e) => setImportType(e.target.value)}
                style={{ marginRight: "8px" }}
              />
              Import as a single thread (all tweets connected sequentially)
            </label>
          </div>

          <PrivacySelector
            privacyLevel={importPrivacy}
            aiUsage={importAiUsage}
            onPrivacyChange={setImportPrivacy}
            onAIUsageChange={setImportAiUsage}
          />

          <div style={{ display: "flex", gap: "10px", marginTop: "15px" }}>
            <button
              onClick={() => handleConfirmTwitterImport()}
              disabled={importing}
              style={{
                ...primaryBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              {importing ? <ImportSpinner stage={importStage} fallback="Importing…" /> : "Confirm Import"}
            </button>
            <button
              onClick={handleCancelTwitterImport}
              disabled={importing}
              style={{
                ...cancelBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
