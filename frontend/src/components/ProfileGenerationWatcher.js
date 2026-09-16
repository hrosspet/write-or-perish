import { useEffect, useRef, useState } from 'react';
import { useAsyncTaskPolling } from '../hooks/useAsyncTaskPolling';
import { useToast } from '../contexts/ToastContext';
import { useUser } from '../contexts/UserContext';

// The batch poller on the backend runs every 60 s, so polling the batch
// state faster shows nothing new. The synchronous task reports progress
// per chunk, so it is polled more often.
const BATCH_INTERVAL_MS = 60 * 1000;
const SYNC_INTERVAL_MS = 5 * 1000;
// Backstop for an endpoint that keeps failing for some user state: with
// no duration cap, this is what ends the polling.
const MAX_CONSECUTIVE_ERRORS = 10;

/**
 * App-wide watcher for profile generation (#131, #258).
 *
 * The single poller for /export/profile-progress — mounted in App so the
 * user is notified wherever they are when a build finishes. That endpoint
 * is the source of truth for BOTH pipelines: the synchronous Celery task
 * (progress from the task's state; staleness decided server-side) and
 * the Batch API chain (which sets no task id and was invisible to the
 * old per-task poller). Polling starts when the backend's user payload
 * says a build is running and stops when the endpoint says it is not, so
 * it needs no duration cap: a long chunked build is followed to the end.
 *
 * Broadcasts 'loore_profile_progress' (ProfilePage renders its inline
 * label from it) and 'loore_profile_done' on terminal states. Renders
 * nothing.
 */
export default function ProfileGenerationWatcher() {
  const { addToast } = useToast();
  const { user, setUser } = useUser();
  const [active, setActive] = useState(false);
  // Which pipeline is running: from the endpoint once it has answered,
  // before that from the user payload, so the batch cadence applies from
  // the first request and the hook does not restart on the first answer.
  const [source, setSource] = useState(null);
  const effectiveSource = source
    || (user?.profile_batch_pending ? 'batch' : 'sync');
  // The sync task last seen running: its `finally` clears the guard
  // before we can observe the terminal state, so the endpoint resolves
  // the outcome from this id once nothing is in flight.
  const [syncTaskId, setSyncTaskId] = useState(null);
  // Newest saved version when polling started; a batch chain has no
  // terminal task state, so "a new version landed" is its success signal.
  const startVersionRef = useRef(undefined);

  // Pre-#258 the task id was mirrored here; the backend payload is the
  // only source now.
  useEffect(() => {
    localStorage.removeItem('loore_profile_task_id');
  }, []);

  // Adopt a build the backend reports as running (fresh load,
  // cross-browser continuation).
  useEffect(() => {
    if (user?.profile_generation_task_id || user?.profile_batch_pending) {
      setActive(true);
    }
  }, [user]);

  // Generation started from the UI in this tab.
  useEffect(() => {
    const handler = () => setActive(true);
    window.addEventListener('loore_profile_started', handler);
    return () => window.removeEventListener('loore_profile_started', handler);
  }, []);

  const endpoint = active
    ? `/export/profile-progress${syncTaskId ? `?task_id=${encodeURIComponent(syncTaskId)}` : ''}`
    : null;
  const { data, error } = useAsyncTaskPolling(endpoint, {
    interval: effectiveSource === 'batch' ? BATCH_INTERVAL_MS : SYNC_INTERVAL_MS,
    enabled: active,
    maxDuration: 0,
    maxConsecutiveErrors: MAX_CONSECUTIVE_ERRORS,
  });

  const reset = () => {
    setActive(false);
    setSource(null);
    setSyncTaskId(null);
    startVersionRef.current = undefined;
    // Drop the running flags from the cached user so this build is not
    // re-adopted (re-fetched flags reflect any new one).
    setUser((prev) => (prev
      ? { ...prev, profile_generation_task_id: null, profile_batch_pending: false }
      : prev));
  };

  // The hook gave up (consecutive request failures): stop quietly and
  // clear the indicator rather than leave a label nothing updates.
  useEffect(() => {
    if (!active || !error || data) return;
    console.warn('Profile progress polling stopped:', error);
    reset();
    window.dispatchEvent(new Event('loore_profile_done'));
  }, [error]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!active || !data) return;

    if (data.running) {
      if (data.source !== source) setSource(data.source);
      if (data.source === 'sync' && data.task_id && data.task_id !== syncTaskId) {
        setSyncTaskId(data.task_id);
      }
      if (startVersionRef.current === undefined) {
        startVersionRef.current = data.latest_profile?.id ?? null;
      }
      window.dispatchEvent(new CustomEvent('loore_profile_progress', {
        detail: {
          running: true,
          status: data.status,
          progress: data.progress || 0,
          message: data.message,
          source: data.source,
          latestProfileId: data.latest_profile?.id ?? null,
        },
      }));
      return;
    }

    // Terminal. `status` is authoritative for the sync task. A batch
    // chain ends as `idle`: its last step failed (the backend says so),
    // or it finished — a version landed since the watch began.
    const sawRunning = startVersionRef.current !== undefined;
    const newVersion = sawRunning
      && (data.latest_profile?.id ?? null) !== startVersionRef.current;
    let outcome = null;
    if (data.status === 'failed') {
      outcome = 'failed';
    } else if (data.status === 'stalled' || (data.status === 'idle' && sawRunning && data.batch_step_failed)) {
      outcome = 'stalled';
    } else if (data.status === 'completed' || (data.status === 'idle' && newVersion)) {
      outcome = 'completed';
    } else if (data.status === 'idle' && sawRunning) {
      outcome = 'stalled';
    }

    reset();

    if (outcome) {
      window.dispatchEvent(new CustomEvent('loore_profile_progress', {
        detail: { running: false, status: outcome, progress: 0, message: '' },
      }));
    }
    window.dispatchEvent(new Event('loore_profile_done'));
    if (!outcome) return; // nothing was running (stale cached flag)
    if (outcome === 'completed') {
      addToast('Your profile has been updated ✓', 6000);
    } else if (outcome === 'failed') {
      addToast('Profile generation failed');
    } else {
      addToast('Profile generation stopped before finishing — it will be retried in the background');
    }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  return null;
}
