import React, { useState, useEffect } from "react";
import ReactDOM from "react-dom";
import JSZip from "jszip";
import {
  ANALYZE_UPLOAD_TIMEOUT_MS,
  stripDocxMedia,
  FileReadError,
  FILE_READ_MESSAGE,
  describeAnalyzeError,
  localFileError,
} from "../utils/docxUpload";
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
  preparing: "Preparing…",
  uploading: "Uploading…",
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
function ImportSpinner({ stage, fallback, detail }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.4em" }}>
      <FaSpinner className="spin" aria-hidden="true" />
      {STAGE_LABELS[stage] || fallback}
      {detail && <span style={{ opacity: 0.75 }}>{detail}</span>}
    </span>
  );
}

// Centered modal overlay matching the import-picker pattern: portal to
// document.body, dimmed blurred backdrop, card with a quiet rise-in.
// Backdrop click and Escape both call onDismiss.
function ModalShell({ onDismiss, children, maxWidth = "440px" }) {
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") onDismiss();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onDismiss]);

  return ReactDOM.createPortal(
    <div
      onClick={onDismiss}
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
        animation: "loore-modal-fade 0.25s ease-out",
      }}
    >
      <style>{`
        @keyframes loore-modal-fade { from { opacity: 0; } }
        @keyframes loore-modal-rise {
          from { opacity: 0; transform: translateY(14px) scale(0.98); }
        }
      `}</style>
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-card)",
          border: "1px solid var(--border)",
          borderRadius: "12px",
          padding: "2.5rem",
          minWidth: "300px",
          maxWidth,
          width: "90vw",
          boxShadow: "0 24px 80px rgba(0,0,0,0.5)",
          maxHeight: "85vh",
          overflowY: "auto",
          animation: "loore-modal-rise 0.35s cubic-bezier(0.22, 1, 0.36, 1)",
        }}
      >
        {children}
      </div>
    </div>,
    document.body
  );
}

// Serif modal heading with a short amber hairline underneath.
function ModalTitle({ children }) {
  return (
    <>
      <h3 style={{
        fontFamily: "var(--serif)",
        fontWeight: 300,
        fontSize: "1.35rem",
        color: "var(--text-primary)",
        margin: 0,
      }}>{children}</h3>
      <div style={{
        width: "2rem",
        height: "1px",
        backgroundColor: "var(--accent)",
        opacity: 0.6,
        margin: "0.9rem 0 1.4rem",
      }} />
    </>
  );
}

// Extract the conversation files from a Claude/ChatGPT export zip in the
// browser, so the full (potentially multi-GB) export with images/audio
// never has to traverse the network. Returns a list of {blob, name}, one per
// file to upload; the server concatenates them in this order.
//
// Entry matching is intentionally flexible:
//   1. Prefer every file entry whose basename is exactly "conversations.json"
//      or a numbered shard "conversations-<digits>.json" (ChatGPT exports
//      are split this way), sorted by name. Other names such as
//      "sectioned_conversations.json" or "shared_conversations.json" and
//      macOS "__MACOSX/._conversations.json" files do not match.
//   2. Otherwise fall back to the largest .json entry whose parsed
//      top-level value is an array (the export's conversation list).
// Throws an Error with a `.userMessage` if no suitable entry is found or
// the zip cannot be read.
const CONVERSATION_FILE_RE = /(^|\/)conversations(-\d+)?\.json$/;

async function extractConversationFiles(file) {
  let zip;
  try {
    zip = await JSZip.loadAsync(file);
  } catch {
    throw importErr(
      "Could not read the zip file. Please make sure it's a valid data export."
    );
  }

  const entries = Object.values(zip.files).filter((f) => !f.dir);

  const named = entries
    .filter((f) => CONVERSATION_FILE_RE.test(f.name))
    .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  if (named.length) {
    return Promise.all(
      named.map(async (f) => ({
        blob: await f.async("blob"),
        name: f.name.split("/").pop(),
      }))
    );
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
        return [{
          blob: new Blob([text], { type: "application/json" }),
          name: "conversations.json",
        }];
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
  // {done, total} while a background import task runs (Twitter)
  const [importProgress, setImportProgress] = useState(null);
  const [uploadPercent, setUploadPercent] = useState(null);
  const uploadDetail =
    importStage === "uploading" && uploadPercent != null ? ` ${uploadPercent} %` : null;
  const [importPrivacy, setImportPrivacy] = useState("private");
  const [importAiUsage, setImportAiUsage] = useState("none");
  // "Whose writing is this?" for a PDF: null until the user picks
  // "mine" or "someone_else". No default: a wrong guess would feed
  // another author's text into the user's profile.
  const [whoseWriting, setWhoseWriting] = useState(null);
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

  // Confirm-response stats ({ created, skipped, restored, empty, ... }) shown
  // as a summary after the import finishes; dismissing it reloads the
  // page so the new nodes appear.
  const [importResult, setImportResult] = useState(null);

  // An analyzed PDF (always a single upload) asks whose writing it is.
  const pdfFile = importFiles?.files?.find((f) => f.source_format === "pdf") || null;
  const hasPdf = !!pdfFile;
  const savingReference = hasPdf && whoseWriting === "someone_else";

  // Backdrop/Escape dismiss for the confirm-dialog modals: ignored while
  // a request is in flight or while a prompt modal is stacked on top
  // (Escape would otherwise close both layers at once).
  const dismissGuard = (cancel) => () => {
    if (importing || deletedPrompt || importResult) return;
    cancel();
  };

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

  const handleImportFile = async (event) => {
    const picked = event.target.files[0];
    // Reset so picking the same file again after an error fires onChange
    event.target.value = "";
    if (!picked) return;

    setShowPicker(false);
    const localError = localFileError(picked);
    if (localError) {
      setError(localError);
      return;
    }
    setError("");
    setWhoseWriting(null);
    setImporting(true);
    setImportStage("preparing");
    setUploadPercent(null);
    // The server never reads pictures; leave them out of the upload.
    // Always upload an in-memory copy (a PDF or zip too): a file picked from
    // a cloud app on Android can fail when it is read again during upload.
    let file;
    try {
      file = await stripDocxMedia(picked);
    } catch (err) {
      console.error("Could not read the picked file:", err);
      setError(err instanceof FileReadError ? err.message : FILE_READ_MESSAGE);
      setImporting(false);
      setImportStage(null);
      setUploadPercent(null);
      return;
    }

    const formData = new FormData();
    formData.append("zip_file", file);

    setImportStage("uploading");
    setUploadPercent(0);
    api.post("/import/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" },
      timeout: ANALYZE_UPLOAD_TIMEOUT_MS,
      onUploadProgress: (e) => {
        if (!e.total) return;
        setUploadPercent(Math.min(100, Math.round((e.loaded / e.total) * 100)));
        if (e.loaded >= e.total) setImportStage("analyzing");
      },
    })
      .then((response) => {
        setImportFiles(response.data);
        setShowImportDialog(true);
        setImporting(false);
        setImportStage(null);
        setUploadPercent(null);
      })
      .catch((err) => {
        console.error("Error analyzing import file:", err);
        setError(describeAnalyzeError(err, picked));
        setImporting(false);
        setImportStage(null);
        setUploadPercent(null);
      });
  };

  const handleConfirmImport = (onDeleted) => {
    if (!importFiles) return;
    if (hasPdf && !whoseWriting) return;

    setImporting(true);
    setImportStage("importing");
    api.post("/import/confirm", {
      files: importFiles.files,
      import_type: importType,
      date_ordering: dateOrdering,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage,
      ...(hasPdf ? { whose_writing: whoseWriting } : {}),
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
    setWhoseWriting(null);
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
    setImportProgress(null);

    const finishWith = (result) => {
      setShowTwitterImportDialog(false);
      setTwitterImportData(null);
      setImporting(false);
      setImportStage(null);
      setImportProgress(null);
      setError("");
      if (result.profile_update_task_id && onProfileUpdateStarted) {
        onProfileUpdateStarted(result.profile_update_task_id);
      }
      setImportResult(result);
    };
    const failWith = (message) => {
      setError(message || "Error importing Twitter data. Please try again.");
      setImporting(false);
      setImportStage(null);
      setImportProgress(null);
    };

    // The confirm request only queues a Celery task (node creation for a
    // large archive takes minutes); progress and the result come from
    // polling /import/status until it completes.
    const poll = (taskId) => {
      api.get(`/import/status/${taskId}`)
        .then(({ data }) => {
          if (data.status === "completed") return finishWith(data.result || {});
          if (data.status === "failed") return failWith(data.error);
          if (data.status === "running") setImportProgress({ done: data.done, total: data.total });
          setTimeout(() => poll(taskId), 1500);
        })
        .catch((err) => {
          console.error("Error polling Twitter import status:", err);
          failWith("Lost track of the import — reload the page to check whether it finished.");
        });
    };

    api.post("/import/twitter/confirm", {
      import_token: twitterImportData.import_token,
      import_type: importType,
      include_replies: includeReplies,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage,
      ...(onDeleted ? { on_deleted: onDeleted } : {})
    })
      .then((response) => {
        setImportProgress({ done: 0, total: response.data.total });
        poll(response.data.task_id);
      })
      .catch((err) => {
        if (handleDeletedConflict(err, handleConfirmTwitterImport)) return;
        console.error("Error importing Twitter data:", err);
        failWith(err.response?.data?.error);
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

    let conversationFiles;
    try {
      conversationFiles = await extractConversationFiles(file);
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
    conversationFiles.forEach((f) => formData.append("conversations_file", f.blob, f.name));

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

    let conversationFiles;
    try {
      conversationFiles = await extractConversationFiles(file);
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
    conversationFiles.forEach((f) => formData.append("conversations_file", f.blob, f.name));

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

      {/* A PDF saved as someone else's writing: a Reference, not entries */}
      {importResult && importResult.saved_as === "reference" && (
        <ModalShell onDismiss={() => window.location.reload()}>
          <ModalTitle>{importResult.created ? "Saved as a Reference" : "Already in your References"}</ModalTitle>
          <p style={{ fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem", color: "var(--text-secondary)", margin: "0 0 1.4rem" }}>
            {importResult.created
              ? "The PDF is saved in your References. It is searchable, you can listen to it on its page, and it stays out of your profile."
              : "This PDF was saved as a Reference before; nothing new was added."}
            {importResult.truncated && " It was longer than a Reference holds, so only its beginning was saved."}
          </p>
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap" }}>
            <button
              onClick={() => { window.location.href = `/references/${importResult.reference_id}`; }}
              style={primaryBtnStyle}
            >
              Open the Reference
            </button>
            <button onClick={() => window.location.reload()} style={cancelBtnStyle}>
              OK
            </button>
          </div>
        </ModalShell>
      )}

      {/* Post-import summary: what was actually imported vs skipped */}
      {importResult && importResult.saved_as !== "reference" && (
        <ModalShell onDismiss={() => window.location.reload()}>
          <ModalTitle>Import Finished</ModalTitle>
          <div style={{ display: "flex", gap: "2.4rem", marginBottom: "1.3rem" }}>
            {[
              { label: "Imported", value: importResult.created, highlight: importResult.created > 0 },
              { label: "Restored", value: importResult.restored || 0, highlight: importResult.restored > 0 },
              { label: "Updated", value: importResult.updated || 0, highlight: importResult.updated > 0 },
              { label: "Skipped", value: importResult.skipped || 0, highlight: false },
              { label: "No text", value: importResult.empty || 0, highlight: false },
            ].filter((s) => s.label === "Imported" || s.value > 0).map((s) => (
              <div key={s.label}>
                <div style={{
                  fontFamily: "var(--serif)",
                  fontWeight: 300,
                  fontSize: "2.2rem",
                  lineHeight: 1.1,
                  color: s.highlight ? "var(--accent)" : "var(--text-primary)",
                }}>{s.value}</div>
                <div style={{
                  fontFamily: "var(--sans)",
                  fontWeight: 400,
                  fontSize: "0.7rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.14em",
                  color: "var(--text-muted)",
                  marginTop: "0.3rem",
                }}>{s.label}</div>
              </div>
            ))}
          </div>
          {importResult.created === 0 && !importResult.restored && !importResult.updated ? (
            <p style={{ fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem", color: "var(--text-secondary)", margin: "0 0 1.4rem" }}>
              Everything in this archive was already imported — nothing new was added.
            </p>
          ) : (
            <>
              {importResult.updated > 0 && (
                <p style={{ fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.88rem", color: "var(--text-secondary)", margin: "0 0 1.4rem" }}>
                  Updated items were already imported; their privacy and
                  AI-usage settings now match this import.
                </p>
              )}
              {importResult.skipped > 0 && (
                <p style={{ fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.88rem", color: "var(--text-secondary)", margin: "0 0 1.4rem" }}>
                  Skipped items were already imported and left untouched.
                </p>
              )}
              {importResult.empty > 0 && (
                <p style={{ fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.88rem", color: "var(--text-secondary)", margin: "0 0 1.4rem" }}>
                  Posts with no text — usually media-only tweets — were not
                  imported. There was nothing to write.
                </p>
              )}
            </>
          )}
          <button
            onClick={() => window.location.reload()}
            style={primaryBtnStyle}
          >
            OK
          </button>
        </ModalShell>
      )}

      {/* Restore-or-skip prompt for imports matching deleted content */}
      {deletedPrompt && (
        <ModalShell onDismiss={() => setDeletedPrompt(null)}>
          <ModalTitle>Previously Deleted Content</ModalTitle>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300, margin: "0 0 1.4rem" }}>
            <strong style={{ color: "var(--text-primary)" }}>{deletedPrompt.count}</strong>{" "}
            message{deletedPrompt.count !== 1 ? "s" : ""} in this import{" "}
            {deletedPrompt.count !== 1 ? "match" : "matches"} content you
            previously deleted. Restore{" "}
            {deletedPrompt.count !== 1 ? "them" : "it"}, or keep{" "}
            {deletedPrompt.count !== 1 ? "them" : "it"} deleted?
          </p>
          <div style={{ display: "flex", gap: "10px", flexWrap: "wrap", alignItems: "center" }}>
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
              style={{ ...ghostBtnStyle, border: "none", color: "var(--text-muted)" }}
            >
              Cancel import
            </button>
          </div>
        </ModalShell>
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
                <ImportSpinner stage={importStage} fallback="Working…" detail={uploadDetail} />
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
                  Import Markdown, Word (.docx) or PDF
                  <input type="file" accept=".zip,.docx,.doc,.pdf" onChange={handleImportFile} style={{ display: "none" }} />
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
                // Keyboard focus shows the hover style: the global
                // `button { outline: none }` leaves a borderless button
                // (the Welcome page's) with no focus indication otherwise.
                onFocus={(e) => { if (e.target.matches(':focus-visible')) setHovered(true); }}
                onBlur={() => setHovered(false)}
                disabled={importing}
                style={{
                  ...btnStyle,
                  ...(hovered && buttonHoverStyle ? buttonHoverStyle : {}),
                  cursor: importing ? "not-allowed" : "pointer",
                  opacity: importing ? 0.6 : 1,
                }}
              >
                {importing
                  ? <ImportSpinner stage={importStage} fallback="Analyzing…" detail={uploadDetail} />
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
        <ModalShell onDismiss={dismissGuard(handleCancelImport)} maxWidth="520px">
          <ModalTitle>Confirm Import</ModalTitle>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Found <strong style={{ color: "var(--text-primary)" }}>{importFiles.total_files}</strong> document{importFiles.total_files !== 1 ? 's' : ''}
            ({importFiles.total_size.toLocaleString()} bytes)
          </p>
          <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
            Estimated tokens: <strong style={{ color: "var(--text-primary)" }}>{importFiles.total_tokens.toLocaleString()}</strong>
          </p>
          {importFiles.errors && importFiles.errors.length > 0 && (
            <div style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              <p>These files could not be imported and are left out:</p>
              <ul>
                {/* One line per message, so many PDFs in one zip don't
                    repeat the same sentence */}
                {Object.entries(importFiles.errors.reduce((byMessage, e) => {
                  (byMessage[e.error] = byMessage[e.error] || []).push(e.name);
                  return byMessage;
                }, {})).map(([message, names]) => (
                  <li key={message}>{names.join(", ")}: {message}</li>
                ))}
              </ul>
            </div>
          )}

          {hasPdf && (
            <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
              {pdfFile.pages} page{pdfFile.pages !== 1 ? "s" : ""}
              {pdfFile.chapters > 0 ? `, ${pdfFile.chapters} chapters from its bookmarks` : ""}
            </p>
          )}

          {hasPdf && (
            <div role="radiogroup" aria-labelledby="whose-writing-label" style={{ marginTop: "15px", marginBottom: "15px" }}>
              <div id="whose-writing-label" style={{
                marginBottom: "10px",
                fontFamily: "var(--sans)",
                fontWeight: 400,
                fontSize: "0.75rem",
                textTransform: "uppercase",
                letterSpacing: "0.12em",
                color: "var(--text-muted)"
              }}>
                Whose writing is this?
              </div>
              <label style={{ display: "block", marginBottom: "8px", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
                <input
                  type="radio"
                  name="whose_writing"
                  value="mine"
                  checked={whoseWriting === "mine"}
                  onChange={(e) => setWhoseWriting(e.target.value)}
                  style={{ marginRight: "8px" }}
                />
                Mine: import it as my own entries
              </label>
              <label style={{ display: "block", cursor: "pointer", color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
                <input
                  type="radio"
                  name="whose_writing"
                  value="someone_else"
                  checked={whoseWriting === "someone_else"}
                  onChange={(e) => setWhoseWriting(e.target.value)}
                  style={{ marginRight: "8px" }}
                />
                Someone else's: save it as a Reference
              </label>
              {savingReference && (
                <p style={{ color: "var(--text-muted)", fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.85rem", margin: "10px 0 0" }}>
                  A Reference is searchable and you can listen to it, chapter
                  by chapter. It isn't your writing, so it stays out of your
                  profile.
                  {pdfFile.reference?.truncated && (
                    ` This PDF is longer than a Reference holds: only the first ${(pdfFile.reference.char_cap || 0).toLocaleString()} characters are saved.`
                  )}
                </p>
              )}
            </div>
          )}

          {!savingReference && (<>
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
          </>)}

          <div style={{ display: "flex", gap: "10px", marginTop: "15px" }}>
            {(() => {
              // A PDF needs an answer first (no default, see whoseWriting)
              const confirmDisabled = importing || (hasPdf && !whoseWriting);
              return (
                <button
                  onClick={() => handleConfirmImport()}
                  disabled={confirmDisabled}
                  style={{
                    ...primaryBtnStyle,
                    cursor: confirmDisabled ? "not-allowed" : "pointer",
                    opacity: confirmDisabled ? 0.6 : 1
                  }}
                >
                  {importing
                    ? <ImportSpinner stage={importStage} fallback="Importing…" />
                    : (savingReference ? "Save as Reference" : "Confirm Import")}
                </button>
              );
            })()}
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
        </ModalShell>
      )}

      {/* Claude import confirmation dialog */}
      {showClaudeImportDialog && claudeImportData && (
        <ModalShell onDismiss={dismissGuard(handleCancelClaudeImport)} maxWidth="520px">
          <ModalTitle>Confirm Claude Import</ModalTitle>
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
        </ModalShell>
      )}

      {/* ChatGPT import confirmation dialog */}
      {showChatGPTImportDialog && chatGPTImportData && (
        <ModalShell onDismiss={dismissGuard(handleCancelChatGPTImport)} maxWidth="520px">
          <ModalTitle>Confirm ChatGPT Import</ModalTitle>
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
        </ModalShell>
      )}

      {/* Twitter import confirmation dialog */}
      {showTwitterImportDialog && twitterImportData && (
        <ModalShell onDismiss={dismissGuard(handleCancelTwitterImport)} maxWidth="520px">
          <ModalTitle>Confirm Twitter Import</ModalTitle>
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
              {(includeReplies
                ? twitterImportData.total_tokens
                : twitterImportData.original_tokens
              ).toLocaleString()}
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
              {importing
                ? <ImportSpinner
                    stage={importStage}
                    fallback="Importing…"
                    detail={importProgress && importProgress.total
                      ? `${importProgress.done.toLocaleString()} / ${importProgress.total.toLocaleString()}`
                      : null}
                  />
                : "Confirm Import"}
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
        </ModalShell>
      )}
    </div>
  );
}
