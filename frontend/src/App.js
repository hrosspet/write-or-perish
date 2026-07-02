import React, { useState, useEffect, useRef } from 'react';
import { Routes, Route, Navigate, useNavigate, useParams } from 'react-router-dom';
import LandingPage from "./components/LandingPage";
import Feed from "./components/Feed";
import NavBar from "./components/NavBar";
import NodeFormModal from "./components/NodeFormModal";
import SpendCapBanner from "./components/SpendCapBanner";
import TermsModal from "./components/TermsModal";
import AdminPanel from "./components/AdminPanel";
import NodeDetailWrapper from "./components/NodeDetailWrapper";
import LoginPage from "./components/LoginPage";
import ProtectedRoute from "./components/ProtectedRoute";
import VisionPage from "./pages/VisionPage";
import WhyLoorePage from "./pages/WhyLoorePage";
import HowToPage from "./pages/HowToPage";
import AlphaThankYouPage from "./pages/AlphaThankYouPage";
import WelcomePage from "./pages/WelcomePage";
import HomePage from "./pages/HomePage";
import VoicePage from "./pages/VoicePage";
import ConversePage from "./pages/ConversePage";
import WritePage from "./pages/WritePage";
import ProfilePage from "./pages/ProfilePage";
import TodoPage from "./pages/TodoPage";
import ImportPage from "./pages/ImportPage";
import AccountPage from "./pages/AccountPage";
import ArtifactsPage from "./pages/ArtifactsPage";
import SharePage from "./pages/SharePage";
import PublicSharePage from "./pages/PublicSharePage";
import CommonsPage from "./pages/CommonsPage";
import PublicThreadPage from "./pages/PublicThreadPage";
import ProfileGenerationWatcher from "./components/ProfileGenerationWatcher";
import PromptsPage from "./pages/PromptsPage";
import PromptDetailPage from "./pages/PromptDetailPage";
import SearchModal from "./components/SearchModal";
import api from "./api";
import { useUser } from "./contexts/UserContext";
import { AudioProvider } from "./contexts/AudioContext";

// /u/:username/:slug — human-readable permalink (#228). Resolves the slug
// to a node id; logged-out visitors keep the pretty URL and get the public
// thread view, members continue to the canonical /node/<id> thread UI.
function PermalinkRoute({ username, slug }) {
  const { user, loading } = useUser();
  const [nodeId, setNodeId] = useState(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setNodeId(null);
    setFailed(false);
    api.get(`/commons/permalink/${username}/${slug}`)
      .then((res) => { if (!cancelled) setNodeId(res.data.node_id); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [username, slug]);
  if (loading || (!nodeId && !failed)) return null;
  if (failed) return <PublicThreadPage nodeIdOverride="missing" />;
  if (user) return <NodeDetailWrapper nodeIdOverride={nodeId} />;
  return <PublicThreadPage nodeIdOverride={nodeId} />;
}

// /node/:id — members get the full thread UI; logged-out visitors get the
// read-only public thread (the funnel, #228). PublicThreadPage 404s for
// anything non-public, so nothing private is reachable without login.
function NodeRoute() {
  const { user, loading } = useUser();
  if (loading) return null;
  return user ? <NodeDetailWrapper /> : <PublicThreadPage />;
}

// Legacy /dashboard/<username> links (old public dashboard, now deleted)
// land on the user's public page — today's public identity surface.
function DashboardRedirect() {
  const { username } = useParams();
  return <Navigate to={`/@${username}`} replace />;
}

// /@username and /@username/slug — the public identity namespace (#228).
// React Router segments can't mix a literal prefix with a param, so this
// mounts as a root-level dynamic route (declared just above the catch-all;
// all real routes rank ahead of it) and guards on the @.
function AtRoute() {
  const { atName, slug } = useParams();
  if (!atName || !atName.startsWith('@') || atName.length < 2) {
    return <Navigate to="/" replace />;
  }
  const username = atName.slice(1);
  if (slug) return <PermalinkRoute username={username} slug={slug} />;
  return <PublicSharePage usernameOverride={username} />;
}

function RootRoute() {
  const { user, loading } = useUser();
  if (loading) return null;
  if (!user) return <Navigate to="/landing" replace />;
  if (!user.approved) return <Navigate to="/alpha-thank-you" replace />;
  return <HomePage />;
}

function App() {
  const [showNewEntry, setShowNewEntry] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const nodeFormRef = useRef(null);
  const [showTerms, setShowTerms] = useState(false);
  const { user, setUser } = useUser();
  const navigate = useNavigate();

  // When the user info is loaded, check if they have accepted the terms.
  useEffect(() => {
    if (user) {
      // Show terms modal if not accepted or if terms version is outdated.
      if (!user.terms_up_to_date) {
        setShowTerms(true);
      } else {
        setShowTerms(false);
      }
    }
  }, [user]);

  // Cmd+K / Ctrl+K to open search
  useEffect(() => {
    const handleSearchShortcut = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setShowSearch((prev) => !prev);
      }
    };
    window.addEventListener('keydown', handleSearchShortcut);
    return () => window.removeEventListener('keydown', handleSearchShortcut);
  }, []);

  return (
    <AudioProvider>
      <div>
        <NavBar onNewEntryClick={() => setShowNewEntry(true)} />
        <SpendCapBanner />
        <div style={{ paddingTop: "60px" }}>
        {showNewEntry && (
          <NodeFormModal
            title="Write New Entry"
            onClose={() => setShowNewEntry(false)}
            nodeFormProps={{
              ref: nodeFormRef,
              parentId: null,
              allowAgenticPrompt: true,
              onSuccess: (data) => {
                setShowNewEntry(false);
                // When the entry went through /textmode/start, data
                // carries `awaitLlm` so NodeDetail picks up the pending
                // LLM response on the highlighted node.
                const suffix = data.awaitLlm ? `?awaitLlm=${data.awaitLlm}` : '';
                navigate(`/node/${data.id}${suffix}`);
              },
            }}
          />
      )}

      {showSearch && <SearchModal onClose={() => setShowSearch(false)} />}

      {/* Render the Terms Modal if the user hasn't accepted the terms yet */}
      {showTerms && (
        <TermsModal
          onAccepted={() => {
            setUser({ ...user, terms_up_to_date: true });
            setShowTerms(false);
            // If user is not approved, redirect to alpha-thank-you page
            if (!user.approved) {
              navigate('/alpha-thank-you');
            }
          }}
        />
      )}

        <ProfileGenerationWatcher />
        <Routes>
          <Route path="/" element={<RootRoute />} />
          <Route path="/landing" element={<LandingPage />} />
          <Route path="/login" element={<LoginPage />} />
          {/* Public about pages */}
          <Route path="/vision" element={<VisionPage />} />
          <Route path="/why-loore" element={<WhyLoorePage />} />
          <Route path="/how-to" element={<HowToPage />} />
          {/* Alpha thank you - public (for unapproved users) */}
          <Route path="/alpha-thank-you" element={<AlphaThankYouPage />} />
          {/* Welcome - protected (for newly approved users) */}
          <Route path="/welcome" element={<ProtectedRoute><WelcomePage onNewEntryClick={() => setShowNewEntry(true)} /></ProtectedRoute>} />
          {/* Workflow routes */}
          <Route path="/voice" element={<ProtectedRoute><VoicePage /></ProtectedRoute>} />
          <Route path="/converse" element={<ProtectedRoute><ConversePage /></ProtectedRoute>} />
          <Route path="/textmode" element={<ProtectedRoute><WritePage /></ProtectedRoute>} />
          {/* Profile and Todo */}
          <Route path="/profile" element={<ProtectedRoute><ProfilePage /></ProtectedRoute>} />
          <Route path="/todo" element={<ProtectedRoute><TodoPage /></ProtectedRoute>} />
          {/* Log (renamed from feed) */}
          <Route path="/log" element={<ProtectedRoute><Feed onSearchClick={() => setShowSearch(true)} /></ProtectedRoute>} />
          {/* Backward compatibility redirects */}
          <Route path="/feed" element={<Navigate to="/log" replace />} />
          <Route path="/dashboard" element={<Navigate to="/profile" replace />} />
          {/* Public profile view */}
          <Route path="/dashboard/:username" element={<DashboardRedirect />} />
          <Route path="/prompts" element={<ProtectedRoute><PromptsPage /></ProtectedRoute>} />
          <Route path="/prompts/:promptKey" element={<ProtectedRoute><PromptDetailPage /></ProtectedRoute>} />
          <Route path="/import" element={<ProtectedRoute><ImportPage /></ProtectedRoute>} />
          {/* AI preferences folded into the artifact model (#158 Slice 5);
              keep the old path working as a redirect. */}
          <Route path="/ai-preferences" element={<Navigate to="/artifacts/ai_preferences" replace />} />
          <Route path="/artifacts" element={<ProtectedRoute><ArtifactsPage /></ProtectedRoute>} />
          <Route path="/artifacts/:kind" element={<ProtectedRoute><ArtifactsPage /></ProtectedRoute>} />
          <Route path="/share" element={<ProtectedRoute><SharePage /></ProtectedRoute>} />
          <Route path="/commons" element={<ProtectedRoute><CommonsPage /></ProtectedRoute>} />
          <Route path="/account" element={<ProtectedRoute><AccountPage /></ProtectedRoute>} />
          <Route path="/node/:id" element={<NodeRoute />} />
          <Route path="/admin" element={<ProtectedRoute><AdminPanel /></ProtectedRoute>} />
          <Route path="/:atName" element={<AtRoute />} />
          <Route path="/:atName/:slug" element={<AtRoute />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </div>
      </div>
    </AudioProvider>
  );
}

export default App;
