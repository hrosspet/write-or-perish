import { useEffect, useState } from 'react';
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
  const { user } = useUser();
  const [taskId, setTaskId] = useState(
    () => localStorage.getItem('loore_profile_task_id')
  );

  // Pick up task ID from backend if localStorage doesn't have it
  // (cross-browser continuation).
  useEffect(() => {
    if (!taskId && user && user.profile_generation_task_id) {
      localStorage.setItem(
        'loore_profile_task_id', user.profile_generation_task_id);
      setTaskId(user.profile_generation_task_id);
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
    if (status === 'completed') {
      localStorage.removeItem('loore_profile_task_id');
      setTaskId(null);
      window.dispatchEvent(new Event('loore_profile_done'));
      addToast('Your profile has been updated ✓', 6000);
    } else if (status === 'failed') {
      localStorage.removeItem('loore_profile_task_id');
      setTaskId(null);
      window.dispatchEvent(new Event('loore_profile_done'));
      addToast('Profile generation failed');
    }
  }, [status]); // eslint-disable-line react-hooks/exhaustive-deps

  return null;
}
