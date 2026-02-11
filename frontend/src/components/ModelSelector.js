import React, { useState, useEffect } from 'react';
import api from '../api';

/**
 * ModelSelector Component
 *
 * Standalone dropdown component for selecting LLM model.
 * Displays inline next to the "LLM Response" button.
 * Automatically fetches the suggested model based on thread context.
 */
const ModelSelector = ({ nodeId, selectedModel, onModelChange }) => {
  const [loading, setLoading] = useState(true);
  const [models, setModels] = useState([]);

  useEffect(() => {
    // Fetch available models from backend (single source of truth)
    api.get('/nodes/models')
      .then((response) => setModels(response.data.models))
      .catch((error) => console.error('Error fetching models:', error));
  }, []);

  useEffect(() => {
    const fetchSuggestedModel = async () => {
      try {
        const response = await api.get(`/nodes/${nodeId}/suggested-model`);
        onModelChange(response.data.suggested_model);
      } catch (error) {
        console.error('Error fetching suggested model:', error);
        onModelChange('claude-opus-4.5'); // Fallback to default
      } finally {
        setLoading(false);
      }
    };

    if (nodeId) {
      fetchSuggestedModel();
    } else {
      setLoading(false); // No node, so not loading
    }
  }, [nodeId, onModelChange]);

  return (
    <select
      className="model-selector-dropdown"
      value={selectedModel || 'claude-opus-4.5'}
      onChange={(e) => onModelChange(e.target.value)}
      disabled={loading}
      style={{
        marginRight: '8px',
        padding: '8px 12px',
        borderRadius: '6px',
        border: '1px solid var(--border)',
        backgroundColor: 'var(--bg-input)',
        color: 'var(--text-secondary)',
        cursor: loading ? 'not-allowed' : 'pointer',
        fontSize: '14px',
        fontFamily: 'var(--sans)',
        fontWeight: 300,
        maxWidth: '150px',
      }}
    >
      {models.map((m) => (
        <option key={m.id} value={m.id}>
          {m.name}
        </option>
      ))}
    </select>
  );
};

export default ModelSelector;
