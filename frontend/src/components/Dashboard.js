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
  const [editUsername, setEditUsername] = useState("");
  const [editDescription, setEditDescription] = useState("");

  // For AI-generated profile editing
  const [editingAIProfile, setEditingAIProfile] = useState(false);
  const [editAIProfileContent, setEditAIProfileContent] = useState("");

  // For AI profile generation
  const [selectedModel, setSelectedModel] = useState("gpt-5");
  const [generatingProfile, setGeneratingProfile] = useState(false);
  const [showProfileConfirmation, setShowProfileConfirmation] = useState(false);
  const [estimatedTokens, setEstimatedTokens] = useState(0);
  const [profileTaskId, setProfileTaskId] = useState(null);

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
  }, [endpoint, backendUrl]);

  const handleProfileSubmit = (e) => {
    e.preventDefault();
    api.put("/dashboard/user", { username: editUsername, description: editDescription })
      .then((response) => {
        setDashboardData({
          ...dashboardData,
          user: response.data.user
        });
        setEditingProfile(false);
      })
      .catch((err) => {
        console.error(err);
        setError("Error updating profile.");
      });
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
  useAsyncTaskPolling({
    taskId: profileTaskId,
    endpoint: `/export/profile-status/${profileTaskId}`,
    onSuccess: (data) => {
      if (data.profile) {
        setDashboardData({
          ...dashboardData,
          latest_profile: data.profile,
        });
      }
      setGeneratingProfile(false);
      setProfileTaskId(null);
      setError(""); // Clear any previous errors
    },
    onError: (error) => {
      console.error("Error during profile generation polling:", error);
      setError(error.message || "An error occurred during profile generation.");
      setGeneratingProfile(false);
      setProfileTaskId(null);
    },
  });

  const handleCancelProfileGeneration = () => {
    setShowProfileConfirmation(false);
  };

  const handleAIProfileSubmit = (e) => {
    e.preventDefault();
    const profileId = dashboardData.latest_profile.id;
    api.put(`/profile/${profileId}`, { content: editAIProfileContent })
      .then((response) => {
        setDashboardData({
          ...dashboardData,
          latest_profile: response.data.profile
        });
        setEditingAIProfile(false);
      })
      .catch((err) => {
        console.error(err);
        setError(err.response?.data?.error || "Error updating AI profile.");
      });
  };

  if (loading) return <div>Loading dashboard...</div>;
  if (error) return <div>{error}</div>;

  const { user, nodes } = dashboardData;

  return (
    <div style={{ padding: "20px" }}>
      <h1>{user.username}</h1>
      
      {/* Only allow profile editing on your own dashboard */}
      {!username && (
        <>
          {editingProfile ? (
            <form onSubmit={handleProfileSubmit}>
              <div>
                <label>
                  Handle:
                  <input
                    type="text"
                    value={editUsername}
                    onChange={(e) => setEditUsername(e.target.value)}
                  />
                </label>
              </div>
              <div>
                <label>
                  Description:
                  <input
                    type="text"
                    maxLength={128}
                    value={editDescription}
                    onChange={(e) => setEditDescription(e.target.value)}
                  />
                </label>
              </div>
              <button type="submit">Save Profile</button>
              <button type="button" onClick={() => setEditingProfile(false)}>
                Cancel
              </button>
            </form>
          ) : (
            <div>
              <p>{user.description || "No description provided."}</p>
              <div style={{ display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap" }}>
                <button
                  onClick={() => {
                    setEditingProfile(true);
                    setEditUsername(user.username);
                    setEditDescription(user.description);
                  }}
                >
                  Edit Profile
                </button>
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
                <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                  <ModelSelector
                    nodeId={null}
                    selectedModel={selectedModel}
                    onModelChange={setSelectedModel}
                  />
                  <button
                    onClick={handleGenerateProfile}
                    disabled={generatingProfile}
                    style={{
                      backgroundColor: "#2a5a7a",
                      color: "white",
                      border: "none",
                      padding: "8px 16px",
                      cursor: generatingProfile ? "not-allowed" : "pointer",
                      borderRadius: "4px",
                      opacity: generatingProfile ? 0.6 : 1
                    }}
                  >
                    {generatingProfile ? "Generating..." : "Generate Profile"}
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

              {/* Display latest profile if it exists */}
              {dashboardData.latest_profile && (
                <div style={{
                  marginTop: "30px",
                  padding: "20px",
                  backgroundColor: "#1a1a1a",
                  borderRadius: "8px",
                  border: "1px solid #333"
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "15px" }}>
                    <h3 style={{ color: "#e0e0e0", margin: 0 }}>AI-Generated Profile</h3>
                    {!editingAIProfile && (
                      <button
                        onClick={() => {
                          setEditingAIProfile(true);
                          setEditAIProfileContent(dashboardData.latest_profile.content);
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
                  <p style={{ fontSize: "0.9em", color: "#888", marginBottom: "15px" }}>
                    Generated by {dashboardData.latest_profile.generated_by} on{" "}
                    {new Date(dashboardData.latest_profile.created_at).toLocaleString()}
                    {" "}({dashboardData.latest_profile.tokens_used?.toLocaleString()} tokens)
                  </p>
                  {editingAIProfile ? (
                    <form onSubmit={handleAIProfileSubmit}>
                      <textarea
                        value={editAIProfileContent}
                        onChange={(e) => setEditAIProfileContent(e.target.value)}
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
                          onClick={() => setEditingAIProfile(false)}
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
                  ) : (
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
                      <SpeakerIcon profileId={dashboardData.latest_profile.id} />
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
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