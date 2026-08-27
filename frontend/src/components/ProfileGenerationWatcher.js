import { useEffect, useRef, useState } from 'react';
import { useAsyncTaskPolling } from '../hooks/useAsyncTaskPolling';
import { useToast } from '../contexts/ToastContext';
import { useUser } from '../contexts/UserContext';

/**
 * App-wide watcher for profile generation (#131).
 *
 * The single poller for /export/profile-status — mounted in App so the
 * user is notified wherever they are when generation finishes (before
 * this, completion was only visible while ProfilePage was open).
 * Broadcasts progress via the 'loore_profile_progress' CustomEvent
 * (ProfilePage renders its inline indicator from it) and the existing
 * 'loore_profile_done' event on terminal states. Renders nothing.
 */
export default function ProfileGenerationWatcher() {
  const { addToast } = useToast();
  const { user, setUser } = useUser();
  const [taskId, setTaskId] = useState(
    () => localStorage.getItem('loore_profile_task_id')
  );
  // Task ids that already reached a terminal state. The cached `user`
  // object may still carry the finished id (it was fetched while the
  // task was running); re-adopting it would restart polling, re-fire
  // the completion toast, and loop — dozens of toasts in seconds.
  const finishedIdsRef = useRef(new Set());

  // Pick up task ID from backend if localStorage doesn't have it
  // (cross-browser continuation).
  useEffect(() => {
    const backendId = user?.profile_generation_task_id;
    if (!taskId && backendId && !finishedIdsRef.current.has(backendId)) {
      localStorage.setItem('loore_profile_task_id', backendId);
      setTaskId(backendId);
    }
  }, [user, taskId]);

  // Generation started from NavBar or ProfilePage.
  useEffect(() => {
    const handler = (e) => {
      const startedId = e.detail?.taskId
        || localStorage.getItem('loore_profile_task_id');
      if (startedId) setTaskId(startedId);
    };
    window.addEventListener('loore_profile_started', handler);
    return () => window.removeEventListener('loore_profile_started', handler);
  }, []);

  const { status, progress, data } = useAsyncTaskPolling(
    taskId ? `/export/profile-status/${taskId}` : null,
    { interval: 3000, enabled: !!taskId }
  );

  // Broadcast progress so ProfilePage can render its inline indicator
  // without running a second poller.
  useEffect(() => {
    if (!taskId || !status) return;
    window.dispatchEvent(new CustomEvent('loore_profile_progress', {
      detail: { status, progress, message: data?.message },
    }));
  }, [taskId, status, progress, data]);

  useEffect(() => {
    if (status !== 'completed' && status !== 'failed') return;
    if (taskId) finishedIdsRef.current.add(taskId);
    localStorage.removeItem('loore_profile_task_id');
    setTaskId(null);
    // Drop the stale id from the cached user too, so nothing else
    // treats the finished task as still running.
    if (user && user.profile_generation_task_id === taskId) {
      setUser({ ...user, profile_generation_task_id: null });
    }
    window.dispatchEvent(new Event('loore_profile_done'));
    if (status === 'completed') {
      addToast('Your profile has been updated ✓', 6000);
    } else {
      addToast('Profile generation failed');
    }
  }, [status]); // eslint-disable-line react-hooks/exhaustive-deps

  return null;
}
