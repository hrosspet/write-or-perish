import React, { useEffect, useState } from 'react';
import api from '../api';
import { useToast } from '../contexts/ToastContext';
import { formatDate } from '../utils/date';

/**
 * ReferenceReadToggle - the owner's "Mark as read" / "Mark as unread"
 * for a saved reference (POST/DELETE /external/items/<id>/read). The
 * label alone carries the state. Only the user marks a reference read;
 * the AI surfacing it is tracked separately (surfaced_count).
 *
 * Props: itemId, readAt (server value), onChange(readAt) optional,
 * nodeId optional: the reply the reference is shown in, logged with the
 * mark for the recommendation record (#352).
 */
const ReferenceReadToggle = ({ itemId, readAt: serverReadAt, onChange, nodeId }) => {
  const { addToast } = useToast();
  const [readAt, setReadAt] = useState(serverReadAt || null);
  const [marking, setMarking] = useState(false);
  useEffect(() => { setReadAt(serverReadAt || null); }, [serverReadAt]);

  const toggle = (e) => {
    e.stopPropagation();
    setMarking(true);
    const body = nodeId ? { node_id: nodeId } : undefined;
    const req = readAt
      ? api.delete(`/external/items/${itemId}/read`, body ? { data: body } : undefined)
      : api.post(`/external/items/${itemId}/read`, body);
    req
      .then((res) => {
        setReadAt(res.data.read_at);
        if (onChange) onChange(res.data.read_at);
      })
      .catch(() => addToast('Could not update the read mark.', 4000))
      .finally(() => setMarking(false));
  };

  return (
    <button
      type="button"
      className="ext-quote-read-toggle"
      data-read={readAt ? 'true' : 'false'}
      onClick={toggle}
      disabled={marking}
      title={readAt ? `You read it ${formatDate(readAt)}` : undefined}
    >
      {readAt ? 'Mark as unread' : 'Mark as read'}
    </button>
  );
};

export default ReferenceReadToggle;
