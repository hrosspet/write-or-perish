import React, { useState, useEffect, useRef } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import LandingPage from "./components/LandingPage";
import Dashboard from "./components/Dashboard";
import Feed from "./components/Feed";
import NodeDetail from "./components/NodeDetail";
import NavBar from "./components/NavBar";
import NodeForm from "./components/NodeForm";
import TermsModal from "./components/TermsModal";
import AdminPanel from "./components/AdminPanel";
import AlphaModal from "./components/AlphaModal";
import { useUser } from "./contexts/UserContext";

function App() {
  const [showNewEntry, setShowNewEntry] = useState(false);
  const nodeFormRef = useRef(null);
  const [showTerms, setShowTerms] = useState(false);
  const [showAlpha, setShowAlpha] = useState(false);
  const { user, setUser } = useUser();

  // When the user info is loaded, check if they have accepted the terms.
  useEffect(() => {
    if (user) {
      // Show terms modal if not accepted.
      if (!user.accepted_terms_at) {
        setShowTerms(true);
      } else {
        setShowTerms(false);
      }
      // Also, if they have accepted terms but are not approved (and haven’t provided an email),
      // show the waiting-list modal.
      // if (user.accepted_terms_at && user.approved === false && !user.email) {
      if (user.accepted_terms_at && !user.approved) {
        setShowAlpha(true);
      } else {
        setShowAlpha(false);
      }
    }
  }, [user]);

  const handleCloseModal = () => {
    if (nodeFormRef.current && nodeFormRef.current.isDirty()) {
      const confirmed = window.confirm("You have unsaved changes. Are you sure you want to close?");
      if (!confirmed) {
        return;
      }
    }
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
    <div>
      <NavBar onNewEntryClick={() => setShowNewEntry(true)} />
      {showNewEntry && (
        <div
          onClick={handleCloseModal}
          style={{
            position: 'fixed',
            top: 0, left: 0, right: 0, bottom: 0,
            backgroundColor: 'rgba(0, 0, 0, 0.8)',
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
              background: '#1e1e1e',
              padding: '20px',
              borderRadius: '8px',
              width: '800px'
            }}
          >
            <div
              style={{
                position: 'absolute',
                top: '10px',
                right: '10px',
                fontSize: '24px',
                fontWeight: 'bold',
                color: '#e0e0e0',
                cursor: 'pointer'
              }}
              onClick={handleCloseModal}
            >
              &times;
            </div>
            <h2 style={{ color: '#e0e0e0', marginBottom: '20px' }}>
              Write New Entry
            </h2>
            <NodeForm
              ref={nodeFormRef}
              parentId={null}
              onSuccess={() => {
                setShowNewEntry(false);
                window.location.reload();
              }}
            />
          </div>
        </div>
      )}

      {/* Render the Terms Modal if the user hasn’t accepted the terms yet */}
      {showTerms && (
        <TermsModal
          onAccepted={(acceptedTimestamp) => {
            setUser({ ...user, accepted_terms_at: acceptedTimestamp });
            setShowTerms(false);
          }}
        />
      )}

      {showAlpha && (
        <AlphaModal
        user={user}
          onClose={() => setShowAlpha(false)}
          onUpdate={(updatedUser) => setUser(updatedUser)}
        />
      )}

      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/dashboard/:username" element={<Dashboard />} />
        <Route path="/feed" element={<Feed />} />
        <Route path="/node/:id" element={<NodeDetail />} />
        <Route path="/admin" element={<AdminPanel />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}

export default App;