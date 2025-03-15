import React, { useState } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import LandingPage from "./components/LandingPage";
import Dashboard from "./components/Dashboard";
import Feed from "./components/Feed";
import NodeDetail from "./components/NodeDetail";
import NavBar from "./components/NavBar";
import NodeForm from "./components/NodeForm";

function App() {
  // State controlling the display of the new entry modal
  const [showNewEntry, setShowNewEntry] = useState(false);

  return (
    <div>
      {/* Pass the handler to NavBar so clicking "Write New Entry" opens the modal */}
      <NavBar onNewEntryClick={() => setShowNewEntry(true)} />
      
      {/* Global New Entry Modal */}
      {showNewEntry && (
        <div style={{
          position: 'fixed',
          top: 0, left: 0, right: 0, bottom: 0,
          backgroundColor: 'rgba(0, 0, 0, 0.8)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          zIndex: 1000
        }}>
          <div style={{
            position: 'relative',
            background: '#1e1e1e',
            padding: '20px',
            borderRadius: '8px',
            width: '400px'
          }}>
            {/* Cross icon at top-right */}
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
              onClick={() => setShowNewEntry(false)}
            >
              &times;
            </div>
            <h2 style={{ color: '#e0e0e0', marginBottom: '20px' }}>Write New Entry</h2>
            {/* Render NodeForm normally so its internal Submit button is visible */}
            <NodeForm
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