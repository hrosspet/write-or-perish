import React, { useState, useEffect, useRef } from 'react';
import { FaChevronDown } from 'react-icons/fa';
import api from '../api';

// The last option of the short list: picking it swaps in the full list.
const MORE = '__more__';
// Full-list groups, in the order they are shown.
const PROVIDERS = [
  { id: 'anthropic', label: 'Anthropic' },
  { id: 'openai', label: 'OpenAI' },
];

/**
 * The options a picker shows (#355).
 *
 * purpose 'read': only the models a read runs on.
 * purpose 'chat', collapsed: the featured models plus the selected one
 *   (first, when it is not featured), then "More models…".
 * purpose 'chat', expanded: every active model, grouped by provider.
 *
 * `models` comes from /nodes/models: active only, newest first within
 * each provider.
 */
export const pickerOptions = (models, selectedId, purpose, expanded) => {
  if (purpose === 'read') {
    return { kind: 'flat', models: models.filter((m) => m.read) };
  }
  if (expanded) {
    return {
      kind: 'grouped',
      groups: PROVIDERS
        .map((p) => ({ ...p, models: models.filter((m) => m.provider === p.id) }))
        .filter((g) => g.models.length > 0),
    };
  }
  const byProvider = PROVIDERS.flatMap((p) => models.filter((m) => m.provider === p.id));
  const featured = byProvider.filter((m) => m.featured);
  const selected = models.find((m) => m.id === selectedId);
  const short = selected && !selected.featured ? [selected, ...featured] : featured;
  return { kind: 'flat', models: short, more: short.length < models.length };
};

/**
 * Model picker, shown next to the action it sets the model for (LLM
 * Response, Read) and on the Account page.
 *
 * Its default comes from the backend: the model of the nearest earlier
 * reply of the same kind in the thread (a read for purpose 'read', a
 * chat reply otherwise), else the account preference / server default.
 * A thread predecessor overrides whatever was selected; otherwise the
 * default only fills an empty selection or replaces one this picker
 * cannot offer (a deprecated preference, a chat model in the Read picker).
 */
const ModelSelector = ({
  nodeId, selectedModel, onModelChange, purpose = 'chat', disabled = false,
  style: styleProp,
}) => {
  const [loading, setLoading] = useState(true);
  const [models, setModels] = useState(null);
  const [suggestion, setSuggestion] = useState(null);
  const [expanded, setExpanded] = useState(false);
  const selectRef = useRef(null);
  const openOnExpand = useRef(false);
  const appliedFor = useRef(null);

  useEffect(() => {
    // Fetch available models from backend (single source of truth)
    api.get('/nodes/models')
      .then((response) => setModels(response.data.models))
      .catch((error) => console.error('Error fetching models:', error));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const url = nodeId ? `/nodes/${nodeId}/suggested-model` : '/nodes/default-model';
    const params = purpose === 'read' ? { purpose: 'read' } : undefined;
    api.get(url, { params })
      .then((response) => { if (!cancelled) setSuggestion(response.data); })
      .catch((error) => console.error('Error fetching model:', error))
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [nodeId, purpose]);

  // Apply the suggestion once per node, when both lists are in.
  useEffect(() => {
    if (!suggestion || !models) return;
    const key = `${nodeId}:${purpose}`;
    if (appliedFor.current === key) return;
    appliedFor.current = key;
    const offered = models.filter((m) => purpose !== 'read' || m.read);
    const selectable = offered.some((m) => m.id === selectedModel);
    if (suggestion.source === 'predecessor' || !selectable) {
      if (suggestion.suggested_model !== selectedModel) {
        onModelChange(suggestion.suggested_model);
      }
    }
  }, [suggestion, models, nodeId, purpose, selectedModel, onModelChange]);

  // "More models…" reopens the list it swapped in where the browser
  // allows it (showPicker needs the click's user activation); elsewhere
  // the full list is there on the next tap.
  useEffect(() => {
    if (!expanded || !openOnExpand.current) return;
    openOnExpand.current = false;
    try {
      selectRef.current?.showPicker?.();
    } catch (e) {
      // Not supported, or the activation expired: the list stays expanded.
    }
  }, [expanded]);

  const handleChange = (e) => {
    if (e.target.value === MORE) {
      openOnExpand.current = true;
      setExpanded(true);
      return;
    }
    setExpanded(false);
    onModelChange(e.target.value);
  };

  const options = pickerOptions(models || [], selectedModel, purpose, expanded);
  const renderOption = (m) => <option key={m.id} value={m.id}>{m.name}</option>;
  const inactive = loading || disabled;

  return (
    <span style={{ position: 'relative', display: 'inline-flex', alignItems: 'stretch' }}>
      <select
        ref={selectRef}
        className="model-selector-dropdown"
        aria-label={purpose === 'read' ? 'Model for Read' : 'Model'}
        value={selectedModel || ''}
        onChange={handleChange}
        onBlur={() => setExpanded(false)}
        disabled={inactive}
        style={{
          padding: '8px 28px 8px 10px',
          borderRadius: '6px',
          border: '1px solid var(--border)',
          backgroundColor: 'var(--bg-input)',
          color: 'var(--text-secondary)',
          cursor: inactive ? 'not-allowed' : 'pointer',
          opacity: disabled ? 0.45 : 1,
          fontSize: '14px',
          fontFamily: 'var(--sans)',
          fontWeight: 300,
          WebkitAppearance: 'none',
          appearance: 'none',
          ...styleProp,
        }}
      >
        {!selectedModel && <option value="" disabled>…</option>}
        {options.kind === 'grouped'
          ? options.groups.map((g) => (
            <optgroup key={g.id} label={g.label}>
              {g.models.map(renderOption)}
            </optgroup>
          ))
          : options.models.map(renderOption)}
        {options.more && <option value={MORE}>More models…</option>}
      </select>
      <FaChevronDown
        aria-hidden="true"
        size={10}
        style={{
          position: 'absolute', right: '10px', top: '50%',
          transform: 'translateY(-50%)', pointerEvents: 'none',
          color: 'var(--text-muted)', opacity: disabled ? 0.45 : 1,
        }}
      />
    </span>
  );
};

export default ModelSelector;
