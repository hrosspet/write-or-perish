import React, { useState, useEffect, useRef } from 'react';
import { Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom';
import LandingPage from "./components/LandingPage";
import Dashboard from "./components/Dashboard";
import Feed from "./components/Feed";
import NavBar from "./components/NavBar";
import NodeFormModal from "./components/NodeFormModal";
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
import ProfilePage from "./pages/ProfilePage";
import TodoPage from "./pages/TodoPage";
import ImportPage from "./pages/ImportPage";
import AccountPage from "./pages/AccountPage";
import AiPreferencesPage from "./pages/AiPreferencesPage";
import PromptsPage from "./pages/PromptsPage";
import PromptDetailPage from "./pages/PromptDetailPage";
import SearchModal from "./components/SearchModal";
import { useUser } from "./contexts/UserContext";
import { AudioProvider } from "./contexts/AudioContext";

function RedirectToVoice() {
  const location = useLocation();
  return <Navigate to={'/voice' + location.search} replace />;
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
        <div style={{ paddingTop: "60px" }}>
        {showNewEntry && (
          <NodeFormModal
            title="Write New Entry"
            onClose={() => setShowNewEntry(false)}
            nodeFormProps={{
              ref: nodeFormRef,
              parentId: null,
              onSuccess: (data) => {
                setShowNewEntry(false);
                navigate(`/node/${data.id}`);
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
          {/* Profile and Todo */}
          <Route path="/profile" element={<ProtectedRoute><ProfilePage /></ProtectedRoute>} />
          <Route path="/todo" element={<ProtectedRoute><TodoPage /></ProtectedRoute>} />
          {/* Log (renamed from feed) */}
          <Route path="/log" element={<ProtectedRoute><Feed /></ProtectedRoute>} />
          {/* Backward compatibility redirects */}
          <Route path="/reflect" element={<ProtectedRoute><RedirectToVoice /></ProtectedRoute>} />
          <Route path="/orient" element={<ProtectedRoute><RedirectToVoice /></ProtectedRoute>} />
          <Route path="/feed" element={<Navigate to="/log" replace />} />
          <Route path="/dashboard" element={<Navigate to="/profile" replace />} />
          {/* Public profile view */}
          <Route path="/dashboard/:username" element={<Dashboard />} />
          <Route path="/prompts" element={<ProtectedRoute><PromptsPage /></ProtectedRoute>} />
          <Route path="/prompts/:promptKey" element={<ProtectedRoute><PromptDetailPage /></ProtectedRoute>} />
          <Route path="/import" element={<ProtectedRoute><ImportPage /></ProtectedRoute>} />
          <Route path="/ai-preferences" element={<ProtectedRoute><AiPreferencesPage /></ProtectedRoute>} />
          <Route path="/account" element={<ProtectedRoute><AccountPage /></ProtectedRoute>} />
          <Route path="/node/:id" element={<ProtectedRoute><NodeDetailWrapper /></ProtectedRoute>} />
          <Route path="/admin" element={<ProtectedRoute><AdminPanel /></ProtectedRoute>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </div>
      </div>
    </AudioProvider>
  );
}

export default App;
