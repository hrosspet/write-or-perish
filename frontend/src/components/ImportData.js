import React, { useState } from "react";
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

export default function ImportData() {
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

  // Which picker to show: null, "markdown", or "twitter"
  const [showPicker, setShowPicker] = useState(false);

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
      .then(() => {
        setShowImportDialog(false);
        setImportFiles(null);
        setImporting(false);
        setError("");
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
      .then(() => {
        setShowTwitterImportDialog(false);
        setTwitterImportData(null);
        setImporting(false);
        setError("");
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

  return (
    <div>
      {error && <div style={{ color: "var(--accent)", marginBottom: "0.8rem", fontSize: "0.88rem" }}>{error}</div>}

      {/* Main Import Data button */}
      {!showImportDialog && !showTwitterImportDialog && (
        <div style={{ position: "relative", display: "inline-block" }}>
          <button
            onClick={() => setShowPicker(!showPicker)}
            disabled={importing}
            style={{
              ...ghostBtnStyle,
              cursor: importing ? "not-allowed" : "pointer",
              opacity: importing ? 0.6 : 1,
            }}
          >
            {importing ? "Analyzing..." : "Import Data"}
          </button>

          {showPicker && (
            <div style={{
              position: "absolute", top: "100%", left: "50%", transform: "translateX(-50%)",
              marginTop: "8px",
              background: "var(--bg-card)", border: "1px solid var(--border)",
              borderRadius: "8px", padding: "8px 0",
              zIndex: 10, minWidth: "220px",
              boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
            }}>
              <label style={{
                display: "block", padding: "10px 16px", cursor: "pointer",
                fontFamily: "var(--sans)", fontSize: "0.88rem", fontWeight: 300,
                color: "var(--text-secondary)",
                transition: "background 0.2s",
              }}>
                Import Markdown (e.g. Obsidian)
                <input
                  type="file"
                  accept=".zip"
                  onChange={handleImportFile}
                  disabled={importing}
                  style={{ display: "none" }}
                />
              </label>
              <label style={{
                display: "block", padding: "10px 16px", cursor: "pointer",
                fontFamily: "var(--sans)", fontSize: "0.88rem", fontWeight: 300,
                color: "var(--text-secondary)",
                transition: "background 0.2s",
              }}>
                Import Tweets
                <input
                  type="file"
                  accept=".zip"
                  onChange={handleTwitterImportFile}
                  disabled={importing}
                  style={{ display: "none" }}
                />
              </label>
            </div>
          )}
        </div>
      )}

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
