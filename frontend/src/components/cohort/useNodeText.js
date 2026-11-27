import { useCallback, useEffect, useState } from 'react';
import api from '../../api';

// The text of one of the person's own entries (the cohort report, the
// introduction), read and saved through the normal node routes: GET
// /api/nodes/<id>, and PUT with the text the editor loaded as
// `base_content`, so a newer text saved elsewhere is never overwritten
// (the server answers 409 content_changed, with its own sentence).
export default function useNodeText(nodeId) {
  const [base, setBase] = useState(null);
  const [text, setText] = useState('');
  const [loadError, setLoadError] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  useEffect(() => {
    if (!nodeId) {
      setBase(null);
      setText('');
      return undefined;
    }
    let alive = true;
    setBase(null);
    setLoadError('');
    api.get(`/nodes/${nodeId}`)
      .then((res) => {
        if (!alive) return;
        const content = (res.data && res.data.content) || '';
        setBase(content);
        setText(content);
      })
      .catch(() => {
        if (alive) setLoadError('This text could not be loaded. Reload the page to try again.');
      });
    return () => { alive = false; };
  }, [nodeId]);

  const edit = useCallback((value) => {
    setText(value);
    setSavedAt(null);
  }, []);

  // Saves the text if it changed. Resolves to true once the server holds
  // the text in the editor; rejects with the server's answer otherwise.
  const save = useCallback(async () => {
    if (base === null) return false;
    if (text === base) return true;
    setSaving(true);
    try {
      await api.put(`/nodes/${nodeId}`, { content: text, base_content: base });
      setBase(text);
      setSavedAt(Date.now());
      return true;
    } finally {
      setSaving(false);
    }
  }, [base, text, nodeId]);

  return {
    loaded: base !== null,
    text,
    setText: edit,
    dirty: base !== null && text !== base,
    saving,
    saved: savedAt !== null,
    loadError,
    save,
  };
}
