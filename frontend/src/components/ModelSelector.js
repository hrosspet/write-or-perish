import React, { useState, useEffect, useRef, useId } from 'react';
import { FaChevronDown, FaCheck } from 'react-icons/fa';
import api from '../api';

// The last row of the short list: choosing it expands the list in place.
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

const optionModels = (options) => (
  options.kind === 'grouped' ? options.groups.flatMap((g) => g.models) : options.models
);

// Room the list needs below the trigger before it opens upward instead.
const MENU_MAX_HEIGHT = 320;

/**
 * Model picker, shown next to the action it sets the model for (LLM
 * Response, Read) and on the Account page.
 *
 * A listbox of its own rather than a native <select>: a native menu
 * closes on every choice, so "More models…" could only swap the options
 * and reopen it, which flashed and collapsed. Here the list stays open
 * and grows in place. Keyboard: Enter / Space / arrows open it; arrows,
 * Home and End move; Enter or Space chooses; Escape closes.
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
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [activeId, setActiveId] = useState(null);
  const [dropUp, setDropUp] = useState(false);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const listRef = useRef(null);
  const appliedFor = useRef(null);
  const listId = useId();
  const optionDomId = (id) => `${listId}-${id}`;

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

  const inactive = loading || disabled || !models;
  const options = pickerOptions(models || [], selectedModel, purpose, expanded);
  const rows = optionModels(options);
  const navIds = [...rows.map((m) => m.id), ...(options.more ? [MORE] : [])];
  const selected = (models || []).find((m) => m.id === selectedModel);
  const label = purpose === 'read' ? 'Model for Read' : 'Model';

  const closeMenu = (refocus = true) => {
    setOpen(false);
    setExpanded(false);
    if (refocus) triggerRef.current?.focus();
  };

  const openMenu = () => {
    if (inactive) return;
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      const below = window.innerHeight - rect.bottom;
      setDropUp(below < MENU_MAX_HEIGHT && rect.top > below);
    }
    setActiveId(navIds.includes(selectedModel) ? selectedModel : navIds[0]);
    setOpen(true);
  };

  const choose = (id) => {
    if (id === MORE) {
      // Grow the open list; keep the keyboard on the first model it adds.
      const shown = new Set(rows.map((m) => m.id));
      const added = optionModels(pickerOptions(models, selectedModel, purpose, true))
        .find((m) => !shown.has(m.id));
      setExpanded(true);
      setActiveId(added ? added.id : selectedModel);
      return;
    }
    if (id !== selectedModel) onModelChange(id);
    closeMenu();
  };

  // Keys go to the list while it is open.
  useEffect(() => {
    if (open) listRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open || !activeId) return;
    const el = document.getElementById(optionDomId(activeId));
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, activeId, expanded]);

  // A click or tap anywhere else closes it.
  useEffect(() => {
    if (!open) return undefined;
    const onPointer = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) {
        setOpen(false);
        setExpanded(false);
      }
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('touchstart', onPointer);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('touchstart', onPointer);
    };
  }, [open]);

  const onTriggerKeyDown = (e) => {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) {
      e.preventDefault();
      openMenu();
    }
  };

  const onListKeyDown = (e) => {
    const i = navIds.indexOf(activeId);
    const move = (to) => {
      e.preventDefault();
      setActiveId(navIds[Math.max(0, Math.min(navIds.length - 1, to))]);
    };
    switch (e.key) {
      case 'ArrowDown': move(i + 1); break;
      case 'ArrowUp': move(i - 1); break;
      case 'Home': move(0); break;
      case 'End': move(navIds.length - 1); break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        if (activeId) choose(activeId);
        break;
      case 'Escape':
        e.preventDefault();
        closeMenu();
        break;
      case 'Tab':
        setOpen(false);
        setExpanded(false);
        break;
      default:
    }
  };

  const rowStyle = (id) => ({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '16px',
    padding: '8px 12px',
    borderRadius: '5px',
    cursor: 'pointer',
    whiteSpace: 'nowrap',
    fontSize: '14px',
    color: id === selectedModel ? 'var(--text-primary)' : 'var(--text-secondary)',
    backgroundColor: id === activeId ? 'var(--accent-subtle)' : 'transparent',
  });

  const renderOption = (m) => (
    <div
      key={m.id}
      id={optionDomId(m.id)}
      role="option"
      aria-selected={m.id === selectedModel}
      onMouseDown={(e) => e.preventDefault()}
      onMouseEnter={() => setActiveId(m.id)}
      onClick={() => choose(m.id)}
      style={rowStyle(m.id)}
    >
      <span>{m.name}</span>
      {m.id === selectedModel && (
        <FaCheck aria-hidden="true" size={10} style={{ color: 'var(--accent)' }} />
      )}
    </div>
  );

  return (
    <span ref={rootRef} style={{ position: 'relative', display: 'inline-flex', alignItems: 'stretch' }}>
      <button
        ref={triggerRef}
        type="button"
        className="model-selector-dropdown"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={`${label}: ${selected ? selected.name : 'loading'}`}
        disabled={inactive}
        onClick={() => (open ? closeMenu() : openMenu())}
        onKeyDown={onTriggerKeyDown}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '10px',
          padding: '8px 10px',
          borderRadius: '6px',
          border: '1px solid var(--border)',
          backgroundColor: 'var(--bg-input)',
          color: 'var(--text-secondary)',
          cursor: inactive ? 'not-allowed' : 'pointer',
          fontSize: '14px',
          fontFamily: 'var(--sans)',
          fontWeight: 300,
          whiteSpace: 'nowrap',
          ...styleProp,
        }}
      >
        <span>{selected ? selected.name : '…'}</span>
        <FaChevronDown aria-hidden="true" size={10} style={{ color: 'var(--text-muted)' }} />
      </button>
      {open && (
        <div
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label={label}
          aria-activedescendant={activeId ? optionDomId(activeId) : undefined}
          tabIndex={-1}
          onKeyDown={onListKeyDown}
          style={{
            position: 'absolute',
            [dropUp ? 'bottom' : 'top']: 'calc(100% + 4px)',
            left: 0,
            minWidth: '100%',
            maxHeight: `${MENU_MAX_HEIGHT}px`,
            overflowY: 'auto',
            padding: '4px',
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: '8px',
            boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
            zIndex: 1000,
            fontFamily: 'var(--sans)',
            fontWeight: 300,
            outline: 'none',
          }}
        >
          {options.kind === 'grouped'
            ? options.groups.map((g) => (
              <div key={g.id} role="group" aria-labelledby={`${listId}-g-${g.id}`}>
                <div
                  id={`${listId}-g-${g.id}`}
                  style={{
                    padding: '8px 12px 4px',
                    fontSize: '12px',
                    color: 'var(--text-muted)',
                  }}
                >
                  {g.label}
                </div>
                {g.models.map(renderOption)}
              </div>
            ))
            : options.models.map(renderOption)}
          {options.more && (
            <div
              id={optionDomId(MORE)}
              role="option"
              aria-selected={false}
              onMouseDown={(e) => e.preventDefault()}
              onMouseEnter={() => setActiveId(MORE)}
              onClick={() => choose(MORE)}
              style={{
                ...rowStyle(MORE),
                color: 'var(--text-muted)',
                marginTop: '4px',
                borderTop: '1px solid var(--border)',
                borderTopLeftRadius: 0,
                borderTopRightRadius: 0,
              }}
            >
              More models…
            </div>
          )}
        </div>
      )}
    </span>
  );
};

export default ModelSelector;
