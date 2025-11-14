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

  useEffect(() => {
    // Fetch suggested model on mount
    const fetchSuggestedModel = async () => {
      try {
        const response = await api.get(`/nodes/${nodeId}/suggested-model`);
        onModelChange(response.data.suggested_model);
      } catch (error) {
        console.error('Error fetching suggested model:', error);
        // Fallback to default
        onModelChange('gpt-5');
      } finally {
        setLoading(false);
      }
    };

    fetchSuggestedModel();
  }, [nodeId, onModelChange]);

  // Available models for the dropdown
  const models = [
    { id: 'gpt-5', name: 'GPT-5', provider: 'OpenAI' },
    { id: 'gpt-5.1', name: 'GPT-5.1', provider: 'OpenAI' },
    { id: 'claude-sonnet-4.5', name: 'Claude 4.5 Sonnet', provider: 'Anthropic' },
    { id: 'claude-opus-4.1', name: 'Claude 4.1 Opus', provider: 'Anthropic' },
    { id: 'claude-opus-3', name: 'Claude 3 Opus', provider: 'Anthropic' },
  ];

  return (
    <select
      className="model-selector-dropdown"
      value={selectedModel || 'gpt-5'}
      onChange={(e) => onModelChange(e.target.value)}
      disabled={loading}
      style={{
        marginRight: '8px',
        padding: '8px 12px',
        borderRadius: '4px',
        border: '1px solid #ccc',
        backgroundColor: loading ? '#f5f5f5' : 'white',
        cursor: loading ? 'not-allowed' : 'pointer',
        fontSize: '14px',
        fontFamily: 'inherit',
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
