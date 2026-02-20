import React, { useState, useEffect } from "react";
import ReactDOM from "react-dom";
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

export default function ImportData({ buttonStyle: customButtonStyle, buttonLabel, buttonHoverStyle, onProfileUpdateStarted, inline }) {
  const btnStyle = customButtonStyle || ghostBtnStyle;
  const [hovered, setHovered] = useState(false);
  const [showImportDialog, setShowImportDialog] = useState(false);
  const [importFiles, setImportFiles] = useState(null);
  const [importType, setImportType] = useState("separate_nodes");
  const [dateOrdering, setDateOrdering] = useState("modified");
  const [importing, setImporting] = useState(false);
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
    setShowPicker(false);
    api.post("/import/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" }
    })
      .then((response) => {
        setImportFiles(response.data);
        setShowImportDialog(true);
        setImporting(false);
      })
      .catch((err) => {
        console.error("Error analyzing import file:", err);
        setError(err.response?.data?.error || "Error analyzing import file. Please try again.");
        setImporting(false);
      });
  };

  const handleConfirmImport = () => {
    if (!importFiles) return;

    setImporting(true);
    api.post("/import/confirm", {
      files: importFiles.files,
      import_type: importType,
      date_ordering: dateOrdering,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage
    })
      .then((response) => {
        setShowImportDialog(false);
        setImportFiles(null);
        setImporting(false);
        setError("");
        if (response.data.profile_update_task_id && onProfileUpdateStarted) {
          onProfileUpdateStarted(response.data.profile_update_task_id);
        }
        window.location.reload();
      })
      .catch((err) => {
        console.error("Error importing data:", err);
        setError(err.response?.data?.error || "Error importing data. Please try again.");
        setImporting(false);
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
    setShowPicker(false);
    api.post("/import/twitter/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" }
    })
      .then((response) => {
        setTwitterImportData(response.data);
        setShowTwitterImportDialog(true);
        setImporting(false);
      })
      .catch((err) => {
        console.error("Error analyzing Twitter import:", err);
        setError(err.response?.data?.error || "Error analyzing Twitter export. Please try again.");
        setImporting(false);
      });

    event.target.value = "";
  };

  const handleConfirmTwitterImport = () => {
    if (!twitterImportData) return;

    setImporting(true);
    api.post("/import/twitter/confirm", {
      tweets: twitterImportData.tweets,
      import_type: importType,
      include_replies: includeReplies,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage
    })
      .then((response) => {
        setShowTwitterImportDialog(false);
        setTwitterImportData(null);
        setImporting(false);
        setError("");
        if (response.data.profile_update_task_id && onProfileUpdateStarted) {
          onProfileUpdateStarted(response.data.profile_update_task_id);
        }
        window.location.reload();
      })
      .catch((err) => {
        console.error("Error importing Twitter data:", err);
        setError(err.response?.data?.error || "Error importing Twitter data. Please try again.");
        setImporting(false);
      });
  };

  const handleCancelTwitterImport = () => {
    setShowTwitterImportDialog(false);
    setTwitterImportData(null);
  };

  const handleClaudeImportFile = (event) => {
    const file = event.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("zip_file", file);

    setImporting(true);
    setShowPicker(false);
    api.post("/import/claude/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" }
    })
      .then((response) => {
        setClaudeImportData(response.data);
        setShowClaudeImportDialog(true);
        setImporting(false);
      })
      .catch((err) => {
        console.error("Error analyzing Claude import:", err);
        setError(err.response?.data?.error || "Error analyzing Claude export. Please try again.");
        setImporting(false);
      });

    event.target.value = "";
  };

  const handleConfirmClaudeImport = () => {
    if (!claudeImportData) return;

    setImporting(true);
    api.post("/import/claude/confirm", {
      conversations: claudeImportData.conversations,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage
    })
      .then((response) => {
        setShowClaudeImportDialog(false);
        setClaudeImportData(null);
        setImporting(false);
        setError("");
        if (response.data.profile_update_task_id && onProfileUpdateStarted) {
          onProfileUpdateStarted(response.data.profile_update_task_id);
        }
        window.location.reload();
      })
      .catch((err) => {
        console.error("Error importing Claude data:", err);
        setError(err.response?.data?.error || "Error importing Claude data. Please try again.");
        setImporting(false);
      });
  };

  const handleCancelClaudeImport = () => {
    setShowClaudeImportDialog(false);
    setClaudeImportData(null);
  };

  const handleChatGPTImportFile = (event) => {
    const file = event.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("zip_file", file);

    setImporting(true);
    setShowPicker(false);
    api.post("/import/chatgpt/analyze", formData, {
      headers: { "Content-Type": "multipart/form-data" }
    })
      .then((response) => {
        setChatGPTImportData(response.data);
        setShowChatGPTImportDialog(true);
        setImporting(false);
      })
      .catch((err) => {
        console.error("Error analyzing ChatGPT import:", err);
        setError(err.response?.data?.error || "Error analyzing ChatGPT export. Please try again.");
        setImporting(false);
      });

    event.target.value = "";
  };

  const handleConfirmChatGPTImport = () => {
    if (!chatGPTImportData) return;

    setImporting(true);
    api.post("/import/chatgpt/confirm", {
      conversations: chatGPTImportData.conversations,
      privacy_level: importPrivacy,
      ai_usage: importAiUsage
    })
      .then((response) => {
        setShowChatGPTImportDialog(false);
        setChatGPTImportData(null);
        setImporting(false);
        setError("");
        if (response.data.profile_update_task_id && onProfileUpdateStarted) {
          onProfileUpdateStarted(response.data.profile_update_task_id);
        }
        window.location.reload();
      })
      .catch((err) => {
        console.error("Error importing ChatGPT data:", err);
        setError(err.response?.data?.error || "Error importing ChatGPT data. Please try again.");
        setImporting(false);
      });
  };

  const handleCancelChatGPTImport = () => {
    setShowChatGPTImportDialog(false);
    setChatGPTImportData(null);
  };

  return (
    <div>
      {error && <div style={{ color: "var(--accent)", marginBottom: "0.8rem", fontSize: "0.88rem" }}>{error}</div>}

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
            <label style={importLabelStyle}>
              Import Claude
              <input type="file" accept=".zip" onChange={handleClaudeImportFile} disabled={importing} style={{ display: "none" }} />
            </label>
            <label style={importLabelStyle}>
              Import ChatGPT
              <input type="file" accept=".zip" onChange={handleChatGPTImportFile} disabled={importing} style={{ display: "none" }} />
            </label>
            <label style={importLabelStyle}>
              Import Markdown (e.g. Obsidian)
              <input type="file" accept=".zip" onChange={handleImportFile} disabled={importing} style={{ display: "none" }} />
            </label>
            <label style={importLabelStyle}>
              Import Tweets
              <input type="file" accept=".zip" onChange={handleTwitterImportFile} disabled={importing} style={{ display: "none" }} />
            </label>
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
                {importing ? "Analyzing..." : (buttonLabel || "Import Data")}
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
              onClick={handleConfirmImport}
              disabled={importing}
              style={{
                ...primaryBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              {importing ? "Importing..." : "Confirm Import"}
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
              onClick={handleConfirmClaudeImport}
              disabled={importing}
              style={{
                ...primaryBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              {importing ? "Importing..." : "Confirm Import"}
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
              onClick={handleConfirmChatGPTImport}
              disabled={importing}
              style={{
                ...primaryBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              {importing ? "Importing..." : "Confirm Import"}
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
              onClick={handleConfirmTwitterImport}
              disabled={importing}
              style={{
                ...primaryBtnStyle,
                cursor: importing ? "not-allowed" : "pointer",
                opacity: importing ? 0.6 : 1
              }}
            >
              {importing ? "Importing..." : "Confirm Import"}
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
