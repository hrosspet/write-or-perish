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
 * It also follows a pre-fill load's final merge that goes on after the
 * person was let in (#731, `profile_merge_pending`, source "merge"): when
 * the merge saves its version the person gets the usual "updated"
 * notice; when it ends without one, nothing is said.
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
  // The late final merge last seen running (#731): once it ends, the
  // endpoint says from this id whether it saved a version.
  const [mergeId, setMergeId] = useState(null);
  // Newest saved version when polling started; a batch chain has no
  // terminal task state, so "a new version landed" is its success signal.
  const startVersionRef = useRef(undefined);
  // A batch or sync build was seen running during this watch: a late
  // merge that then ends without a version does not hide its outcome.
  const sawBuildRef = useRef(false);

  // Pre-#258 the task id was mirrored here; the backend payload is the
  // only source now.
  useEffect(() => {
    localStorage.removeItem('loore_profile_task_id');
  }, []);

  // Adopt a build the backend reports as running (fresh load,
  // cross-browser continuation).
  useEffect(() => {
    if (user?.profile_generation_task_id || user?.profile_batch_pending
        || user?.profile_merge_pending) {
      setActive(true);
    }
  }, [user]);

  // Generation started from the UI in this tab.
  useEffect(() => {
    const handler = () => setActive(true);
    window.addEventListener('loore_profile_started', handler);
    return () => window.removeEventListener('loore_profile_started', handler);
  }, []);

  const query = [
    syncTaskId && `task_id=${encodeURIComponent(syncTaskId)}`,
    mergeId && `merge_id=${encodeURIComponent(mergeId)}`,
  ].filter(Boolean).join('&');
  const endpoint = active
    ? `/export/profile-progress${query ? `?${query}` : ''}`
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
    setMergeId(null);
    startVersionRef.current = undefined;
    sawBuildRef.current = false;
    // Drop the running flags from the cached user so this build is not
    // re-adopted (re-fetched flags reflect any new one).
    setUser((prev) => (prev
      ? {
        ...prev,
        profile_generation_task_id: null,
        profile_batch_pending: false,
        profile_merge_pending: false,
      }
      : prev));
  };

  // The hook gave up (consecutive request failures): stop quietly and
  // clear the indicator rather than leave a label nothing updates. The
  // hook keeps the last good `data` across failed requests, so that is
  // not a sign the endpoint is still answering; the one case to leave to
  // the data effect is a `failed` answer, which also sets `error`.
  useEffect(() => {
    if (!active || !error || data?.status === 'failed') return;
    console.warn('Profile progress polling stopped:', error);
    reset();
    window.dispatchEvent(new Event('loore_profile_done'));
  }, [error]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!active || !data) return;

    if (data.running) {
      if (data.source !== source) setSource(data.source);
      if (data.source === 'batch' || data.source === 'sync') {
        sawBuildRef.current = true;
      }
      if (data.source === 'sync' && data.task_id && data.task_id !== syncTaskId) {
        setSyncTaskId(data.task_id);
      }
      if (data.source === 'merge' && data.merge_id && data.merge_id !== mergeId) {
        setMergeId(data.merge_id);
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

    // Terminal. `status` is authoritative for the sync task and for a
    // late merge (`completed` or `unchanged`). A batch chain ends as
    // `idle`: its last step failed (the backend says so), or it finished
    // — a version landed since the watch began.
    const sawRunning = startVersionRef.current !== undefined;
    const newVersion = sawRunning
      && (data.latest_profile?.id ?? null) !== startVersionRef.current;
    // `unchanged`: the late merge ended without a version. A build seen
    // during this watch keeps its own outcome (read as `idle`, as when no
    // merge ran); with none, nothing is said.
    const status = data.status === 'unchanged' && sawBuildRef.current
      ? 'idle' : data.status;
    let outcome = null;
    if (status === 'unchanged') {
      // The person keeps the profile they have.
      outcome = null;
    } else if (status === 'failed') {
      outcome = 'failed';
    } else if (status === 'stalled' || (status === 'idle' && sawRunning && data.batch_step_failed)) {
      outcome = 'stalled';
    } else if (status === 'completed' || (status === 'idle' && newVersion)) {
      outcome = 'completed';
    } else if (status === 'idle' && sawRunning) {
      outcome = 'stalled';
    }

    reset();

    if (outcome) {
      window.dispatchEvent(new CustomEvent('loore_profile_progress', {
        detail: { running: false, status: outcome, progress: 0, message: '' },
      }));
    }
    window.dispatchEvent(new Event('loore_profile_done'));
    // Nothing was running (stale cached flag), or a late merge ended
    // without a version.
    if (!outcome) return;
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
