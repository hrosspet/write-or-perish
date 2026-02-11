import React, { useState, useEffect, useRef } from 'react';
import { Routes, Route, Navigate, useNavigate } from 'react-router-dom';
import LandingPage from "./components/LandingPage";
import Dashboard from "./components/Dashboard";
import Feed from "./components/Feed";
import NavBar from "./components/NavBar";
import NodeForm from "./components/NodeForm";
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
import { useUser } from "./contexts/UserContext";
import { AudioProvider } from "./contexts/AudioContext";

function App() {
  const [showNewEntry, setShowNewEntry] = useState(false);
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

  const handleCloseModal = () => {
    // No confirmation needed for new entries since they're auto-saved as drafts
    setShowNewEntry(false);
  };

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") {
        handleCloseModal();
      }
    };
    if (showNewEntry) {
      window.addEventListener("keydown", handleKeyDown);
    }
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [showNewEntry]);

  return (
    <AudioProvider>
      <div>
        <NavBar onNewEntryClick={() => setShowNewEntry(true)} />
        <div style={{ paddingTop: "60px" }}>
        {showNewEntry && (
        <div
          onClick={handleCloseModal}
          style={{
            position: 'fixed',
            top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: 'rgba(5, 4, 3, 0.75)',
            backdropFilter: 'blur(10px)',
            WebkitBackdropFilter: 'blur(10px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              position: 'relative',
              background: 'var(--bg-card)',
              border: '1px solid var(--border)',
              padding: '2rem',
              borderRadius: '12px',
              width: '1170px',
              maxWidth: '90vw',
              maxHeight: '90vh',
              overflowY: 'auto',
              boxShadow: '0 24px 80px rgba(0,0,0,0.5)',
            }}
          >
            <button
              style={{
                position: 'absolute',
                top: '12px',
                right: '16px',
                fontSize: '24px',
                color: 'var(--text-muted)',
                cursor: 'pointer',
                background: 'none',
                border: 'none',
                padding: '4px',
                lineHeight: 1,
              }}
              onClick={handleCloseModal}
            >
              &times;
            </button>
            <h2 style={{
              fontFamily: 'var(--serif)',
              fontSize: '1.4rem',
              fontWeight: 300,
              color: 'var(--text-primary)',
              marginBottom: '20px',
            }}>
              Write New Entry
            </h2>
            <NodeForm
              ref={nodeFormRef}
              parentId={null}
              onSuccess={(data) => {
                setShowNewEntry(false);
                navigate(`/node/${data.id}`);
              }}
            />
          </div>
        </div>
      )}

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
          <Route path="/" element={<LandingPage />} />
          <Route path="/login" element={<LoginPage />} />
          {/* Public about pages */}
          <Route path="/vision" element={<VisionPage />} />
          <Route path="/why-loore" element={<WhyLoorePage />} />
          <Route path="/how-to" element={<HowToPage />} />
          {/* Alpha thank you - public (for unapproved users) */}
          <Route path="/alpha-thank-you" element={<AlphaThankYouPage />} />
          {/* Welcome - protected (for newly approved users) */}
          <Route path="/welcome" element={<ProtectedRoute><WelcomePage onNewEntryClick={() => setShowNewEntry(true)} /></ProtectedRoute>} />
          <Route path="/dashboard" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
          <Route path="/dashboard/:username" element={<Dashboard />} />
          <Route path="/feed" element={<ProtectedRoute><Feed /></ProtectedRoute>} />
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
