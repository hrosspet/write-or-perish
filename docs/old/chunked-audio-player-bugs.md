# Chunked Audio Player - Progress Bar Bugs

## Summary

The audio player has issues with time display and seeking when playing chunked original recordings.

## Bugs Identified

### 1. Time Display Jumps During Playback
- **Observed**: Time counter jumps from 4:59 directly to 10:00
- **Expected**: Should show 5:00, 5:01, 5:02, etc.
- **Note**: The actual audio plays correctly (2nd chunk plays properly), only the display is wrong

### 2. Seeking Limited to First Chunk
- Seeking within the first chunk (0:00 - ~5:00) works correctly
- Clicking beyond the first chunk's range causes incorrect behavior

### 3. Seeking Jumps to Wrong Positions
- Clicking past the first chunk jumps to 10:00
- Clicking further jumps to 20:00
- Clicking even further shows impossible values like "40:00 / 35:00" (current time exceeds total duration!)

### 4. Cannot Seek Within Later Chunks
- No way to seek to arbitrary positions like 7:30 or 12:45
- Only chunk boundaries seem to be "reachable" (and incorrectly calculated)

## Root Cause Analysis

### The Core Problem: WebM Continuous Timestamps

**Confirmed via debug logging**: The WebM files have **continuous timestamps** baked into them.

When recording with `MediaRecorder` using `timeslice`, the chunks have timestamps that continue from where the previous chunk ended:
- Chunk 0: timestamps 0-300s
- Chunk 1: timestamps 300-600s
- Chunk 2: timestamps 600-900s
- etc.

This is verified by `ffprobe`:
```bash
$ ffprobe -v error -show_entries format=duration -of csv=p=0 chunk_0000.webm
N/A
```
All chunks report `N/A` for duration (no metadata), but when loaded in the browser, `audio.currentTime` reports the **absolute timestamp**, not the position within the chunk.

### Debug Evidence

```
[AudioDebug] playChunkAtTime called: {chunkIndex: 1, timeInChunk: 0, ...}
[AudioDebug] calculateCumulativeTime: {chunkIndex: 1, timeInChunk: 0, result: 300}      ← Correct initial
[AudioDebug] calculateCumulativeTime: {chunkIndex: 1, timeInChunk: 299.985, result: 599.9}  ← WRONG!
```

The `timeInChunk` jumps from 0 to ~300 immediately because `audio.currentTime` reports 300+ (the absolute timestamp in the WebM container), not 0.

### Why Time Jumps from 4:59 to 10:00

1. Chunk 0 plays fine: `audio.currentTime` goes 0→300, displayed as 0:00→5:00 ✓
2. Chunk 1 starts, `playChunkAtTime(1, 0)` is called
3. First `ontimeupdate` fires with `audio.currentTime = 300` (not 0!)
4. `calculateCumulativeTime(1, 300)` = `durations[0] + 300` = `300 + 300 = 600` = 10:00
5. Display jumps from 4:59 to 10:00

### Why Seeking is Broken

When seeking within a chunk:
- Code sets `audio.currentTime = timeInChunk` (e.g., 150 for 2:30)
- But the WebM expects absolute timestamps, so it seeks to 150s absolute
- Which is in chunk 0, not the intended chunk

### The Fix

Track the **timestamp offset** for each chunk and adjust accordingly:
1. When a chunk starts playing, detect its starting timestamp offset
2. Subtract the offset when reading `audio.currentTime` to get position within chunk
3. Add the offset when seeking to convert position-in-chunk to absolute timestamp

## Fix Implemented

Changes made to `AudioContext.js`:

### 1. Added timestamp offset tracking
```javascript
// Track the starting timestamp offset of current chunk
const chunkTimestampOffsetRef = useRef(0);
```

### 2. Detect offset on first time update
In `ontimeupdate`, detect whether the chunk has continuous timestamps:
```javascript
if (!hasDetectedOffset) {
  hasDetectedOffset = true;
  const expectedOffset = durations.slice(0, chunkIndex).reduce((a, b) => a + b, 0);
  // If rawTime is close to expected offset, we have continuous timestamps
  chunkTimestampOffsetRef.current = expectedOffset;
}
const timeInCurrentChunk = rawTime - chunkTimestampOffsetRef.current;
```

### 3. Subtract offset when reading time
Both `ontimeupdate` and the interval now subtract the offset:
```javascript
const timeInChunk = rawTime - chunkTimestampOffsetRef.current;
```

### 4. Add offset when seeking
When seeking within a chunk or to a specific position:
```javascript
const rawSeekTime = safeTimeInChunk + expectedOffset;
audio.currentTime = rawSeekTime;
```

### 5. Correct duration calculation in `onended`
```javascript
const rawEndTime = audio.currentTime;
const actualDuration = rawEndTime - chunkTimestampOffsetRef.current;
```

## Attempted Fixes

See git log for previous fix attempts:
- `41545ca` - FIX: audio player timing issues - stop interval during transitions
- `23cfd97` - FIX: audio player race conditions causing time jump and seek issues
- `8cdf94a` - FIX: chunked audio player duration and seeking issues
- `4a7a0b2` - ADD: chunked audio player progress bar, total duration & cross-chunk seeking
- `3b22f85` - ADD: let the audioplayer playback chunks recorded via stream recording
