/**
 * Play an error sound using the Web Audio API.
 * Uses descending tones to create an unmistakable "error" alert.
 * Shared by the recorder (transcription and upload errors) and voice mode
 * (a reply whose audio failed).
 */
export function playErrorSound() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const playTone = (freq, startTime, duration) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = freq;
      osc.type = 'square';
      gain.gain.setValueAtTime(0.15, startTime);
      gain.gain.exponentialRampToValueAtTime(0.01, startTime + duration);
      osc.start(startTime);
      osc.stop(startTime + duration);
    };
    const now = ctx.currentTime;
    playTone(660, now, 0.15);
    playTone(440, now + 0.18, 0.15);
    playTone(330, now + 0.36, 0.25);
    // Close context after sounds finish
    setTimeout(() => ctx.close(), 1000);
  } catch (e) {
    // Audio not available - silently ignore
    console.warn('[ErrorSound] Could not play error sound:', e);
  }
}
