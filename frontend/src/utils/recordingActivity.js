// Whether a recording is in progress anywhere in the app: every
// useStreamingMediaRecorder reports its state here (recording or paused
// counts as in progress). Dialogs that open on their own, such as the
// both-providers question (#599), wait while one is.

const active = new Set();
const listeners = new Set();

export function setRecordingActive(id, on) {
  const had = active.has(id);
  if (on) active.add(id);
  else active.delete(id);
  if (had !== on) listeners.forEach((fn) => fn(active.size > 0));
}

export function isRecordingActive() {
  return active.size > 0;
}

// Calls fn(anyActive) whenever a recorder starts or stops. Returns the
// unsubscribe function.
export function subscribeRecording(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
