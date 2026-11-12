import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api';
import { groupByTurn } from '../components/ToolApprovalDialog';
import { useOptionalAudio } from '../contexts/AudioContext';

const NOT_RUN = "Loore couldn't make this change. It still waits for your approval.";

/**
 * The AI's calls in a thread that wait for the user's approval (Peter,
 * 2026-11-12), as the thread page and the Voice screen show them in
 * ToolApprovalDialog: the list, whether the dialog is open, and each
 * call's busy state and error.
 *
 * show(items, { open }) sets the list (the node page's pending_approvals)
 * and opens the dialog when calls wait, unless open is false. refresh(nodeId,
 * { open }) asks GET /tool-approvals for the thread of nodeId; it resolves to
 * the list, or null when the request failed. decide(item, approve, onDecided)
 * approves or rejects one call; the answer lists the calls of its reply that
 * still wait, and a call another device decided (404) just goes. Closing
 * the dialog decides nothing.
 */
export function useToolApprovals() {
  const [pending, setPending] = useState([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState({});
  const [errors, setErrors] = useState({});
  // The player, where there is one: a reply's spoken approval announcement
  // in its queue goes once that reply's changes are all decided (Peter,
  // 2026-11-12: "when it's not pending, it's not voiced out anymore").
  const audio = useOptionalAudio();
  const audioRef = useRef(audio);
  audioRef.current = audio;
  const recheckAnnouncement = useCallback(() => {
    const player = audioRef.current;
    const nodeId = player && player.announcementNodeId
      ? player.announcementNodeId() : null;
    if (nodeId == null) return;
    api.get('/voice/approval-announcement', { params: { node: nodeId } })
      .then((res) => {
        if (!res?.data?.url && audioRef.current) audioRef.current.dropAnnouncement();
      })
      .catch(() => {});
  }, []);
  // Every call decided: the dialog is closed, so calls found later open it
  // only when asked to.
  useEffect(() => {
    if (!pending.length) setOpen(false);
  }, [pending.length]);

  const show = useCallback((items, { open: openNow = true } = {}) => {
    const list = items || [];
    setPending(list);
    if (list.length && openNow) setOpen(true);
  }, []);

  const refresh = useCallback((nodeId, options) => {
    if (nodeId == null) return Promise.resolve(null);
    return api.get('/tool-approvals', { params: { node: nodeId } })
      .then((res) => {
        const list = res.data?.pending || [];
        show(list, options);
        return list;
      })
      .catch(() => null);
  }, [show]);

  const decide = useCallback(async (item, approve, onDecided) => {
    setBusy((b) => ({ ...b, [item.id]: true }));
    setErrors((e) => ({ ...e, [item.id]: null }));
    const replaceTurn = (rest) => setPending((all) => [
      ...all.filter((p) => p.turn_node_id !== item.turn_node_id),
      ...(rest || []),
    ]);
    try {
      const res = await api.post(
        `/tool-approvals/${item.id}/${approve ? 'approve' : 'reject'}`);
      replaceTurn(res.data?.pending);
      if (res.data?.outcome === 'failed') {
        setErrors((e) => ({ ...e, [item.id]: res.data.error }));
      } else {
        recheckAnnouncement();
        if (onDecided) onDecided(res.data);
      }
    } catch (err) {
      if (err?.response?.status === 404) {
        // Decided elsewhere (another device): the announcement may go too.
        setPending((all) => all.filter((p) => p.id !== item.id));
        recheckAnnouncement();
      } else {
        setErrors((e) => ({
          ...e,
          [item.id]: err?.response?.data?.error || NOT_RUN,
        }));
      }
    } finally {
      setBusy((b) => ({ ...b, [item.id]: false }));
    }
  }, [recheckAnnouncement]);

  return {
    pending,
    groups: groupByTurn(pending),
    open,
    setOpen,
    busy,
    errors,
    show,
    refresh,
    decide,
  };
}
