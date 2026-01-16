import { useState, useRef, useCallback, useEffect } from 'react';

/**
 * Extract the WebM header (initialization segment) from an ArrayBuffer.
 * The header includes EBML, Segment, Info, and Tracks elements.
 * Everything before the first Cluster element (ID: 0x1F 0x43 0xB6 0x75) is the header.
 *
 * @param {ArrayBuffer} buffer - The first chunk's data
 * @returns {ArrayBuffer} - The header bytes
 */
function extractWebMHeader(buffer) {
  const data = new Uint8Array(buffer);

  // WebM Cluster element ID: 0x1F 0x43 0xB6 0x75
  // Find the first occurrence of this sequence
  for (let i = 0; i < data.length - 3; i++) {
    if (data[i] === 0x1F && data[i + 1] === 0x43 && data[i + 2] === 0xB6 && data[i + 3] === 0x75) {
      // Found the Cluster element - everything before it is the header
      return buffer.slice(0, i);
    }
  }

  // If no Cluster found, return a reasonable portion as header (first 4KB)
  // This shouldn't happen in normal recordings
  console.warn('WebM Cluster element not found, using first 4KB as header');
  return buffer.slice(0, Math.min(4096, buffer.byteLength));
}

/**
 * useStreamingMediaRecorder hook for recording audio with real-time chunk emission.
 *
 * This hook uses MediaRecorder with timeslice to emit audio chunks at regular intervals
 * (default 5 minutes), enabling real-time transcription while still recording.
 *
 * Returns status, mediaBlob (final), chunks array, duration, and control functions.
 */
export function useStreamingMediaRecorder({
  chunkIntervalMs = 5 * 60 * 1000, // 5 minutes default
  onChunkReady = null, // Callback when a chunk is ready: (blob, chunkIndex) => void
} = {}) {
  const [status, setStatus] = useState('idle'); // 'idle' | 'recording' | 'recorded'
  const [mediaBlob, setMediaBlob] = useState(null); // Final combined blob
  const [mediaUrl, setMediaUrl] = useState('');
  const [duration, setDuration] = useState(0);
  const [chunkCount, setChunkCount] = useState(0);
  const [error, setError] = useState(null);

  const recorderRef = useRef(null);
  const streamRef = useRef(null);
  const startTimeRef = useRef(null);
  const chunksRef = useRef([]); // All chunks for final assembly
  const chunkIndexRef = useRef(0);
  const durationIntervalRef = useRef(null);
  const webmHeaderRef = useRef(null); // Store WebM header from first chunk

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (durationIntervalRef.current) {
        clearInterval(durationIntervalRef.current);
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach(track => track.stop());
      }
    };
  }, []);

  const resetRecording = useCallback(() => {
    // Clean up previous recording
    if (mediaUrl) {
      URL.revokeObjectURL(mediaUrl);
    }
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.stop();
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(track => track.stop());
    }
    if (durationIntervalRef.current) {
      clearInterval(durationIntervalRef.current);
      durationIntervalRef.current = null;
    }

    recorderRef.current = null;
    streamRef.current = null;
    chunksRef.current = [];
    chunkIndexRef.current = 0;
    webmHeaderRef.current = null;
    setMediaBlob(null);
    setMediaUrl('');
    setDuration(0);
    setChunkCount(0);
    setError(null);
    setStatus('idle');
  }, [mediaUrl]);

  const startRecording = useCallback(async () => {
    resetRecording();

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const err = new Error('MediaDevices API not supported');
      setError(err.message);
      throw err;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const options = { mimeType: 'audio/webm' };
      const mediaRecorder = new MediaRecorder(stream, options);
      recorderRef.current = mediaRecorder;
      chunksRef.current = [];
      chunkIndexRef.current = 0;

      mediaRecorder.ondataavailable = async (e) => {
        if (e.data && e.data.size > 0) {
          chunksRef.current.push(e.data);

          // Call the onChunkReady callback for streaming transcription
          // Upload ALL chunks including the final one - needed for short recordings
          if (onChunkReady) {
            const chunkIndex = chunkIndexRef.current;
            chunkIndexRef.current += 1;
            setChunkCount(prev => prev + 1);

            let chunkBlob;

            if (chunkIndex === 0) {
              // First chunk contains the WebM header - extract and store it
              // The header includes EBML, Segment, Info, and Tracks elements
              const arrayBuffer = await e.data.arrayBuffer();
              const header = extractWebMHeader(arrayBuffer);
              webmHeaderRef.current = header;

              // First chunk is already valid, use as-is
              chunkBlob = new Blob([e.data], { type: mediaRecorder.mimeType });
            } else {
              // Subsequent chunks need the header prepended to be valid WebM files
              if (webmHeaderRef.current) {
                chunkBlob = new Blob([webmHeaderRef.current, e.data], { type: mediaRecorder.mimeType });
              } else {
                // Fallback if header extraction failed
                chunkBlob = new Blob([e.data], { type: mediaRecorder.mimeType });
              }
            }

            onChunkReady(chunkBlob, chunkIndex);
          }
        }
      };

      mediaRecorder.onstop = () => {
        // Combine all chunks into final blob
        const blob = new Blob(chunksRef.current, { type: mediaRecorder.mimeType });
        const url = URL.createObjectURL(blob);
        setMediaBlob(blob);
        setMediaUrl(url);

        const ms = Date.now() - startTimeRef.current;
        setDuration(ms / 1000);
        setStatus('recorded');

        // Stop duration tracking
        if (durationIntervalRef.current) {
          clearInterval(durationIntervalRef.current);
          durationIntervalRef.current = null;
        }

        // Stop all tracks
        stream.getTracks().forEach(track => track.stop());
      };

      mediaRecorder.onerror = (e) => {
        console.error('MediaRecorder error:', e);
        setError(e.error?.message || 'Recording error');
        setStatus('idle');
      };

      startTimeRef.current = Date.now();

      // Start recording with timeslice for chunked output
      // The timeslice parameter makes ondataavailable fire at the specified interval
      mediaRecorder.start(chunkIntervalMs);
      setStatus('recording');

      // Start duration tracking
      durationIntervalRef.current = setInterval(() => {
        if (startTimeRef.current) {
          setDuration((Date.now() - startTimeRef.current) / 1000);
        }
      }, 1000);

    } catch (err) {
      console.error('Error accessing microphone:', err);
      setError(err.message);
      throw err;
    }
  }, [resetRecording, chunkIntervalMs, onChunkReady]);

  const stopRecording = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state === 'recording') {
      // Request final data before stopping
      recorderRef.current.requestData();
      recorderRef.current.stop();
    }
  }, []);

  // Get the current chunk count (for finalization)
  const getTotalChunks = useCallback(() => {
    return chunksRef.current.length;
  }, []);

  return {
    status,
    mediaBlob,
    mediaUrl,
    duration,
    chunkCount,
    error,
    startRecording,
    stopRecording,
    resetRecording,
    getTotalChunks,
    chunkIntervalMs,
  };
}
