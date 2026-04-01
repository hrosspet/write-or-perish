import React, { useState, useEffect } from 'react';
import api from '../api';

/**
 * ModelSelector Component
 *
 * Standalone dropdown component for selecting LLM model.
 * Displays inline next to the "LLM Response" button.
 * Automatically fetches the suggested model based on thread context.
 */
const ModelSelector = ({ nodeId, selectedModel, onModelChange, style: styleProp }) => {
  const [loading, setLoading] = useState(true);
  const [models, setModels] = useState([]);

  useEffect(() => {
    // Fetch available models from backend (single source of truth)
    api.get('/nodes/models')
      .then((response) => setModels(response.data.models))
      .catch((error) => console.error('Error fetching models:', error));
  }, []);

  useEffect(() => {
    const fetchModel = async () => {
      try {
        if (nodeId) {
          // Fetch thread-suggested model
          const response = await api.get(`/nodes/${nodeId}/suggested-model`);
          // Only override user preference if the suggestion comes from
          // an actual predecessor LLM node in the thread, not the default
          if (response.data.source === 'predecessor') {
            onModelChange(response.data.suggested_model);
          } else if (!selectedModel) {
            // Default fallback — only set if nothing selected
            onModelChange(response.data.suggested_model);
          }
        } else if (!selectedModel) {
          // No node context — fetch backend default
          const response = await api.get('/nodes/default-model');
          onModelChange(response.data.suggested_model);
        }
      } catch (error) {
        console.error('Error fetching model:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchModel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId]);

  return (
    <select
      className="model-selector-dropdown"
      value={selectedModel || (models.length > 0 ? models[0].id : '')}
      onChange={(e) => onModelChange(e.target.value)}
      disabled={loading}
      style={{
        padding: '8px 8px',
        borderRadius: '6px',
        border: '1px solid var(--border)',
        backgroundColor: 'var(--bg-input)',
        color: 'var(--text-secondary)',
        cursor: loading ? 'not-allowed' : 'pointer',
        fontSize: '14px',
        fontFamily: 'var(--sans)',
        fontWeight: 300,
        WebkitAppearance: 'none',
        appearance: 'none',
        ...styleProp,
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
