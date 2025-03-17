import React, { useState, useEffect, useRef } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import LandingPage from "./components/LandingPage";
import Dashboard from "./components/Dashboard";
import Feed from "./components/Feed";
import NodeDetail from "./components/NodeDetail";
import NavBar from "./components/NavBar";
import NodeForm from "./components/NodeForm";

function App() {
  const [showNewEntry, setShowNewEntry] = useState(false);
  const nodeFormRef = useRef(null);

  // Function to handle closing the modal.
  const handleCloseModal = () => {
    // If NodeForm is dirty (contains text), ask for confirmation.
    if (nodeFormRef.current && nodeFormRef.current.isDirty()) {
      const confirmed = window.confirm(
        "You have unsaved changes. Are you sure you want to close?"
      );
      if (!confirmed) {
        return; // Do nothing if not confirmed.
      }
    }
    setShowNewEntry(false);
  };

  // Escape key handler to close modal with confirmation.
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
      {/* Pass onNewEntryClick to NavBar */}
      <NavBar onNewEntryClick={() => setShowNewEntry(true)} />
      
      {/* Global New Entry Modal */}
      {showNewEntry && (
        <div
          onClick={handleCloseModal}  // Clicking outside triggers close with confirmation.
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
            onClick={(e) => e.stopPropagation()}  // Prevent clicks inside the modal from closing it.
            style={{
              position: 'relative',
              background: '#1e1e1e',
              padding: '20px',
              borderRadius: '8px',
              width: '400px'
            }}
          >
            {/* Cross icon to cancel */}
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
            {/* Render NodeForm normally so its internal Submit button is visible */}
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
      
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/dashboard/:username" element={<Dashboard />} />
        <Route path="/feed" element={<Feed />} />
        <Route path="/node/:id" element={<NodeDetail />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}

export default App;