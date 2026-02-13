import React, { useState, useEffect, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import api from "../api";

import Bubble from "./Bubble";
import ModelSelector from "./ModelSelector";
import SpeakerIcon from "./SpeakerIcon";
import PrivacySelector from "./PrivacySelector";
import ImportData from "./ImportData";
import { useAsyncTaskPolling } from "../hooks/useAsyncTaskPolling";

function Dashboard() {
  const { username } = useParams(); // if present, we're viewing someone else's dashboard
  const [dashboardData, setDashboardData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // For profile editing—only allowed for your own dashboard.
  const [editingProfile, setEditingProfile] = useState(false);
  const [editProfileContent, setEditProfileContent] = useState("");
  const [profilePrivacyLevel, setProfilePrivacyLevel] = useState("private");
  const [profileAiUsage, setProfileAiUsage] = useState("chat");

  // For AI profile generation
  const [selectedModel, setSelectedModel] = useState(null);
  const [generatingProfile, setGeneratingProfile] = useState(false);
  const [showProfileConfirmation, setShowProfileConfirmation] = useState(false);
  const [estimatedTokens, setEstimatedTokens] = useState(0);
  const [profileTaskId, setProfileTaskId] = useState(null);

  const [hasMoreNodes, setHasMoreNodes] = useState(false);
  const [nodesPage, setNodesPage] = useState(1);
  const [loadingMoreNodes, setLoadingMoreNodes] = useState(false);

  const navigate = useNavigate();

  // Decide which endpoint to call based on the URL.
  const endpoint = username ? `/dashboard/${username}` : "/dashboard";

  useEffect(() => {
    api.get(`${endpoint}?page=1&per_page=20`)
      .then((response) => {
        setDashboardData(response.data);
        setHasMoreNodes(response.data.has_more);
        setNodesPage(1);
        setLoading(false);
      })
      .catch((err) => {
        console.error(err);
        setError("Error fetching dashboard data.");
        setLoading(false);
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
  }, [endpoint]);

  const handleProfileSubmit = (e) => {
    e.preventDefault();
    const profileId = dashboardData.latest_profile?.id;

    if (profileId) {
      // Update existing profile
      api.put(`/profile/${profileId}`, {
        content: editProfileContent,
        privacy_level: profilePrivacyLevel,
        ai_usage: profileAiUsage
      })
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
      api.post("/export/create_profile", {
        content: editProfileContent,
        privacy_level: profilePrivacyLevel,
        ai_usage: profileAiUsage
      })
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

  const fetchMoreNodes = useCallback((nextPage) => {
    setLoadingMoreNodes(true);
    api.get(`${endpoint}?page=${nextPage}&per_page=20`)
      .then((response) => {
        setDashboardData(prev => ({
          ...prev,
          nodes: [...prev.nodes, ...response.data.nodes]
        }));
        setHasMoreNodes(response.data.has_more);
        setNodesPage(nextPage);
      })
      .catch((err) => {
        console.error("Error loading more nodes:", err);
      })
      .finally(() => {
        setLoadingMoreNodes(false);
      });
  }, [endpoint]);

  // Auto-load on scroll near bottom
  useEffect(() => {
    if (!hasMoreNodes || loading || loadingMoreNodes) return;

    const handleScroll = () => {
      const scrollBottom = window.innerHeight + window.scrollY;
      const docHeight = document.documentElement.scrollHeight;
      if (docHeight - scrollBottom < 300) {
        fetchMoreNodes(nodesPage + 1);
      }
    };

    window.addEventListener("scroll", handleScroll, { passive: true });
    handleScroll();

    return () => window.removeEventListener("scroll", handleScroll);
  }, [hasMoreNodes, loading, loadingMoreNodes, nodesPage, fetchMoreNodes]);


  if (loading) return <div style={{ padding: "20px", color: "var(--text-muted)" }}>Loading dashboard...</div>;
  if (error) return <div style={{ padding: "20px", color: "var(--accent)" }}>{error}</div>;

  const { user, nodes } = dashboardData;

  // Button styles
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

  return (
    <div style={{ padding: "3rem 2rem 4rem", maxWidth: "820px", margin: "0 auto" }}>
      <h1 style={{
        fontFamily: "var(--serif)",
        fontWeight: 300,
        fontSize: "2rem",
        color: "var(--text-primary)",
      }}>{user.username}</h1>

      {/* Only allow profile actions on your own dashboard */}
      {!username && (
        <>
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap", marginBottom: "2.5rem" }}>
            <button onClick={handleExportData} style={ghostBtnStyle}>
              Export Data
            </button>
            <ImportData />
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
                  ...primaryBtnStyle,
                  cursor: (generatingProfile || profileTaskId) ? "not-allowed" : "pointer",
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
              padding: "2rem",
              backgroundColor: "var(--bg-card)",
              borderRadius: "10px",
              border: "1px solid var(--border)"
            }}>
              <h3 style={{ fontFamily: "var(--serif)", fontWeight: 300, color: "var(--text-primary)", margin: "0 0 12px 0" }}>Confirm Profile Generation</h3>
              <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>
                This will use approximately <strong style={{ color: "var(--text-primary)" }}>{estimatedTokens.toLocaleString()}</strong> tokens
                to analyze all your writing and generate a profile using <strong style={{ color: "var(--text-primary)" }}>{selectedModel}</strong>.
              </p>
              <p style={{ color: "var(--text-secondary)", fontFamily: "var(--sans)", fontWeight: 300 }}>Do you want to proceed?</p>
              <div style={{ display: "flex", gap: "10px", marginTop: "10px" }}>
                <button onClick={handleConfirmProfileGeneration} style={primaryBtnStyle}>
                  Yes, Generate Profile
                </button>
                <button onClick={handleCancelProfileGeneration} style={cancelBtnStyle}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          {/* Display unified profile */}
          <div style={{
            marginTop: "30px",
            padding: "2rem",
            backgroundColor: "var(--bg-card)",
            borderRadius: "10px",
            border: "1px solid var(--border)"
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "15px" }}>
              <h3 style={{
                fontFamily: "var(--serif)",
                fontSize: "1.4rem",
                fontWeight: 300,
                color: "var(--text-primary)",
                margin: 0
              }}>Profile</h3>
              {!editingProfile && (
                <button
                  onClick={() => {
                    setEditingProfile(true);
                    setEditProfileContent(dashboardData.latest_profile?.content || "");
                    setProfilePrivacyLevel(dashboardData.latest_profile?.privacy_level || "private");
                    setProfileAiUsage(dashboardData.latest_profile?.ai_usage || "chat");
                  }}
                  style={ghostBtnStyle}
                >
                  Edit Profile
                </button>
              )}
            </div>
            {dashboardData.latest_profile && (
              <p style={{
                fontSize: "0.8rem",
                color: "var(--text-muted)",
                marginBottom: "15px",
                fontFamily: "var(--sans)",
                fontWeight: 300,
              }}>
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
                    backgroundColor: "var(--bg-input)",
                    color: "var(--text-secondary)",
                    border: "1px solid var(--border)",
                    borderRadius: "8px",
                    padding: "14px 16px",
                    fontFamily: "var(--sans)",
                    fontSize: "0.95rem",
                    fontWeight: 300,
                    lineHeight: "1.6",
                    boxSizing: "border-box",
                  }}
                  placeholder="Write your profile here..."
                />
                <PrivacySelector
                  privacyLevel={profilePrivacyLevel}
                  aiUsage={profileAiUsage}
                  onPrivacyChange={setProfilePrivacyLevel}
                  onAIUsageChange={setProfileAiUsage}
                />
                <div style={{ display: "flex", gap: "10px", marginTop: "10px" }}>
                  <button type="submit" style={primaryBtnStyle}>
                    Save
                  </button>
                  <button type="button" onClick={() => setEditingProfile(false)} style={cancelBtnStyle}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : dashboardData.latest_profile ? (
              <div className="loore-profile" style={{
                lineHeight: "1.6",
                color: "var(--text-secondary)",
              }}>
                <style>{`
                  .loore-profile h1 {
                    font-family: var(--sans);
                    font-weight: 500;
                    font-size: 0.7rem;
                    letter-spacing: 0.2em;
                    text-transform: uppercase;
                    color: var(--accent);
                    margin-bottom: 2rem;
                    padding-bottom: 0.8rem;
                    border-bottom: 1px solid var(--border);
                  }
                  .loore-profile h2 {
                    font-family: var(--sans);
                    font-size: 0.75rem;
                    font-weight: 400;
                    text-transform: uppercase;
                    letter-spacing: 0.12em;
                    color: var(--text-muted);
                    margin-top: 1.5rem;
                    margin-bottom: 1rem;
                    padding-bottom: 0.5rem;
                    border-bottom: 1px solid var(--border);
                  }
                  .loore-profile h3 {
                    font-family: var(--serif);
                    font-weight: 300;
                    font-size: 1rem;
                    color: var(--text-primary);
                  }
                  .loore-profile p, .loore-profile li {
                    font-family: var(--sans);
                    font-weight: 300;
                    color: var(--text-secondary);
                    font-size: 0.95rem;
                  }
                  .loore-profile ul {
                    list-style: none;
                    padding: 0;
                  }
                  .loore-profile ul li {
                    padding: 0.35rem 0;
                    padding-left: 1rem;
                    position: relative;
                  }
                  .loore-profile ul li::before {
                    content: '';
                    position: absolute;
                    left: 0;
                    top: 0.85em;
                    width: 4px;
                    height: 4px;
                    border-radius: 50%;
                    background: var(--accent);
                  }
                  .loore-profile ol {
                    list-style: none;
                    counter-reset: surface-counter;
                    padding: 0;
                  }
                  .loore-profile ol li {
                    counter-increment: surface-counter;
                    padding: 0.35rem 0;
                    padding-left: 1.5rem;
                    position: relative;
                  }
                  .loore-profile ol li::before {
                    content: counter(surface-counter) '.';
                    position: absolute;
                    left: 0;
                    color: var(--accent);
                    font-weight: 500;
                    font-size: 0.85rem;
                  }
                  .loore-profile strong {
                    color: var(--text-primary);
                    font-weight: 500;
                  }
                  .loore-profile a {
                    color: var(--accent);
                  }
                  .loore-profile hr {
                    border-color: var(--border);
                  }
                `}</style>
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
              <p style={{ color: "var(--text-muted)", fontStyle: "italic", fontFamily: "var(--serif)" }}>
                No profile yet. Click "Edit Profile" to create one or use "Generate Profile" to have AI create one for you.
              </p>
            )}
          </div>
        </>
      )}

      {/* Pinned nodes section */}
      {dashboardData.pinned_nodes && dashboardData.pinned_nodes.length > 0 && (
        <>
          <h3 style={{
            fontFamily: "var(--serif)",
            fontWeight: 300,
            fontSize: "1.4rem",
            color: "var(--text-primary)",
            marginTop: "40px",
            textAlign: "left"
          }}>
            Pinned
          </h3>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              maxWidth: "1000px",
              alignItems: "flex-start"
            }}
          >
            {dashboardData.pinned_nodes.map((node) => (
              <Bubble
                key={`pinned-${node.id}`}
                node={node}
                isHighlighted={true}
                leftAlign={true}
                onClick={() => navigate(`/node/${node.id}`)}
              />
            ))}
          </div>
        </>
      )}

      {/* Graphical (bubble-style) view of top-level entries */}
      <h3 style={{
        fontFamily: "var(--serif)",
        fontWeight: 300,
        fontSize: "1.4rem",
        color: "var(--text-primary)",
        marginTop: "40px",
        textAlign: "left"
      }}>
        Top-Level Entries
      </h3>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          maxWidth: "1000px",
          alignItems: "flex-start"
        }}
      >
        {nodes.map((node) => (
          <Bubble
            key={node.id}
            node={node}
            isHighlighted={true}
            leftAlign={true}
            onClick={() => navigate(`/node/${node.id}`)}
          />
        ))}
      </div>
      {loadingMoreNodes && <div style={{ padding: "20px", textAlign: "center", color: "var(--text-muted)" }}>Loading more...</div>}
      {hasMoreNodes && !loadingMoreNodes && (
        <div
          style={{ padding: "20px", textAlign: "center", cursor: "pointer", color: "var(--text-muted)" }}
          onClick={() => fetchMoreNodes(nodesPage + 1)}
        >
          Load more...
        </div>
      )}
    </div>
  );
}

export default Dashboard;
