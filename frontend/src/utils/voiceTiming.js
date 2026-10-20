import api from '../api';

// Where a voice turn's wait goes (#371 step 0). The browser marks the
// moments only it sees (recording stopped, first chunk received, audio
// playing) and, once the first audio plays, sends them to the server,
// which joins them with the backend's marks for the same reply node.
// GET /api/voice/timing lists the recent turns with per-stage medians.
// Timing never affects the turn: every failure here is ignored.

let turn = null;

// Wall-clock milliseconds; the marks span the browser and the server.
const now = () => Date.now();

export function startTurn() {
  turn = { nodeId: null, marks: { rec_stop: now() }, sent: false };
}

// The turn was cancelled or replaced: nothing more is marked for it.
export function endTurn() {
  turn = null;
}

// The turn's first reply node, which keys its record on the server.
export function setTurnNode(nodeId) {
  if (turn && turn.nodeId == null && nodeId != null) turn.nodeId = nodeId;
}

// nodeId, when given, must be the turn's first reply node (a chunk of a
// turn that was never marked must not land in another turn's record).
export function mark(stage, nodeId = null) {
  if (!turn || stage in turn.marks) return;
  if (nodeId != null && turn.nodeId !== nodeId) return;
  turn.marks[stage] = now();
}

// Server clock minus the browser's, from the fastest of a few round trips.
async function measureClock() {
  let best = null;
  for (let i = 0; i < 3; i += 1) {
    const t0 = now();
    const res = await api.get('/voice/timing/clock');
    const t1 = now();
    const rtt = t1 - t0;
    if (!best || rtt < best.rtt) {
      best = { rtt, offset: res.data.t * 1000 - (t0 + t1) / 2 };
    }
  }
  return best;
}

// The first audio is playing: send the turn's marks (once).
export async function markPlaying() {
  const current = turn;
  if (!current || current.sent || !('chunk_ready' in current.marks)) return;
  current.marks.playing = now();
  current.sent = true;
  if (current.nodeId == null) return;
  try {
    const clock = await measureClock();
    const res = await api.post('/voice/timing', {
      node_id: current.nodeId,
      marks: current.marks,
      offset_ms: clock.offset,
      rtt_ms: clock.rtt,
    });
    console.info('[voice-timing]', JSON.stringify(res.data));
  } catch (err) {
    console.warn('[voice-timing] report failed', err);
  }
}
