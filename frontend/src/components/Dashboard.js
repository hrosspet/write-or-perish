import React, { useState, useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import api from "../api";
import DashboardContent from "./DashboardContent";
import Bubble from "./Bubble";
import ModelSelector from "./ModelSelector";
import SpeakerIcon from "./SpeakerIcon";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";

function Dashboard() {
  const { username } = useParams(); // if present, we're viewing someone else's dashboard
  const [dashboardData, setDashboardData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // For profile editing—only allowed for your own dashboard.
  const [editingProfile, setEditingProfile] = useState(false);
  const [editProfileContent, setEditProfileContent] = useState("");

  // For AI profile generation
  const [selectedModel, setSelectedModel] = useState(null);
  const [generatingProfile, setGeneratingProfile] = useState(false);
  const [showProfileConfirmation, setShowProfileConfirmation] = useState(false);
  const [estimatedTokens, setEstimatedTokens] = useState(0);
  const [profileTaskId, setProfileTaskId] = useState(null);

  // For data import
  const [showImportDialog, setShowImportDialog] = useState(false);
  const [importFiles, setImportFiles] = useState(null);
  const [importType, setImportType] = useState("separate_nodes");
  const [dateOrdering, setDateOrdering] = useState("modified");
  const [importing, setImporting] = useState(false);

  const navigate = useNavigate();
  const backendUrl = process.env.REACT_APP_BACKEND_URL;

  // Decide which endpoint to call based on the URL.
  const endpoint = username ? `/dashboard/${username}` : "/dashboard";

  useEffect(() => {
    api.get(endpoint)
      .then((response) => {
        setDashboardData(response.data);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        if (err.response && err.response.status === 401) {
          window.location.href = `${backendUrl}/auth/login`;
        } else {
          setError("Error fetching dashboard data. Are you logged in?");
          setLoading(false);
        }
      });

    // Fetch default model for profile generation
    api.get("/nodes/default-model")
      .then((response) => {
        setSelectedModel(response.data.suggested_model);
      })
      .catch((err) => {
        console.error("Error fetching default model:", err);
        setSelectedModel("claude-opus-4.5"); // Fallback
      });
  }, [endpoint, backendUrl]);

  const handleProfileSubmit = (e) => {
    e.preventDefault();
    const profileId = dashboardData.latest_profile?.id;

    if (profileId) {
      // Update existing profile
      api.put(`/profile/${profileId}`, { content: editProfileContent })
        .then((response) => {
          setDashboardData({
            ...dashboardData,
            latest_profile: response.data.profile
          });
          setEditingProfile(false);
        })
        .catch((err) => {
          console.error(err);
          setError(err.response?.data?.error || "Error updating profile.");
        });
    } else {
      // Create new profile (user-generated)
      api.post("/export/create_profile", { content: editProfileContent })
        .then((response) => {
          setDashboardData({
            ...dashboardData,
            latest_profile: response.data.profile
          });
          setEditingProfile(false);
        })
        .catch((err) => {
          console.error(err);
          setError(err.response?.data?.error || "Error creating profile.");
        });
    }
  };

  const handleExportData = () => {
    // Call the export API endpoint
    api.get("/export/threads", {
      responseType: "blob", // Important for file download
    })
      .then((response) => {
        // Create a blob from the response
        const blob = new Blob([response.data], { type: "text/plain" });

        // Create a temporary download link
        const url = window.URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;

        // Extract filename from Content-Disposition header if available
        const contentDisposition = response.headers["content-disposition"];
        let filename = `write-or-perish-export-${new Date().toISOString().slice(0, 19).replace(/:/g, "-")}.txt`;

        if (contentDisposition) {
          const filenameMatch = contentDisposition.match(/filename="?(.+)"?/);
          if (filenameMatch && filenameMatch[1]) {
            filename = filenameMatch[1];
          }
        }

        link.download = filename;

        // Trigger the download
        document.body.appendChild(link);
        link.click();

        // Clean up
        document.body.removeChild(link);
        window.URL.revokeObjectURL(url);
      })
      .catch((err) => {
        console.error("Error exporting data:", err);
        setError("Error exporting data. Please try again.");
      });
  };

  const handleGenerateProfile = () => {
    // First, estimate the tokens
    setGeneratingProfile(true);
    api.post("/export/estimate_profile_tokens", { model: selectedModel })
      .then((response) => {
        setEstimatedTokens(response.data.estimated_tokens);
        setShowProfileConfirmation(true);
        setGeneratingProfile(false);
      })
      .catch((err) => {
        console.error("Error estimating tokens:", err);
        setError(err.response?.data?.error || "Error estimating tokens. Please try again.");
        setGeneratingProfile(false);
      });
  };

  const handleConfirmProfileGeneration = () => {
    setShowProfileConfirmation(false);
    setGeneratingProfile(true);

    api.post("/export/generate_profile", { model: selectedModel })
      .then((response) => {
        setProfileTaskId(response.data.task_id);
      })
      .catch((err) => {
        console.error("Error generating profile:", err);
        setError(err.response?.data?.error || "Error generating profile. Please try again.");
        setGeneratingProfile(false);
      });
  };

  // Polling for profile generation status
  const {
    status: profileStatus,
    progress: profileProgress,
    data: profileData,
    error: profileError
  } = useAsyncTaskPolling(
    profileTaskId ? `/export/profile-status/${profileTaskId}` : null,
    { enabled: !!profileTaskId }
  );

  // Handle profile generation completion
  useEffect(() => {
    if (profileStatus === 'completed' && profileData?.profile) {
      setDashboardData(prev => ({
        ...prev,
        latest_profile: profileData.profile,
      }));
      setGeneratingProfile(false);
      setProfileTaskId(null);
      setError("");
    } else if (profileStatus === 'failed') {
      setError(profileError || "An error occurred during profile generation.");
      setGeneratingProfile(false);
      setProfileTaskId(null);
    }
  }, [profileStatus, profileData, profileError]);

  const handleCancelProfileGeneration = () => {
    setShowProfileConfirmation(false);
  };

  const handleImportFile = (event) => {
    const file = event.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append("zip_file", file);

    setImporting(true);
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
      date_ordering: dateOrdering
    })
      .then((response) => {
        setShowImportDialog(false);
        setImportFiles(null);
        setImporting(false);
        setError("");
        // Refresh dashboard to show new nodes
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


  if (loading) return <div>Loading dashboard...</div>;
  if (error) return <div>{error}</div>;

  const { user, nodes } = dashboardData;

  return (
    <div style={{ padding: "20px" }}>
      <h1>{user.username}</h1>
      
      {/* Only allow profile actions on your own dashboard */}
      {!username && (
        <>
          <div style={{ display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap", marginBottom: "20px" }}>
            <button
              onClick={handleExportData}
              style={{
                backgroundColor: "#2a5f2a",
                color: "white",
                border: "none",
                padding: "8px 16px",
                cursor: "pointer",
                borderRadius: "4px"
              }}
            >
              Export Data
            </button>
            <label
              style={{
                backgroundColor: "#2a5f2a",
                color: "white",
                border: "none",
                padding: "8px 16px",
                cursor: importing ? "not-allowed" : "pointer",
                borderRadius: "4px",
                opacity: importing ? 0.6 : 1,
                fontSize: "inherit",
                fontFamily: "inherit",
                fontWeight: "inherit",
                lineHeight: "inherit"
              }}
            >
              {importing ? "Analyzing..." : "Import Data"}
              <input
                type="file"
                accept=".zip"
                onChange={handleImportFile}
                disabled={importing}
                style={{ display: "none" }}
              />
            </label>
            <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <ModelSelector
                nodeId={null}
                selectedModel={selectedModel}
                onModelChange={setSelectedModel}
              />
              <button
                onClick={handleGenerateProfile}
                disabled={generatingProfile || !!profileTaskId}
                style={{
                  backgroundColor: "#2a5a7a",
                  color: "white",
                  border: "none",
                  padding: "8px 16px",
                  cursor: (generatingProfile || profileTaskId) ? "not-allowed" : "pointer",
                  borderRadius: "4px",
                  opacity: (generatingProfile || profileTaskId) ? 0.6 : 1
                }}
              >
                {profileTaskId && profileStatus === 'progress' && profileProgress > 0
                  ? `Generating... ${profileProgress}%`
                  : profileTaskId && profileStatus === 'pending'
                  ? "Starting..."
                  : profileTaskId
                  ? `Generating... ${profileProgress}%`
                  : generatingProfile
                  ? "Estimating..."
                  : "Generate Profile"}
              </button>
            </div>
          </div>

          {/* Token confirmation dialog */}
          {showProfileConfirmation && (
            <div style={{
              marginTop: "20px",
              padding: "15px",
              backgroundColor: "#2a2a2a",
              borderRadius: "4px",
              border: "1px solid #444"
            }}>
              <h3>Confirm Profile Generation</h3>
              <p>
                This will use approximately <strong>{estimatedTokens.toLocaleString()}</strong> tokens
                to analyze all your writing and generate a profile using <strong>{selectedModel}</strong>.
              </p>
              <p>Do you want to proceed?</p>
              <div style={{ display: "flex", gap: "10px", marginTop: "10px" }}>
                <button
                  onClick={handleConfirmProfileGeneration}
                  style={{
                    backgroundColor: "#2a5a7a",
                    color: "white",
                    border: "none",
                    padding: "8px 16px",
                    cursor: "pointer",
                    borderRadius: "4px"
                  }}
                >
                  Yes, Generate Profile
                </button>
                <button
                  onClick={handleCancelProfileGeneration}
                  style={{
                    backgroundColor: "#444",
                    color: "white",
                    border: "none",
                    padding: "8px 16px",
                    cursor: "pointer",
                    borderRadius: "4px"
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {/* Import confirmation dialog */}
          {showImportDialog && importFiles && (
            <div style={{
              marginTop: "20px",
              padding: "15px",
              backgroundColor: "#2a2a2a",
              borderRadius: "4px",
              border: "1px solid #444"
            }}>
              <h3>Confirm Import</h3>
              <p>
                Found <strong>{importFiles.total_files}</strong> .md file{importFiles.total_files !== 1 ? 's' : ''}
                ({importFiles.total_size.toLocaleString()} bytes)
              </p>
              <p>
                Estimated tokens: <strong>{importFiles.total_tokens.toLocaleString()}</strong>
              </p>

              <div style={{ marginTop: "15px", marginBottom: "15px" }}>
                <label style={{ display: "block", marginBottom: "10px", fontWeight: "bold" }}>
                  Import Type:
                </label>
                <label style={{ display: "block", marginBottom: "8px", cursor: "pointer" }}>
                  <input
                    type="radio"
                    value="separate_nodes"
                    checked={importType === "separate_nodes"}
                    onChange={(e) => setImportType(e.target.value)}
                    style={{ marginRight: "8px" }}
                  />
                  Import as separate top-level nodes (one thread per file)
                </label>
                <label style={{ display: "block", cursor: "pointer" }}>
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
                <label style={{ display: "block", marginBottom: "10px", fontWeight: "bold" }}>
                  Date Ordering:
                </label>
                <label style={{ display: "block", marginBottom: "8px", cursor: "pointer" }}>
                  <input
                    type="radio"
                    value="modified"
                    checked={dateOrdering === "modified"}
                    onChange={(e) => setDateOrdering(e.target.value)}
                    style={{ marginRight: "8px" }}
                  />
                  Order by modification date
                </label>
                <label style={{ display: "block", cursor: "pointer" }}>
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

              <div style={{ display: "flex", gap: "10px", marginTop: "15px" }}>
                <button
                  onClick={handleConfirmImport}
                  disabled={importing}
                  style={{
                    backgroundColor: "#2a5a7a",
                    color: "white",
                    border: "none",
                    padding: "8px 16px",
                    cursor: importing ? "not-allowed" : "pointer",
                    borderRadius: "4px",
                    opacity: importing ? 0.6 : 1
                  }}
                >
                  {importing ? "Importing..." : "Confirm Import"}
                </button>
                <button
                  onClick={handleCancelImport}
                  disabled={importing}
                  style={{
                    backgroundColor: "#444",
                    color: "white",
                    border: "none",
                    padding: "8px 16px",
                    cursor: importing ? "not-allowed" : "pointer",
                    borderRadius: "4px",
                    opacity: importing ? 0.6 : 1
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {/* Display unified profile */}
          <div style={{
            marginTop: "30px",
            padding: "20px",
            backgroundColor: "#1a1a1a",
            borderRadius: "8px",
            border: "1px solid #333"
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "15px" }}>
              <h3 style={{ color: "#e0e0e0", margin: 0 }}>Profile</h3>
              {!editingProfile && (
                <button
                  onClick={() => {
                    setEditingProfile(true);
                    setEditProfileContent(dashboardData.latest_profile?.content || "");
                  }}
                  style={{
                    backgroundColor: "#2a5a7a",
                    color: "white",
                    border: "none",
                    padding: "6px 12px",
                    cursor: "pointer",
                    borderRadius: "4px",
                    fontSize: "0.9em"
                  }}
                >
                  Edit Profile
                </button>
              )}
            </div>
            {dashboardData.latest_profile && (
              <p style={{ fontSize: "0.9em", color: "#888", marginBottom: "15px" }}>
                {dashboardData.latest_profile.generated_by === "user" ? "Edited" : "Generated"} by {dashboardData.latest_profile.generated_by} on{" "}
                {new Date(dashboardData.latest_profile.created_at).toLocaleString()}
                {dashboardData.latest_profile.tokens_used > 0 && (
                  <> ({dashboardData.latest_profile.tokens_used?.toLocaleString()} tokens)</>
                )}
              </p>
            )}
            {editingProfile ? (
              <form onSubmit={handleProfileSubmit}>
                <textarea
                  value={editProfileContent}
                  onChange={(e) => setEditProfileContent(e.target.value)}
                  rows={20}
                  style={{
                    width: "100%",
                    backgroundColor: "#2a2a2a",
                    color: "#d0d0d0",
                    border: "1px solid #444",
                    borderRadius: "4px",
                    padding: "10px",
                    fontFamily: "inherit",
                    fontSize: "inherit",
                    lineHeight: "1.6"
                  }}
                  placeholder="Write your profile here..."
                />
                <div style={{ display: "flex", gap: "10px", marginTop: "10px" }}>
                  <button
                    type="submit"
                    style={{
                      backgroundColor: "#2a5a7a",
                      color: "white",
                      border: "none",
                      padding: "8px 16px",
                      cursor: "pointer",
                      borderRadius: "4px"
                    }}
                  >
                    Save
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditingProfile(false)}
                    style={{
                      backgroundColor: "#444",
                      color: "white",
                      border: "none",
                      padding: "8px 16px",
                      cursor: "pointer",
                      borderRadius: "4px"
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </form>
            ) : dashboardData.latest_profile ? (
              <div style={{
                lineHeight: "1.6",
                color: "#d0d0d0"
              }}>
                <ReactMarkdown
                  components={{
                    p: ({ node, ...props }) => (
                      <p style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props} />
                    ),
                    code: ({ node, inline, className, children, ...props }) =>
                      inline ? (
                        <code style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props}>
                          {children}
                        </code>
                      ) : (
                        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props}>
                          <code>{children}</code>
                        </pre>
                      ),
                    li: ({ node, ...props }) => (
                      <li style={{ whiteSpace: "pre-wrap", overflowWrap: "break-word" }} {...props} />
                    )
                  }}
                >
                  {dashboardData.latest_profile.content}
                </ReactMarkdown>
                <SpeakerIcon profileId={dashboardData.latest_profile.id} content={dashboardData.latest_profile.content} />
              </div>
            ) : (
              <p style={{ color: "#888", fontStyle: "italic" }}>
                No profile yet. Click "Edit Profile" to create one or use "Generate Profile" to have AI create one for you.
              </p>
            )}
          </div>
        </>
      )}

      {/* Display the chart with totals */}
      {dashboardData.stats && <DashboardContent dashboardData={dashboardData} username={username} />}

      {/* Graphical (bubble-style) view of top-level entries */}
      <h3 style={{ color: "#e0e0e0", marginTop: "40px", textAlign: "left"}}>
        Top-Level Entries
      </h3>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          maxWidth: "1000px",
          alignItems: "flex-start" // Aligns children to the left.
        }}
      >
        {nodes.map((node) => (
          <Bubble
            key={node.id}
            node={node}
            isHighlighted={true}
            leftAlign={true}  // Ensures the bubble is left aligned.
            onClick={() => navigate(`/node/${node.id}`)}
          />
        ))}
      </div>
    </div>
  );
}

export default Dashboard;